"""Server-derived keys and manifests for classroom artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

_CONTENT_TYPES_BY_SUFFIX = {
    ".gif": frozenset({"image/gif"}),
    ".css": frozenset({"text/css"}),
    ".html": frozenset({"text/html"}),
    ".htm": frozenset({"text/html"}),
    ".js": frozenset({"application/javascript", "text/javascript"}),
    ".json": frozenset({"application/json"}),
    ".jpeg": frozenset({"image/jpeg"}),
    ".jpg": frozenset({"image/jpeg"}),
    ".mermaid": frozenset({"text/vnd.mermaid"}),
    ".mmd": frozenset({"text/vnd.mermaid"}),
    ".mp3": frozenset({"audio/mpeg"}),
    ".mp4": frozenset({"video/mp4"}),
    ".png": frozenset({"image/png"}),
    ".pptx": frozenset(
        {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
    ),
    ".svg": frozenset({"image/svg+xml"}),
    ".wav": frozenset({"audio/wav", "audio/x-wav"}),
    ".webp": frozenset({"image/webp"}),
    ".zip": frozenset({"application/zip"}),
}


class ArtifactManifestError(ValueError):
    """A promotion manifest is not safe or internally consistent."""


def _identifier(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field} must be a single safe path segment")
    return value


def _relative_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or PureWindowsPath(value).drive
    ):
        raise ValueError("relative_name must be a safe relative path")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("relative_name must be a safe relative path")
    return value


def classroom_artifact_key(
    tenant_id: str,
    asset_id: str,
    version: int,
    relative_name: str,
) -> str:
    """Derive the only supported permanent classroom artifact key."""

    tenant = _identifier(tenant_id, "tenant_id")
    asset = _identifier(asset_id, "asset_id")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("version must be a positive integer")
    name = _relative_name(relative_name)
    return f"tenants/{tenant}/classrooms/{asset}/versions/{version}/{name}"


def temporary_artifact_key(
    tenant_id: str,
    job_id: str,
    relative_name: str,
) -> str:
    """Derive a tenant-scoped staging key for one generation job."""

    tenant = _identifier(tenant_id, "tenant_id")
    job = _identifier(job_id, "job_id")
    name = _relative_name(relative_name)
    return f"tenants/{tenant}/temporary/{job}/{name}"


def tenant_artifact_prefix(tenant_id: str) -> str:
    """Return the sole object-store prefix assigned to a tenant."""

    return f"tenants/{_identifier(tenant_id, 'tenant_id')}/"


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Integrity metadata for an object accepted by a classroom store."""

    key: str
    sha256: str
    size: int
    content_type: str = "application/octet-stream"
    ownership_token: str | None = field(default=None, repr=False, compare=False)
    revision: str | None = field(default=None, repr=False, compare=False)
    version_id: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ArtifactManifestEntry:
    """One generated file declared by the minimal classroom DSL contract."""

    relative_name: str
    content_type: str
    sha256: str
    size: int

    def validate(self) -> None:
        try:
            name = _relative_name(self.relative_name)
        except ValueError as exc:
            raise ArtifactManifestError("manifest entry name is unsafe") from exc
        if name.startswith(".deeptutor-"):
            raise ArtifactManifestError("manifest entry name is reserved")
        if (
            not isinstance(self.content_type, str)
            or not self.content_type
            or self.content_type != self.content_type.strip().lower()
            or ";" in self.content_type
        ):
            raise ArtifactManifestError("manifest content type is invalid")
        suffix = Path(name).suffix.lower()
        allowed_types = _CONTENT_TYPES_BY_SUFFIX.get(suffix)
        if allowed_types is None or self.content_type not in allowed_types:
            raise ArtifactManifestError("manifest content type does not match the file extension")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ArtifactManifestError("manifest sha256 is invalid")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0:
            raise ArtifactManifestError("manifest size is invalid")


@dataclass(frozen=True, slots=True)
class ClassroomArtifactManifest:
    """Minimal promotion contract; it is intentionally not the OpenMAIC DSL."""

    tenant_id: str
    job_id: str
    asset_id: str
    version: int
    entries: tuple[ArtifactManifestEntry, ...]

    def validate_for_tenant(self, tenant_id: str) -> None:
        try:
            _identifier(self.tenant_id, "tenant_id")
            _identifier(self.job_id, "job_id")
            _identifier(self.asset_id, "asset_id")
        except ValueError as exc:
            raise ArtifactManifestError("manifest identity is unsafe") from exc
        if self.tenant_id != tenant_id:
            raise ArtifactManifestError("manifest does not belong to the current tenant")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ArtifactManifestError("manifest version is invalid")
        if not isinstance(self.entries, tuple) or not self.entries:
            raise ArtifactManifestError("manifest must declare at least one entry")
        names: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, ArtifactManifestEntry):
                raise ArtifactManifestError("manifest entry is invalid")
            entry.validate()
            if entry.relative_name in names:
                raise ArtifactManifestError("manifest entry names must be unique")
            names.add(entry.relative_name)
