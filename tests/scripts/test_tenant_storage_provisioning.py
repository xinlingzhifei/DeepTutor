from __future__ import annotations

import asyncio
from functools import cache
import hashlib
import importlib.util
from pathlib import Path
import sys

from pydantic import SecretStr
import pytest

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching import minio_tenant_storage as storage_module
from deeptutor.teaching.provisioning_worker import ProvisioningStepError

ROOT = Path(__file__).resolve().parents[2]


@cache
def _module():
    path = ROOT / "scripts" / "provision_tenant_storage.py"
    spec = importlib.util.spec_from_file_location(
        "provision_tenant_storage_under_test",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _settings(tmp_path: Path) -> PlatformSettings:
    return PlatformSettings(
        enabled=True,
        database_url=SecretStr("postgresql+asyncpg://user:pass@db/platform"),
        object_store_mode="s3",
        object_store_endpoint="http://minio:9000",
        object_store_namespace_id="test-minio-primary",
        object_store_bucket="classrooms",
        object_store_region="us-east-1",
        object_store_tenant_credentials_dir=tmp_path,
    )


def test_tenant_secret_reference_is_server_derived_and_opaque() -> None:
    tenant_secret_ref = _module().tenant_secret_ref

    expected = f"tenant_{hashlib.sha256(b'tenant-a').hexdigest()[:16]}"
    assert tenant_secret_ref("tenant-a") == expected
    assert "tenant-a" not in tenant_secret_ref("tenant-a")


def test_tenant_policy_allows_only_the_exact_tenant_prefix() -> None:
    build_tenant_policy = _module().build_tenant_policy

    policy = build_tenant_policy(
        bucket="classrooms",
        tenant_prefix="tenants/tenant-a/",
    )
    statements = policy["Statement"]

    assert statements == [
        {
            "Sid": "TenantPrefixList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ["arn:aws:s3:::classrooms"],
            "Condition": {"StringLike": {"s3:prefix": ["tenants/tenant-a/*"]}},
        },
        {
            "Sid": "TenantObjects",
            "Effect": "Allow",
            "Action": [
                "s3:DeleteObject",
                "s3:GetObject",
                "s3:ListBucketMultipartUploads",
                "s3:ListMultipartUploadParts",
                "s3:PutObject",
            ],
            "Resource": ["arn:aws:s3:::classrooms/tenants/tenant-a/*"],
        },
    ]


def test_provisioning_is_idempotent_and_rotation_revokes_only_after_publish(
    tmp_path: Path,
) -> None:
    module = _module()
    TenantCredentialPair = module.TenantCredentialPair
    TenantSecretStore = module.TenantSecretStore
    provision_tenant_storage = module.provision_tenant_storage

    events: list[str] = []

    class Admin:
        def __init__(self) -> None:
            self.created = 0

        async def create(self, *, tenant_id: str, policy: dict) -> TenantCredentialPair:
            self.created += 1
            events.append(f"create:{self.created}")
            return TenantCredentialPair(
                access_key=f"ACCESS{self.created}",
                secret_key=f"SECRET{self.created}",
            )

        async def verify(
            self,
            *,
            credentials: TenantCredentialPair,
            own_prefix: str,
            denied_prefix: str,
        ) -> None:
            events.append(f"verify:{credentials.access_key}")
            assert own_prefix == "tenants/tenant-a/"
            assert denied_prefix != own_prefix

        async def revoke(self, access_key: str) -> None:
            events.append(f"revoke:{access_key}")

    class Publisher:
        def __init__(self) -> None:
            self.current_ref: str | None = None

        async def current_secret_ref(self, tenant_id: str) -> str | None:
            assert tenant_id == "tenant-a"
            return self.current_ref

        async def publish(self, result) -> None:
            events.append(f"publish:{result.access_key_fingerprint}")
            self.current_ref = result.secret_ref

    admin = Admin()
    store = TenantSecretStore(tmp_path)
    publisher = Publisher()

    first = asyncio.run(
        provision_tenant_storage(
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            admin=admin,
            secret_store=store,
            publisher=publisher,
        )
    )
    second = asyncio.run(
        provision_tenant_storage(
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            admin=admin,
            secret_store=store,
            publisher=publisher,
        )
    )
    rotated = asyncio.run(
        provision_tenant_storage(
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            admin=admin,
            secret_store=store,
            publisher=publisher,
            rotate=True,
        )
    )
    rotated_again = asyncio.run(
        provision_tenant_storage(
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            admin=admin,
            secret_store=store,
            publisher=publisher,
            rotate=True,
        )
    )
    current = asyncio.run(
        provision_tenant_storage(
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            admin=admin,
            secret_store=store,
            publisher=publisher,
        )
    )

    assert first.secret_ref == tenant_secret_ref_for("tenant-a")
    assert second.secret_ref == first.secret_ref
    assert rotated.secret_ref.startswith(f"{first.secret_ref}/rotations/")
    assert rotated_again.secret_ref.startswith(f"{first.secret_ref}/rotations/")
    assert rotated_again.secret_ref != rotated.secret_ref
    assert current.secret_ref == rotated_again.secret_ref
    assert admin.created == 3
    assert "revoke:ACCESS2" in events
    assert events[-2] == "verify:ACCESS3"
    assert events[-1].startswith("publish:")


def tenant_secret_ref_for(tenant_id: str) -> str:
    return f"tenant_{hashlib.sha256(tenant_id.encode()).hexdigest()[:16]}"


def test_initial_publish_failure_can_retry_without_reusing_revoked_credentials(
    tmp_path: Path,
) -> None:
    module = _module()
    TenantCredentialPair = module.TenantCredentialPair
    TenantSecretStore = module.TenantSecretStore
    provision_tenant_storage = module.provision_tenant_storage

    class Admin:
        def __init__(self) -> None:
            self.created = 0
            self.revoked: set[str] = set()

        async def create(self, *, tenant_id: str, policy: dict) -> TenantCredentialPair:
            del tenant_id, policy
            self.created += 1
            return TenantCredentialPair(
                access_key=f"ACCESS{self.created}",
                secret_key=f"SECRET{self.created}",
            )

        async def verify(
            self,
            *,
            credentials: TenantCredentialPair,
            own_prefix: str,
            denied_prefix: str,
        ) -> None:
            del own_prefix, denied_prefix
            if credentials.access_key in self.revoked:
                raise ProvisioningStepError(
                    category="storage",
                    code="own_prefix_probe_failed",
                    retryable=False,
                )

        async def revoke(self, access_key: str) -> None:
            self.revoked.add(access_key)

    class Publisher:
        def __init__(self) -> None:
            self.fail = True
            self.current_ref: str | None = None

        async def current_secret_ref(self, tenant_id: str) -> str | None:
            assert tenant_id == "tenant-a"
            return self.current_ref

        async def publish(self, result) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("database unavailable")
            self.current_ref = result.secret_ref

    admin = Admin()
    publisher = Publisher()
    store = TenantSecretStore(tmp_path)
    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(
            provision_tenant_storage(
                settings=_settings(tmp_path),
                tenant_id="tenant-a",
                admin=admin,
                secret_store=store,
                publisher=publisher,
            )
        )

    recovered = asyncio.run(
        provision_tenant_storage(
            settings=_settings(tmp_path),
            tenant_id="tenant-a",
            admin=admin,
            secret_store=store,
            publisher=publisher,
        )
    )

    assert admin.created == 2
    assert "ACCESS1" in admin.revoked
    assert recovered.secret_ref.startswith(f"{tenant_secret_ref_for('tenant-a')}/rotations/")
    assert publisher.current_ref == recovered.secret_ref


def test_cli_run_binds_credential_publisher_to_selected_database_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    settings = _settings(tmp_path)
    config_path = tmp_path / "platform.json"
    events: list[object] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("dispose")

    engine = Engine()

    def create_engine(url: str):
        events.append(("engine", url))
        return engine

    class Publisher:
        def __init__(self, tenant_id: str, database_engine) -> None:
            events.append(("publisher", tenant_id, database_engine))

    class Admin:
        @classmethod
        def from_settings(cls, selected_settings):
            assert selected_settings is settings
            return cls()

    async def provision(**kwargs) -> None:
        events.append(("provision", kwargs["publisher"]))

    monkeypatch.setattr(module, "load_platform_settings", lambda path: settings)
    monkeypatch.setattr(module, "create_async_engine", create_engine, raising=False)
    monkeypatch.setattr(module, "SqlAlchemyTenantCredentialPublisher", Publisher)
    monkeypatch.setattr(module, "RuntimeMinioTenantStorageAdmin", Admin)
    monkeypatch.setattr(module, "provision_tenant_storage", provision)

    asyncio.run(module._run(tenant_id="tenant-a", rotate=False, config=config_path))

    assert events[0] == (
        "engine",
        settings.database_url.get_secret_value(),
    )
    assert events[1] == ("publisher", "tenant-a", engine)
    assert events[-1] == "dispose"


def test_secret_store_recovers_after_interrupted_credential_pair_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    TenantCredentialPair = module.TenantCredentialPair
    store = module.TenantSecretStore(tmp_path)
    original_write = storage_module._write_secret
    write_count = 0

    def interrupted_write(path: Path, value: str) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("simulated interrupted secret write")
        original_write(path, value)

    monkeypatch.setattr(storage_module, "_write_secret", interrupted_write)
    with pytest.raises(OSError, match="simulated interrupted secret write"):
        store.publish(
            tenant_id="tenant-a",
            credentials=TenantCredentialPair("ACCESS1", "SECRET1"),
            rotate=False,
        )
    assert not store.exists(store.base_ref("tenant-a"))

    monkeypatch.setattr(storage_module, "_write_secret", original_write)
    recovered_ref = store.publish(
        tenant_id="tenant-a",
        credentials=TenantCredentialPair("ACCESS2", "SECRET2"),
        rotate=False,
    )

    assert recovered_ref == store.base_ref("tenant-a")
    assert store.load(recovered_ref, tenant_id="tenant-a") == TenantCredentialPair(
        "ACCESS2",
        "SECRET2",
    )


def test_runtime_s3_worker_injects_real_admin_adapter(tmp_path: Path) -> None:
    from deeptutor.teaching.provisioning_worker import (
        ConfiguredS3TenantStorageProvisioner,
        UnavailableS3TenantStorageAdmin,
        build_provisioning_worker,
    )

    bootstrap_access = tmp_path / "minio-access"
    bootstrap_secret = tmp_path / "minio-secret"
    bootstrap_access.write_text("ROOT_ACCESS", encoding="utf-8")
    bootstrap_secret.write_text("ROOT_SECRET", encoding="utf-8")
    settings = _settings(tmp_path).model_copy(
        update={
            "minio_bootstrap_access_key_file": bootstrap_access,
            "minio_bootstrap_secret_key_file": bootstrap_secret,
        }
    )

    worker = build_provisioning_worker(settings=settings, worker_id="worker-a")
    provisioner = worker._storage_provisioner

    assert isinstance(provisioner, ConfiguredS3TenantStorageProvisioner)
    assert not isinstance(provisioner._admin, UnavailableS3TenantStorageAdmin)


def test_tenant_provisioner_is_a_real_process_entrypoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from deeptutor.teaching import processes

    calls: list[bool] = []

    class Worker:
        async def run_once(self) -> bool:
            calls.append(True)
            return True

    monkeypatch.setattr(processes, "build_provisioning_worker", lambda **_kwargs: Worker())

    assert "tenant-provisioner" in processes.PROCESS_NAMES
    handled = asyncio.run(
        processes.run_process(
            "tenant-provisioner",
            once=True,
            settings=_settings(tmp_path),
        )
    )
    assert handled is True
    assert calls == [True]
