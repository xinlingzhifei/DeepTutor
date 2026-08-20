"""Initialize missing private-platform secret files without overwriting them."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import os
from pathlib import Path
import secrets
import stat
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deeptutor.teaching.secret_permissions import restrict_secret_file

GENERATED_SECRET_SPECS: dict[str, int] = {
    "platform_database_password": 32,
    "platform_database_app_password": 32,
    "platform_database_migration_password": 32,
    "minio_bootstrap_access_key": 20,
    "minio_bootstrap_secret_key": 40,
    "classroom_ticket_secret": 32,
    "openmaic_service_secret": 32,
}


def _default_value(name: str, bytes_count: int) -> str:
    if name == "minio_bootstrap_access_key":
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(secrets.choice(alphabet) for _ in range(bytes_count))
    if name == "minio_bootstrap_secret_key":
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        return "".join(secrets.choice(alphabet) for _ in range(bytes_count))
    return secrets.token_urlsafe(bytes_count)


def initialize_secret(
    path: Path,
    *,
    bytes_count: int,
    value_factory: Callable[[], str] | None = None,
) -> bool:
    """Create one mode-0600 secret exactly once.

    The value is fully written and permissioned under a same-directory staging
    name, then published with a no-overwrite hard link.  Readers therefore
    never observe a partially written first-boot value.
    """

    target = Path(path)
    if bytes_count < 16:
        raise ValueError("secret entropy must be at least 16 bytes")
    if target.exists() or target.is_symlink():
        return False
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink():
        raise ValueError("secret directory must not be a symlink")
    value = (value_factory or (lambda: secrets.token_urlsafe(bytes_count)))()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("generated secret is invalid")
    staging = target.parent / f".{target.name}.{secrets.token_urlsafe(12)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        payload = f"{value}\n".encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        restrict_secret_file(staging)
        try:
            os.link(staging, target)
        except FileExistsError:
            return False
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass


def initialize_platform_secrets(secret_dir: Path) -> tuple[Path, ...]:
    root = Path(secret_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("platform secret directory is unsafe")
    created: list[Path] = []
    for name, bytes_count in GENERATED_SECRET_SPECS.items():
        target = root / name
        if initialize_secret(
            target,
            bytes_count=bytes_count,
            value_factory=lambda name=name, count=bytes_count: _default_value(name, count),
        ):
            created.append(target)
    return tuple(created)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize missing platform secrets")
    parser.add_argument(
        "--secret-dir",
        type=Path,
        default=Path("data/system/secrets"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    initialize_platform_secrets(arguments.secret_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
