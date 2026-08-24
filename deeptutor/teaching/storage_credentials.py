"""Secret-file resolution for per-tenant object-store credentials."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path, PureWindowsPath
from typing import Protocol

from sqlalchemy import and_, select

from deeptutor.teaching.database import platform_session
from deeptutor.teaching.models import (
    Tenant,
    TenantStorageCredential,
    TenantStorageState,
)

_ACCESS_KEY_FILE = "object-store-access-key"
_SECRET_KEY_FILE = "object-store-secret-key"


class StorageCredentialError(ValueError):
    """A tenant credential record or secret directory is unsafe."""


@dataclass(frozen=True, slots=True)
class TenantStorageCredentialRecord:
    """Secret-free platform record used by the object-store factory."""

    tenant_id: str
    secret_ref: str
    access_key_fingerprint: str
    status: str


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedStorageCredentials:
    """In-memory credentials whose representation is always redacted."""

    tenant_id: str
    access_key: str
    secret_key: str

    def __repr__(self) -> str:
        return (
            "ResolvedStorageCredentials("
            "tenant_id=<redacted>, access_key=<redacted>, secret_key=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class StorageHealthInventory:
    """Aggregate-only active tenant credential inventory for S3 health."""

    active_tenants: int
    credentials: tuple[TenantStorageCredentialRecord, ...]
    unavailable_tenants: int


class ActiveStorageCredentialRepository(Protocol):
    """Lookup boundary for the active credential of one tenant."""

    async def get_active(
        self,
        tenant_id: str,
    ) -> TenantStorageCredentialRecord | None: ...


class StorageHealthInventoryRepository(Protocol):
    async def fetch_health_inventory(self) -> StorageHealthInventory: ...


def build_storage_health_inventory_statement():
    """Read every active tenant and only its active credential metadata."""

    return (
        select(
            Tenant.id.label("tenant_id"),
            TenantStorageCredential.secret_ref,
            TenantStorageCredential.access_key_fingerprint,
            TenantStorageCredential.status.label("credential_status"),
            TenantStorageState.tenant_id.label("storage_state_tenant_id"),
        )
        .select_from(Tenant)
        .outerjoin(
            TenantStorageCredential,
            and_(
                TenantStorageCredential.tenant_id == Tenant.id,
                TenantStorageCredential.status == "active",
            ),
        )
        .outerjoin(
            TenantStorageState,
            and_(
                TenantStorageState.tenant_id == Tenant.id,
                TenantStorageState.mode == "s3",
                TenantStorageState.status == "active",
                TenantStorageState.credential_secret_ref == TenantStorageCredential.secret_ref,
                TenantStorageState.credential_fingerprint
                == TenantStorageCredential.access_key_fingerprint,
            ),
        )
        .where(Tenant.status == "active")
        .order_by(Tenant.id)
    )


class SqlAlchemyStorageCredentialRepository:
    """Read active credential metadata from the platform schema."""

    async def get_active(
        self,
        tenant_id: str,
    ) -> TenantStorageCredentialRecord | None:
        async with platform_session() as session:
            model = await session.scalar(
                select(TenantStorageCredential)
                .join(Tenant, Tenant.id == TenantStorageCredential.tenant_id)
                .where(
                    TenantStorageCredential.tenant_id == tenant_id,
                    TenantStorageCredential.status == "active",
                    Tenant.status == "active",
                )
            )
        if model is None:
            return None
        return TenantStorageCredentialRecord(
            tenant_id=model.tenant_id,
            secret_ref=model.secret_ref,
            access_key_fingerprint=model.access_key_fingerprint,
            status=model.status,
        )

    async def fetch_health_inventory(self) -> StorageHealthInventory:
        async with platform_session() as session:
            rows = (await session.execute(build_storage_health_inventory_statement())).all()
        credentials: list[TenantStorageCredentialRecord] = []
        unavailable_tenants = 0
        for row in rows:
            if (
                row.secret_ref is None
                or row.access_key_fingerprint is None
                or row.credential_status != "active"
                or row.storage_state_tenant_id != row.tenant_id
            ):
                unavailable_tenants += 1
                continue
            credentials.append(
                TenantStorageCredentialRecord(
                    tenant_id=str(row.tenant_id),
                    secret_ref=str(row.secret_ref),
                    access_key_fingerprint=str(row.access_key_fingerprint),
                    status="active",
                )
            )
        return StorageHealthInventory(
            active_tenants=len(rows),
            credentials=tuple(credentials),
            unavailable_tenants=unavailable_tenants,
        )


def _safe_secret_ref(secret_ref: str) -> tuple[str, ...]:
    if (
        not isinstance(secret_ref, str)
        or not secret_ref
        or secret_ref.startswith("/")
        or secret_ref.endswith("/")
        or "\\" in secret_ref
        or ":" in secret_ref
        or "\x00" in secret_ref
        or PureWindowsPath(secret_ref).drive
    ):
        raise StorageCredentialError("tenant storage secret reference is unsafe")
    parts = tuple(secret_ref.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise StorageCredentialError("tenant storage secret reference is unsafe")
    return parts


def _read_secret_file(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise StorageCredentialError("tenant storage credential file is unavailable")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except StorageCredentialError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageCredentialError("tenant storage credential file could not be read") from exc
    if not value or "\x00" in value:
        raise StorageCredentialError("tenant storage credential file is empty")
    return value


class TenantStorageCredentialResolver:
    """Resolve a DB ``secret_ref`` below one explicit credential root."""

    def __init__(self, credentials_root: Path) -> None:
        root = Path(credentials_root)
        if not root.is_absolute():
            raise StorageCredentialError("tenant storage credentials root must be absolute")
        if root.is_symlink() or not root.is_dir():
            raise StorageCredentialError("tenant storage credentials root is unavailable")
        try:
            self._root = root.resolve(strict=True)
        except OSError as exc:
            raise StorageCredentialError("tenant storage credentials root is unavailable") from exc

    def _secret_directory(self, secret_ref: str) -> Path:
        parts = _safe_secret_ref(secret_ref)
        current = self._root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise StorageCredentialError("tenant storage secret directory is unsafe")
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as exc:
            raise StorageCredentialError("tenant storage secret directory is unavailable") from exc
        if not resolved.is_dir():
            raise StorageCredentialError("tenant storage secret directory is unavailable")
        return resolved

    def resolve(
        self,
        record: TenantStorageCredentialRecord,
        *,
        tenant_id: str,
    ) -> ResolvedStorageCredentials:
        if record.tenant_id != tenant_id or record.status != "active":
            raise StorageCredentialError(
                "tenant storage credential is not active for the current tenant"
            )
        secret_parts = _safe_secret_ref(record.secret_ref)
        from deeptutor.teaching.minio_tenant_storage import tenant_secret_ref

        if secret_parts[0] not in {tenant_id, tenant_secret_ref(tenant_id)}:
            raise StorageCredentialError(
                "tenant storage secret reference is not bound to the current tenant"
            )
        directory = self._secret_directory(record.secret_ref)
        access_path = directory / _ACCESS_KEY_FILE
        secret_path = directory / _SECRET_KEY_FILE
        try:
            access_path.resolve(strict=True).relative_to(self._root)
        except (OSError, ValueError) as exc:
            raise StorageCredentialError("tenant storage credential file is unavailable") from exc
        access_key = _read_secret_file(access_path)
        fingerprint = hashlib.sha256(access_key.encode("utf-8")).hexdigest()
        expected = record.access_key_fingerprint
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
            or not hmac.compare_digest(fingerprint, expected)
        ):
            raise StorageCredentialError("tenant storage access key fingerprint does not match")
        try:
            secret_path.resolve(strict=True).relative_to(self._root)
        except (OSError, ValueError) as exc:
            raise StorageCredentialError("tenant storage credential file is unavailable") from exc
        secret_key = _read_secret_file(secret_path)
        return ResolvedStorageCredentials(
            tenant_id=tenant_id,
            access_key=access_key,
            secret_key=secret_key,
        )
