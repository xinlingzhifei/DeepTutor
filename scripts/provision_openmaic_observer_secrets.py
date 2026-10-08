"""Provision one run-bound OpenMAIC observer secret set."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from openmaic_observer_service import (
    OpenMAICObserverServiceError,
    _parse_utc,
    parse_observer_auth_expiry,
    validate_observer_endpoints,
)

_RELEASE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PUBLIC_KEY_FILE = "openmaic-observer-attestation-public-key.pem"
_MANIFEST_FILE = "observer-release-manifest.json"
_OWNER_MARKER_FILE = ".yfeistai-observer-provisioner-owner.json"
_OWNER_MARKER_KIND = "yfeistai-openmaic-observer-secret-provisioner"
_MANIFEST_SCHEMA_VERSION = 2
_OWNER_MARKER_SCHEMA_VERSION = 1
_DEPLOYMENT_INPUTS = {
    "YFEISTAI_ENVIRONMENT_ID": "environment_id",
    "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": "observer_id",
    "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": "observer_url",
    "YFEISTAI_SHARED_INGRESS_CONTROL_URL": "control_url",
    "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL": "upstream_url",
    "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL": "candidate_health_url",
    "YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT": "auth_expires_at",
}
_MAX_CONTROL_FILE_BYTES = 64 * 1024
_IMMUTABLE_IMAGE_NAME = re.compile(
    r"^(?=.{1,512}$)"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$"
)
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_TAG = re.compile(r"^yfeistai-first-release-[0-9]{8}-([0-9a-f]{8})$")
_CANDIDATE_SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
_CANDIDATE_OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
_CANDIDATE_IMAGE_NAMES = ("deeptutor", "openmaic", "openmaic_render")
_OBSERVER_SIGNER_REPOSITORY = "ghcr.io/xinlingzhifei/yfeistai-observer-signer"
_ZERO_DIGEST = "sha256:" + "0" * 64
_IMAGE_SPECS: dict[str, tuple[str, str | None]] = {
    "deeptutor": ("ghcr.io/xinlingzhifei/deeptutor", None),
    "openmaic": ("ghcr.io/xinlingzhifei/openmaic", None),
    "openmaic_render": ("ghcr.io/xinlingzhifei/openmaic-render", None),
    "nginx": ("nginx", "1.29.8-alpine3.23"),
    "postgres": ("postgres", "16.14-alpine3.24"),
    "minio": ("ghcr.io/xinlingzhifei/deeptutor", "dependency-minio-RELEASE.2025-04-22T22-12-26Z"),
    "minio_client": ("ghcr.io/xinlingzhifei/deeptutor", "dependency-mc-RELEASE.2025-04-16T18-13-26Z"),
    "pocketbase": ("ghcr.io/muchobien/pocketbase", "latest"),
}


def _private_metadata(path: Path) -> os.stat_result | SimpleNamespace:
    """Return link-local metadata; tests replace this at the OS boundary."""

    return Path(path).lstat()


def _is_link_or_reparse(path: Path) -> bool:
    target = Path(path)
    try:
        details = target.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    is_junction = getattr(target, "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return True
    except OSError:
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(details, "st_file_attributes", 0) & reparse_flag)


def _validate_identity_value(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise ValueError(f"{name} is invalid")
    return value


def _validate_mode(mode: int) -> int:
    if type(mode) is not int or mode < 0 or mode & ~0o777:
        raise ValueError("private metadata mode is invalid")
    return mode


def _apply_private_metadata(path: Path, *, uid: int, gid: int, mode: int) -> None:
    """Apply and verify exact POSIX ownership and permissions, failing closed."""

    target = Path(path)
    uid = _validate_identity_value(uid, "private metadata uid")
    gid = _validate_identity_value(gid, "private metadata gid")
    mode = _validate_mode(mode)
    if _is_link_or_reparse(target):
        raise RuntimeError("private metadata target must not be a link or reparse point")
    try:
        before = _private_metadata(target)
    except OSError:
        raise RuntimeError("private metadata target is unavailable") from None
    if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
        raise RuntimeError("private metadata target type is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if stat.S_ISDIR(before.st_mode):
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or stat.S_IFMT(
            opened.st_mode
        ) != stat.S_IFMT(before.st_mode):
            raise RuntimeError("private metadata target changed while opening")
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        confirmed_descriptor = os.fstat(descriptor)
    except (AttributeError, NotImplementedError, OSError):
        raise RuntimeError("private metadata could not be applied") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        confirmed = _private_metadata(target)
    except OSError:
        raise RuntimeError("private metadata target changed after application") from None
    if (
        _is_link_or_reparse(target)
        or (confirmed_descriptor.st_dev, confirmed_descriptor.st_ino)
        != (before.st_dev, before.st_ino)
        or confirmed_descriptor.st_uid != uid
        or confirmed_descriptor.st_gid != gid
        or stat.S_IMODE(confirmed_descriptor.st_mode) != mode
        or (confirmed.st_dev, confirmed.st_ino) != (before.st_dev, before.st_ino)
        or confirmed.st_uid != uid
        or confirmed.st_gid != gid
        or stat.S_IMODE(confirmed.st_mode) != mode
        or stat.S_IFMT(confirmed.st_mode) != stat.S_IFMT(before.st_mode)
    ):
        raise RuntimeError("private metadata verification failed")


def _existing_directory(path: Path, name: str) -> tuple[Path, tuple[int, int]]:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError(f"{name} must be absolute")
    if _is_link_or_reparse(target):
        raise ValueError(f"{name} must not be a link or reparse point")
    try:
        before = _private_metadata(target)
        resolved = target.resolve(strict=True)
        confirmed = _private_metadata(target)
    except OSError:
        raise ValueError(f"{name} is unavailable") from None
    if (
        not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(confirmed.st_mode)
        or (before.st_dev, before.st_ino) != (confirmed.st_dev, confirmed.st_ino)
        or _is_link_or_reparse(target)
    ):
        raise ValueError(f"{name} is unsafe")
    return resolved, (int(confirmed.st_dev), int(confirmed.st_ino))


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _validate_roots(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
) -> tuple[Path, Path, Path, Path]:
    candidate, _ = _existing_directory(candidate_root, "candidate root")
    secret, _ = _existing_directory(secret_root, "observer secret root")
    trust, _ = _existing_directory(trust_root, "observer trust root")
    attestation, _ = _existing_directory(
        attestation_root,
        "observer attestation root",
    )
    external_roots = (secret, trust, attestation)
    for root in external_roots:
        details = _private_metadata(root)
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("observer roots must be owner-only")
    if any(_paths_overlap(candidate, root) for root in external_roots):
        raise ValueError("observer roots must be outside the candidate root")
    for index, root in enumerate(external_roots):
        if any(_paths_overlap(root, other) for other in external_roots[index + 1 :]):
            raise ValueError("observer roots must be disjoint")
    return candidate, secret, trust, attestation


def _root_identity(path: Path, name: str) -> tuple[int, int]:
    _resolved, identity = _existing_directory(path, name)
    return identity


def _assert_root_identity(path: Path, expected: tuple[int, int], name: str) -> None:
    if _root_identity(path, name) != expected:
        raise ValueError(f"{name} changed")


def _operator_identity(path: Path) -> tuple[int, int]:
    details = _private_metadata(path)
    uid = os.geteuid() if hasattr(os, "geteuid") else int(details.st_uid)
    gid = os.getegid() if hasattr(os, "getegid") else int(details.st_gid)
    return (
        _validate_identity_value(uid, "operator uid"),
        _validate_identity_value(gid, "operator gid"),
    )


def _verify_metadata(
    path: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    directory: bool,
) -> None:
    try:
        details = _private_metadata(path)
    except OSError:
        raise RuntimeError("private metadata verification target is unavailable") from None
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if (
        _is_link_or_reparse(path)
        or stat.S_IFMT(details.st_mode) != expected_type
        or details.st_uid != uid
        or details.st_gid != gid
        or stat.S_IMODE(details.st_mode) != mode
    ):
        raise RuntimeError("private metadata verification failed")


def _secure_created_directory(path: Path, *, uid: int, gid: int) -> None:
    target = Path(path)
    _apply_private_metadata(target, uid=uid, gid=gid, mode=0o700)
    _verify_metadata(
        target,
        uid=uid,
        gid=gid,
        mode=0o700,
        directory=True,
    )


def _write_new_file(
    path: Path,
    body: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(body):
            count = os.write(descriptor, body[written:])
            if count <= 0:
                raise OSError("observer secret staging write made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _apply_private_metadata(path, uid=uid, gid=gid, mode=mode)
    _verify_metadata(
        path,
        uid=uid,
        gid=gid,
        mode=mode,
        directory=False,
    )


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _entry_exists(path: Path) -> bool:
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _read_regular_file(path: Path, *, label: str) -> bytes:
    target = Path(path)
    if _is_link_or_reparse(target):
        raise ValueError(f"{label} must not be a link or reparse point")
    try:
        before = target.lstat()
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CONTROL_FILE_BYTES:
        raise ValueError(f"{label} is invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(target, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
            or opened.st_size > _MAX_CONTROL_FILE_BYTES
        ):
            raise ValueError(f"{label} changed while opening")
        chunks: list[bytes] = []
        remaining = _MAX_CONTROL_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        if len(body) > _MAX_CONTROL_FILE_BYTES:
            raise ValueError(f"{label} is invalid")
        confirmed = os.fstat(descriptor)
        if (
            (confirmed.st_dev, confirmed.st_ino) != (opened.st_dev, opened.st_ino)
            or confirmed.st_size != opened.st_size
            or confirmed.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError(f"{label} changed while reading")
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after = target.lstat()
    except OSError:
        raise ValueError(f"{label} changed while reading") from None
    if (
        _is_link_or_reparse(target)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or stat.S_IFMT(after.st_mode) != stat.S_IFMT(before.st_mode)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ValueError(f"{label} changed while reading")
    return body


def _decode_json_document(body: bytes, *, label: str) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"{label} contains duplicate keys")
            document[key] = value
        return document

    try:
        document = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"{label} is invalid") from None
    if not isinstance(document, dict):
        raise ValueError(f"{label} is invalid")
    return document


def _read_json_document(path: Path, *, label: str) -> dict[str, object]:
    return _decode_json_document(
        _read_regular_file(path, label=label),
        label=label,
    )


def _canonical_json_bytes(document: dict[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _remove_files(paths: list[Path]) -> list[OSError]:
    errors: list[OSError] = []
    for path in reversed(paths):
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(error)
    return errors


def _target_paths(
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    release_run_id: str,
) -> dict[str, Path]:
    run_directory = secret_root / release_run_id
    trust_directory = trust_root / release_run_id
    attestation_directory = attestation_root / release_run_id
    return {
        "secretRoot": secret_root,
        "runDirectory": run_directory,
        "observerAuthToken": run_directory / "observer-auth.token",
        "candidateHealthToken": run_directory / "candidate-health.token",
        "attestationSigningKey": run_directory / "attestation-signing-key.pem",
        "trustRoot": trust_root,
        "trustDirectory": trust_directory,
        "attestationPublicKey": trust_directory / _PUBLIC_KEY_FILE,
        "manifest": trust_directory / _MANIFEST_FILE,
        "attestationDirectory": attestation_directory,
        "observerAttestation": (
            attestation_directory / "openmaic-shared-ingress-observer-attestation.json"
        ),
        "observerAttestationSignature": (
            attestation_directory / "openmaic-shared-ingress-observer-attestation.sig"
        ),
    }


def _owner_marker_document(*, release_run_id: str, root_role: str) -> dict[str, object]:
    return {
        "schemaVersion": _OWNER_MARKER_SCHEMA_VERSION,
        "owner": _OWNER_MARKER_KIND,
        "releaseRunId": release_run_id,
        "rootRole": root_role,
    }


def _validate_deployment_environment(
    value: object, *, release_run_id: str, now: datetime | None = None
) -> dict[str, str]:
    try:
        if not isinstance(value, dict) or set(value) != set(_DEPLOYMENT_INPUTS):
            raise ValueError
        validate_observer_endpoints(
            environment_id=value["YFEISTAI_ENVIRONMENT_ID"],
            observer_id=value["YFEISTAI_SHARED_INGRESS_OBSERVER_ID"],
            release_run_id=release_run_id,
            observer_url=value["YFEISTAI_SHARED_INGRESS_OBSERVER_URL"],
            shared_ingress_control_url=value["YFEISTAI_SHARED_INGRESS_CONTROL_URL"],
            candidate_health_url=value["YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL"],
            shared_ingress_upstream_url=value["YFEISTAI_SHARED_INGRESS_UPSTREAM_URL"],
            internal_proxy_url="http://openmaic-shared-ingress-observer:8000",
        )
        expiry = value["YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT"]
        # Recovery needs structural validation even after this batch has expired.
        _parse_utc(expiry)
        if now is not None:
            parse_observer_auth_expiry(expiry, now=now)
    except (OpenMAICObserverServiceError, KeyError, TypeError, ValueError):
        raise ValueError("observer deployment configuration is invalid") from None
    return dict(value)


def _release_manifest_document(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    paths: dict[str, Path],
    release_run_id: str,
    status: str,
    public_key_spki_sha256: str,
    candidate_binding: dict[str, object],
    deployment_environment: dict[str, str],
) -> dict[str, object]:
    return {
        "schemaVersion": _MANIFEST_SCHEMA_VERSION,
        "status": status,
        "releaseRunId": release_run_id,
        "roots": {
            "candidateRoot": str(candidate_root),
            "secretRoot": str(secret_root),
            "trustRoot": str(trust_root),
            "attestationRoot": str(attestation_root),
        },
        "paths": {name: str(path) for name, path in paths.items()},
        "candidateBinding": candidate_binding,
        "deploymentEnvironment": deployment_environment,
        "publicKeySpkiSha256": public_key_spki_sha256,
    }


def _validate_release_manifest(
    document: dict[str, object],
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    paths: dict[str, Path],
    release_run_id: str,
    expected_candidate_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    status = document.get("status")
    spki_sha256 = document.get("publicKeySpkiSha256")
    if (
        type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != _MANIFEST_SCHEMA_VERSION
        or status not in {"uncommitted", "committed"}
        or not isinstance(spki_sha256, str)
    ):
        raise ValueError("observer release manifest is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", spki_sha256) is None:
        raise ValueError("observer release manifest is invalid")
    candidate_binding = _validate_candidate_binding_document(document.get("candidateBinding"))
    deployment_environment = _validate_deployment_environment(
        document.get("deploymentEnvironment"), release_run_id=release_run_id
    )
    if expected_candidate_binding is not None and candidate_binding != expected_candidate_binding:
        raise ValueError("observer release manifest candidate binding is invalid")
    expected = _release_manifest_document(
        candidate_root=candidate_root,
        secret_root=secret_root,
        trust_root=trust_root,
        attestation_root=attestation_root,
        paths=paths,
        release_run_id=release_run_id,
        status=status,
        public_key_spki_sha256=spki_sha256,
        candidate_binding=candidate_binding,
        deployment_environment=deployment_environment,
    )
    if document != expected:
        raise ValueError("observer release manifest is invalid")
    return document


def _validate_observer_signer_image(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("observer signer image is invalid")
    image_name, separator, image_digest = value.rpartition("@sha256:")
    if (
        not separator
        or _IMMUTABLE_IMAGE_NAME.fullmatch(image_name) is None
        or re.fullmatch(r"[0-9a-f]{64}", image_digest) is None
        or image_digest == "0" * 64
    ):
        raise ValueError("observer signer image is invalid")
    return value


def _validate_candidate_identity(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "sourceRepository",
        "sourceHead",
        "releaseTag",
        "openmaicHead",
        "imageDigests",
    }:
        raise ValueError("observer release manifest candidate binding is invalid")
    source_head = value.get("sourceHead")
    release_tag = value.get("releaseTag")
    release_match = _RELEASE_TAG.fullmatch(release_tag) if isinstance(release_tag, str) else None
    image_digests = value.get("imageDigests")
    if (
        value.get("sourceRepository") != _CANDIDATE_SOURCE_REPOSITORY
        or not isinstance(source_head, str)
        or _COMMIT_SHA.fullmatch(source_head) is None
        or source_head == "0" * 40
        or release_match is None
        or release_match.group(1) != source_head[:8]
        or value.get("openmaicHead") != _CANDIDATE_OPENMAIC_HEAD
        or not isinstance(image_digests, dict)
        or set(image_digests) != set(_CANDIDATE_IMAGE_NAMES)
    ):
        raise ValueError("observer release manifest candidate binding is invalid")
    digests: dict[str, str] = {}
    for name in _CANDIDATE_IMAGE_NAMES:
        digest = image_digests.get(name)
        if (
            not isinstance(digest, str)
            or _IMAGE_DIGEST.fullmatch(digest) is None
            or digest == _ZERO_DIGEST
        ):
            raise ValueError("observer release manifest candidate binding is invalid")
        digests[name] = digest
    if len(set(digests.values())) != len(digests):
        raise ValueError("observer release manifest candidate binding is invalid")
    return {
        "sourceRepository": _CANDIDATE_SOURCE_REPOSITORY,
        "sourceHead": source_head,
        "releaseTag": release_tag,
        "openmaicHead": _CANDIDATE_OPENMAIC_HEAD,
        "imageDigests": digests,
    }


def _candidate_binding_from_image_lock_bytes(body: bytes) -> dict[str, object]:
    try:
        if not isinstance(body, bytes) or not body or len(body) > _MAX_CONTROL_FILE_BYTES:
            raise ValueError
        lock = _decode_json_document(body, label="candidate image lock")
        if (
            set(lock) != {"schemaVersion", "candidate", "images", "supportImages"}
            or lock.get("schemaVersion") != 3
        ):
            raise ValueError
        candidate = _validate_candidate_identity(lock.get("candidate"))
        images = lock.get("images")
        if not isinstance(images, dict) or set(images) != set(_IMAGE_SPECS):
            raise ValueError
        for name, (repository, fixed_tag) in _IMAGE_SPECS.items():
            record = images.get(name)
            if not isinstance(record, dict):
                raise ValueError
            digest = record.get("digest")
            tag = fixed_tag or candidate["releaseTag"]
            if (
                not isinstance(digest, str)
                or _IMAGE_DIGEST.fullmatch(digest) is None
                or digest == _ZERO_DIGEST
                or record.get("repository") != repository
                or record.get("tag") != tag
                or record.get("reference") != f"{repository}:{tag}@{digest}"
            ):
                raise ValueError
        candidate_image_digests = candidate["imageDigests"]
        if any(
            images[name]["digest"] != candidate_image_digests[name]
            for name in _CANDIDATE_IMAGE_NAMES
        ):
            raise ValueError
        support_images = lock.get("supportImages")
        if not isinstance(support_images, dict) or set(support_images) != {"observer_signer"}:
            raise ValueError
        signer = support_images.get("observer_signer")
        if not isinstance(signer, dict) or set(signer) != {
            "repository",
            "tag",
            "digest",
            "reference",
        }:
            raise ValueError
        signer_digest = signer.get("digest")
        signer_reference = _validate_observer_signer_image(signer.get("reference"))
        _image_name, _separator, image_digest = signer_reference.rpartition("@sha256:")
        if (
            signer.get("repository") != _OBSERVER_SIGNER_REPOSITORY
            or signer.get("tag") != candidate["releaseTag"]
            or not isinstance(signer_digest, str)
            or _IMAGE_DIGEST.fullmatch(signer_digest) is None
            or signer_digest == _ZERO_DIGEST
            or signer_digest in candidate["imageDigests"].values()
            or signer_digest != f"sha256:{image_digest}"
            or signer_reference
            != f"{_OBSERVER_SIGNER_REPOSITORY}:{candidate['releaseTag']}@{signer_digest}"
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("observer release manifest candidate binding is invalid") from None
    return {
        "imageLockSha256": hashlib.sha256(body).hexdigest(),
        "candidate": candidate,
        "observerSignerImage": signer_reference,
    }


def _candidate_binding_snapshot(
    candidate_root: Path,
    *,
    image_lock_bytes: bytes | None = None,
) -> tuple[bytes, dict[str, object]]:
    if image_lock_bytes is None:
        lock_directory, _identity = _existing_directory(
            candidate_root / "deploy",
            "candidate image lock directory",
        )
        if lock_directory.parent != candidate_root:
            raise ValueError("observer release manifest candidate binding is invalid")
        body = _read_regular_file(
            lock_directory / "image-lock.json",
            label="candidate image lock",
        )
    else:
        body = image_lock_bytes
    return body, _candidate_binding_from_image_lock_bytes(body)


def _validate_candidate_binding_document(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "imageLockSha256",
        "candidate",
        "observerSignerImage",
    }:
        raise ValueError("observer release manifest candidate binding is invalid")
    lock_sha256 = value.get("imageLockSha256")
    signer_image = value.get("observerSignerImage")
    if not isinstance(lock_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", lock_sha256) is None:
        raise ValueError("observer release manifest candidate binding is invalid")
    candidate = _validate_candidate_identity(value.get("candidate"))
    try:
        signer_image = _validate_observer_signer_image(signer_image)
    except ValueError:
        raise ValueError("observer release manifest candidate binding is invalid") from None
    return {
        "imageLockSha256": lock_sha256,
        "candidate": candidate,
        "observerSignerImage": signer_image,
    }


def _observer_release_wiring(
    *,
    paths: dict[str, Path],
    manifest: dict[str, object],
    secret_root: Path,
    release_run_id: str,
    observer_signer_image: str | None = None,
) -> dict[str, object]:
    spki_sha256 = manifest["publicKeySpkiSha256"]
    assert isinstance(spki_sha256, str)
    candidate_binding = manifest["candidateBinding"]
    assert isinstance(candidate_binding, dict)
    candidate = candidate_binding["candidate"]
    assert isinstance(candidate, dict)
    candidate_sha256 = hashlib.sha256(
        json.dumps(
            candidate,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    image_lock_sha256 = candidate_binding["imageLockSha256"]
    assert isinstance(image_lock_sha256, str)
    environment = {
        **manifest["deploymentEnvironment"],
        "YFEISTAI_OBSERVER_SECRET_DIR": str(secret_root),
        "YFEISTAI_RELEASE_RUN_ID": release_run_id,
        "YFEISTAI_OBSERVER_ATTESTATION_DIR": str(paths["attestationDirectory"]),
        "YFEISTAI_OBSERVER_TRUST_ROOT": str(paths["trustDirectory"]),
        "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256": spki_sha256,
        "YFEISTAI_CANDIDATE_SHA256": candidate_sha256,
        "YFEISTAI_IMAGE_LOCK_SHA256": image_lock_sha256,
    }
    preflight_inputs: dict[str, str] = {
        "observer_secret_dir": str(secret_root),
        "release_run_id": release_run_id,
    }
    if observer_signer_image is not None:
        environment["YFEISTAI_OBSERVER_SIGNER_IMAGE"] = observer_signer_image
        preflight_inputs["observer_signer_image"] = observer_signer_image
    return {
        "environment": environment,
        "paths": {name: str(path) for name, path in paths.items()},
        "releaseManifest": manifest,
        "platformPreflightInputs": preflight_inputs,
        "verifierArguments": {
            "--openmaic-observer-public-key": str(paths["attestationPublicKey"]),
            "--openmaic-observer-public-key-spki-sha256": spki_sha256,
        },
        "outageProbeEnvironment": {
            "YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE": str(paths["observerAuthToken"]),
            "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PATH": str(paths["observerAttestation"]),
            "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_SIGNATURE_PATH": str(
                paths["observerAttestationSignature"]
            ),
            "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PUBLIC_KEY_PATH": str(
                paths["attestationPublicKey"]
            ),
            "YFEISTAI_SHARED_INGRESS_OBSERVER_ATTESTATION_PUBLIC_KEY_SPKI_SHA256": (spki_sha256),
        },
    }


def _validate_owner_marker(directory: Path, *, release_run_id: str, root_role: str) -> None:
    try:
        before = directory.lstat()
    except OSError:
        raise ValueError("observer owner directory is unavailable") from None
    if _is_link_or_reparse(directory) or not stat.S_ISDIR(before.st_mode):
        raise ValueError("observer owner directory is invalid")
    marker = _read_json_document(
        directory / _OWNER_MARKER_FILE,
        label="observer owner marker",
    )
    if type(marker.get("schemaVersion")) is not int or marker != _owner_marker_document(
        release_run_id=release_run_id,
        root_role=root_role,
    ):
        raise ValueError("observer owner marker is invalid")
    try:
        after = directory.lstat()
    except OSError:
        raise ValueError("observer owner directory changed") from None
    if (
        _is_link_or_reparse(directory)
        or not stat.S_ISDIR(after.st_mode)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise ValueError("observer owner directory changed")


def load_committed_observer_release_manifest(
    *,
    manifest_path: Path,
    candidate_root: Path,
    observer_signer_image: str,
    image_lock_bytes: bytes | None = None,
) -> dict[str, object]:
    """Load one committed provisioner manifest as the sole runtime wiring authority."""

    signer_image = _validate_observer_signer_image(observer_signer_image)
    manifest_target = Path(manifest_path)
    if not manifest_target.is_absolute():
        raise ValueError("observer release manifest path must be absolute")
    document = _read_json_document(manifest_target, label="observer release manifest")
    roots = document.get("roots")
    release_run_id = document.get("releaseRunId")
    if (
        not isinstance(roots, dict)
        or set(roots) != {"candidateRoot", "secretRoot", "trustRoot", "attestationRoot"}
        or any(not isinstance(value, str) for value in roots.values())
        or not isinstance(release_run_id, str)
        or _RELEASE_RUN_ID.fullmatch(release_run_id) is None
    ):
        raise ValueError("observer release manifest is invalid")
    try:
        expected_candidate, _identity = _existing_directory(candidate_root, "candidate root")
        candidate, secret, trust, attestation = _validate_roots(
            candidate_root=Path(roots["candidateRoot"]),
            secret_root=Path(roots["secretRoot"]),
            trust_root=Path(roots["trustRoot"]),
            attestation_root=Path(roots["attestationRoot"]),
        )
    except (OSError, ValueError):
        raise ValueError("observer release manifest is invalid") from None
    if candidate != expected_candidate:
        raise ValueError("observer release manifest candidate root is invalid")
    _leased_lock_bytes, expected_candidate_binding = _candidate_binding_snapshot(
        candidate,
        image_lock_bytes=image_lock_bytes,
    )
    paths = _target_paths(secret, trust, attestation, release_run_id)
    if os.path.normcase(os.path.abspath(manifest_target)) != os.path.normcase(
        os.path.abspath(paths["manifest"])
    ):
        raise ValueError("observer release manifest path is invalid")
    manifest = _validate_release_manifest(
        document,
        candidate_root=candidate,
        secret_root=secret,
        trust_root=trust,
        attestation_root=attestation,
        paths=paths,
        release_run_id=release_run_id,
        expected_candidate_binding=expected_candidate_binding,
    )
    if manifest["status"] != "committed":
        raise ValueError("observer release manifest is not committed")
    _validate_deployment_environment(
        manifest["deploymentEnvironment"], release_run_id=release_run_id, now=datetime.now(UTC)
    )
    bound_signer_image = manifest["candidateBinding"]["observerSignerImage"]
    if signer_image != bound_signer_image:
        raise ValueError("observer release manifest candidate binding is invalid")
    for role, directory, _root in _run_directory_roles(paths):
        _validate_owner_marker(directory, release_run_id=release_run_id, root_role=role)
    try:
        public_key = serialization.load_pem_public_key(
            _read_regular_file(paths["attestationPublicKey"], label="observer public key")
        )
        public_spki = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except (TypeError, ValueError):
        raise ValueError("observer release manifest public key is invalid") from None
    if hashlib.sha256(public_spki).hexdigest() != manifest["publicKeySpkiSha256"]:
        raise ValueError("observer release manifest public key is invalid")
    return _observer_release_wiring(
        paths=paths,
        manifest=manifest,
        secret_root=secret,
        release_run_id=release_run_id,
        observer_signer_image=bound_signer_image,
    )


def _quarantine_path(root: Path, release_run_id: str) -> Path:
    release_digest = hashlib.sha256(release_run_id.encode("utf-8")).hexdigest()
    return root / f".yfeistai-observer-recovered-{release_digest}"


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return
    if os.name != "posix":
        raise RuntimeError("atomic no-replace directory rename is unavailable")
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("atomic no-replace directory rename is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(destination))


@contextmanager
def _release_operation_lock(trust_root: Path, release_run_id: str) -> Iterator[None]:
    release_digest = hashlib.sha256(release_run_id.encode("utf-8")).hexdigest()
    lock_path = trust_root / f".yfeistai-observer-operation-{release_digest}.lock"
    if _is_link_or_reparse(lock_path):
        raise RuntimeError("observer release operation lock is unsafe")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        before = lock_path.lstat()
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(lock_path)
            or not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise RuntimeError("observer release operation lock is unsafe")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(lock_path, 0o600)
        root_control = _private_metadata(trust_root)
        lock_control = _private_metadata(lock_path)
        if (
            lock_control.st_uid != root_control.st_uid
            or lock_control.st_gid != root_control.st_gid
            or stat.S_IMODE(lock_control.st_mode) != 0o600
        ):
            raise RuntimeError("observer release operation lock ownership is invalid")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        confirmed = lock_path.lstat()
        if _is_link_or_reparse(lock_path) or (confirmed.st_dev, confirmed.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise RuntimeError("observer release operation lock changed")
        _fsync_directory(trust_root)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise RuntimeError("observer release operation is already active") from None
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def _run_directory_roles(
    paths: dict[str, Path],
) -> tuple[tuple[str, Path, Path], ...]:
    return (
        ("secret", paths["runDirectory"], paths["secretRoot"]),
        ("trust", paths["trustDirectory"], paths["trustRoot"]),
        (
            "attestation",
            paths["attestationDirectory"],
            paths["attestationDirectory"].parent,
        ),
    )


def _validate_run_directory(path: Path, *, label: str) -> tuple[int, int]:
    if _is_link_or_reparse(path):
        raise ValueError(f"{label} must not be a link or reparse point")
    try:
        details = path.lstat()
    except OSError:
        raise ValueError(f"{label} is unavailable") from None
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError(f"{label} is invalid")
    return int(details.st_dev), int(details.st_ino)


def _assert_created_directory_identities(
    created_directories: list[tuple[str, Path, Path, tuple[int, int]]],
) -> None:
    for role, directory, _root, identity in created_directories:
        try:
            current_identity = _validate_run_directory(
                directory,
                label=f"observer {role} release directory",
            )
        except ValueError:
            raise ValueError(f"observer {role} release directory changed") from None
        if current_identity != identity:
            raise ValueError(f"observer {role} release directory changed")


def _recover_observer_release_batch_locked(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    release_run_id: str,
) -> dict[str, str]:
    """Move only this provisioner's uncommitted run directories into quarantine."""

    if not isinstance(release_run_id, str) or _RELEASE_RUN_ID.fullmatch(release_run_id) is None:
        raise ValueError("observer release run id is invalid")
    candidate, secret, trust, attestation = _validate_roots(
        candidate_root=Path(candidate_root),
        secret_root=Path(secret_root),
        trust_root=Path(trust_root),
        attestation_root=Path(attestation_root),
    )
    root_identities = {
        "secret": _root_identity(secret, "observer secret root"),
        "trust": _root_identity(trust, "observer trust root"),
        "attestation": _root_identity(attestation, "observer attestation root"),
    }
    paths = _target_paths(secret, trust, attestation, release_run_id)
    trust_quarantine = _quarantine_path(trust, release_run_id)
    manifest_locations = (
        paths["manifest"],
        trust_quarantine / _MANIFEST_FILE,
    )
    existing_manifests = [path for path in manifest_locations if _entry_exists(path)]
    if len(existing_manifests) != 1:
        raise ValueError("observer release manifest is unavailable or ambiguous")
    manifest = _validate_release_manifest(
        _read_json_document(existing_manifests[0], label="observer release manifest"),
        candidate_root=candidate,
        secret_root=secret,
        trust_root=trust,
        attestation_root=attestation,
        paths=paths,
        release_run_id=release_run_id,
    )
    if manifest["status"] == "committed":
        raise ValueError("observer release batch is committed")

    locations: dict[str, tuple[Path | None, Path, tuple[int, int] | None]] = {}
    for role, source, root in _run_directory_roles(paths):
        _assert_root_identity(root, root_identities[role], f"observer {role} root")
        quarantine = _quarantine_path(root, release_run_id)
        source_exists = _entry_exists(source)
        quarantine_exists = _entry_exists(quarantine)
        if source_exists and quarantine_exists:
            raise ValueError("observer recovery source and quarantine are ambiguous")
        if not source_exists and not quarantine_exists:
            locations[role] = (None, quarantine, None)
            continue
        current = source if source_exists else quarantine
        identity = _validate_run_directory(current, label="observer recovery directory")
        _validate_owner_marker(
            current,
            release_run_id=release_run_id,
            root_role=role,
        )
        locations[role] = (source if source_exists else None, quarantine, identity)

    recovered: dict[str, str] = {}
    for role in ("secret", "attestation", "trust"):
        source, quarantine, identity = locations[role]
        root = next(
            root for item_role, _source, root in _run_directory_roles(paths) if item_role == role
        )
        _assert_root_identity(root, root_identities[role], f"observer {role} root")
        if source is not None:
            assert identity is not None
            current_manifest_location = (
                paths["manifest"]
                if _entry_exists(paths["manifest"])
                else trust_quarantine / _MANIFEST_FILE
            )
            current_manifest = _validate_release_manifest(
                _read_json_document(
                    current_manifest_location,
                    label="observer release manifest",
                ),
                candidate_root=candidate,
                secret_root=secret,
                trust_root=trust,
                attestation_root=attestation,
                paths=paths,
                release_run_id=release_run_id,
            )
            if current_manifest["status"] != "uncommitted":
                raise ValueError("observer release batch is committed")
            if _validate_run_directory(source, label="observer recovery source") != identity:
                raise ValueError("observer recovery source changed")
            _validate_owner_marker(
                source,
                release_run_id=release_run_id,
                root_role=role,
            )
            if _entry_exists(quarantine):
                raise ValueError("observer recovery quarantine already exists")
            try:
                _rename_directory_no_replace(source, quarantine)
            except OSError:
                raise RuntimeError("observer release quarantine rename failed") from None
            _fsync_directory(quarantine.parent)
        if _entry_exists(quarantine):
            _validate_run_directory(quarantine, label="observer recovery quarantine")
            _validate_owner_marker(
                quarantine,
                release_run_id=release_run_id,
                root_role=role,
            )
            recovered[role] = str(quarantine)
    return recovered


def recover_observer_release_batch(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    release_run_id: str,
) -> dict[str, str]:
    if not isinstance(release_run_id, str) or _RELEASE_RUN_ID.fullmatch(release_run_id) is None:
        raise ValueError("observer release run id is invalid")
    _, _, trust, _ = _validate_roots(
        candidate_root=Path(candidate_root),
        secret_root=Path(secret_root),
        trust_root=Path(trust_root),
        attestation_root=Path(attestation_root),
    )
    with _release_operation_lock(trust, release_run_id):
        return _recover_observer_release_batch_locked(
            candidate_root=candidate_root,
            secret_root=secret_root,
            trust_root=trust_root,
            attestation_root=attestation_root,
            release_run_id=release_run_id,
        )


def _quarantine_known_created_directories(
    created_directories: list[tuple[str, Path, Path, tuple[int, int]]],
    *,
    release_run_id: str,
) -> None:
    for _role, source, root, identity in reversed(created_directories):
        quarantine = _quarantine_path(root, release_run_id)
        source_exists = _entry_exists(source)
        quarantine_exists = _entry_exists(quarantine)
        if source_exists and quarantine_exists:
            raise RuntimeError("observer rollback quarantine is ambiguous")
        if source_exists:
            try:
                current_identity = _validate_run_directory(
                    source,
                    label="observer rollback source",
                )
            except ValueError:
                raise RuntimeError("observer rollback source changed") from None
            if current_identity != identity:
                raise RuntimeError("observer rollback source changed")
            try:
                _rename_directory_no_replace(source, quarantine)
            except OSError:
                raise RuntimeError("observer rollback quarantine rename failed") from None
            _fsync_directory(root)


def _new_material() -> tuple[bytes, bytes, bytes, bytes, str]:
    observer_token = secrets.token_urlsafe(48)
    health_token = secrets.token_urlsafe(48)
    while health_token == observer_token:
        health_token = secrets.token_urlsafe(48)
    private_key = Ed25519PrivateKey.generate()
    private_body = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_key = private_key.public_key()
    public_body = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return (
        f"{observer_token}\n".encode("ascii"),
        f"{health_token}\n".encode("ascii"),
        private_body,
        public_body,
        hashlib.sha256(public_spki).hexdigest(),
    )


def _write_owner_marker(
    directory: Path,
    *,
    release_run_id: str,
    root_role: str,
    uid: int,
    gid: int,
) -> None:
    _write_new_file(
        directory / _OWNER_MARKER_FILE,
        _canonical_json_bytes(
            _owner_marker_document(
                release_run_id=release_run_id,
                root_role=root_role,
            )
        ),
        uid=uid,
        gid=gid,
        mode=0o400,
    )


def _commit_release_manifest(
    *,
    manifest_path: Path,
    uncommitted_manifest: dict[str, object],
    committed_manifest: dict[str, object],
    uid: int,
    gid: int,
) -> None:
    if (
        _read_json_document(
            manifest_path,
            label="observer release manifest",
        )
        != uncommitted_manifest
    ):
        raise RuntimeError("observer release manifest changed before commit")
    staging = manifest_path.parent / f".{_MANIFEST_FILE}.{secrets.token_hex(16)}.tmp"
    _write_new_file(
        staging,
        _canonical_json_bytes(committed_manifest),
        uid=uid,
        gid=gid,
        mode=0o400,
    )
    if (
        _read_json_document(
            manifest_path,
            label="observer release manifest",
        )
        != uncommitted_manifest
    ):
        raise RuntimeError("observer release manifest changed before commit")
    try:
        os.replace(staging, manifest_path)
    except OSError:
        raise RuntimeError("observer release manifest commit failed") from None
    _apply_private_metadata(manifest_path, uid=uid, gid=gid, mode=0o400)
    _verify_metadata(
        manifest_path,
        uid=uid,
        gid=gid,
        mode=0o400,
        directory=False,
    )
    if (
        _read_json_document(
            manifest_path,
            label="observer release manifest",
        )
        != committed_manifest
    ):
        raise RuntimeError("observer release manifest commit verification failed")
    _fsync_directory(manifest_path.parent)


def _provision_observer_release_secrets_locked(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    release_run_id: str,
    container_uid: int,
    container_gid: int,
    deployment_environment: dict[str, str],
) -> dict[str, object]:
    """Create one immutable observer secret batch and return non-secret wiring."""

    if not isinstance(release_run_id, str) or _RELEASE_RUN_ID.fullmatch(release_run_id) is None:
        raise ValueError("observer release run id is invalid")
    deployment_environment = _validate_deployment_environment(
        deployment_environment, release_run_id=release_run_id, now=datetime.now(UTC)
    )
    container_uid = _validate_identity_value(container_uid, "observer container uid")
    container_gid = _validate_identity_value(container_gid, "observer container gid")
    candidate, secret, trust, attestation = _validate_roots(
        candidate_root=Path(candidate_root),
        secret_root=Path(secret_root),
        trust_root=Path(trust_root),
        attestation_root=Path(attestation_root),
    )
    root_identities = {
        "secret": _root_identity(secret, "observer secret root"),
        "trust": _root_identity(trust, "observer trust root"),
        "attestation": _root_identity(attestation, "observer attestation root"),
    }
    candidate_lock_bytes, candidate_binding = _candidate_binding_snapshot(candidate)
    paths = _target_paths(secret, trust, attestation, release_run_id)
    run_directories = [
        paths["runDirectory"],
        paths["trustDirectory"],
        paths["attestationDirectory"],
    ]
    recovery_directories = [
        _quarantine_path(root, release_run_id) for root in (secret, trust, attestation)
    ]
    if any(_entry_exists(path) for path in (*run_directories, *recovery_directories)):
        raise ValueError("observer release directory already exists")

    operator_uid, operator_gid = _operator_identity(secret)
    directory_specs = (
        ("trust", paths["trustDirectory"], trust, operator_uid, operator_gid),
        ("secret", paths["runDirectory"], secret, operator_uid, operator_gid),
        (
            "attestation",
            paths["attestationDirectory"],
            attestation,
            container_uid,
            container_gid,
        ),
    )
    observer_body, health_body, private_body, public_body, spki_sha256 = _new_material()
    uncommitted_manifest = _release_manifest_document(
        candidate_root=candidate,
        secret_root=secret,
        trust_root=trust,
        attestation_root=attestation,
        paths=paths,
        release_run_id=release_run_id,
        status="uncommitted",
        public_key_spki_sha256=spki_sha256,
        candidate_binding=candidate_binding,
        deployment_environment=deployment_environment,
    )
    committed_manifest = {
        **uncommitted_manifest,
        "status": "committed",
    }
    file_specs = (
        (
            paths["observerAuthToken"],
            observer_body,
            container_uid,
            container_gid,
            0o400,
        ),
        (
            paths["candidateHealthToken"],
            health_body,
            container_uid,
            container_gid,
            0o400,
        ),
        (
            paths["attestationSigningKey"],
            private_body,
            container_uid,
            container_gid,
            0o600,
        ),
        (
            paths["attestationPublicKey"],
            public_body,
            operator_uid,
            container_gid,
            0o440,
        ),
    )
    created_directories: list[tuple[str, Path, Path, tuple[int, int]]] = []
    staged_files: list[Path] = []
    manifest_written = False
    try:
        for role, directory, root, uid, gid in directory_specs:
            _assert_root_identity(root, root_identities[role], f"observer {role} root")
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                raise ValueError("observer release directory already exists") from None
            created_directories.append(
                (
                    role,
                    directory,
                    root,
                    _validate_run_directory(
                        directory,
                        label="observer release directory",
                    ),
                )
            )
            _secure_created_directory(
                directory,
                uid=uid,
                gid=gid,
            )
            _write_owner_marker(
                directory,
                release_run_id=release_run_id,
                root_role=role,
                uid=uid,
                gid=gid,
            )
            _assert_created_directory_identities(created_directories)
            if role == "trust":
                _write_new_file(
                    paths["manifest"],
                    _canonical_json_bytes(uncommitted_manifest),
                    uid=operator_uid,
                    gid=operator_gid,
                    mode=0o400,
                )
                manifest_written = True
            _fsync_directory(directory)
            _assert_root_identity(root, root_identities[role], f"observer {role} root")
            _fsync_directory(root)
        for target, body, uid, gid, mode in file_specs:
            _assert_created_directory_identities(created_directories)
            role = "secret" if target.is_relative_to(paths["runDirectory"]) else "trust"
            root = secret if role == "secret" else trust
            _assert_root_identity(root, root_identities[role], f"observer {role} root")
            staging = target.parent / f".{target.name}.{secrets.token_hex(16)}.tmp"
            staged_files.append(staging)
            _write_new_file(staging, body, uid=uid, gid=gid, mode=mode)
        for (target, _body, uid, gid, mode), staging in zip(
            file_specs,
            staged_files,
            strict=True,
        ):
            _assert_created_directory_identities(created_directories)
            if target.exists() or _is_link_or_reparse(target):
                raise ValueError("observer release target already exists")
            try:
                os.link(staging, target)
            except FileExistsError:
                raise ValueError("observer release target already exists") from None
            _apply_private_metadata(target, uid=uid, gid=gid, mode=mode)
            _verify_metadata(
                target,
                uid=uid,
                gid=gid,
                mode=mode,
                directory=False,
            )
            _assert_created_directory_identities(created_directories)
        staging_cleanup_errors = _remove_files(staged_files)
        if staging_cleanup_errors:
            raise RuntimeError("observer secret staging cleanup failed")
        staged_files.clear()
        for directory in run_directories:
            _fsync_directory(directory)
        for role, root in (("secret", secret), ("trust", trust), ("attestation", attestation)):
            _assert_root_identity(root, root_identities[role], f"observer {role} root")
        _assert_created_directory_identities(created_directories)
        current_lock_bytes, current_candidate_binding = _candidate_binding_snapshot(candidate)
        if (
            current_lock_bytes != candidate_lock_bytes
            or current_candidate_binding != candidate_binding
        ):
            raise ValueError("observer release manifest candidate binding changed before commit")
        _commit_release_manifest(
            manifest_path=paths["manifest"],
            uncommitted_manifest=uncommitted_manifest,
            committed_manifest=committed_manifest,
            uid=operator_uid,
            gid=operator_gid,
        )
        _assert_created_directory_identities(created_directories)
    except BaseException as error:
        if manifest_written and _entry_exists(paths["manifest"]):
            try:
                current_manifest = _read_json_document(
                    paths["manifest"],
                    label="observer release manifest",
                )
            except ValueError:
                current_manifest = {}
            if current_manifest.get("status") == "committed":
                raise RuntimeError(
                    "observer secret publication reached a committed state requiring review"
                ) from error
        try:
            if manifest_written:
                try:
                    _recover_observer_release_batch_locked(
                        candidate_root=candidate,
                        secret_root=secret,
                        trust_root=trust,
                        attestation_root=attestation,
                        release_run_id=release_run_id,
                    )
                except BaseException:
                    _quarantine_known_created_directories(
                        created_directories,
                        release_run_id=release_run_id,
                    )
            else:
                _quarantine_known_created_directories(
                    created_directories,
                    release_run_id=release_run_id,
                )
        except BaseException as rollback_error:
            raise RuntimeError("observer secret publication rollback failed") from rollback_error
        raise

    return _observer_release_wiring(
        paths=paths,
        manifest=committed_manifest,
        secret_root=secret,
        release_run_id=release_run_id,
    )


def provision_observer_release_secrets(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    release_run_id: str,
    container_uid: int,
    container_gid: int,
    environment_id: str,
    observer_id: str,
    observer_url: str,
    control_url: str,
    upstream_url: str,
    candidate_health_url: str,
    auth_expires_at: str,
) -> dict[str, object]:
    if not isinstance(release_run_id, str) or _RELEASE_RUN_ID.fullmatch(release_run_id) is None:
        raise ValueError("observer release run id is invalid")
    _, _, trust, _ = _validate_roots(
        candidate_root=Path(candidate_root),
        secret_root=Path(secret_root),
        trust_root=Path(trust_root),
        attestation_root=Path(attestation_root),
    )
    with _release_operation_lock(trust, release_run_id):
        return _provision_observer_release_secrets_locked(
            candidate_root=candidate_root,
            secret_root=secret_root,
            trust_root=trust_root,
            attestation_root=attestation_root,
            release_run_id=release_run_id,
            container_uid=container_uid,
            container_gid=container_gid,
            deployment_environment={
                "YFEISTAI_ENVIRONMENT_ID": environment_id,
                "YFEISTAI_SHARED_INGRESS_OBSERVER_ID": observer_id,
                "YFEISTAI_SHARED_INGRESS_OBSERVER_URL": observer_url,
                "YFEISTAI_SHARED_INGRESS_CONTROL_URL": control_url,
                "YFEISTAI_SHARED_INGRESS_UPSTREAM_URL": upstream_url,
                "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL": candidate_health_url,
                "YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT": auth_expires_at,
            },
        )


def _refuse_existing_cli_release(
    *,
    candidate_root: Path,
    secret_root: Path,
    trust_root: Path,
    attestation_root: Path,
    release_run_id: str,
) -> None:
    candidate, secret, trust, attestation = _validate_roots(
        candidate_root=candidate_root,
        secret_root=secret_root,
        trust_root=trust_root,
        attestation_root=attestation_root,
    )
    paths = _target_paths(secret, trust, attestation, release_run_id)
    active_directories = [
        paths["runDirectory"],
        paths["trustDirectory"],
        paths["attestationDirectory"],
    ]
    quarantine_directories = [
        _quarantine_path(root, release_run_id) for root in (secret, trust, attestation)
    ]
    if not any(_entry_exists(path) for path in (*active_directories, *quarantine_directories)):
        return

    manifest_locations = (
        paths["manifest"],
        _quarantine_path(trust, release_run_id) / _MANIFEST_FILE,
    )
    existing_manifests = [path for path in manifest_locations if _entry_exists(path)]
    if len(existing_manifests) == 1:
        manifest = _validate_release_manifest(
            _read_json_document(
                existing_manifests[0],
                label="observer release manifest",
            ),
            candidate_root=candidate,
            secret_root=secret,
            trust_root=trust,
            attestation_root=attestation,
            paths=paths,
            release_run_id=release_run_id,
        )
        if manifest["status"] == "committed":
            raise ValueError("observer release batch is committed")
        raise ValueError("observer release batch is uncommitted and must be recovered")
    raise ValueError("observer release directory already exists without a valid manifest")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision one immutable OpenMAIC observer secret batch.",
    )
    parser.add_argument("--candidate-root", required=True, type=Path)
    parser.add_argument("--secret-root", required=True, type=Path)
    parser.add_argument("--trust-root", required=True, type=Path)
    parser.add_argument("--attestation-root", required=True, type=Path)
    parser.add_argument("--release-run-id", required=True)
    parser.add_argument("--container-uid", required=True, type=int)
    parser.add_argument("--container-gid", required=True, type=int)
    for name in _DEPLOYMENT_INPUTS.values():
        parser.add_argument("--" + name.replace("_", "-"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    if _RELEASE_RUN_ID.fullmatch(args.release_run_id) is None:
        raise ValueError("observer release run id is invalid")
    candidate, secret, trust, attestation = _validate_roots(
        candidate_root=args.candidate_root,
        secret_root=args.secret_root,
        trust_root=args.trust_root,
        attestation_root=args.attestation_root,
    )
    with _release_operation_lock(trust, args.release_run_id):
        _refuse_existing_cli_release(
            candidate_root=candidate,
            secret_root=secret,
            trust_root=trust,
            attestation_root=attestation,
            release_run_id=args.release_run_id,
        )
        result = _provision_observer_release_secrets_locked(
            candidate_root=candidate,
            secret_root=secret,
            trust_root=trust,
            attestation_root=attestation,
            release_run_id=args.release_run_id,
            container_uid=args.container_uid,
            container_gid=args.container_gid,
            deployment_environment={
                environment_name: getattr(args, argument_name)
                for environment_name, argument_name in _DEPLOYMENT_INPUTS.items()
            },
        )
    manifest = result.get("releaseManifest")
    if not isinstance(manifest, dict) or manifest.get("status") != "committed":
        raise RuntimeError("observer release manifest was not committed")
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
