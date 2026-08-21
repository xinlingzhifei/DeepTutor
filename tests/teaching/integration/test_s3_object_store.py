from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from pydantic import SecretStr
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import HttpWaitStrategy

from deeptutor.multi_user.context import reset_current_tenant, set_current_tenant
from deeptutor.services.config import PlatformSettings
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
    ObjectStoreAccessDenied,
    ObjectStoreConfigurationError,
    ObjectStoreConflictError,
    ObjectStoreError,
    ObjectStoreIntegrityError,
    ObjectStoreNotFound,
    S3ClassroomArtifactStore,
)
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.storage_credentials import TenantStorageCredentialRecord
from deeptutor.teaching.tenant_context import TenantContext

_MINIO_IMAGE = "minio/minio:RELEASE.2025-04-22T22-12-26Z"
_MC_IMAGE = "minio/mc:RELEASE.2025-04-16T18-13-26Z"
_BUCKET = "classroom-artifacts"


async def _body(payload: bytes) -> AsyncIterator[bytes]:
    midpoint = len(payload) // 2
    yield payload[:midpoint]
    yield payload[midpoint:]


async def _read_all(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


@contextmanager
def _current_tenant(tenant_id: str):
    token = set_current_tenant(
        TenantContext(
            tenant_id=tenant_id,
            schema_name=tenant_schema_name(tenant_id),
            user_id="minio-test-user",
            permissions=frozenset(),
        )
    )
    try:
        yield
    finally:
        reset_current_tenant(token)


def _s3_client(
    endpoint: str,
    access_key: str,
    secret_key: str,
):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=None,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


@dataclass(frozen=True)
class MinioHarness:
    endpoint: str
    credentials_root: Path
    records: dict[str, TenantStorageCredentialRecord]
    raw_clients: dict[str, object]
    admin_access: str = field(repr=False)
    admin_secret: str = field(repr=False)
    tenant_credentials: dict[str, tuple[str, str]] = field(repr=False)


def _policy(tenant_id: str) -> dict[str, object]:
    prefix = f"tenants/{tenant_id}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{_BUCKET}"],
                "Condition": {
                    "StringLike": {
                        "s3:prefix": [prefix, f"{prefix}/*"],
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetBucketVersioning"],
                "Resource": [f"arn:aws:s3:::{_BUCKET}"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": [f"arn:aws:s3:::{_BUCKET}/{prefix}/*"],
            },
        ],
    }


def _exec_mc(container: DockerContainer, *arguments: str) -> None:
    result = container.exec(["mc", *arguments])
    output = result.output.decode("utf-8", errors="replace")
    assert result.exit_code == 0, output


@pytest.fixture(scope="module")
def minio_harness(tmp_path_factory) -> MinioHarness:
    admin_access = "fixture-admin"
    admin_secret = "FIXTURE_ADMIN_SECRET_2026"
    tenant_credentials = {
        "tenant-a": ("tenant-a-access", "TENANT_A_SECRET_2026"),
        "tenant-b": ("tenant-b-access", "TENANT_B_SECRET_2026"),
    }
    runtime_root = tmp_path_factory.mktemp("object-store-runtime")
    credentials_root = runtime_root / "tenant-credentials"
    credentials_root.mkdir()
    policy_paths: dict[str, Path] = {}
    records: dict[str, TenantStorageCredentialRecord] = {}
    for tenant_id, (access_key, secret_key) in tenant_credentials.items():
        secret_ref = f"{tenant_id}/object-store"
        secret_directory = credentials_root / secret_ref
        secret_directory.mkdir(parents=True)
        (secret_directory / "object-store-access-key").write_text(
            f"{access_key}\n",
            encoding="utf-8",
        )
        (secret_directory / "object-store-secret-key").write_text(
            f"{secret_key}\n",
            encoding="utf-8",
        )
        records[tenant_id] = TenantStorageCredentialRecord(
            tenant_id=tenant_id,
            secret_ref=secret_ref,
            access_key_fingerprint=hashlib.sha256(access_key.encode("utf-8")).hexdigest(),
            status="active",
        )
        policy_path = runtime_root / f"{tenant_id}-policy.json"
        policy_path.write_text(
            json.dumps(_policy(tenant_id)),
            encoding="utf-8",
        )
        policy_paths[tenant_id] = policy_path

    with Network() as network:
        minio = (
            DockerContainer(_MINIO_IMAGE)
            .with_network(network)
            .with_network_aliases("minio")
            .with_env("MINIO_ROOT_USER", admin_access)
            .with_env("MINIO_ROOT_PASSWORD", admin_secret)
            .with_env("MINIO_BROWSER", "off")
            .with_exposed_ports(9000)
            .with_command("server /data --console-address :9001")
            .waiting_for(
                HttpWaitStrategy(9000, "/minio/health/live")
                .for_status_code(200)
                .with_startup_timeout(120)
            )
        )
        with minio:
            endpoint = f"http://{minio.get_container_host_ip()}:{minio.get_exposed_port(9000)}"
            mc = (
                DockerContainer(_MC_IMAGE)
                .with_network(network)
                .with_kwargs(entrypoint="/bin/sh")
                .with_command(["-c", "while true; do sleep 3600; done"])
            )
            for tenant_id, policy_path in policy_paths.items():
                mc.with_copy_into_container(
                    policy_path,
                    f"/tmp/{tenant_id}-policy.json",
                )
            with mc:
                _exec_mc(
                    mc,
                    "alias",
                    "set",
                    "fixture",
                    "http://minio:9000",
                    admin_access,
                    admin_secret,
                )
                _exec_mc(mc, "mb", "--ignore-existing", f"fixture/{_BUCKET}")
                _exec_mc(mc, "version", "enable", f"fixture/{_BUCKET}")
                for tenant_id, (access_key, secret_key) in tenant_credentials.items():
                    policy_name = f"{tenant_id}-only"
                    _exec_mc(
                        mc,
                        "admin",
                        "policy",
                        "create",
                        "fixture",
                        policy_name,
                        f"/tmp/{tenant_id}-policy.json",
                    )
                    _exec_mc(
                        mc,
                        "admin",
                        "user",
                        "add",
                        "fixture",
                        access_key,
                        secret_key,
                    )
                    _exec_mc(
                        mc,
                        "admin",
                        "policy",
                        "attach",
                        "fixture",
                        policy_name,
                        "--user",
                        access_key,
                    )

                raw_clients = {
                    tenant_id: _s3_client(endpoint, access_key, secret_key)
                    for tenant_id, (access_key, secret_key) in tenant_credentials.items()
                }
                yield MinioHarness(
                    endpoint=endpoint,
                    credentials_root=credentials_root,
                    records=records,
                    raw_clients=raw_clients,
                    admin_access=admin_access,
                    admin_secret=admin_secret,
                    tenant_credentials=tenant_credentials,
                )


@pytest.fixture
def runtime_minio_harness(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> MinioHarness:
    endpoint = os.environ.get("YFEISTAI_TEST_MINIO_ENDPOINT")
    if endpoint is None:
        return request.getfixturevalue("minio_harness")
    admin_access = os.environ["YFEISTAI_TEST_MINIO_ACCESS_KEY"]
    admin_secret = os.environ["YFEISTAI_TEST_MINIO_SECRET_KEY"]
    client = _s3_client(endpoint, admin_access, admin_secret)
    try:
        client.head_bucket(Bucket=_BUCKET)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {
            "404",
            "NoSuchBucket",
            "NotFound",
        }:
            raise
        client.create_bucket(Bucket=_BUCKET)
    return MinioHarness(
        endpoint=endpoint,
        credentials_root=tmp_path / "external-tenant-credentials",
        records={},
        raw_clients={},
        admin_access=admin_access,
        admin_secret=admin_secret,
        tenant_credentials={},
    )


def _assert_access_denied(action) -> None:
    with pytest.raises(ClientError) as caught:
        action()
    response = caught.value.response
    assert response["ResponseMetadata"]["HTTPStatusCode"] == 403
    assert response["Error"]["Code"] == "AccessDenied"


def _store_factory(minio_harness: MinioHarness) -> ClassroomArtifactStoreFactory:
    class Repository:
        async def get_active(self, tenant_id: str):
            return minio_harness.records.get(tenant_id)

    return ClassroomArtifactStoreFactory(
        PlatformSettings(
            enabled=True,
            database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
            object_store_mode="s3",
            object_store_endpoint=minio_harness.endpoint,
            object_store_bucket=_BUCKET,
            object_store_region="us-east-1",
            object_store_tenant_credentials_dir=minio_harness.credentials_root,
        ),
        credential_repository=Repository(),
    )


def test_minio_policies_enforce_real_cross_prefix_isolation(
    minio_harness: MinioHarness,
) -> None:
    tenant_a = minio_harness.raw_clients["tenant-a"]
    tenant_b = minio_harness.raw_clients["tenant-b"]
    key_a = "tenants/tenant-a/temporary/policy-test/own.txt"
    key_b = "tenants/tenant-b/temporary/policy-test/own.txt"
    tenant_a.put_object(Bucket=_BUCKET, Key=key_a, Body=b"tenant-a")
    tenant_b.put_object(Bucket=_BUCKET, Key=key_b, Body=b"tenant-b")

    response_a = tenant_a.get_object(Bucket=_BUCKET, Key=key_a)
    response_b = tenant_b.get_object(Bucket=_BUCKET, Key=key_b)
    try:
        assert response_a["Body"].read() == b"tenant-a"
        assert response_b["Body"].read() == b"tenant-b"
    finally:
        response_a["Body"].close()
        response_b["Body"].close()
    assert [
        item["Key"]
        for item in tenant_a.list_objects_v2(
            Bucket=_BUCKET,
            Prefix="tenants/tenant-a/",
        )["Contents"]
    ] == [key_a]
    assert [
        item["Key"]
        for item in tenant_b.list_objects_v2(
            Bucket=_BUCKET,
            Prefix="tenants/tenant-b/",
        )["Contents"]
    ] == [key_b]

    _assert_access_denied(
        lambda: tenant_a.list_objects_v2(
            Bucket=_BUCKET,
            Prefix="tenants/tenant-b/",
        )
    )
    _assert_access_denied(lambda: tenant_a.get_object(Bucket=_BUCKET, Key=key_b))
    _assert_access_denied(
        lambda: tenant_a.put_object(
            Bucket=_BUCKET,
            Key="tenants/tenant-b/temporary/policy-test/blocked.txt",
            Body=b"blocked",
        )
    )


def test_minio_supports_the_put_atomic_create_primitive(
    minio_harness: MinioHarness,
) -> None:
    tenant_a = minio_harness.raw_clients["tenant-a"]
    key = "tenants/tenant-a/temporary/conditions/probe.json"
    tenant_a.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=b"{}",
        IfNoneMatch="*",
    )

    with pytest.raises(ClientError) as duplicate:
        tenant_a.put_object(
            Bucket=_BUCKET,
            Key=key,
            Body=b'{"duplicate":true}',
            IfNoneMatch="*",
        )
    assert duplicate.value.response["ResponseMetadata"]["HTTPStatusCode"] in {
        409,
        412,
    }
    tenant_a.delete_object(Bucket=_BUCKET, Key=key)
    assert (
        tenant_a.list_objects_v2(
            Bucket=_BUCKET,
            Prefix="tenants/tenant-a/temporary/conditions/",
        ).get("KeyCount", 0)
        == 0
    )


@pytest.mark.asyncio
async def test_factory_and_promotion_use_distinct_tenant_clients(
    minio_harness: MinioHarness,
) -> None:
    factory = _store_factory(minio_harness)
    with _current_tenant("tenant-a"):
        tenant_a = await factory.create("tenant-a")
    with _current_tenant("tenant-b"):
        tenant_b = await factory.create("tenant-b")
    assert (
        minio_harness.records["tenant-a"].secret_ref != minio_harness.records["tenant-b"].secret_ref
    )

    own_a = b"factory-a"
    own_b = b"factory-b"
    tenant_a_key = temporary_artifact_key("tenant-a", "factory", "own.txt")
    tenant_a_artifact = await tenant_a.put_verified(
        tenant_a_key,
        _body(own_a),
        hashlib.sha256(own_a).hexdigest(),
        len(own_a),
    )
    await tenant_b.put_verified(
        temporary_artifact_key("tenant-b", "factory", "own.txt"),
        _body(own_b),
        hashlib.sha256(own_b).hexdigest(),
        len(own_b),
    )
    with pytest.raises(ObjectStoreAccessDenied):
        await tenant_a.list_prefix("tenants/tenant-b/")
    with pytest.raises(ObjectStoreAccessDenied):
        await tenant_a.open(temporary_artifact_key("tenant-b", "factory", "own.txt"))
    with pytest.raises(ObjectStoreNotFound):
        await tenant_a.open(temporary_artifact_key("tenant-a", "missing", "missing.txt"))

    tenant_a_access, tenant_a_secret = minio_harness.tenant_credentials["tenant-a"]
    policy_mismatched_store = S3ClassroomArtifactStore(
        tenant_id="tenant-b",
        endpoint=minio_harness.endpoint,
        bucket=_BUCKET,
        region="us-east-1",
        access_key=tenant_a_access,
        secret_key=tenant_a_secret,
    )
    with pytest.raises(ObjectStoreConfigurationError):
        await policy_mismatched_store.list_prefix("tenants/tenant-b/")

    payload = b'{"scenes":[{"id":"intro"}]}'
    digest = hashlib.sha256(payload).hexdigest()
    manifest = ClassroomArtifactManifest(
        tenant_id="tenant-a",
        job_id="promotion-ok",
        asset_id="asset-ok",
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
    promoted = await ClassroomArtifactPromotionService(tenant_a).promote(
        manifest,
        {"classroom.json": _body(payload)},
    )
    final_key = classroom_artifact_key(
        "tenant-a",
        "asset-ok",
        3,
        "classroom.json",
    )
    assert promoted[0].key == final_key
    assert await _read_all(await tenant_a.open(final_key)) == payload
    download_url = await tenant_a.presign_download(final_key, 60)
    assert download_url.startswith(minio_harness.endpoint)
    assert parse_qs(urlparse(download_url).query)["versionId"] == [promoted[0].version_id]
    assert await tenant_a.list_prefix("tenants/tenant-a/temporary/promotion-ok/") == ()
    final_head = minio_harness.raw_clients["tenant-a"].head_object(
        Bucket=_BUCKET,
        Key=final_key,
    )
    assert final_head["VersionId"] == promoted[0].version_id
    assert final_head["ContentType"] == "application/json"
    assert final_head["Metadata"]["sha256"] == digest

    newer = b"factory-a-newer"
    raw_tenant_a = minio_harness.raw_clients["tenant-a"]
    raw_tenant_a.put_object(
        Bucket=_BUCKET,
        Key=tenant_a_key,
        Body=newer,
        ContentType="application/octet-stream",
        Metadata={
            "owner": "external",
            "sha256": hashlib.sha256(newer).hexdigest(),
        },
    )
    await tenant_a.delete_owned(tenant_a_artifact)
    current_response = raw_tenant_a.get_object(Bucket=_BUCKET, Key=tenant_a_key)
    try:
        assert current_response["Body"].read() == newer
        assert current_response["VersionId"] != tenant_a_artifact.version_id
    finally:
        current_response["Body"].close()

    invalid_cases = (
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-a",
                job_id="bad-sha",
                asset_id="asset-bad-sha",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "application/json",
                        "0" * 64,
                        len(payload),
                    ),
                ),
            ),
            {"classroom.json": _body(payload)},
            ObjectStoreIntegrityError,
            "asset-bad-sha",
        ),
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-a",
                job_id="bad-size",
                asset_id="asset-bad-size",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "application/json",
                        digest,
                        len(payload) + 1,
                    ),
                ),
            ),
            {"classroom.json": _body(payload)},
            ObjectStoreIntegrityError,
            "asset-bad-size",
        ),
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-a",
                job_id="bad-mime",
                asset_id="asset-bad-mime",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "text/html",
                        digest,
                        len(payload),
                    ),
                ),
            ),
            {"classroom.json": _body(payload)},
            ArtifactManifestError,
            "asset-bad-mime",
        ),
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-a",
                job_id="bad-manifest",
                asset_id="asset-bad-manifest",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "application/json",
                        digest,
                        len(payload),
                    ),
                ),
            ),
            {"undeclared.json": _body(payload)},
            ArtifactManifestError,
            "asset-bad-manifest",
        ),
        (
            ClassroomArtifactManifest(
                tenant_id="tenant-b",
                job_id="wrong-tenant",
                asset_id="asset-wrong-tenant",
                version=1,
                entries=(
                    ArtifactManifestEntry(
                        "classroom.json",
                        "application/json",
                        digest,
                        len(payload),
                    ),
                ),
            ),
            {"classroom.json": _body(payload)},
            ArtifactManifestError,
            "asset-wrong-tenant",
        ),
    )
    for invalid_manifest, bodies, error_type, asset_id in invalid_cases:
        with pytest.raises(error_type):
            await ClassroomArtifactPromotionService(tenant_a).promote(
                invalid_manifest,
                bodies,
            )
        assert await tenant_a.list_prefix(f"tenants/tenant-a/classrooms/{asset_id}/") == ()


@pytest.mark.asyncio
async def test_real_minio_atomic_publish_and_source_integrity(
    minio_harness: MinioHarness,
) -> None:
    with _current_tenant("tenant-a"):
        store = await _store_factory(minio_harness).create("tenant-a")
    first = b'{"winner":"first"}'
    second = b'{"winner":"second"}'

    def manifest(payload: bytes, job_id: str) -> ClassroomArtifactManifest:
        return ClassroomArtifactManifest(
            tenant_id="tenant-a",
            job_id=job_id,
            asset_id="asset-concurrent",
            version=1,
            entries=(
                ArtifactManifestEntry(
                    "classroom.json",
                    "application/json",
                    hashlib.sha256(payload).hexdigest(),
                    len(payload),
                ),
            ),
        )

    results = await asyncio.gather(
        ClassroomArtifactPromotionService(store).promote(
            manifest(first, "minio-first"),
            {"classroom.json": _body(first)},
        ),
        ClassroomArtifactPromotionService(store).promote(
            manifest(second, "minio-second"),
            {"classroom.json": _body(second)},
        ),
        return_exceptions=True,
    )
    successes = [result for result in results if isinstance(result, tuple)]
    conflicts = [result for result in results if isinstance(result, ObjectStoreConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1, results
    assert await _read_all(await store.open(successes[0][0].key)) in {first, second}

    source_payload = b'{"value":"good"}'
    tampered_payload = b'{"value":"evil"}'
    source_key = temporary_artifact_key(
        "tenant-a",
        "minio-tamper",
        "classroom.json",
    )
    destination_key = classroom_artifact_key(
        "tenant-a",
        "asset-tamper",
        1,
        "classroom.json",
    )
    source_digest = hashlib.sha256(source_payload).hexdigest()
    await store.put_verified(
        source_key,
        _body(source_payload),
        source_digest,
        len(source_payload),
        content_type="application/json",
    )
    minio_harness.raw_clients["tenant-a"].put_object(
        Bucket=_BUCKET,
        Key=source_key,
        Body=tampered_payload,
        ContentType="application/json",
    )

    with pytest.raises(ObjectStoreIntegrityError):
        await store.copy(
            source_key,
            destination_key,
            sha256=source_digest,
            size=len(source_payload),
            content_type="application/json",
        )
    assert (
        minio_harness.raw_clients["tenant-a"]
        .list_objects_v2(Bucket=_BUCKET, Prefix=destination_key)
        .get("KeyCount", 0)
        == 0
    )


@pytest.mark.asyncio
async def test_real_minio_ambiguous_staging_failure_preserves_newer_version(
    minio_harness: MinioHarness,
) -> None:
    replacement_payload = b'{"replacement":"newer"}'
    raw_client = minio_harness.raw_clients["tenant-a"]
    access_key, secret_key = minio_harness.tenant_credentials["tenant-a"]

    class ReplaceThenRaiseStore(S3ClassroomArtifactStore):
        staging_key: str | None = None

        async def put_verified(self, key, body, sha256, size, **kwargs):
            await super().put_verified(key, body, sha256, size, **kwargs)
            self.staging_key = key
            await asyncio.to_thread(
                raw_client.put_object,
                Bucket=_BUCKET,
                Key=key,
                Body=replacement_payload,
                ContentType="application/json",
                Metadata={
                    "owner": "external",
                    "sha256": hashlib.sha256(replacement_payload).hexdigest(),
                },
            )
            raise ObjectStoreError("simulated ambiguous MinIO staging write")

    store = ReplaceThenRaiseStore(
        tenant_id="tenant-a",
        endpoint=minio_harness.endpoint,
        bucket=_BUCKET,
        region="us-east-1",
        access_key=access_key,
        secret_key=secret_key,
    )
    source_payload = b'{"source":"owned"}'
    manifest = ClassroomArtifactManifest(
        tenant_id="tenant-a",
        job_id="minio-ambiguous-write",
        asset_id="asset-minio-ambiguous-write",
        version=1,
        entries=(
            ArtifactManifestEntry(
                "classroom.json",
                "application/json",
                hashlib.sha256(source_payload).hexdigest(),
                len(source_payload),
            ),
        ),
    )

    with pytest.raises(ObjectStoreError, match="ambiguous MinIO"):
        await ClassroomArtifactPromotionService(store).promote(
            manifest,
            {"classroom.json": _body(source_payload)},
        )

    assert store.staging_key is not None
    key_parts = store.staging_key.split("/")
    assert key_parts[:4] == [
        "tenants",
        "tenant-a",
        "temporary",
        "minio-ambiguous-write",
    ]
    response = raw_client.get_object(Bucket=_BUCKET, Key=store.staging_key)
    try:
        assert response["Body"].read() == replacement_payload
    finally:
        response["Body"].close()
    versions = raw_client.list_object_versions(
        Bucket=_BUCKET,
        Prefix=store.staging_key,
    )
    assert not [
        marker for marker in versions.get("DeleteMarkers", []) if marker["Key"] == store.staging_key
    ]
    assert len(key_parts[4]) == 32


@pytest.mark.asyncio
async def test_real_minio_hides_and_preserves_legacy_formal_object(
    minio_harness: MinioHarness,
) -> None:
    with _current_tenant("tenant-a"):
        store = await _store_factory(minio_harness).create("tenant-a")
    raw_client = minio_harness.raw_clients["tenant-a"]
    legacy_key = classroom_artifact_key(
        "tenant-a",
        "asset-legacy",
        1,
        "classroom.json",
    )
    legacy_payload = b'{"legacy":true}'
    raw_client.put_object(
        Bucket=_BUCKET,
        Key=legacy_key,
        Body=legacy_payload,
        ContentType="application/json",
    )

    assert await store.list_prefix("tenants/tenant-a/classrooms/asset-legacy/versions/1/") == ()
    with pytest.raises(ObjectStoreNotFound):
        await store.open(legacy_key)
    manifest = ClassroomArtifactManifest(
        tenant_id="tenant-a",
        job_id="legacy-retry",
        asset_id="asset-legacy",
        version=1,
        entries=(
            ArtifactManifestEntry(
                "classroom.json",
                "application/json",
                hashlib.sha256(b'{"replacement":true}').hexdigest(),
                len(b'{"replacement":true}'),
            ),
        ),
    )
    with pytest.raises(ObjectStoreConflictError):
        await ClassroomArtifactPromotionService(store).promote(
            manifest,
            {"classroom.json": _body(b'{"replacement":true}')},
        )
    response = raw_client.get_object(Bucket=_BUCKET, Key=legacy_key)
    try:
        assert response["Body"].read() == legacy_payload
    finally:
        response["Body"].close()


@pytest.mark.asyncio
async def test_runtime_minio_admin_provisions_isolates_and_rotates_credentials(
    runtime_minio_harness: MinioHarness,
    tmp_path: Path,
) -> None:
    from deeptutor.teaching.minio_tenant_storage import (
        RuntimeMinioTenantStorageAdmin,
        TenantSecretStore,
        provision_tenant_storage,
    )

    access_file = tmp_path / "minio-root-access"
    secret_file = tmp_path / "minio-root-secret"
    access_file.write_text(runtime_minio_harness.admin_access, encoding="utf-8")
    secret_file.write_text(runtime_minio_harness.admin_secret, encoding="utf-8")
    (tmp_path / "minio_bootstrap_access_key").write_text(
        runtime_minio_harness.admin_access,
        encoding="utf-8",
    )
    (tmp_path / "minio_bootstrap_secret_key").write_text(
        runtime_minio_harness.admin_secret,
        encoding="utf-8",
    )
    credential_root = tmp_path / "runtime-tenant-credentials"
    settings = PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        object_store_mode="s3",
        object_store_endpoint=runtime_minio_harness.endpoint,
        object_store_bucket=_BUCKET,
        object_store_region="us-east-1",
        object_store_tenant_credentials_dir=credential_root,
    )
    admin = RuntimeMinioTenantStorageAdmin(
        settings=settings,
        bootstrap_access_key_file=access_file,
        bootstrap_secret_key_file=secret_file,
    )
    store = TenantSecretStore(credential_root)
    published = []

    class Publisher:
        async def publish(self, result) -> None:
            published.append(result)

    first = await provision_tenant_storage(
        settings=settings,
        tenant_id="tenant-runtime",
        admin=admin,
        secret_store=store,
        publisher=Publisher(),
    )
    first_pair = store.load(first.secret_ref, tenant_id="tenant-runtime")
    first_client = _s3_client(
        runtime_minio_harness.endpoint,
        first_pair.access_key,
        first_pair.secret_key,
    )
    own_key = "tenants/tenant-runtime/provisioning/own.txt"
    denied_key = "tenants/tenant-b/provisioning/denied.txt"
    first_client.put_object(Bucket=_BUCKET, Key=own_key, Body=b"own")
    _assert_access_denied(
        lambda: first_client.put_object(
            Bucket=_BUCKET,
            Key=denied_key,
            Body=b"denied",
        )
    )
    _assert_access_denied(
        lambda: first_client.list_objects_v2(
            Bucket=_BUCKET,
            Prefix="tenants/tenant-b/",
        )
    )

    rotated = await provision_tenant_storage(
        settings=settings,
        tenant_id="tenant-runtime",
        admin=admin,
        secret_store=store,
        publisher=Publisher(),
        rotate=True,
    )
    rotated_pair = store.load(rotated.secret_ref, tenant_id="tenant-runtime")
    rotated_client = _s3_client(
        runtime_minio_harness.endpoint,
        rotated_pair.access_key,
        rotated_pair.secret_key,
    )
    rotated_client.put_object(
        Bucket=_BUCKET,
        Key="tenants/tenant-runtime/provisioning/rotated.txt",
        Body=b"rotated",
    )
    with pytest.raises(ClientError) as revoked:
        first_client.list_objects_v2(Bucket=_BUCKET, Prefix="tenants/tenant-runtime/")
    assert revoked.value.response["Error"]["Code"] in {
        "AccessDenied",
        "InvalidAccessKeyId",
    }
    assert [result.secret_ref for result in published] == [
        first.secret_ref,
        rotated.secret_ref,
    ]

    preflight_path = Path(__file__).resolve().parents[3] / "scripts" / "platform_preflight.py"
    spec = importlib.util.spec_from_file_location("platform_preflight_s3_test", preflight_path)
    assert spec is not None and spec.loader is not None
    preflight = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = preflight
    spec.loader.exec_module(preflight)

    preflight_errors = await preflight._inspect_object_store_runtime(
        settings,
        tmp_path,
        (
            preflight._ActiveTenant(
                tenant_id="tenant-runtime",
                schema_name=tenant_schema_name("tenant-runtime"),
                secret_ref=rotated.secret_ref,
            ),
        ),
    )
    assert preflight_errors == ()
