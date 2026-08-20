"""Provision or rotate one tenant's prefix-scoped MinIO credential."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from deeptutor.services.config import load_platform_settings
from deeptutor.teaching.minio_tenant_storage import (
    RuntimeMinioTenantStorageAdmin,
    TenantCredentialPair,
    TenantSecretStore,
    build_tenant_policy,
    provision_tenant_storage,
    secret_ref_is_bound_to_tenant,
    tenant_secret_ref,
)
from deeptutor.teaching.models import TenantStorageCredential, TenantStorageState
from deeptutor.teaching.provisioning_worker import StorageProvisioningResult


class SqlAlchemyTenantCredentialPublisher:
    """Atomically publish active credential metadata without secret material."""

    def __init__(self, tenant_id: str, database_engine: AsyncEngine) -> None:
        self._tenant_id = tenant_id
        self._session_factory = async_sessionmaker(
            database_engine,
            expire_on_commit=False,
        )

    async def current_secret_ref(self, tenant_id: str) -> str | None:
        if tenant_id != self._tenant_id:
            raise ValueError("tenant credential publisher binding mismatch")
        async with self._session_factory() as session:
            record = await session.scalar(
                select(TenantStorageCredential).where(
                    TenantStorageCredential.tenant_id == tenant_id,
                    TenantStorageCredential.status == "active",
                )
            )
        return None if record is None else record.secret_ref

    async def publish(self, result: StorageProvisioningResult) -> None:
        result.validate(self._tenant_id)
        async with self._session_factory() as session:
            async with session.begin():
                now = func.now()
                await session.execute(
                    insert(TenantStorageCredential)
                    .values(
                        tenant_id=self._tenant_id,
                        secret_ref=result.secret_ref,
                        access_key_fingerprint=result.access_key_fingerprint,
                        status="active",
                        rotated_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[TenantStorageCredential.tenant_id],
                        set_={
                            "secret_ref": result.secret_ref,
                            "access_key_fingerprint": result.access_key_fingerprint,
                            "status": "active",
                            "rotated_at": now,
                            "updated_at": now,
                        },
                    )
                )
                await session.execute(
                    insert(TenantStorageState)
                    .values(
                        tenant_id=self._tenant_id,
                        mode=result.mode,
                        policy_version=result.policy_version,
                        policy_payload=result.policy_payload,
                        policy_hash=result.policy_hash,
                        credential_secret_ref=result.secret_ref,
                        credential_fingerprint=result.access_key_fingerprint,
                        status="active",
                        verified_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=[TenantStorageState.tenant_id],
                        set_={
                            "mode": result.mode,
                            "policy_version": result.policy_version,
                            "policy_payload": result.policy_payload,
                            "policy_hash": result.policy_hash,
                            "credential_secret_ref": result.secret_ref,
                            "credential_fingerprint": result.access_key_fingerprint,
                            "status": "active",
                            "verified_at": now,
                            "updated_at": now,
                        },
                    )
                )


async def _run(*, tenant_id: str, rotate: bool, config: Path | None) -> None:
    settings = load_platform_settings(config)
    root = settings.object_store_tenant_credentials_dir
    if not settings.enabled or settings.object_store_mode != "s3" or root is None:
        raise ValueError("platform S3 tenant provisioning is not configured")
    if settings.database_url is None:
        raise ValueError("platform database is not configured")
    database_engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        await provision_tenant_storage(
            settings=settings,
            tenant_id=tenant_id,
            admin=RuntimeMinioTenantStorageAdmin.from_settings(settings),
            secret_store=TenantSecretStore(root),
            publisher=SqlAlchemyTenantCredentialPublisher(tenant_id, database_engine),
            rotate=rotate,
        )
    finally:
        await database_engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision tenant MinIO credentials")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--config", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    asyncio.run(
        _run(
            tenant_id=arguments.tenant_id,
            rotate=arguments.rotate,
            config=arguments.config,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RuntimeMinioTenantStorageAdmin",
    "SqlAlchemyTenantCredentialPublisher",
    "TenantCredentialPair",
    "TenantSecretStore",
    "build_tenant_policy",
    "main",
    "provision_tenant_storage",
    "secret_ref_is_bound_to_tenant",
    "tenant_secret_ref",
]
