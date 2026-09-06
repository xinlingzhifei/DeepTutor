"""Sign one immutable OpenMAIC observer attestation with an external Ed25519 key."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from openmaic_smoke_contract import (  # noqa: E402
    parse_openmaic_shared_ingress_observer_attestation,
    validate_openmaic_shared_ingress_observer_bound_context,
)

_MAX_ATTESTATION_BYTES = 64 * 1024
_MAX_PRIVATE_KEY_BYTES = 16 * 1024


class ObserverAttestationSignerError(RuntimeError):
    """Secret-free signing failure."""


def _identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _durable_identity(details: os.stat_result) -> tuple[int, ...]:
    """Compare one published inode without link-count ctime churn."""

    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _object_identity(details: os.stat_result) -> tuple[int, ...]:
    """Identify one filesystem object without mutable content metadata."""

    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_gid,
    )


def _read_descriptor(descriptor: int, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_regular_file(
    path: Path,
    *,
    limit: int,
    owner_only: bool,
) -> tuple[Path, int, tuple[int, ...], bytes]:
    supplied = Path(path)
    descriptor: int | None = None
    try:
        if not supplied.is_absolute():
            raise OSError
        resolved = supplied.resolve(strict=True)
        if supplied != resolved or any(
            parent.is_symlink() for parent in (resolved, *resolved.parents)
        ):
            raise OSError
        before = resolved.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise OSError
        if (
            owner_only
            and os.name == "posix"
            and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077)
        ):
            raise OSError
        descriptor = os.open(
            resolved,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        body = _read_descriptor(descriptor, limit=limit)
        after = os.fstat(descriptor)
        final = resolved.lstat()
        if (
            len(body) > limit
            or _identity(before) != _identity(opened)
            or _identity(opened) != _identity(after)
            or _identity(opened) != _identity(final)
        ):
            raise OSError
        stable_descriptor = descriptor
        descriptor = None
        return resolved, stable_descriptor, _identity(opened), body
    except OSError as exc:
        raise ObserverAttestationSignerError("observer_attestation_signing_input_invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_regular_file(path: Path, *, limit: int, owner_only: bool) -> bytes:
    _resolved, descriptor, _expected_identity, body = _open_regular_file(
        path,
        limit=limit,
        owner_only=owner_only,
    )
    try:
        return body
    finally:
        os.close(descriptor)


def _assert_open_file_unchanged(
    path: Path,
    descriptor: int,
    *,
    expected_identity: tuple[int, ...],
    expected_body: bytes,
    limit: int,
) -> None:
    try:
        before = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        body = _read_descriptor(descriptor, limit=limit)
        after = os.fstat(descriptor)
        final = path.lstat()
        if (
            body != expected_body
            or _identity(before) != expected_identity
            or _identity(after) != expected_identity
            or _identity(final) != expected_identity
        ):
            raise OSError
    except OSError as exc:
        raise ObserverAttestationSignerError("observer_attestation_changed_during_signing") from exc


def _invalidate_signature(descriptor: int) -> None:
    try:
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
    except OSError as exc:
        raise ObserverAttestationSignerError(
            "observer_attestation_signature_invalidation_failed"
        ) from exc


def _close_descriptor_preserving_failure(descriptor: int, *, failure: str) -> None:
    active_error = sys.exception()
    try:
        os.close(descriptor)
    except OSError as exc:
        if active_error is None:
            raise ObserverAttestationSignerError(failure) from exc


def _invalidate_open_signature(
    descriptor: int,
    *,
    published: bool,
    target: Path,
    expected_object_identity: tuple[int, ...] | None,
) -> None:
    _invalidate_signature(descriptor)
    try:
        invalidated = os.fstat(descriptor)
    except OSError as exc:
        raise ObserverAttestationSignerError(
            "observer_attestation_signature_identity_lost"
        ) from exc
    if invalidated.st_size != 0 or (
        expected_object_identity is not None
        and _object_identity(invalidated) != expected_object_identity
    ):
        raise ObserverAttestationSignerError("observer_attestation_signature_identity_lost")
    if not published:
        return
    if expected_object_identity is None or (
        _published_signature_state(
            target,
            expected_object_identity=expected_object_identity,
        )
        not in {"missing", "invalidated"}
    ):
        raise ObserverAttestationSignerError("observer_attestation_signature_identity_lost")


def _invalidate_exact_signature_alias(
    alias: Path,
    *,
    expected_identity: tuple[int, ...],
    expected_object_identity: tuple[int, ...],
) -> str:
    descriptor: int | None = None
    try:
        try:
            before = alias.lstat()
        except FileNotFoundError:
            return "missing"
        if not stat.S_ISREG(before.st_mode) or _durable_identity(before) != expected_identity:
            return "foreign"
        try:
            descriptor = os.open(
                alias,
                os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return "missing"
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _durable_identity(opened) != expected_identity:
            return "foreign"
        _invalidate_signature(descriptor)
        invalidated = os.fstat(descriptor)
        if _object_identity(invalidated) != expected_object_identity or invalidated.st_size != 0:
            raise ObserverAttestationSignerError("observer_attestation_signature_identity_lost")
        descriptor_to_close = descriptor
        descriptor = None
        os.close(descriptor_to_close)
        try:
            final = alias.lstat()
        except FileNotFoundError:
            return "missing"
        if (
            stat.S_ISREG(final.st_mode)
            and _object_identity(final) == expected_object_identity
            and final.st_size == 0
        ):
            return "invalidated"
        return "foreign"
    except OSError as exc:
        raise ObserverAttestationSignerError(
            "observer_attestation_signature_identity_lost"
        ) from exc
    finally:
        if descriptor is not None:
            descriptor_to_close = descriptor
            descriptor = None
            try:
                os.close(descriptor_to_close)
            except OSError as exc:
                raise ObserverAttestationSignerError(
                    "observer_attestation_signature_identity_lost"
                ) from exc


def _published_signature_state(
    target: Path,
    *,
    expected_object_identity: tuple[int, ...],
) -> str:
    try:
        details = target.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise ObserverAttestationSignerError(
            "observer_attestation_signature_identity_lost"
        ) from exc
    if (
        stat.S_ISREG(details.st_mode)
        and _object_identity(details) == expected_object_identity
        and details.st_size == 0
    ):
        return "invalidated"
    return "foreign"


def _invalidate_published_signature(
    target: Path,
    staged: Path,
    *,
    expected_identity: tuple[int, ...],
    expected_object_identity: tuple[int, ...],
) -> None:
    failures: list[Exception] = []
    try:
        target_status = _invalidate_exact_signature_alias(
            target,
            expected_identity=expected_identity,
            expected_object_identity=expected_object_identity,
        )
    except ObserverAttestationSignerError as exc:
        failures.append(exc)
        target_status = "foreign"
    if target_status == "invalidated":
        return
    try:
        staged_status = _invalidate_exact_signature_alias(
            staged,
            expected_identity=expected_identity,
            expected_object_identity=expected_object_identity,
        )
    except ObserverAttestationSignerError as exc:
        failures.append(exc)
        staged_status = "foreign"
    if target_status == "missing" and staged_status in {"missing", "invalidated"}:
        try:
            if (
                _published_signature_state(
                    target,
                    expected_object_identity=expected_object_identity,
                )
                == "missing"
            ):
                return
        except ObserverAttestationSignerError as exc:
            failures.append(exc)
    error = ObserverAttestationSignerError("observer_attestation_signature_identity_lost")
    if failures:
        raise error from BaseExceptionGroup(
            "observer signature aliases could not be safely invalidated",
            failures,
        )
    raise error


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_attestation_binding(
    body: bytes,
    *,
    expected_release_run_id: str | None,
    expected_environment_id: str | None,
    expected_observer_id: str | None,
    expected_candidate_health_url: str | None,
    expected_candidate_sha256: str | None,
    expected_image_lock_sha256: str | None,
    expected_public_key_spki_sha256: str | None,
    trusted_now: datetime,
) -> None:
    expected = (
        expected_release_run_id,
        expected_environment_id,
        expected_observer_id,
        expected_candidate_health_url,
        expected_candidate_sha256,
        expected_image_lock_sha256,
        expected_public_key_spki_sha256,
    )
    try:
        if any(value is not None for value in expected) and any(
            value is None for value in expected
        ):
            raise ValueError
        document = json.loads(body)
        release_run = document.get("releaseRun") if isinstance(document, dict) else None
        if (
            not isinstance(release_run, dict)
            or set(release_run) != {"runId", "environmentId"}
            or not all(isinstance(value, str) for value in release_run.values())
        ):
            raise ValueError
        report = parse_openmaic_shared_ingress_observer_attestation(
            body,
            release_run=release_run,
        )
        observer = report.get("observer")
        if not isinstance(observer, dict):
            raise ValueError
        if expected_release_run_id is not None and (
            release_run.get("runId") != expected_release_run_id
            or release_run.get("environmentId") != expected_environment_id
            or observer.get("observerId") != expected_observer_id
            or observer.get("candidateHealthUrl") != expected_candidate_health_url
        ):
            raise ValueError
        if expected_candidate_sha256 is not None:
            assert expected_image_lock_sha256 is not None
            assert expected_public_key_spki_sha256 is not None
            validate_openmaic_shared_ingress_observer_bound_context(
                report,
                expected_candidate_binding={
                    "candidateSha256": expected_candidate_sha256,
                    "imageLockSha256": expected_image_lock_sha256,
                },
                expected_public_key_spki_sha256=expected_public_key_spki_sha256,
                trusted_now=trusted_now,
            )
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ObserverAttestationSignerError("observer_attestation_binding_invalid") from exc


def sign_observer_attestation(
    attestation_path: Path,
    signature_path: Path,
    signing_key_path: Path,
    *,
    expected_release_run_id: str | None = None,
    expected_environment_id: str | None = None,
    expected_observer_id: str | None = None,
    expected_candidate_health_url: str | None = None,
    expected_candidate_sha256: str | None = None,
    expected_image_lock_sha256: str | None = None,
    expected_public_key_spki_sha256: str | None = None,
    trusted_now: datetime | None = None,
) -> bytes:
    attestation, attestation_descriptor, attestation_identity, body = _open_regular_file(
        attestation_path,
        limit=_MAX_ATTESTATION_BYTES,
        owner_only=False,
    )
    attestation_descriptor_to_close: int | None = attestation_descriptor
    try:
        _validate_attestation_binding(
            body,
            expected_release_run_id=expected_release_run_id,
            expected_environment_id=expected_environment_id,
            expected_observer_id=expected_observer_id,
            expected_candidate_health_url=expected_candidate_health_url,
            expected_candidate_sha256=expected_candidate_sha256,
            expected_image_lock_sha256=expected_image_lock_sha256,
            expected_public_key_spki_sha256=expected_public_key_spki_sha256,
            trusted_now=trusted_now or datetime.now(timezone.utc),
        )
        key_body = _read_regular_file(
            signing_key_path,
            limit=_MAX_PRIVATE_KEY_BYTES,
            owner_only=True,
        )
        try:
            private_key = serialization.load_pem_private_key(key_body, password=None)
        except (TypeError, ValueError) as exc:
            raise ObserverAttestationSignerError(
                "observer_attestation_signing_key_invalid"
            ) from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ObserverAttestationSignerError("observer_attestation_signing_key_invalid")
        if expected_public_key_spki_sha256 is not None:
            public_key_spki = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if hashlib.sha256(public_key_spki).hexdigest() != expected_public_key_spki_sha256:
                raise ObserverAttestationSignerError("observer_attestation_signing_key_invalid")
        signature = base64.b64encode(private_key.sign(body)) + b"\n"

        target = Path(signature_path)
        if not target.is_absolute():
            raise OSError
        parent = target.parent.resolve(strict=True)
        if target.parent != parent or any(item.is_symlink() for item in (parent, *parent.parents)):
            raise OSError
        staged = parent / (f".{target.name}.staged-{os.getpid()}-{time.monotonic_ns()}")
        signature_descriptor: int | None = None
        published_identity: tuple[int, ...] | None = None
        published_object_identity: tuple[int, ...] | None = None
        published = False
        try:
            signature_descriptor = os.open(
                staged,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            view = memoryview(signature)
            while view:
                view = view[os.write(signature_descriptor, view) :]
            os.fsync(signature_descriptor)
            staged_details = os.fstat(signature_descriptor)
            staged_path_details = staged.lstat()
            if _identity(staged_details) != _identity(staged_path_details) or (
                os.name == "posix" and stat.S_IMODE(staged_details.st_mode) != 0o600
            ):
                raise OSError
            _assert_open_file_unchanged(
                attestation,
                attestation_descriptor,
                expected_identity=attestation_identity,
                expected_body=body,
                limit=_MAX_ATTESTATION_BYTES,
            )
            published_identity = _durable_identity(staged_details)
            published_object_identity = _object_identity(staged_details)
            os.link(staged, target, follow_symlinks=False)
            published = True
            linked_descriptor = os.fstat(signature_descriptor)
            if _identity(staged.lstat()) != _identity(linked_descriptor) or _identity(
                target.lstat()
            ) != _identity(linked_descriptor):
                raise OSError
            _fsync_directory(parent)
            _assert_open_file_unchanged(
                attestation,
                attestation_descriptor,
                expected_identity=attestation_identity,
                expected_body=body,
                limit=_MAX_ATTESTATION_BYTES,
            )
            if _identity(staged.lstat()) != _identity(linked_descriptor):
                raise OSError
            descriptor_to_close = signature_descriptor
            signature_descriptor = None
            os.close(descriptor_to_close)
            staged.unlink()
            if _durable_identity(target.lstat()) != published_identity:
                raise OSError
            _fsync_directory(parent)
            descriptor_to_close = attestation_descriptor_to_close
            attestation_descriptor_to_close = None
            assert descriptor_to_close is not None
            _close_descriptor_preserving_failure(
                descriptor_to_close,
                failure="observer_attestation_signing_input_invalid",
            )
        except ObserverAttestationSignerError as exc:
            try:
                if signature_descriptor is not None:
                    _invalidate_open_signature(
                        signature_descriptor,
                        published=published,
                        target=target,
                        expected_object_identity=published_object_identity,
                    )
                elif (
                    published
                    and published_identity is not None
                    and published_object_identity is not None
                ):
                    _invalidate_published_signature(
                        target,
                        staged,
                        expected_identity=published_identity,
                        expected_object_identity=published_object_identity,
                    )
            except ObserverAttestationSignerError as invalidation_error:
                raise ObserverAttestationSignerError(
                    "observer_attestation_signature_identity_lost"
                ) from BaseExceptionGroup(
                    "observer signature publication and invalidation both failed",
                    (exc, invalidation_error),
                )
            raise
        except OSError as exc:
            try:
                if signature_descriptor is not None:
                    _invalidate_open_signature(
                        signature_descriptor,
                        published=published,
                        target=target,
                        expected_object_identity=published_object_identity,
                    )
                elif (
                    published
                    and published_identity is not None
                    and published_object_identity is not None
                ):
                    _invalidate_published_signature(
                        target,
                        staged,
                        expected_identity=published_identity,
                        expected_object_identity=published_object_identity,
                    )
            except ObserverAttestationSignerError as invalidation_error:
                raise ObserverAttestationSignerError(
                    "observer_attestation_signature_identity_lost"
                ) from BaseExceptionGroup(
                    "observer signature publication and invalidation both failed",
                    (exc, invalidation_error),
                )
            raise ObserverAttestationSignerError(
                "observer_attestation_signature_publish_failed"
            ) from exc
        finally:
            if signature_descriptor is not None:
                descriptor_to_close = signature_descriptor
                signature_descriptor = None
                _close_descriptor_preserving_failure(
                    descriptor_to_close,
                    failure="observer_attestation_signature_publish_failed",
                )
        return signature
    except OSError as exc:
        raise ObserverAttestationSignerError(
            "observer_attestation_signature_publish_failed"
        ) from exc
    finally:
        if attestation_descriptor_to_close is not None:
            descriptor_to_close = attestation_descriptor_to_close
            attestation_descriptor_to_close = None
            _close_descriptor_preserving_failure(
                descriptor_to_close,
                failure="observer_attestation_signing_input_invalid",
            )


def _wait_for_attestation(path: Path, *, timeout_seconds: float) -> None:
    if not path.is_absolute() or not 0 < timeout_seconds <= 300:
        raise ObserverAttestationSignerError("observer_attestation_wait_invalid")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if path.exists() or path.is_symlink():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ObserverAttestationSignerError("observer_attestation_wait_timeout")
        time.sleep(min(0.1, remaining))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--signing-key-file", type=Path, required=True)
    parser.add_argument("--wait-timeout-seconds", type=float, required=True)
    parser.add_argument("--expected-release-run-id", required=True)
    parser.add_argument("--expected-environment-id", required=True)
    parser.add_argument("--expected-observer-id", required=True)
    parser.add_argument("--expected-candidate-health-url", required=True)
    parser.add_argument("--expected-candidate-sha256", required=True)
    parser.add_argument("--expected-image-lock-sha256", required=True)
    parser.add_argument("--expected-public-key-spki-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        _wait_for_attestation(
            arguments.attestation,
            timeout_seconds=arguments.wait_timeout_seconds,
        )
        sign_observer_attestation(
            arguments.attestation,
            arguments.signature,
            arguments.signing_key_file,
            expected_release_run_id=arguments.expected_release_run_id,
            expected_environment_id=arguments.expected_environment_id,
            expected_observer_id=arguments.expected_observer_id,
            expected_candidate_health_url=arguments.expected_candidate_health_url,
            expected_candidate_sha256=arguments.expected_candidate_sha256,
            expected_image_lock_sha256=arguments.expected_image_lock_sha256,
            expected_public_key_spki_sha256=(arguments.expected_public_key_spki_sha256),
        )
        return 0
    except ObserverAttestationSignerError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ObserverAttestationSignerError",
    "sign_observer_attestation",
]
