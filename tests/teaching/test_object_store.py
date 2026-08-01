from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
import hashlib
import io
import os
from pathlib import Path
import threading
import traceback

from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import SecretStr
import pytest

from deeptutor.multi_user.context import (
    get_current_tenant_or_none,
    reset_current_tenant,
    set_current_tenant,
)
from deeptutor.services.config import PlatformSettings
from deeptutor.teaching import object_store as object_store_module
from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ArtifactManifestError,
    ClassroomArtifactManifest,
    classroom_artifact_key,
    temporary_artifact_key,
)
from deeptutor.teaching.object_store import (
    ClassroomArtifactPromotionService,
    ClassroomArtifactStoreFactory,
    LocalClassroomArtifactStore,
    ObjectStoreAccessDenied,
    ObjectStoreConfigurationError,
    ObjectStoreConflictError,
    ObjectStoreError,
    ObjectStoreIntegrityError,
    ObjectStoreNotFound,
    S3ClassroomArtifactStore,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.storage_credentials import (
    StorageCredentialError,
    TenantStorageCredentialRecord,
    TenantStorageCredentialResolver,
)
from deeptutor.teaching.tenant_context import TenantContext


async def _body(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def _read_all(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


@contextmanager
def _current_tenant(tenant_id: str):
    token = set_current_tenant(
        TenantContext(
            tenant_id=tenant_id,
            schema_name=tenant_schema_name(tenant_id),
            user_id="object-store-test-user",
            permissions=frozenset(),
        )
    )
    try:
        yield
    finally:
        reset_current_tenant(token)


_FORMAL_ROOT = "tenants/tenant-a/classrooms/asset-1/versions"
_FORMAL_LEADING_ZERO = f"{_FORMAL_ROOT}/03/classroom.json"


def _manifest(
    payload: bytes,
    job_id: str,
    *extra_entries: ArtifactManifestEntry,
) -> ClassroomArtifactManifest:
    return ClassroomArtifactManifest(
        "tenant-a",
        job_id,
        "asset-1",
        1,
        (
            ArtifactManifestEntry(
                "classroom.json",
                "application/json",
                hashlib.sha256(payload).hexdigest(),
                len(payload),
            ),
            *extra_entries,
        ),
    )


def _client_error(code: str, status: int, operation: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _MemoryS3Client:
    def __init__(
        self,
        *,
        fail_after_put_key: str | None = None,
        preserve_put_owner: bool = True,
        versioning_enabled: bool = True,
    ) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.versions: dict[str, dict[str, dict[str, object]]] = {}
        self.fail_after_put_key = fail_after_put_key
        self.preserve_put_owner = preserve_put_owner
        self.versioning_enabled = versioning_enabled
        self.failed_after_put = False
        self.get_payload_overrides: dict[str, bytes] = {}
        self.put_calls: list[dict[str, object]] = []
        self.presign_calls: list[dict[str, object]] = []
        self.version_sequence = 0

    def get_bucket_versioning(self, **_kwargs):
        return {"Status": "Enabled"} if self.versioning_enabled else {}

    @staticmethod
    def _payload(body: object) -> bytes:
        if isinstance(body, bytes):
            return body
        return body.read()

    def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _client_error("PreconditionFailed", 412, "PutObject")
        payload = self._payload(kwargs["Body"])
        self.version_sequence += 1
        version_id = f"version-{self.version_sequence}" if self.versioning_enabled else "null"
        record: dict[str, object] = {
            "Body": payload,
            "ContentLength": len(payload),
            "ContentType": kwargs.get("ContentType", "application/octet-stream"),
            "ETag": f'"{hashlib.sha256(payload).hexdigest()}"',
            "Metadata": dict(kwargs.get("Metadata", {})),
            "VersionId": version_id,
        }
        self.objects[key] = record
        self.versions.setdefault(key, {})[version_id] = record
        if key == self.fail_after_put_key and not self.failed_after_put:
            self.failed_after_put = True
            if not self.preserve_put_owner:
                record["Metadata"] = {"owner": "external", "sha256": "0" * 64}
            raise EndpointConnectionError(endpoint_url="http://minio.example:9000")
        return {"ETag": record["ETag"], "VersionId": version_id}

    def head_object(self, **kwargs):
        key = kwargs["Key"]
        version_id = kwargs.get("VersionId")
        try:
            record = self.versions[key][version_id] if version_id is not None else self.objects[key]
            return dict(record)
        except KeyError:
            raise _client_error("NoSuchKey", 404, "HeadObject") from None

    def get_object(self, **kwargs):
        key = kwargs["Key"]
        version_id = kwargs.get("VersionId")
        try:
            record = self.versions[key][version_id] if version_id is not None else self.objects[key]
        except KeyError:
            raise _client_error("NoSuchKey", 404, "GetObject") from None
        if kwargs.get("IfMatch") not in {None, record["ETag"]}:
            raise _client_error("PreconditionFailed", 412, "GetObject")
        response = dict(record)
        response["Body"] = io.BytesIO(self.get_payload_overrides.get(key, record["Body"]))
        return response

    def delete_object(self, **kwargs):
        key = kwargs["Key"]
        version_id = kwargs.get("VersionId")
        if version_id is None:
            self.objects.pop(key, None)
            return {}
        versions = self.versions.get(key, {})
        versions.pop(version_id, None)
        current = self.objects.get(key)
        if current is not None and current.get("VersionId") == version_id:
            if versions:
                self.objects[key] = max(
                    versions.values(),
                    key=lambda item: int(str(item["VersionId"]).rsplit("-", 1)[1]),
                )
            else:
                self.objects.pop(key, None)
        return {}

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        contents = [{"Key": key} for key in sorted(self.objects) if key.startswith(prefix)]
        return {"Contents": contents, "IsTruncated": False, "KeyCount": len(contents)}

    def generate_presigned_url(self, *_args, **kwargs):
        self.presign_calls.append(dict(kwargs))
        params = kwargs["Params"]
        version_id = params.get("VersionId")
        suffix = f"?versionId={version_id}" if version_id is not None else ""
        return f"https://download.example/{params['Key']}{suffix}"


def _s3_store(monkeypatch, client: object) -> S3ClassroomArtifactStore:
    monkeypatch.setattr(
        "deeptutor.teaching.object_store.boto3.client",
        lambda *_args, **_kwargs: client,
    )
    return S3ClassroomArtifactStore(
        tenant_id="tenant-a",
        endpoint="http://minio.example:9000",
        bucket="classrooms",
        region="us-east-1",
        access_key="tenant-a-access",
        secret_key="tenant-a-secret",
    )


async def _assert_published_version_is_all_or_none(
    store: LocalClassroomArtifactStore | S3ClassroomArtifactStore,
    manifest: ClassroomArtifactManifest,
    bodies: dict[str, AsyncIterator[bytes]],
    survivor_payload: bytes,
    mutate: Callable[[str], None],
) -> None:
    promoted = await ClassroomArtifactPromotionService(store).promote(manifest, bodies)
    expected_keys = tuple(sorted(artifact.key for artifact in promoted))
    survivor_key = classroom_artifact_key(
        manifest.tenant_id,
        manifest.asset_id,
        manifest.version,
        "classroom.json",
    )
    invalid_key = classroom_artifact_key(
        manifest.tenant_id,
        manifest.asset_id,
        manifest.version,
        "index.html",
    )
    version_prefix = survivor_key.rsplit("/", 1)[0] + "/"

    assert await store.list_prefix(version_prefix) == expected_keys
    assert await _read_all(await store.open(survivor_key)) == survivor_payload
    mutate(invalid_key)

    assert await store.list_prefix(version_prefix) == ()
    with pytest.raises(ObjectStoreNotFound):
        await store.open(survivor_key)
    with pytest.raises(ObjectStoreNotFound):
        await store.presign_download(survivor_key, 60)


def test_artifact_key_is_server_derived() -> None:
    assert (
        classroom_artifact_key(
            tenant_id="tenant-a",
            asset_id="asset-1",
            version=3,
            relative_name="classroom.json",
        )
        == "tenants/tenant-a/classrooms/asset-1/versions/3/classroom.json"
    )


def test_temporary_key_is_server_derived() -> None:
    assert (
        temporary_artifact_key("tenant-a", "job-1", "nested/classroom.json")
        == "tenants/tenant-a/temporary/job-1/nested/classroom.json"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant", "tenant/a"),
        ("tenant", ".."),
        ("asset", r"asset\one"),
        ("asset", ""),
        ("job", "/absolute"),
        ("job", "job\x00one"),
        ("relative", "../secret"),
        ("relative", "/root"),
        ("relative", "C:/root"),
        ("relative", "nested/C:/root"),
        ("relative", "a//b"),
        ("relative", "a/./b"),
        ("relative", "a/../b"),
        ("relative", r"a\b"),
        ("relative", "a/\x00b"),
    ],
)
def test_artifact_keys_reject_path_escape(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        if field == "job":
            temporary_artifact_key("tenant-a", value, "classroom.json")
        else:
            classroom_artifact_key(
                value if field == "tenant" else "tenant-a",
                value if field == "asset" else "asset-1",
                3,
                value if field == "relative" else "classroom.json",
            )


@pytest.mark.asyncio
async def test_local_store_streams_verified_temporary_object(tmp_path) -> None:
    payload = b'{"scenes":[]}'
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    key = temporary_artifact_key("tenant-a", "job-1", "classroom.json")

    stored = await store.put_verified(
        key,
        _body(payload[:5], payload[5:]),
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )

    assert stored.key == key
    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.size == len(payload)
    assert await _read_all(await store.open(key)) == payload


@pytest.mark.asyncio
async def test_store_rejects_cross_tenant_prefix_before_listing(tmp_path) -> None:
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")

    with pytest.raises(ObjectStoreAccessDenied):
        await store.list_prefix("tenants/tenant-b/")


def test_tenant_storage_credentials_are_resolved_from_fixed_secret_files(
    tmp_path,
) -> None:
    access_key = "tenant-a-access"
    secret_key = "TENANT_A_SECRET_SENTINEL"
    tenant_directory = tmp_path / "tenant-a"
    tenant_directory.mkdir()
    (tenant_directory / "object-store-access-key").write_text(
        f"{access_key}\n",
        encoding="utf-8",
    )
    (tenant_directory / "object-store-secret-key").write_text(
        f"{secret_key}\n",
        encoding="utf-8",
    )
    record = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref="tenant-a",
        access_key_fingerprint=hashlib.sha256(access_key.encode()).hexdigest(),
        status="active",
    )

    credentials = TenantStorageCredentialResolver(tmp_path).resolve(
        record,
        tenant_id="tenant-a",
    )

    assert credentials.access_key == access_key
    assert credentials.secret_key == secret_key
    assert access_key not in repr(credentials)
    assert secret_key not in repr(credentials)


@pytest.mark.asyncio
async def test_valid_manifest_is_staged_promoted_and_temporary_object_removed(
    tmp_path,
) -> None:
    payload = b'{"scenes":[]}'
    digest = hashlib.sha256(payload).hexdigest()
    manifest = ClassroomArtifactManifest(
        tenant_id="tenant-a",
        job_id="job-1",
        asset_id="asset-1",
        version=3,
        entries=(
            ArtifactManifestEntry(
                relative_name="classroom.json",
                content_type="application/json",
                sha256=digest,
                size=len(payload),
            ),
        ),
    )
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")

    promoted = await ClassroomArtifactPromotionService(store).promote(
        manifest,
        {"classroom.json": _body(payload)},
    )

    final_key = classroom_artifact_key(
        "tenant-a",
        "asset-1",
        3,
        "classroom.json",
    )
    assert promoted[0].key == final_key
    assert await _read_all(await store.open(final_key)) == payload
    assert await store.list_prefix("tenants/tenant-a/temporary/job-1/") == ()


@pytest.mark.asyncio
async def test_enabled_platform_local_store_fails_closed_without_opt_in(
    tmp_path,
) -> None:
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        object_store_mode="local",
    )

    with _current_tenant("tenant-a"):
        with pytest.raises(ObjectStoreConfigurationError):
            await ClassroomArtifactStoreFactory(
                settings,
                local_root=tmp_path,
            ).create("tenant-a")

        store = await ClassroomArtifactStoreFactory(
            settings,
            local_root=tmp_path,
            allow_local=True,
        ).create("tenant-a")
    assert isinstance(store, LocalClassroomArtifactStore)


@pytest.mark.asyncio
async def test_disabled_platform_s3_fails_before_credential_lookup(
    tmp_path,
) -> None:
    calls: list[str] = []

    class Repository:
        async def get_active(self, tenant_id: str):
            calls.append(tenant_id)
            return None

    with _current_tenant("tenant-a"):
        with pytest.raises(ObjectStoreConfigurationError):
            await ClassroomArtifactStoreFactory(
                PlatformSettings(enabled=False, object_store_mode="s3"),
                credential_repository=Repository(),
            ).create("tenant-a")

        local_store = await ClassroomArtifactStoreFactory(
            PlatformSettings(enabled=False, object_store_mode="local"),
            local_root=tmp_path,
            allow_local=True,
        ).create("tenant-a")

    assert get_current_tenant_or_none() is None
    assert calls == []
    assert isinstance(local_store, LocalClassroomArtifactStore)


@pytest.mark.asyncio
async def test_factory_rejects_tenant_mismatch_before_credential_lookup(
    tmp_path,
) -> None:
    calls: list[str] = []

    class Repository:
        async def get_active(self, tenant_id: str):
            calls.append(tenant_id)
            return None

    factory = ClassroomArtifactStoreFactory(
        PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
            object_store_mode="s3",
            object_store_endpoint="http://minio:9000",
            object_store_tenant_credentials_dir=tmp_path,
        ),
        credential_repository=Repository(),
    )

    with _current_tenant("tenant-a"):
        with pytest.raises(ObjectStoreConfigurationError, match="current tenant"):
            await factory.create("tenant-b")

    assert calls == []


@pytest.mark.asyncio
async def test_factory_requires_tenant_context_before_credential_lookup(
    tmp_path,
) -> None:
    calls: list[str] = []

    class Repository:
        async def get_active(self, tenant_id: str):
            calls.append(tenant_id)
            return None

    assert get_current_tenant_or_none() is None
    with pytest.raises(ObjectStoreConfigurationError, match="tenant context"):
        await ClassroomArtifactStoreFactory(
            PlatformSettings(
                enabled=True,
                database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
                object_store_mode="s3",
                object_store_endpoint="http://minio:9000",
                object_store_tenant_credentials_dir=tmp_path,
            ),
            credential_repository=Repository(),
        ).create("tenant-a")

    assert calls == []


@pytest.mark.asyncio
async def test_s3_store_rejects_cross_tenant_before_client_call(
    monkeypatch,
) -> None:
    class UnexpectedClient:
        def list_objects_v2(self, **_kwargs):
            raise AssertionError("S3 client must not be called")

    store = _s3_store(monkeypatch, UnexpectedClient())

    with pytest.raises(ObjectStoreAccessDenied):
        await store.list_prefix("tenants/tenant-b/")


@pytest.mark.asyncio
async def test_s3_access_error_is_mapped_without_credential_leak(
    monkeypatch,
) -> None:
    secret = "SECRET_SENTINEL_MUST_NOT_APPEAR"

    class DeniedClient:
        def list_objects_v2(self, **_kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDenied",
                        "Message": f"denied with {secret}",
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "ListObjectsV2",
            )

    store = _s3_store(monkeypatch, DeniedClient())

    with pytest.raises(ObjectStoreAccessDenied) as caught:
        await store.list_prefix("tenants/tenant-a/")

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None


def test_s3_store_never_falls_back_when_explicit_credentials_are_missing(
    monkeypatch,
) -> None:
    called = False

    def unexpected_client(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("ambient credential client must not be created")

    monkeypatch.setattr(
        "deeptutor.teaching.object_store.boto3.client",
        unexpected_client,
    )

    with pytest.raises(ObjectStoreConfigurationError):
        S3ClassroomArtifactStore(
            tenant_id="tenant-a",
            endpoint="http://minio.example:9000",
            bucket="classrooms",
            region="us-east-1",
            access_key="",
            secret_key="",
        )
    assert called is False


@pytest.mark.asyncio
async def test_direct_upload_to_permanent_prefix_is_rejected(tmp_path) -> None:
    payload = b"{}"
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")

    with pytest.raises(ObjectStoreAccessDenied):
        await store.put_verified(
            classroom_artifact_key(
                "tenant-a",
                "asset-1",
                1,
                "classroom.json",
            ),
            _body(payload),
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )


@pytest.mark.parametrize(
    ("operation", "unsafe_value"),
    [
        ("put", "tenants/tenant-a/temporary/job-1"),
        ("copy", f"{_FORMAL_ROOT}/0/classroom.json"),
        ("copy", _FORMAL_LEADING_ZERO),
        ("copy", "tenants/tenant-a/classrooms/asset-1/3/classroom.json"),
        (
            "copy",
            "tenants/tenant-a/classrooms/asset-1/extra/versions/3/classroom.json",
        ),
        ("open", _FORMAL_LEADING_ZERO),
        ("delete", _FORMAL_LEADING_ZERO),
        ("presign_download", _FORMAL_LEADING_ZERO),
        ("list_prefix", f"{_FORMAL_ROOT}/03/"),
        ("list_prefix", "tenants/tenant-a/not-artifacts/"),
    ],
)
@pytest.mark.asyncio
async def test_store_methods_reject_noncanonical_artifact_paths(
    tmp_path,
    operation: str,
    unsafe_value: str,
) -> None:
    payload = b"{}"
    digest = hashlib.sha256(payload).hexdigest()
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    source_key = temporary_artifact_key("tenant-a", "job-1", "source.json")
    await store.put_verified(source_key, _body(payload), digest, len(payload))

    with pytest.raises(ObjectStoreAccessDenied):
        if operation == "put":
            await store.put_verified(unsafe_value, _body(payload), digest, len(payload))
        elif operation == "copy":
            await store.copy(
                source_key,
                unsafe_value,
                sha256=digest,
                size=len(payload),
            )
        elif operation == "presign_download":
            await store.presign_download(unsafe_value, 60)
        else:
            await getattr(store, operation)(unsafe_value)


@pytest.mark.parametrize(
    "failure",
    ["bad-format", "wrong-digest", "too-short", "too-long", "non-bytes"],
)
@pytest.mark.asyncio
async def test_failed_stream_integrity_leaves_no_object_or_temporary_file(
    tmp_path,
    failure: str,
) -> None:
    payload = b"verified-payload"
    chunks: tuple[object, ...] = (payload,)
    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    if failure == "bad-format":
        digest = "not-a-sha256"
    elif failure == "wrong-digest":
        digest = "0" * 64
    elif failure == "too-short":
        size += 1
    elif failure == "too-long":
        size -= 1
    else:
        chunks = (bytearray(payload),)

    async def untyped_body() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk  # type: ignore[misc]

    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    key = temporary_artifact_key("tenant-a", "job-1", "classroom.json")

    with pytest.raises(ObjectStoreIntegrityError):
        await store.put_verified(key, untyped_body(), digest, size)

    with pytest.raises(ObjectStoreNotFound):
        await store.open(key)
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_local_store_resolve_blocks_symlink_escape(tmp_path) -> None:
    root = tmp_path / "objects"
    outside = tmp_path / "outside"
    staging_parent = root / "tenants" / "tenant-a" / "temporary"
    staging_parent.mkdir(parents=True)
    outside.mkdir()
    (staging_parent / "job-1").symlink_to(outside, target_is_directory=True)
    payload = b"blocked"

    with pytest.raises(ObjectStoreAccessDenied):
        await LocalClassroomArtifactStore(root, "tenant-a").put_verified(
            temporary_artifact_key(
                "tenant-a",
                "job-1",
                "classroom.json",
            ),
            _body(payload),
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "secret_ref",
    ["../outside", "/absolute", r"tenant-a\child", "tenant-a//child", "C:/root"],
)
def test_credential_resolver_rejects_escaping_secret_reference(
    tmp_path,
    secret_ref: str,
) -> None:
    record = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref=secret_ref,
        access_key_fingerprint="0" * 64,
        status="active",
    )

    with pytest.raises(StorageCredentialError):
        TenantStorageCredentialResolver(tmp_path).resolve(
            record,
            tenant_id="tenant-a",
        )


def test_credential_resolver_rejects_symlink_and_redacts_failure(
    tmp_path,
) -> None:
    access_key = "tenant-a-access"
    secret_key = "SECRET_SENTINEL_MUST_NOT_LEAK"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "object-store-access-key").write_text(
        access_key,
        encoding="utf-8",
    )
    (outside / "object-store-secret-key").write_text(
        secret_key,
        encoding="utf-8",
    )
    (tmp_path / "tenant-a").symlink_to(outside, target_is_directory=True)
    record = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref="tenant-a",
        access_key_fingerprint=hashlib.sha256(access_key.encode()).hexdigest(),
        status="active",
    )

    with pytest.raises(StorageCredentialError) as caught:
        TenantStorageCredentialResolver(tmp_path).resolve(
            record,
            tenant_id="tenant-a",
        )

    assert access_key not in str(caught.value)
    assert secret_key not in str(caught.value)


def test_credential_resolver_rejects_fingerprint_or_tenant_mismatch(
    tmp_path,
) -> None:
    tenant_directory = tmp_path / "tenant-a"
    tenant_directory.mkdir()
    (tenant_directory / "object-store-access-key").write_text(
        "tenant-a-access",
        encoding="utf-8",
    )
    (tenant_directory / "object-store-secret-key").write_text(
        "tenant-a-secret",
        encoding="utf-8",
    )
    resolver = TenantStorageCredentialResolver(tmp_path)
    mismatched_fingerprint = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref="tenant-a",
        access_key_fingerprint="0" * 64,
        status="active",
    )

    with pytest.raises(StorageCredentialError):
        resolver.resolve(mismatched_fingerprint, tenant_id="tenant-a")
    with pytest.raises(StorageCredentialError):
        resolver.resolve(
            TenantStorageCredentialRecord(
                tenant_id="tenant-b",
                secret_ref="tenant-a",
                access_key_fingerprint=hashlib.sha256(b"tenant-a-access").hexdigest(),
                status="active",
            ),
            tenant_id="tenant-a",
        )


def test_credential_resolver_rejects_cross_tenant_secret_reference(
    tmp_path,
) -> None:
    access_key = "tenant-b-access"
    foreign_directory = tmp_path / "tenant-b" / "object-store"
    foreign_directory.mkdir(parents=True)
    (foreign_directory / "object-store-access-key").write_text(
        access_key,
        encoding="utf-8",
    )
    (foreign_directory / "object-store-secret-key").write_text(
        "tenant-b-secret",
        encoding="utf-8",
    )
    record = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref="tenant-b/object-store",
        access_key_fingerprint=hashlib.sha256(access_key.encode()).hexdigest(),
        status="active",
    )

    with pytest.raises(StorageCredentialError):
        TenantStorageCredentialResolver(tmp_path).resolve(
            record,
            tenant_id="tenant-a",
        )


@pytest.mark.asyncio
async def test_factory_builds_distinct_explicit_s3_clients_from_secret_refs(
    tmp_path,
    monkeypatch,
) -> None:
    records: dict[str, TenantStorageCredentialRecord] = {}
    for tenant in ("tenant-a", "tenant-b"):
        access_key = f"{tenant}-access"
        directory = tmp_path / tenant
        directory.mkdir()
        (directory / "object-store-access-key").write_text(
            access_key,
            encoding="utf-8",
        )
        (directory / "object-store-secret-key").write_text(
            f"{tenant}-secret",
            encoding="utf-8",
        )
        records[tenant] = TenantStorageCredentialRecord(
            tenant_id=tenant,
            secret_ref=tenant,
            access_key_fingerprint=hashlib.sha256(access_key.encode()).hexdigest(),
            status="active",
        )

    class Repository:
        async def get_active(self, tenant_id: str):
            return records.get(tenant_id)

    client_calls: list[dict[str, object]] = []
    versioning_checks: list[dict[str, object]] = []

    class VersionedClient:
        def get_bucket_versioning(self, **kwargs):
            versioning_checks.append(kwargs)
            return {"Status": "Enabled"}

    def client_factory(_service: str, **kwargs):
        client_calls.append(kwargs)
        return VersionedClient()

    monkeypatch.setattr(
        "deeptutor.teaching.object_store.boto3.client",
        client_factory,
    )
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_bucket="classrooms",
        object_store_region="us-east-1",
        object_store_tenant_credentials_dir=tmp_path,
    )
    factory = ClassroomArtifactStoreFactory(
        settings,
        credential_repository=Repository(),
    )

    with _current_tenant("tenant-a"):
        store_a = await factory.create("tenant-a")
    with _current_tenant("tenant-b"):
        store_b = await factory.create("tenant-b")

    assert store_a.tenant_id == "tenant-a"
    assert store_b.tenant_id == "tenant-b"
    assert [call["aws_access_key_id"] for call in client_calls] == [
        "tenant-a-access",
        "tenant-b-access",
    ]
    assert [call["aws_secret_access_key"] for call in client_calls] == [
        "tenant-a-secret",
        "tenant-b-secret",
    ]
    assert all(call["aws_session_token"] is None for call in client_calls)
    assert all(call["config"].s3["addressing_style"] == "path" for call in client_calls)
    assert all(call["config"].signature_version == "s3v4" for call in client_calls)
    assert versioning_checks == [
        {"Bucket": "classrooms"},
        {"Bucket": "classrooms"},
    ]


@pytest.mark.asyncio
async def test_factory_rejects_an_unversioned_s3_bucket(tmp_path, monkeypatch) -> None:
    record = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref="tenant-a",
        access_key_fingerprint="0" * 64,
        status="active",
    )

    class Repository:
        async def get_active(self, _tenant_id: str):
            return record

    class Credentials:
        access_key = "tenant-a-access"
        secret_key = "tenant-a-secret"

    monkeypatch.setattr(
        "deeptutor.teaching.object_store.TenantStorageCredentialResolver.resolve",
        lambda *_args, **_kwargs: Credentials(),
    )
    monkeypatch.setattr(
        "deeptutor.teaching.object_store.boto3.client",
        lambda *_args, **_kwargs: _MemoryS3Client(versioning_enabled=False),
    )
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_bucket="classrooms",
        object_store_region="us-east-1",
        object_store_tenant_credentials_dir=tmp_path,
    )

    with _current_tenant("tenant-a"):
        with pytest.raises(ObjectStoreConfigurationError, match="versioning"):
            await ClassroomArtifactStoreFactory(
                settings,
                credential_repository=Repository(),
            ).create("tenant-a")


@pytest.mark.asyncio
async def test_unversioned_s3_store_write_fails_closed(monkeypatch) -> None:
    client = _MemoryS3Client(versioning_enabled=False)
    store = _s3_store(monkeypatch, client)
    payload = b"unversioned"

    with pytest.raises(ObjectStoreConfigurationError, match="versioning"):
        await store.put_verified(
            temporary_artifact_key("tenant-a", "unversioned", "object.txt"),
            _body(payload),
            hashlib.sha256(payload).hexdigest(),
            len(payload),
        )


@pytest.mark.asyncio
async def test_promotion_validation_failure_never_creates_permanent_object(
    tmp_path,
) -> None:
    payload = b"{}"
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    final_prefix = "tenants/tenant-a/classrooms/"
    cases = (
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-b",
                job_id="job-1",
                asset_id="asset-1",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "application/json",
                        hashlib.sha256(payload).hexdigest(),
                        len(payload),
                    ),
                ),
            ),
            {"classroom.json": _body(payload)},
        ),
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-a",
                job_id="job-1",
                asset_id="asset-1",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "text/html",
                        hashlib.sha256(payload).hexdigest(),
                        len(payload),
                    ),
                ),
            ),
            {"classroom.json": _body(payload)},
        ),
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-a",
                job_id="job-1",
                asset_id="asset-1",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "application/json",
                        hashlib.sha256(payload).hexdigest(),
                        len(payload),
                    ),
                ),
            ),
            {"not-declared.json": _body(payload)},
        ),
    )

    for manifest, bodies in cases:
        with pytest.raises(ArtifactManifestError):
            await ClassroomArtifactPromotionService(store).promote(
                manifest,
                bodies,
            )
        assert await store.list_prefix(final_prefix) == ()


@pytest.mark.asyncio
async def test_multi_file_staging_failure_cleans_temp_and_never_promotes(
    tmp_path,
) -> None:
    json_payload = b"{}"
    html_payload = b"<main></main>"
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    manifest = ClassroomArtifactManifest(
        tenant_id="tenant-a",
        job_id="job-1",
        asset_id="asset-1",
        version=1,
        entries=(
            ArtifactManifestEntry(
                "classroom.json",
                "application/json",
                hashlib.sha256(json_payload).hexdigest(),
                len(json_payload),
            ),
            ArtifactManifestEntry(
                "index.html",
                "text/html",
                "0" * 64,
                len(html_payload),
            ),
        ),
    )

    with pytest.raises(ObjectStoreIntegrityError):
        await ClassroomArtifactPromotionService(store).promote(
            manifest,
            {
                "classroom.json": _body(json_payload),
                "index.html": _body(html_payload),
            },
        )

    assert await store.list_prefix("tenants/tenant-a/temporary/job-1/") == ()
    assert await store.list_prefix("tenants/tenant-a/classrooms/") == ()


@pytest.mark.asyncio
async def test_promotion_leaves_unconfirmed_temporary_write_for_gc(
    tmp_path,
) -> None:
    payload = b"{}"

    class WriteThenRaiseStore(LocalClassroomArtifactStore):
        staging_key: str | None = None

        async def put_verified(self, key, body, sha256, size, **kwargs):
            await super().put_verified(key, body, sha256, size, **kwargs)
            self.staging_key = key
            raise ObjectStoreError("simulated uncertain remote write")

    store = WriteThenRaiseStore(tmp_path, "tenant-a")
    manifest = _manifest(payload, "uncertain-write")

    with pytest.raises(ObjectStoreError):
        await ClassroomArtifactPromotionService(store).promote(
            manifest,
            {"classroom.json": _body(payload)},
        )

    assert store.staging_key is not None
    assert await store.list_prefix("tenants/tenant-a/temporary/uncertain-write/") == (
        store.staging_key,
    )
    assert await _read_all(await store.open(store.staging_key)) == payload


@pytest.mark.asyncio
async def test_ambiguous_staging_failure_preserves_newer_local_replacements(
    tmp_path,
) -> None:
    payload = b"{}"
    replacements = (b'{"replacement":1}', b'{"replacement":2}')

    class ReplaceThenRaiseStore(LocalClassroomArtifactStore):
        def __init__(self) -> None:
            super().__init__(tmp_path, "tenant-a")
            self.staging_keys: list[str] = []

        async def put_verified(self, key, body, sha256, size, **kwargs):
            await super().put_verified(key, body, sha256, size, **kwargs)
            self.staging_keys.append(key)
            target = tmp_path.joinpath(*key.split("/"))
            replacement = tmp_path / f"replacement-{len(self.staging_keys)}"
            replacement.write_bytes(replacements[len(self.staging_keys) - 1])
            replacement.replace(target)
            raise ObjectStoreError("simulated ambiguous staging write")

    store = ReplaceThenRaiseStore()
    manifest = _manifest(payload, "ambiguous-write")

    for _ in replacements:
        with pytest.raises(ObjectStoreError, match="ambiguous staging"):
            await ClassroomArtifactPromotionService(store).promote(
                manifest,
                {"classroom.json": _body(payload)},
            )

    assert len(set(store.staging_keys)) == len(replacements)
    for key, replacement_payload in zip(store.staging_keys, replacements, strict=True):
        parts = key.split("/")
        assert parts[:4] == ["tenants", "tenant-a", "temporary", "ambiguous-write"]
        assert len(parts[4]) == 32
        assert all(character in "0123456789abcdef" for character in parts[4])
        assert parts[5:] == ["classroom.json"]
        assert tmp_path.joinpath(*parts).read_bytes() == replacement_payload


@pytest.mark.asyncio
async def test_promotion_fails_before_staging_and_preserves_existing_version(
    tmp_path,
) -> None:
    old_payload = b'{"version":"old"}'
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    old_manifest = _manifest(old_payload, "initial")
    existing = await ClassroomArtifactPromotionService(store).promote(
        old_manifest,
        {"classroom.json": _body(old_payload)},
    )
    new_payload = b'{"version":"new"}'
    failing_payload = b"<main>broken</main>"
    retry_manifest = _manifest(
        new_payload,
        "retry",
        ArtifactManifestEntry(
            "index.html",
            "text/html",
            "0" * 64,
            len(failing_payload),
        ),
    )

    with pytest.raises(ObjectStoreConflictError):
        await ClassroomArtifactPromotionService(store).promote(
            retry_manifest,
            {
                "classroom.json": _body(new_payload),
                "index.html": _body(failing_payload),
            },
        )

    assert await _read_all(await store.open(existing[0].key)) == old_payload
    assert await store.list_prefix("tenants/tenant-a/temporary/retry/") == ()


@pytest.mark.asyncio
async def test_concurrent_local_promotions_publish_exactly_one_version(
    tmp_path,
) -> None:
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    first = b'{"winner":"first"}'
    second = b'{"winner":"second"}'

    results = await asyncio.gather(
        ClassroomArtifactPromotionService(store).promote(
            _manifest(first, "concurrent-first"),
            {"classroom.json": _body(first)},
        ),
        ClassroomArtifactPromotionService(store).promote(
            _manifest(second, "concurrent-second"),
            {"classroom.json": _body(second)},
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, tuple)]
    conflicts = [result for result in results if isinstance(result, ObjectStoreConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1, results
    winner = await _read_all(await store.open(successes[0][0].key))
    assert winner in {first, second}


@pytest.mark.asyncio
async def test_partial_multi_file_promotion_is_invisible_until_commit(
    tmp_path,
) -> None:
    first_copied = asyncio.Event()
    finish_copying = asyncio.Event()

    class PausingStore(LocalClassroomArtifactStore):
        copy_count = 0

        async def copy(self, *args, **kwargs):
            artifact = await super().copy(*args, **kwargs)
            self.copy_count += 1
            if self.copy_count == 1:
                first_copied.set()
                await finish_copying.wait()
            return artifact

    json_payload = b'{"scenes":[]}'
    html_payload = b"<main>ready</main>"
    manifest = _manifest(
        json_payload,
        "slow-publish",
        ArtifactManifestEntry(
            "index.html",
            "text/html",
            hashlib.sha256(html_payload).hexdigest(),
            len(html_payload),
        ),
    )
    store = PausingStore(tmp_path, "tenant-a")
    task = asyncio.create_task(
        ClassroomArtifactPromotionService(store).promote(
            manifest,
            {
                "classroom.json": _body(json_payload),
                "index.html": _body(html_payload),
            },
        )
    )
    await asyncio.wait_for(first_copied.wait(), timeout=5)
    first_key = classroom_artifact_key(
        "tenant-a",
        "asset-1",
        1,
        "classroom.json",
    )

    with pytest.raises(ObjectStoreNotFound):
        await store.open(first_key)
    with pytest.raises(ObjectStoreNotFound):
        await store.presign_download(first_key, 60)
    assert await store.list_prefix(f"{_FORMAL_ROOT}/1/") == ()

    finish_copying.set()
    promoted = await asyncio.wait_for(task, timeout=5)
    assert await store.list_prefix(f"{_FORMAL_ROOT}/1/") == tuple(
        sorted(artifact.key for artifact in promoted)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "same-size-tamper"])
async def test_local_published_version_visibility_is_all_or_none(
    tmp_path,
    mutation: str,
) -> None:
    json_payload = b'{"ok":true}'
    html_payload = b"published"
    manifest = _manifest(
        json_payload,
        f"local-all-or-none-{mutation}",
        ArtifactManifestEntry(
            "index.html",
            "text/html",
            hashlib.sha256(html_payload).hexdigest(),
            len(html_payload),
        ),
    )
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")

    def mutate(invalid_key: str) -> None:
        invalid_path = tmp_path.joinpath(*invalid_key.split("/"))
        if mutation == "missing":
            invalid_path.replace(tmp_path / "removed-index.html")
        else:
            invalid_path.write_bytes(b"tampered!")

    await _assert_published_version_is_all_or_none(
        store,
        manifest,
        {
            "classroom.json": _body(json_payload),
            "index.html": _body(html_payload),
        },
        json_payload,
        mutate,
    )


@pytest.mark.asyncio
async def test_local_formal_open_binds_the_validated_revision(tmp_path) -> None:
    original = b'{"value":"old"}'
    tampered = b'{"value":"new"}'
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    promoted = await ClassroomArtifactPromotionService(store).promote(
        _manifest(original, "local-open-revision"),
        {"classroom.json": _body(original)},
    )

    stream = await store.open(promoted[0].key)
    tmp_path.joinpath(*promoted[0].key.split("/")).write_bytes(tampered)

    with pytest.raises((ObjectStoreIntegrityError, ObjectStoreNotFound)):
        await anext(stream)


@pytest.mark.asyncio
async def test_local_presign_fails_closed_without_an_immutable_revision(tmp_path) -> None:
    payload = b"{}"
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    promoted = await ClassroomArtifactPromotionService(store).promote(
        _manifest(payload, "local-presign"),
        {"classroom.json": _body(payload)},
    )

    with pytest.raises(ObjectStoreConfigurationError, match="verified open"):
        await store.presign_download(promoted[0].key, 60)


@pytest.mark.asyncio
async def test_ambiguous_copy_keeps_claim_and_invisible_orphan(
    tmp_path,
) -> None:
    class WriteThenRaiseCopyStore(LocalClassroomArtifactStore):
        copy_count = 0

        async def copy(self, *args, **kwargs):
            artifact = await super().copy(*args, **kwargs)
            self.copy_count += 1
            if self.copy_count == 2:
                raise ObjectStoreError("simulated lost copy response")
            return artifact

    json_payload = b'{"scenes":[]}'
    html_payload = b"<main>orphan</main>"
    manifest = _manifest(
        json_payload,
        "ambiguous-copy",
        ArtifactManifestEntry(
            "index.html",
            "text/html",
            hashlib.sha256(html_payload).hexdigest(),
            len(html_payload),
        ),
    )
    store = WriteThenRaiseCopyStore(tmp_path, "tenant-a")

    with pytest.raises(ObjectStoreError):
        await ClassroomArtifactPromotionService(store).promote(
            manifest,
            {
                "classroom.json": _body(json_payload),
                "index.html": _body(html_payload),
            },
        )

    orphan_key = classroom_artifact_key(
        "tenant-a",
        "asset-1",
        1,
        "index.html",
    )
    orphan_path = tmp_path.joinpath(*orphan_key.split("/"))
    assert orphan_path.read_bytes() == html_payload
    assert await store.list_prefix(f"{_FORMAL_ROOT}/1/") == ()
    with pytest.raises(ObjectStoreNotFound):
        await store.open(orphan_key)

    retry_payload = b'{"retry":true}'
    with pytest.raises(ObjectStoreConflictError):
        await ClassroomArtifactPromotionService(store).promote(
            _manifest(retry_payload, "ambiguous-retry"),
            {"classroom.json": _body(retry_payload)},
        )
    assert orphan_path.read_bytes() == html_payload


@pytest.mark.asyncio
async def test_legacy_formal_object_conflicts_without_being_deleted(tmp_path) -> None:
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    key = classroom_artifact_key("tenant-a", "asset-1", 1, "classroom.json")
    legacy_path = tmp_path.joinpath(*key.split("/"))
    legacy_path.parent.mkdir(parents=True)
    legacy_payload = b'{"legacy":true}'
    legacy_path.write_bytes(legacy_payload)

    with pytest.raises(ObjectStoreConflictError):
        await ClassroomArtifactPromotionService(store).promote(
            _manifest(b'{"replacement":true}', "legacy-conflict"),
            {"classroom.json": _body(b'{"replacement":true}')},
        )

    assert legacy_path.read_bytes() == legacy_payload
    with pytest.raises(ObjectStoreNotFound):
        await store.open(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preserve_put_owner", "expect_success"),
    [(True, True), (False, False)],
    ids=("owned-write-is-reconciled", "unowned-write-stays-hidden"),
)
async def test_s3_copy_write_then_raise_requires_current_claim_ownership(
    monkeypatch,
    preserve_put_owner: bool,
    expect_success: bool,
) -> None:
    payload = b'{"reconciled":true}'
    final_key = classroom_artifact_key(
        "tenant-a",
        "asset-1",
        1,
        "classroom.json",
    )
    claim_key = f"{_FORMAL_ROOT}/1/.deeptutor-publish-claim.json"
    marker_key = f"{_FORMAL_ROOT}/1/.deeptutor-commit.json"
    client = _MemoryS3Client(
        fail_after_put_key=final_key,
        preserve_put_owner=preserve_put_owner,
    )
    store = _s3_store(monkeypatch, client)

    promotion = ClassroomArtifactPromotionService(store).promote(
        _manifest(payload, f"copy-reconcile-{preserve_put_owner}"),
        {"classroom.json": _body(payload)},
    )
    if expect_success:
        promoted = await promotion
        assert await _read_all(await store.open(promoted[0].key)) == payload
        assert claim_key not in client.objects
        assert marker_key in client.objects
    else:
        with pytest.raises(ObjectStoreConflictError):
            await promotion
        assert claim_key in client.objects
        assert marker_key not in client.objects
        assert final_key in client.objects
        assert await store.list_prefix(f"{_FORMAL_ROOT}/1/") == ()
        with pytest.raises(ObjectStoreNotFound):
            await store.open(final_key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preserve_marker_owner",
    [True, False],
    ids=["owned-marker-is-confirmed", "external-marker-is-rejected"],
)
async def test_s3_commit_write_then_raise_requires_owned_marker(
    monkeypatch,
    preserve_marker_owner: bool,
) -> None:
    payload = b'{"committed":true}'
    final_key = classroom_artifact_key(
        "tenant-a",
        "asset-1",
        1,
        "classroom.json",
    )
    marker_key = f"{_FORMAL_ROOT}/1/.deeptutor-commit.json"
    claim_key = f"{_FORMAL_ROOT}/1/.deeptutor-publish-claim.json"
    client = _MemoryS3Client(
        fail_after_put_key=marker_key,
        preserve_put_owner=preserve_marker_owner,
    )
    store = _s3_store(monkeypatch, client)

    promotion = ClassroomArtifactPromotionService(store).promote(
        _manifest(payload, f"commit-reconcile-{preserve_marker_owner}"),
        {"classroom.json": _body(payload)},
    )

    if preserve_marker_owner:
        promoted = await promotion
        assert promoted[0].key == final_key
        assert await _read_all(await store.open(final_key)) == payload
        assert marker_key in client.objects
        assert claim_key not in client.objects
    else:
        with pytest.raises(ObjectStoreConflictError):
            await promotion
        assert marker_key in client.objects
        assert claim_key in client.objects
        assert final_key in client.objects
        assert await store.list_prefix(f"{_FORMAL_ROOT}/1/") == ()
        with pytest.raises(ObjectStoreNotFound):
            await store.open(final_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "same-size-tamper"])
async def test_s3_published_version_visibility_is_all_or_none(
    monkeypatch,
    mutation: str,
) -> None:
    json_payload = b'{"ok":true}'
    html_payload = b"published"
    manifest = _manifest(
        json_payload,
        f"s3-all-or-none-{mutation}",
        ArtifactManifestEntry(
            "index.html",
            "text/html",
            hashlib.sha256(html_payload).hexdigest(),
            len(html_payload),
        ),
    )
    client = _MemoryS3Client()
    store = _s3_store(monkeypatch, client)

    def mutate(invalid_key: str) -> None:
        if mutation == "missing":
            client.objects.pop(invalid_key)
        else:
            tampered = b"tampered!"
            client.objects[invalid_key].update(
                {
                    "Body": tampered,
                    "ContentLength": len(tampered),
                    "ETag": f'"{hashlib.sha256(tampered).hexdigest()}"',
                }
            )

    await _assert_published_version_is_all_or_none(
        store,
        manifest,
        {
            "classroom.json": _body(json_payload),
            "index.html": _body(html_payload),
        },
        json_payload,
        mutate,
    )


@pytest.mark.asyncio
async def test_s3_formal_open_binds_the_validated_revision(monkeypatch) -> None:
    original = b'{"value":"old"}'
    tampered = b'{"value":"new"}'
    client = _MemoryS3Client()
    store = _s3_store(monkeypatch, client)
    promoted = await ClassroomArtifactPromotionService(store).promote(
        _manifest(original, "s3-open-revision"),
        {"classroom.json": _body(original)},
    )

    stream = await store.open(promoted[0].key)
    client.put_object(
        Bucket="classrooms",
        Key=promoted[0].key,
        Body=tampered,
        ContentType="application/json",
        Metadata={
            "owner": "external",
            "sha256": hashlib.sha256(tampered).hexdigest(),
        },
    )

    assert await _read_all(stream) == original


@pytest.mark.asyncio
async def test_s3_presign_binds_the_validated_version_id(monkeypatch) -> None:
    payload = b"{}"
    client = _MemoryS3Client()
    store = _s3_store(monkeypatch, client)
    promoted = await ClassroomArtifactPromotionService(store).promote(
        _manifest(payload, "s3-presign-version"),
        {"classroom.json": _body(payload)},
    )

    url = await store.presign_download(promoted[0].key, 60)

    params = client.presign_calls[-1]["Params"]
    assert params["VersionId"] == client.objects[promoted[0].key]["VersionId"]
    assert f"versionId={params['VersionId']}" in url


@pytest.mark.asyncio
async def test_corrupt_commit_marker_keeps_formal_objects_invisible(tmp_path) -> None:
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    final_key = classroom_artifact_key(
        "tenant-a",
        "asset-corrupt-marker",
        1,
        "classroom.json",
    )
    marker_key = classroom_artifact_key(
        "tenant-a",
        "asset-corrupt-marker",
        1,
        ".deeptutor-commit.json",
    )
    final_path = tmp_path.joinpath(*final_key.split("/"))
    marker_path = tmp_path.joinpath(*marker_key.split("/"))
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"{}")
    marker_path.write_bytes(b'{"schema":1}')

    assert (
        await store.list_prefix("tenants/tenant-a/classrooms/asset-corrupt-marker/versions/1/")
        == ()
    )
    with pytest.raises(ObjectStoreNotFound):
        await store.open(final_key)


@pytest.mark.asyncio
async def test_copy_verifies_actual_local_source_and_destination(
    tmp_path,
    monkeypatch,
) -> None:
    payload = b'{"verified":true}'
    digest = hashlib.sha256(payload).hexdigest()
    source_key = temporary_artifact_key("tenant-a", "copy-integrity", "source.json")
    destination_key = classroom_artifact_key(
        "tenant-a",
        "asset-copy",
        1,
        "classroom.json",
    )
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    await store.put_verified(
        source_key,
        _body(payload),
        digest,
        len(payload),
        content_type="application/json",
    )
    source_path = tmp_path.joinpath(*source_key.split("/"))
    source_path.write_bytes(b'{"tampered":true}')

    with pytest.raises(ObjectStoreIntegrityError):
        await store.copy(
            source_key,
            destination_key,
            sha256=digest,
            size=len(payload),
            content_type="application/json",
        )
    assert not tmp_path.joinpath(*destination_key.split("/")).exists()

    source_path.write_bytes(payload)
    real_link = os.link

    def corrupt_after_link(source, destination):
        real_link(source, destination)
        Path(destination).write_bytes(b'{"corrupt":true}')

    monkeypatch.setattr("deeptutor.teaching.object_store.os.link", corrupt_after_link)
    with pytest.raises(ObjectStoreIntegrityError):
        await store.copy(
            source_key,
            destination_key,
            sha256=digest,
            size=len(payload),
            content_type="application/json",
        )


@pytest.mark.asyncio
async def test_local_delete_owned_preserves_a_replacement_after_the_check(
    tmp_path,
    monkeypatch,
) -> None:
    owned = b"owned"
    foreign = b"other"
    key = temporary_artifact_key("tenant-a", "delete-race", "object.txt")
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    artifact = await store.put_verified(
        key,
        _body(owned),
        hashlib.sha256(owned).hexdigest(),
        len(owned),
    )
    path = tmp_path.joinpath(*key.split("/"))
    replacement = tmp_path / "foreign-replacement"
    replacement.write_bytes(foreign)
    real_revision = object_store_module._local_revision
    replaced = False

    def replace_after_check(checked_path):
        nonlocal replaced
        revision = real_revision(checked_path)
        if not replaced and Path(checked_path) == path:
            replaced = True
            replacement.replace(path)
        return revision

    monkeypatch.setattr(
        "deeptutor.teaching.object_store._local_revision",
        replace_after_check,
    )

    with pytest.raises(ObjectStoreError, match="ownership"):
        await store.delete_owned(artifact)
    assert path.read_bytes() == foreign


@pytest.mark.asyncio
async def test_s3_delete_owned_removes_only_the_owned_version(monkeypatch) -> None:
    owned = b"owned"
    foreign = b"other"

    class PutNewerAfterHeadClient(_MemoryS3Client):
        race_key: str | None = None
        armed = False

        def head_object(self, **kwargs):
            response = super().head_object(**kwargs)
            if self.armed and kwargs["Key"] == self.race_key:
                self.armed = False
                self.put_object(
                    Bucket=kwargs["Bucket"],
                    Key=kwargs["Key"],
                    Body=foreign,
                    ContentType="application/octet-stream",
                    Metadata={
                        "owner": "external",
                        "sha256": hashlib.sha256(foreign).hexdigest(),
                    },
                )
            return response

    client = PutNewerAfterHeadClient()
    store = _s3_store(monkeypatch, client)
    key = temporary_artifact_key("tenant-a", "delete-race", "object.txt")
    artifact = await store.put_verified(
        key,
        _body(owned),
        hashlib.sha256(owned).hexdigest(),
        len(owned),
    )
    client.race_key = key
    client.armed = True

    await store.delete_owned(artifact)

    assert client.objects[key]["Body"] == foreign
    assert artifact.version_id not in client.versions[key]


@pytest.mark.asyncio
async def test_application_json_payload_must_be_utf8_json(tmp_path) -> None:
    payload = b"{not-json}"
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")

    with pytest.raises(ObjectStoreIntegrityError):
        await ClassroomArtifactPromotionService(store).promote(
            _manifest(payload, "invalid-json"),
            {"classroom.json": _body(payload)},
        )
    assert await store.list_prefix("tenants/tenant-a/") == ()


@pytest.mark.asyncio
async def test_cleanup_is_independent_and_preserves_primary_error(tmp_path) -> None:
    sentinel = "SECRET_CLEANUP_SENTINEL"
    deleted: list[str] = []

    class CleanupFailureStore(LocalClassroomArtifactStore):
        async def delete_owned(self, artifact):
            deleted.append(artifact.key)
            if artifact.key.endswith("classroom.json"):
                raise ObjectStoreError(sentinel)
            await super().delete_owned(artifact)

    first = b"{}"
    second = b"<main>bad</main>"
    manifest = _manifest(
        first,
        "cleanup-errors",
        ArtifactManifestEntry(
            "index.html",
            "text/html",
            "0" * 64,
            len(second),
        ),
    )
    store = CleanupFailureStore(tmp_path, "tenant-a")

    with pytest.raises(ObjectStoreIntegrityError) as caught:
        await ClassroomArtifactPromotionService(store).promote(
            manifest,
            {
                "classroom.json": _body(first),
                "index.html": _body(second),
            },
        )

    assert len(deleted) == 1
    assert deleted[0].startswith("tenants/tenant-a/temporary/cleanup-errors/")
    assert deleted[0].endswith("/classroom.json")
    rendered = "".join(traceback.format_exception(caught.value))
    assert "object body sha256 does not match" in rendered
    assert "cleanup failed: temporary object (ObjectStoreError)" in rendered
    assert sentinel not in rendered


@pytest.mark.asyncio
async def test_promotion_cancellation_runs_all_cleanup_without_being_swallowed(
    tmp_path,
) -> None:
    started = asyncio.Event()
    deleted: list[str] = []

    async def blocked_body() -> AsyncIterator[bytes]:
        yield b"{"
        started.set()
        await asyncio.Event().wait()

    class RecordingStore(LocalClassroomArtifactStore):
        async def delete_owned(self, artifact):
            deleted.append(artifact.key)
            await super().delete_owned(artifact)

    payload = b"{}"
    store = RecordingStore(tmp_path, "tenant-a")
    task = asyncio.create_task(
        ClassroomArtifactPromotionService(store).promote(
            _manifest(payload, "cancelled"),
            {"classroom.json": blocked_body()},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert deleted == [
        f"{_FORMAL_ROOT}/1/.deeptutor-publish-claim.json",
    ]
    assert await store.list_prefix("tenants/tenant-a/") == ()


@pytest.mark.asyncio
async def test_local_listing_rejects_symlinked_object(tmp_path) -> None:
    store = LocalClassroomArtifactStore(tmp_path, "tenant-a")
    payload = b"safe"
    target_key = temporary_artifact_key("tenant-a", "links", "target.txt")
    await store.put_verified(
        target_key,
        _body(payload),
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )
    target_path = tmp_path.joinpath(*target_key.split("/"))
    alias_path = target_path.with_name("alias.txt")
    try:
        alias_path.symlink_to(target_path)
    except OSError:
        pytest.skip("filesystem does not permit symlink creation")

    with pytest.raises(ObjectStoreAccessDenied):
        await store.list_prefix("tenants/tenant-a/temporary/links/")


@pytest.mark.asyncio
async def test_s3_listing_revalidates_every_backend_key(monkeypatch) -> None:
    class MaliciousListClient:
        def list_objects_v2(self, **_kwargs):
            return {
                "Contents": [
                    {
                        "Key": "tenants/tenant-b/temporary/foreign/stolen.json",
                    }
                ],
                "IsTruncated": False,
            }

    store = _s3_store(monkeypatch, MaliciousListClient())

    with pytest.raises(ObjectStoreAccessDenied):
        await store.list_prefix("tenants/tenant-a/")


@pytest.mark.asyncio
async def test_s3_open_defers_get_and_closes_body_on_early_exit(monkeypatch) -> None:
    class StreamingBody:
        def __init__(self, failure: BaseException | None = None) -> None:
            self.closed = False
            self.failure = failure
            self.read_count = 0

        def read(self, _size):
            if self.failure is not None:
                raise self.failure
            self.read_count += 1
            return b"chunk" if self.read_count == 1 else b""

        def close(self):
            self.closed = True

    bodies: list[StreamingBody] = []

    class StreamingClient:
        failure: BaseException | None = None
        block_get = False
        get_started = threading.Event()
        release_get = threading.Event()

        def head_object(self, **_kwargs):
            return {"ETag": '"stream"'}

        def get_object(self, **_kwargs):
            if self.block_get:
                self.get_started.set()
                self.release_get.wait(timeout=5)
            body = StreamingBody(self.failure)
            bodies.append(body)
            return {"Body": body}

    client = StreamingClient()
    store = _s3_store(monkeypatch, client)
    key = temporary_artifact_key("tenant-a", "stream", "chunk.txt")

    unused = await store.open(key)
    await unused.aclose()
    assert bodies == []

    stream = await store.open(key)
    assert await anext(stream) == b"chunk"
    assert bodies[0].closed is False
    await stream.aclose()
    assert bodies[0].closed is True

    client.failure = RuntimeError("read failed")
    failed_stream = await store.open(key)
    with pytest.raises(RuntimeError, match="read failed"):
        await anext(failed_stream)
    assert bodies[-1].closed is True

    client.failure = asyncio.CancelledError()
    cancelled_stream = await store.open(key)
    with pytest.raises(asyncio.CancelledError):
        await anext(cancelled_stream)
    assert bodies[-1].closed is True

    client.failure = None
    client.block_get = True
    blocked_stream = await store.open(key)
    blocked_read = asyncio.create_task(anext(blocked_stream))
    assert await asyncio.to_thread(client.get_started.wait, 5)
    blocked_read.cancel()
    client.release_get.set()
    with pytest.raises(asyncio.CancelledError):
        await blocked_read
    assert bodies[-1].closed is True


@pytest.mark.asyncio
async def test_s3_copy_rejects_corrupt_destination_and_sets_content_type(
    monkeypatch,
) -> None:
    payload = b'{"verified":true}'
    digest = hashlib.sha256(payload).hexdigest()
    source_key = temporary_artifact_key("tenant-a", "copy", "source.json")
    destination_key = classroom_artifact_key(
        "tenant-a",
        "asset-copy",
        1,
        "classroom.json",
    )
    client = _MemoryS3Client()
    store = _s3_store(monkeypatch, client)
    await store.put_verified(
        source_key,
        _body(payload),
        digest,
        len(payload),
        content_type="application/json",
    )
    client.get_payload_overrides[destination_key] = b'{"corrupt":true}'

    with pytest.raises(ObjectStoreIntegrityError):
        await store.copy(
            source_key,
            destination_key,
            sha256=digest,
            size=len(payload),
            content_type="application/json",
        )
    destination_put = client.put_calls[-1]
    assert destination_put["IfNoneMatch"] == "*"
    assert destination_put["ContentType"] == "application/json"


def test_fingerprint_is_checked_before_secret_file_is_read(tmp_path) -> None:
    directory = tmp_path / "tenant-a"
    directory.mkdir()
    (directory / "object-store-access-key").write_text(
        "tenant-a-access",
        encoding="utf-8",
    )
    record = TenantStorageCredentialRecord(
        tenant_id="tenant-a",
        secret_ref="tenant-a",
        access_key_fingerprint="0" * 64,
        status="active",
    )

    with pytest.raises(StorageCredentialError, match="fingerprint"):
        TenantStorageCredentialResolver(tmp_path).resolve(
            record,
            tenant_id="tenant-a",
        )


@pytest.mark.asyncio
async def test_factory_suppresses_secret_bearing_credential_exception(
    tmp_path,
    monkeypatch,
) -> None:
    secret = "SECRET_RESOLVER_SENTINEL"

    class Repository:
        async def get_active(self, tenant_id):
            return TenantStorageCredentialRecord(
                tenant_id=tenant_id,
                secret_ref="tenant-a",
                access_key_fingerprint="0" * 64,
                status="active",
            )

    def fail_resolve(*_args, **_kwargs):
        raise StorageCredentialError(secret)

    monkeypatch.setattr(
        "deeptutor.teaching.object_store.TenantStorageCredentialResolver.resolve",
        fail_resolve,
    )
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_tenant_credentials_dir=tmp_path,
    )

    with _current_tenant("tenant-a"):
        with pytest.raises(ObjectStoreConfigurationError) as caught:
            await ClassroomArtifactStoreFactory(
                settings,
                credential_repository=Repository(),
            ).create("tenant-a")

    rendered = "".join(traceback.format_exception(caught.value))
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in rendered
    assert caught.value.__cause__ is None
