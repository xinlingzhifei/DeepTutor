#!/usr/bin/env python
"""Render and verify digest-pinned private platform Compose configuration."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from typing import Any
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
IMAGE_LOCK_PATH = PROJECT_ROOT / "deploy" / "image-lock.json"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
RELEASE_TAG_PATTERN = re.compile(r"^yfeistai-first-release-[0-9]{8}-([0-9a-f]{8})$")
SOURCE_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ZERO_DIGEST = "sha256:" + ("0" * 64)
GITHUB_RUN_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,31}$")
MAX_PROMOTION_DOCUMENT_BYTES = 64 * 1024
MAX_ACTIONS_METADATA_BYTES = 1024 * 1024
PROMOTION_OBSERVED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
PROMOTION_ARCHIVE_MAX_BYTES = 16 * 1024 * 1024
PROMOTION_ARCHIVE_MAX_MEMBER_BYTES = 4 * 1024 * 1024
PROMOTION_ARCHIVE_MAX_TOTAL_BYTES = 8 * 1024 * 1024
PROMOTION_ARCHIVE_MAX_COMPRESSION_RATIO = 100
PROMOTION_ARCHIVE_MEMBERS = (
    "deploy/image-lock.json",
    "docker-compose.yml",
    "docker-compose.platform.yml",
    "docker-compose.data-plane.yml",
    "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
    "artifacts/source_head.json",
    "artifacts/image_digests.json",
    "artifacts/promotion_intent.json",
    "artifacts/promotion_receipt.json",
)
PROMOTION_COMPOSE_MEMBERS = PROMOTION_ARCHIVE_MEMBERS[1:5]
PUBLISH_JOURNAL_OWNER = "yfeistai-image-lock-writer"
SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
CUSTOM_IMAGE_NAMES = ("deeptutor", "openmaic", "openmaic_render")
OBSERVER_SIGNER_NAME = "observer_signer"
CANDIDATE_IMAGE_NAMES = (*CUSTOM_IMAGE_NAMES, OBSERVER_SIGNER_NAME)
_ACTIVE_PUBLISH_LEASES: set[str] = set()
_ACTIVE_PUBLISH_LEASES_GUARD = threading.Lock()
OBSERVER_SIGNER_SPEC: dict[str, str] = {
    "repository": "ghcr.io/xinlingzhifei/yfeistai-observer-signer",
}
COMPOSE_IMAGE_NAMES = {
    "docker-compose.yml": ("pocketbase",),
    "docker-compose.platform.yml": (
        "deeptutor",
        "openmaic",
        "openmaic_render",
        "nginx",
        "postgres",
        "minio",
        "minio_client",
    ),
    "docker-compose.data-plane.yml": ("openmaic", "openmaic_render"),
    "docker-compose.openmaic-shared-ingress-observer.yml": (),
}


@dataclass(frozen=True)
class CandidateArtifactPaths:
    root: Path
    image_lock: Path
    base_compose: Path
    platform_compose: Path
    data_plane_compose: Path
    observer_overlay: Path


class CandidateArtifactError(ValueError):
    """The fixed release-candidate set was invalid or changed mid-operation."""


@dataclass(frozen=True)
class ValidatedCandidateArtifact:
    paths: CandidateArtifactPaths
    image_lock_bytes: bytes
    image_lock: Mapping[str, Any]
    candidate: Mapping[str, Any]
    compose_bytes: tuple[tuple[Path, bytes], ...]
    _check_unchanged: Callable[[], None]

    def check_unchanged(self) -> None:
        """Recheck the original snapshot while its descriptor lease remains open."""
        try:
            _reject_in_progress_publish_journal(self.paths.image_lock)
            self._check_unchanged()
        except ValueError as exc:
            raise CandidateArtifactError(str(exc)) from exc

    def candidate_copy(self) -> dict[str, Any]:
        """Return a detached mutable candidate for existing consumer APIs."""
        return json.loads(json.dumps(self.candidate))


def candidate_artifact_paths(candidate_root: Path) -> CandidateArtifactPaths:
    """Resolve the fixed file layout of one immutable release artifact."""
    root = Path(candidate_root)
    if not root.is_absolute():
        raise ValueError("candidate root must be an absolute path")
    root = root.resolve()
    return CandidateArtifactPaths(
        root=root,
        image_lock=root / "deploy" / "image-lock.json",
        base_compose=root / "docker-compose.yml",
        platform_compose=root / "docker-compose.platform.yml",
        data_plane_compose=root / "docker-compose.data-plane.yml",
        observer_overlay=(root / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"),
    )


IMAGE_SPECS: dict[str, dict[str, Any]] = {
    "deeptutor": {
        "repository": "ghcr.io/xinlingzhifei/deeptutor",
        "tag": "first-release",
    },
    "openmaic": {
        "repository": "ghcr.io/xinlingzhifei/openmaic",
        "tag": "0.3.1-0cf2a330",
        "source": {
            "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
            "revision": "0cf2a330411681190e89f48e20f305345ff99f87",
            "dockerfile": "integrations/openmaic/Dockerfile",
        },
    },
    "openmaic_render": {
        "repository": "ghcr.io/xinlingzhifei/openmaic-render",
        "tag": "0.3.1-0cf2a330",
        "source": {
            "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
            "revision": "0cf2a330411681190e89f48e20f305345ff99f87",
            "dockerfile": "render-service/Dockerfile",
        },
    },
    "nginx": {
        "repository": "nginx",
        "tag": "1.29.8-alpine3.23",
    },
    "postgres": {
        "repository": "postgres",
        "tag": "16.14-alpine3.24",
    },
    "minio": {
        "repository": "minio/minio",
        "tag": "RELEASE.2025-04-22T22-12-26Z",
    },
    "minio_client": {
        "repository": "minio/mc",
        "tag": "RELEASE.2025-04-16T18-13-26Z",
    },
    "pocketbase": {
        "repository": "ghcr.io/muchobien/pocketbase",
        "tag": "latest",
    },
}


def _tagged_reference(spec: Mapping[str, Any], *, tag: str | None = None) -> str:
    return f"{spec['repository']}:{tag or spec['tag']}"


def _candidate_identity(
    *,
    source_repository: object,
    source_head: object,
    release_tag: object,
    openmaic_head: object,
    image_digests: object | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(source_repository, str)
        or SOURCE_REPOSITORY_PATTERN.fullmatch(source_repository) is None
        or source_repository != SOURCE_REPOSITORY
    ):
        raise ValueError("image lock candidate source repository is invalid")
    if (
        not isinstance(source_head, str)
        or COMMIT_PATTERN.fullmatch(source_head) is None
        or source_head == "0" * 40
    ):
        raise ValueError("image lock candidate source head is invalid")
    release_match = (
        RELEASE_TAG_PATTERN.fullmatch(release_tag) if isinstance(release_tag, str) else None
    )
    if release_match is None or release_match.group(1) != source_head[:8]:
        raise ValueError("image lock candidate release tag is invalid")
    if openmaic_head != OPENMAIC_HEAD:
        raise ValueError("image lock candidate OpenMAIC head is invalid")
    candidate: dict[str, Any] = {
        "sourceRepository": source_repository,
        "sourceHead": source_head,
        "releaseTag": release_tag,
        "openmaicHead": openmaic_head,
    }
    if image_digests is not None:
        if not isinstance(image_digests, dict) or set(image_digests) != set(CUSTOM_IMAGE_NAMES):
            raise ValueError("image lock candidate image digests are invalid")
        for name in CUSTOM_IMAGE_NAMES:
            digest = image_digests.get(name)
            if (
                not isinstance(digest, str)
                or DIGEST_PATTERN.fullmatch(digest) is None
                or digest == ZERO_DIGEST
            ):
                raise ValueError("image lock candidate image digests are invalid")
        candidate["imageDigests"] = dict(image_digests)
    return candidate


def _registry_digest(reference: str) -> str | None:
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("docker was not found on PATH while resolving registry digest")
    result = subprocess.run(
        [
            docker,
            "buildx",
            "imagetools",
            "inspect",
            "--raw",
            reference,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    if not isinstance(result.stdout, bytes):
        return None
    try:
        manifest = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(manifest, dict):
        return None
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def _render_compose_references_text(
    compose_name: str,
    template: str,
    images: Mapping[str, Mapping[str, Any]],
    support_images: Mapping[str, Mapping[str, Any]],
) -> str:
    image_names = COMPOSE_IMAGE_NAMES.get(compose_name)
    if image_names is None:
        raise ValueError(f"unsupported production Compose file: {compose_name}")
    rendered = template
    for name in image_names:
        repository = str(IMAGE_SPECS[name]["repository"])
        template_tag = str(IMAGE_SPECS[name]["tag"])
        desired_tag = str(images[name]["tag"])
        allowed_tags = "|".join(
            re.escape(tag) for tag in dict.fromkeys((template_tag, desired_tag))
        )
        digest_suffix = (
            r"(?:@sha256:[0-9a-f]{64})?" if name == "pocketbase" else r"@sha256:[0-9a-f]{64}"
        )
        pattern = re.compile(rf"{re.escape(repository)}:(?:{allowed_tags})" + digest_suffix)
        rendered, count = pattern.subn(str(images[name]["reference"]), rendered)
        if count != 1:
            raise ValueError(f"production Compose image reference is invalid for {name}")
    if compose_name == "docker-compose.openmaic-shared-ingress-observer.yml":
        signer = support_images.get(OBSERVER_SIGNER_NAME)
        if not isinstance(signer, Mapping):
            raise ValueError("observer signer support image is required")
        repository = OBSERVER_SIGNER_SPEC["repository"]
        pattern = re.compile(
            r"\$\{YFEISTAI_OBSERVER_SIGNER_IMAGE:\?[^}\r\n]+\}"
            rf"|{re.escape(repository)}:[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}"
            rf"@sha256:[0-9a-f]{{64}}"
        )
        rendered, count = pattern.subn(str(signer["reference"]), rendered)
        if count != 2:
            raise ValueError("production Compose image reference is invalid for observer signer")
    return rendered


def _render_compose_references(
    compose_path: Path,
    images: Mapping[str, Mapping[str, Any]],
    support_images: Mapping[str, Mapping[str, Any]],
) -> str:
    if compose_path.name not in COMPOSE_IMAGE_NAMES:
        raise ValueError(f"unsupported production Compose file: {compose_path.name}")
    try:
        template = compose_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"production Compose file could not be read: {compose_path.name}") from exc
    return _render_compose_references_text(
        compose_path.name,
        template,
        images,
        support_images,
    )


def _read_source_compose_templates(source_head: str) -> dict[str, bytes]:
    if (
        not isinstance(source_head, str)
        or COMMIT_PATTERN.fullmatch(source_head) is None
        or source_head == "0" * 40
    ):
        raise ValueError("promotion source head is invalid")
    git = shutil.which("git")
    if git is None:
        raise ValueError("promotion source templates are unavailable")

    templates: dict[str, bytes] = {}
    total = 0
    for repository_path in PROMOTION_COMPOSE_MEMBERS:
        object_spec = f"{source_head}:{repository_path}"
        try:
            size_result = subprocess.run(
                [git, "--no-replace-objects", "cat-file", "-s", object_spec],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                check=False,
                timeout=30,
            )
            raw_size = size_result.stdout.strip() if isinstance(size_result.stdout, bytes) else None
            if (
                type(size_result.returncode) is not int
                or size_result.returncode != 0
                or raw_size is None
                or re.fullmatch(rb"[0-9]+", raw_size) is None
            ):
                raise ValueError("promotion source template Git object is invalid")
            size = int(raw_size)
            total += size
            if (
                size <= 0
                or size > PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
                or total > PROMOTION_ARCHIVE_MAX_TOTAL_BYTES
            ):
                raise ValueError("promotion source template is too large")
            blob_result = subprocess.run(
                [git, "--no-replace-objects", "cat-file", "blob", object_spec],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("promotion source templates are unavailable") from exc
        if (
            type(blob_result.returncode) is not int
            or blob_result.returncode != 0
            or not isinstance(blob_result.stdout, bytes)
            or len(blob_result.stdout) != size
        ):
            raise ValueError("promotion source template Git object is invalid")
        templates[repository_path] = blob_result.stdout
    return templates


def _stage_text(path: Path, content: str) -> _StagedPath:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_identity = _entry_identity(path.parent, directory=True)
    payload = content.encode("utf-8")
    temporary_path: Path | None = None
    staged_path: _StagedPath | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            details = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or _entry_identity(path.parent, directory=True) != parent_identity
            ):
                raise ValueError("image lock staged publish topology changed")
            staged_path = _StagedPath(
                path=temporary_path,
                parent_identity=parent_identity,
                file_state=(
                    int(details.st_dev),
                    int(details.st_ino),
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
            os.fsync(handle.fileno())
    except BaseException as stage_error:
        if temporary_path is not None:
            if staged_path is None:
                cleanup_error = ValueError(
                    "image lock staged cleanup topology changed; "
                    f"residual preserved at {os.path.abspath(temporary_path)}"
                )
                raise BaseExceptionGroup(
                    "image lock stage and staged cleanup failed",
                    [stage_error, cleanup_error],
                ) from None
            _cleanup_staged_paths_preserving_error((staged_path,), stage_error)
        raise
    if temporary_path is None or staged_path is None:
        raise RuntimeError("image lock staged publish target is missing")
    return _capture_staged_path(
        temporary_path,
        parent_identity,
        expected_state=staged_path.file_state,
    )


def _restore_bytes(path: Path, content: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_journal_path(lock_path: Path) -> Path:
    return lock_path.with_name(f".{lock_path.name}.publish-journal.json")


def _publish_lease_path(publish_path: Path) -> Path:
    lease_root = Path(tempfile.gettempdir()) / "yfeistai-image-lock-publish-leases"
    _assert_plain_publish_path(lease_root)
    lease_root.mkdir(mode=0o700, exist_ok=True)
    _assert_plain_publish_path(lease_root)
    if not stat.S_ISDIR(lease_root.lstat().st_mode):
        raise ValueError("image lock publish lease directory is invalid")
    target_key = hashlib.sha256(_path_key(publish_path).encode("utf-8")).hexdigest()
    return lease_root / f"{target_key}.lock"


def _stat_is_link_or_reparse(details: os.stat_result) -> bool:
    if stat.S_ISLNK(details.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(details, "st_file_attributes", 0) & reparse_flag)


def _is_link_or_reparse(path: Path) -> bool:
    target = Path(path)
    try:
        details = target.lstat()
    except FileNotFoundError:
        return False
    if _stat_is_link_or_reparse(details):
        return True
    is_junction = getattr(target, "is_junction", None)
    try:
        return bool(callable(is_junction) and is_junction())
    except OSError:
        return True


def _assert_plain_publish_path(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            if current == absolute:
                return
            raise ValueError("image lock publish target topology is invalid")
        if _is_link_or_reparse(current):
            raise ValueError("image lock linked publish target is not allowed")


def _entry_identity(path: Path, *, directory: bool) -> tuple[int, int]:
    try:
        details = path.lstat()
    except OSError:
        raise ValueError("image lock publish target topology changed") from None
    expected_type = stat.S_IFDIR if directory else stat.S_IFREG
    if _is_link_or_reparse(path) or stat.S_IFMT(details.st_mode) != expected_type:
        raise ValueError("image lock publish target topology changed")
    return int(details.st_dev), int(details.st_ino)


@dataclass
class _PublishTargetBinding:
    path: Path
    parent: Path
    parent_identity: tuple[int, int]
    target_state: tuple[int, int, str] | None


@dataclass(frozen=True)
class _StagedPath:
    path: Path
    parent_identity: tuple[int, int]
    file_state: tuple[int, int, str]


@dataclass(frozen=True)
class _LoadedPublishJournal:
    schema_version: int
    transaction_id: str | None
    status: str
    targets: tuple[tuple[Path, bytes | None], ...]


@dataclass
class _LeasedCandidateArtifact:
    path: Path
    descriptor: int
    signature: tuple[int, int, int, int, int, int]
    max_bytes: int | None
    content: bytes = b""
    digest: str = ""


@dataclass(frozen=True)
class _CandidateArtifactDescriptorLease:
    contents: Mapping[str, bytes]
    check_unchanged: Callable[[], None]


def _publish_paths(output_path: Path, compose_paths: Sequence[Path]) -> tuple[Path, ...]:
    paths = tuple(
        Path(os.path.abspath(path))
        for path in (*tuple(Path(path) for path in compose_paths), Path(output_path))
    )
    if len({_path_key(path) for path in paths}) != len(paths):
        raise ValueError("image lock publish targets must be distinct")
    for path in paths:
        _assert_plain_publish_path(path)
    return paths


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _stable_file_signature(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
    )


def _read_fixed_file_bytes(
    path: Path,
    *,
    error_message: str,
    allow_missing: bool = False,
    max_bytes: int | None = None,
) -> tuple[bytes, tuple[int, int, str]] | None:
    target = Path(path)
    try:
        before = target.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ValueError(error_message) from None
    except OSError as exc:
        raise ValueError(error_message) from exc
    if _stat_is_link_or_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError(error_message)

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                _stat_is_link_or_reparse(opened)
                or not stat.S_ISREG(opened.st_mode)
                or _stable_file_signature(opened) != _stable_file_signature(before)
                or (max_bytes is not None and int(opened.st_size) > max_bytes)
            ):
                raise ValueError(error_message)

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ValueError(error_message)
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError(error_message)
                chunks.append(chunk)

            after_handle = os.fstat(descriptor)
            after_path = target.lstat()
            if (
                _stat_is_link_or_reparse(after_handle)
                or _stat_is_link_or_reparse(after_path)
                or not stat.S_ISREG(after_handle.st_mode)
                or not stat.S_ISREG(after_path.st_mode)
                or _stable_file_signature(after_handle) != _stable_file_signature(opened)
                or _stable_file_signature(after_path) != _stable_file_signature(opened)
                or total != int(opened.st_size)
            ):
                raise ValueError(error_message)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError(error_message) from exc

    content = b"".join(chunks)
    return (
        content,
        (
            int(opened.st_dev),
            int(opened.st_ino),
            hashlib.sha256(content).hexdigest(),
        ),
    )


def _plain_directory_signature(
    path: Path,
    *,
    error_message: str,
) -> tuple[int, int, int]:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError(error_message) from exc
    if _stat_is_link_or_reparse(details) or not stat.S_ISDIR(details.st_mode):
        raise ValueError(error_message)
    # Evidence may be published beside inputs; retain directory identity, not
    # child-entry timestamps. Protected files keep their full stable signatures.
    return int(details.st_dev), int(details.st_ino), int(details.st_mode)


def _read_leased_candidate_artifact(
    artifact: _LeasedCandidateArtifact,
    *,
    error_message: str,
) -> bytes:
    try:
        before = os.fstat(artifact.descriptor)
        if (
            _stat_is_link_or_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or _stable_file_signature(before) != artifact.signature
            or (artifact.max_bytes is not None and int(before.st_size) > artifact.max_bytes)
            or os.lseek(artifact.descriptor, 0, os.SEEK_SET) != 0
        ):
            raise ValueError(error_message)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(artifact.descriptor, 64 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValueError(error_message)
            total += len(chunk)
            if artifact.max_bytes is not None and total > artifact.max_bytes:
                raise ValueError(error_message)
            chunks.append(chunk)
        after = os.fstat(artifact.descriptor)
    except OSError as exc:
        raise ValueError(error_message) from exc
    if (
        _stat_is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or _stable_file_signature(after) != artifact.signature
        or total != int(before.st_size)
    ):
        raise ValueError(error_message)
    return b"".join(chunks)


def _verify_candidate_artifact_lease_topology(
    artifacts: Sequence[_LeasedCandidateArtifact],
    directories: Mapping[
        str,
        tuple[Path, tuple[int, int, int]],
    ],
    *,
    error_message: str,
) -> None:
    for path, expected in directories.values():
        if _plain_directory_signature(path, error_message=error_message) != expected:
            raise ValueError(error_message)
    for artifact in artifacts:
        try:
            opened = os.fstat(artifact.descriptor)
            linked = artifact.path.lstat()
        except OSError as exc:
            raise ValueError(error_message) from exc
        if (
            _stat_is_link_or_reparse(opened)
            or _stat_is_link_or_reparse(linked)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or _stable_file_signature(opened) != artifact.signature
            or _stable_file_signature(linked) != artifact.signature
        ):
            raise ValueError(error_message)


@contextmanager
def _candidate_artifact_descriptor_lease(
    root: Path,
    paths: Sequence[Path],
    *,
    max_bytes: Mapping[str, int] | None = None,
) -> Iterator[_CandidateArtifactDescriptorLease]:
    error_message = "promotion candidate artifact changed during verification"
    root = Path(os.path.abspath(root))
    protected = tuple(Path(os.path.abspath(path)) for path in paths)
    if len({_path_key(path) for path in protected}) != len(protected) or any(
        os.path.commonpath((_path_key(root), _path_key(path))) != _path_key(root)
        for path in protected
    ):
        raise ValueError("promotion candidate artifact layout is invalid")
    for path in protected:
        _assert_plain_publish_path(path)

    directory_paths = tuple(
        {_path_key(path): path for path in (root, *(path.parent for path in protected))}.values()
    )
    directories = {
        _path_key(path): (
            path,
            _plain_directory_signature(path, error_message=error_message),
        )
        for path in directory_paths
    }
    limits = max_bytes or {}
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    artifacts: list[_LeasedCandidateArtifact] = []
    body_error: BaseException | None = None
    try:
        for path in protected:
            descriptor: int | None = None
            try:
                linked = path.lstat()
                descriptor = os.open(path, flags)
                opened = os.fstat(descriptor)
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise ValueError("promotion candidate artifact is incomplete") from exc
            if (
                _stat_is_link_or_reparse(linked)
                or _stat_is_link_or_reparse(opened)
                or not stat.S_ISREG(linked.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or _stable_file_signature(linked) != _stable_file_signature(opened)
            ):
                os.close(descriptor)
                raise ValueError(error_message)
            artifacts.append(
                _LeasedCandidateArtifact(
                    path=path,
                    descriptor=descriptor,
                    signature=_stable_file_signature(opened),
                    max_bytes=limits.get(_path_key(path)),
                )
            )

        _verify_candidate_artifact_lease_topology(
            artifacts,
            directories,
            error_message=error_message,
        )
        for artifact in artifacts:
            artifact.content = _read_leased_candidate_artifact(
                artifact,
                error_message=error_message,
            )
            artifact.digest = hashlib.sha256(artifact.content).hexdigest()
        _verify_candidate_artifact_lease_topology(
            artifacts,
            directories,
            error_message=error_message,
        )

        def check_unchanged() -> None:
            for artifact in reversed(artifacts):
                reread = _read_leased_candidate_artifact(
                    artifact,
                    error_message=error_message,
                )
                if hashlib.sha256(reread).hexdigest() != artifact.digest:
                    raise ValueError(error_message)
            _verify_candidate_artifact_lease_topology(
                artifacts,
                directories,
                error_message=error_message,
            )

        yield _CandidateArtifactDescriptorLease(
            contents={_path_key(artifact.path): artifact.content for artifact in artifacts},
            check_unchanged=check_unchanged,
        )

        check_unchanged()
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        close_errors: list[BaseException] = []
        for artifact in reversed(artifacts):
            try:
                os.close(artifact.descriptor)
            except BaseException as exc:
                close_errors.append(exc)
        if close_errors:
            close_error: BaseException = (
                close_errors[0]
                if len(close_errors) == 1
                else BaseExceptionGroup(
                    "promotion candidate artifact lease close failed",
                    close_errors,
                )
            )
            if body_error is not None:
                raise BaseExceptionGroup(
                    "promotion candidate verification and lease close failed",
                    [body_error, close_error],
                ) from None
            raise close_error.with_traceback(close_error.__traceback__)


def _target_file_state(path: Path) -> tuple[int, int, str] | None:
    snapshot = _read_fixed_file_bytes(
        path,
        error_message="image lock publish target topology changed",
        allow_missing=True,
    )
    if snapshot is None:
        return None
    return snapshot[1]


def _capture_staged_path(
    path: Path,
    parent_identity: tuple[int, int],
    *,
    expected_state: tuple[int, int, str],
) -> _StagedPath:
    try:
        if _entry_identity(path.parent, directory=True) != parent_identity:
            raise ValueError
        file_state = _target_file_state(path)
        if file_state != expected_state:
            raise ValueError
        if _entry_identity(path.parent, directory=True) != parent_identity:
            raise ValueError
    except ValueError:
        raise ValueError("image lock staged publish topology changed") from None
    return _StagedPath(
        path=path,
        parent_identity=parent_identity,
        file_state=file_state,
    )


def _staged_file_state(
    staged_path: _StagedPath,
    *,
    allow_missing: bool,
    error_message: str,
) -> tuple[int, int, str] | None:
    try:
        if _entry_identity(staged_path.path.parent, directory=True) != staged_path.parent_identity:
            raise ValueError
        file_state = _target_file_state(staged_path.path)
        if file_state is None:
            if not allow_missing:
                raise ValueError
        elif file_state != staged_path.file_state:
            raise ValueError
        if _entry_identity(staged_path.path.parent, directory=True) != staged_path.parent_identity:
            raise ValueError
    except ValueError:
        raise ValueError(error_message) from None
    return file_state


def _capture_publish_topology(paths: Sequence[Path]) -> list[_PublishTargetBinding]:
    bindings: list[_PublishTargetBinding] = []
    for path in paths:
        _assert_plain_publish_path(path)
        parent = path.parent
        parent_identity = _entry_identity(parent, directory=True)
        target_state = _target_file_state(path)
        bindings.append(
            _PublishTargetBinding(
                path=path,
                parent=parent,
                parent_identity=parent_identity,
                target_state=target_state,
            )
        )
    return bindings


def _assert_publish_topology_unchanged(bindings: Sequence[_PublishTargetBinding]) -> None:
    for binding in bindings:
        try:
            _assert_plain_publish_path(binding.path)
            if _entry_identity(binding.parent, directory=True) != binding.parent_identity:
                raise ValueError
            if _target_file_state(binding.path) != binding.target_state:
                raise ValueError
        except ValueError:
            raise ValueError("image lock publish target topology changed") from None


def _refresh_published_target(
    bindings: Sequence[_PublishTargetBinding],
    target: Path,
) -> None:
    target_key = _path_key(target)
    for binding in bindings:
        if _path_key(binding.path) != target_key:
            continue
        if _entry_identity(binding.parent, directory=True) != binding.parent_identity:
            raise ValueError("image lock publish target topology changed")
        binding.target_state = _target_file_state(binding.path)
        return
    raise ValueError("image lock publish target topology changed")


def _replace_published_target(
    staged_path: _StagedPath,
    target: Path,
    bindings: Sequence[_PublishTargetBinding],
) -> None:
    target_key = _path_key(target)
    binding = next(
        (item for item in bindings if _path_key(item.path) == target_key),
        None,
    )
    if binding is None:
        raise ValueError("image lock publish target topology changed")
    staged_state = _staged_file_state(
        staged_path,
        allow_missing=False,
        error_message="image lock staged publish topology changed",
    )
    if staged_state is None:
        raise ValueError("image lock staged publish topology changed")
    previous_state = binding.target_state
    _assert_publish_topology_unchanged(bindings)
    try:
        os.replace(staged_path.path, target)
    except BaseException as replace_error:
        try:
            current_state = _target_file_state(target)
        except ValueError:
            raise ValueError("image lock publish target topology changed") from replace_error
        if current_state == staged_state:
            binding.target_state = current_state
        elif current_state != previous_state:
            raise ValueError("image lock publish target topology changed") from replace_error
        raise
    current_state = _target_file_state(target)
    if current_state != staged_state:
        raise ValueError("image lock publish target topology changed")
    binding.target_state = current_state


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _FileAttributeTagInformation(ctypes.Structure):
            _fields_ = (
                ("fileAttributes", wintypes.DWORD),
                ("reparseTag", wintypes.DWORD),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(directory),
            0x40000000,  # GENERIC_WRITE is required by FlushFileBuffers.
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in (None, invalid_handle):
            error = ctypes.get_last_error()
            raise OSError(error, f"image lock publish directory could not be opened: {directory}")
        body_error: BaseException | None = None
        try:
            get_information = kernel32.GetFileInformationByHandleEx
            get_information.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            get_information.restype = wintypes.BOOL
            attributes = _FileAttributeTagInformation()
            if not get_information(
                handle,
                9,
                ctypes.byref(attributes),
                ctypes.sizeof(attributes),
            ):
                error = ctypes.get_last_error()
                raise OSError(
                    error,
                    f"image lock publish directory could not be inspected: {directory}",
                )
            if not attributes.fileAttributes & 0x00000010 or attributes.fileAttributes & 0x00000400:
                raise ValueError("image lock publish directory is not a plain directory")
            flush = kernel32.FlushFileBuffers
            flush.argtypes = (wintypes.HANDLE,)
            flush.restype = wintypes.BOOL
            if not flush(handle):
                error = ctypes.get_last_error()
                raise OSError(
                    error,
                    f"image lock publish directory could not be flushed: {directory}",
                )
        except BaseException as error:
            body_error = error
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        if not close(handle):
            close_error = OSError(
                ctypes.get_last_error(),
                f"image lock publish directory handle could not be closed: {directory}",
            )
            if body_error is not None:
                raise BaseExceptionGroup(
                    "image lock publish directory flush and cleanup failed",
                    [body_error, close_error],
                ) from None
            raise close_error
        if body_error is not None:
            raise body_error.with_traceback(body_error.__traceback__)
        return
    descriptor = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("image lock publish directory is not a plain directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _staged_cleanup_topology_error(*residual_paths: Path) -> ValueError:
    message = "image lock staged cleanup topology changed"
    residual_path = next(
        (path for path in residual_paths if os.path.lexists(path)),
        None,
    )
    if residual_path is not None:
        message += f"; residual preserved at {os.path.abspath(residual_path)}"
    return ValueError(message)


def _retire_staged_quarantine(
    quarantine: Path,
    quarantine_identity: tuple[int, int],
    parent: Path,
    parent_identity: tuple[int, int],
    quarantined_path: Path,
    retained_path: Path,
) -> None:
    try:
        if _entry_identity(quarantine, directory=True) != quarantine_identity:
            raise ValueError
    except ValueError:
        raise _staged_cleanup_topology_error(
            quarantined_path,
            quarantine,
            retained_path,
        ) from None
    try:
        _sync_directory(quarantine)
        quarantine.rmdir()
    except BaseException as cleanup_error:
        raise BaseExceptionGroup(
            "image lock staged quarantine retirement failed",
            [
                cleanup_error,
                _staged_cleanup_topology_error(
                    quarantined_path,
                    quarantine,
                    retained_path,
                ),
            ],
        ) from None
    try:
        if _entry_identity(parent, directory=True) != parent_identity:
            raise ValueError
    except ValueError:
        raise _staged_cleanup_topology_error(
            quarantined_path,
            quarantine,
            retained_path,
        ) from None
    try:
        _sync_directory(parent)
    except BaseException as cleanup_error:
        raise BaseExceptionGroup(
            "image lock staged quarantine parent sync failed",
            [
                cleanup_error,
                _staged_cleanup_topology_error(
                    quarantined_path,
                    quarantine,
                    retained_path,
                ),
            ],
        ) from None


def _restore_foreign_staged_entry(
    candidate: _StagedPath,
    quarantine: Path,
    quarantine_identity: tuple[int, int],
    quarantined_path: Path,
    quarantined_state: tuple[int, int, str],
) -> None:
    try:
        os.link(quarantined_path, candidate.path)
    except BaseException as restore_error:
        raise BaseExceptionGroup(
            "image lock staged cleanup topology recovery failed",
            [
                restore_error,
                _staged_cleanup_topology_error(quarantined_path, quarantine),
            ],
        ) from None
    try:
        if (
            _entry_identity(candidate.path.parent, directory=True) != candidate.parent_identity
            or _entry_identity(quarantine, directory=True) != quarantine_identity
            or _target_file_state(candidate.path) != quarantined_state
            or _target_file_state(quarantined_path) != quarantined_state
        ):
            raise ValueError
    except ValueError as restore_error:
        raise BaseExceptionGroup(
            "image lock staged cleanup topology recovery failed",
            [
                restore_error,
                _staged_cleanup_topology_error(
                    quarantined_path,
                    quarantine,
                    candidate.path,
                ),
            ],
        ) from None
    try:
        quarantined_path.unlink()
    except BaseException as restore_error:
        raise BaseExceptionGroup(
            "image lock staged cleanup topology recovery failed",
            [
                restore_error,
                _staged_cleanup_topology_error(
                    quarantined_path,
                    quarantine,
                    candidate.path,
                ),
            ],
        ) from None
    _retire_staged_quarantine(
        quarantine,
        quarantine_identity,
        candidate.path.parent,
        candidate.parent_identity,
        quarantined_path,
        candidate.path,
    )
    raise _staged_cleanup_topology_error(candidate.path)


def _cleanup_staged_path(candidate: _StagedPath) -> None:
    staged_path = candidate.path
    file_state = _staged_file_state(
        candidate,
        allow_missing=True,
        error_message="image lock staged cleanup topology changed",
    )
    if file_state is None:
        return

    parent = staged_path.parent
    quarantine = Path(
        tempfile.mkdtemp(
            prefix=".yfeistai-staged-cleanup-",
            dir=parent,
        )
    )
    try:
        quarantine_identity = _entry_identity(quarantine, directory=True)
        if (
            _entry_identity(parent, directory=True) != candidate.parent_identity
            or next(quarantine.iterdir(), None) is not None
        ):
            raise ValueError
    except (OSError, ValueError):
        raise _staged_cleanup_topology_error(quarantine) from None
    quarantined_path = quarantine / staged_path.name
    if os.path.lexists(quarantined_path):
        raise _staged_cleanup_topology_error(quarantined_path)

    try:
        os.rename(staged_path, quarantined_path)
    except FileNotFoundError as rename_error:
        try:
            if _entry_identity(parent, directory=True) != candidate.parent_identity:
                raise ValueError
            _retire_staged_quarantine(
                quarantine,
                quarantine_identity,
                parent,
                candidate.parent_identity,
                quarantined_path,
                candidate.path,
            )
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "image lock staged cleanup and quarantine retirement failed",
                [rename_error, cleanup_error],
            ) from None
        return
    except BaseException as rename_error:
        try:
            _retire_staged_quarantine(
                quarantine,
                quarantine_identity,
                parent,
                candidate.parent_identity,
                quarantined_path,
                candidate.path,
            )
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "image lock staged cleanup and quarantine retirement failed",
                [rename_error, cleanup_error],
            ) from None
        raise

    try:
        if (
            _entry_identity(parent, directory=True) != candidate.parent_identity
            or _entry_identity(quarantine, directory=True) != quarantine_identity
        ):
            raise ValueError
        quarantined_state = _target_file_state(quarantined_path)
        if quarantined_state is None:
            raise ValueError
    except ValueError:
        raise _staged_cleanup_topology_error(quarantined_path, quarantine) from None

    if quarantined_state != candidate.file_state:
        _restore_foreign_staged_entry(
            candidate,
            quarantine,
            quarantine_identity,
            quarantined_path,
            quarantined_state,
        )

    try:
        quarantined_path.unlink()
    except BaseException as cleanup_error:
        raise BaseExceptionGroup(
            "image lock staged quarantine payload cleanup failed",
            [
                cleanup_error,
                _staged_cleanup_topology_error(quarantined_path, quarantine),
            ],
        ) from None
    _retire_staged_quarantine(
        quarantine,
        quarantine_identity,
        parent,
        candidate.parent_identity,
        quarantined_path,
        candidate.path,
    )


def _cleanup_staged_paths(paths: Sequence[_StagedPath]) -> None:
    cleanup_errors: list[BaseException] = []
    seen_paths: set[str] = set()
    for candidate in paths:
        staged_key = _path_key(candidate.path)
        if staged_key in seen_paths:
            continue
        seen_paths.add(staged_key)
        try:
            _cleanup_staged_path(candidate)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)

    if len(cleanup_errors) == 1:
        cleanup_error = cleanup_errors[0]
        raise cleanup_error.with_traceback(cleanup_error.__traceback__)
    if cleanup_errors:
        raise BaseExceptionGroup(
            "image lock staged publish cleanup failed",
            cleanup_errors,
        )


def _cleanup_staged_paths_preserving_error(
    paths: Sequence[_StagedPath],
    primary_error: BaseException,
) -> None:
    try:
        _cleanup_staged_paths(paths)
    except BaseException as cleanup_error:
        raise BaseExceptionGroup(
            "image lock operation and staged publish cleanup failed",
            [primary_error, cleanup_error],
        ) from None


def _lock_publish_lease(descriptor: int) -> object | None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class _Overlapped(ctypes.Structure):
            _fields_ = (
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            )

        overlapped = _Overlapped()
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        lock_file = ctypes.WinDLL("kernel32", use_last_error=True).LockFileEx
        lock_file.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_Overlapped),
        )
        lock_file.restype = wintypes.BOOL
        if not lock_file(
            handle,
            0x00000002 | 0x00000001,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        ):
            error = ctypes.get_last_error()
            if error == 33:
                raise ValueError("image lock publish lease is active or busy") from None
            raise OSError(error, "image lock publish lease could not be acquired")
        return overlapped
    if os.name == "posix":
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise ValueError("image lock publish lease is active or busy") from None
            raise
        return None
    raise RuntimeError("image lock publish leases are unsupported")


def _unlock_publish_lease(descriptor: int, token: object | None) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        unlock_file = ctypes.WinDLL("kernel32", use_last_error=True).UnlockFileEx
        unlock_file.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        )
        unlock_file.restype = wintypes.BOOL
        if token is None:
            raise RuntimeError("image lock publish lease token is missing")
        if not unlock_file(handle, 0, 1, 0, ctypes.byref(token)):
            error = ctypes.get_last_error()
            raise OSError(error, "image lock publish lease could not be released")
        return
    if os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    raise RuntimeError("image lock publish leases are unsupported")


def _open_publish_lease(lease_path: Path) -> int:
    if os.name != "nt":
        return os.open(
            lease_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )

    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(lease_path),
        0x80000000 | 0x40000000,
        0x00000001 | 0x00000002,
        None,
        4,
        0x00000080 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise OSError(error, "image lock publish lease could not be opened")
    raw_handle = int(handle)
    try:
        return msvcrt.open_osfhandle(raw_handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except BaseException:
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        close(handle)
        raise


@contextmanager
def _publish_transaction_lease(publish_paths: Sequence[Path]) -> Iterator[str]:
    lease_paths = tuple(
        sorted(
            {_publish_lease_path(path) for path in publish_paths},
            key=_path_key,
        )
    )
    lease_keys = {_path_key(path) for path in lease_paths}
    with _ACTIVE_PUBLISH_LEASES_GUARD:
        if lease_keys & _ACTIVE_PUBLISH_LEASES:
            raise ValueError("image lock publish lease is active or busy")
        _ACTIVE_PUBLISH_LEASES.update(lease_keys)

    resources: list[tuple[int, object | None, bool]] = []
    body_error: BaseException | None = None
    try:
        for lease_path in lease_paths:
            descriptor = _open_publish_lease(lease_path)
            resources.append((descriptor, None, False))
            opened = os.fstat(descriptor)
            linked = lease_path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_link_or_reparse(lease_path)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                raise ValueError("image lock publish lease path is invalid")
            token = _lock_publish_lease(descriptor)
            resources[-1] = (descriptor, token, True)
            try:
                confirmed = lease_path.lstat()
            except OSError:
                raise ValueError("image lock publish lease path changed") from None
            if (
                _is_link_or_reparse(lease_path)
                or not stat.S_ISREG(confirmed.st_mode)
                or (opened.st_dev, opened.st_ino) != (confirmed.st_dev, confirmed.st_ino)
            ):
                raise ValueError("image lock publish lease path changed")
        yield secrets.token_hex(32)
    except BaseException as error:
        body_error = error
        raise
    finally:
        release_errors: list[BaseException] = []
        for descriptor, token, acquired in reversed(resources):
            if acquired:
                try:
                    _unlock_publish_lease(descriptor, token)
                except BaseException as error:
                    release_errors.append(error)
            try:
                os.close(descriptor)
            except BaseException as error:
                release_errors.append(error)
        with _ACTIVE_PUBLISH_LEASES_GUARD:
            _ACTIVE_PUBLISH_LEASES.difference_update(lease_keys)
        if release_errors:
            release_error: BaseException = (
                release_errors[0]
                if len(release_errors) == 1
                else BaseExceptionGroup(
                    "image lock publish lease release failed",
                    release_errors,
                )
            )
            if body_error is not None:
                raise BaseExceptionGroup(
                    "image lock publish transaction and lease cleanup failed",
                    [body_error, release_error],
                ) from None
            raise release_error.with_traceback(release_error.__traceback__)


def _load_publish_journal(
    journal_path: Path,
    publish_paths: Sequence[Path],
    *,
    expected_transaction_id: str | None,
    allow_legacy: bool,
) -> _LoadedPublishJournal:
    if _is_link_or_reparse(journal_path):
        raise ValueError("image lock publish journal is invalid")
    try:
        document = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("image lock publish journal is invalid") from exc
    if not isinstance(document, dict):
        raise ValueError("image lock publish journal is invalid")
    schema_version = document.get("schemaVersion")
    if type(schema_version) is not int:
        raise ValueError("image lock publish journal is invalid")
    expected_keys = {"schemaVersion", "owner", "status", "targets"}
    if schema_version == 2:
        expected_keys.add("transactionId")
    if set(document) != expected_keys:
        raise ValueError("image lock publish journal is invalid")
    status = document.get("status")
    if document.get("owner") != PUBLISH_JOURNAL_OWNER or status not in {
        "in-progress",
        "committed",
        "rolled-back",
    }:
        raise ValueError("image lock publish journal is invalid")
    transaction_id: str | None = None
    if schema_version == 1:
        if status != "in-progress" or not allow_legacy or expected_transaction_id is not None:
            raise ValueError("image lock publish journal transaction is invalid")
    elif schema_version == 2:
        transaction_id = document.get("transactionId")
        if (
            not isinstance(transaction_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", transaction_id) is None
            or (expected_transaction_id is not None and transaction_id != expected_transaction_id)
        ):
            raise ValueError("image lock publish journal transaction is invalid")
    else:
        raise ValueError("image lock publish journal is invalid")
    targets = document.get("targets")
    if not isinstance(targets, list) or len(targets) != len(publish_paths):
        raise ValueError("image lock publish journal is invalid")

    recovered: list[tuple[Path, bytes | None]] = []
    expected_paths = tuple(Path(os.path.abspath(path)) for path in publish_paths)
    if len({_path_key(path) for path in expected_paths}) != len(expected_paths):
        raise ValueError("image lock publish journal targets are invalid")
    for target, publish_path, expected_path in zip(
        targets,
        publish_paths,
        expected_paths,
        strict=True,
    ):
        if not isinstance(target, dict) or set(target) != {
            "path",
            "existed",
            "contentBase64",
            "sha256",
        }:
            raise ValueError("image lock publish journal target is invalid")
        raw_path = target.get("path")
        existed = target.get("existed")
        encoded_content = target.get("contentBase64")
        expected_sha256 = target.get("sha256")
        if (
            not isinstance(raw_path, str)
            or not Path(raw_path).is_absolute()
            or _path_key(Path(raw_path)) != _path_key(expected_path)
            or type(existed) is not bool
        ):
            raise ValueError("image lock publish journal target is invalid")
        if not existed:
            if encoded_content is not None or expected_sha256 is not None:
                raise ValueError("image lock publish journal target is invalid")
            recovered.append((publish_path, None))
            continue
        if (
            not isinstance(encoded_content, str)
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise ValueError("image lock publish journal target is invalid")
        try:
            content = base64.b64decode(encoded_content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image lock publish journal target is invalid") from exc
        if hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("image lock publish journal target is invalid")
        recovered.append((publish_path, content))
    return _LoadedPublishJournal(
        schema_version=schema_version,
        transaction_id=transaction_id,
        status=status,
        targets=tuple(recovered),
    )


def _create_publish_journal(
    lock_path: Path,
    publish_paths: Sequence[Path],
    original_bytes: Mapping[Path, bytes | None],
    *,
    transaction_id: str,
) -> Path:
    journal_path = _publish_journal_path(lock_path)
    if os.path.lexists(journal_path):
        raise ValueError("image lock publish journal already exists")
    targets: list[dict[str, Any]] = []
    for publish_path in publish_paths:
        content = original_bytes[publish_path]
        targets.append(
            {
                "path": str(publish_path),
                "existed": content is not None,
                "contentBase64": (
                    base64.b64encode(content).decode("ascii") if content is not None else None
                ),
                "sha256": hashlib.sha256(content).hexdigest() if content is not None else None,
            }
        )
    journal_text = (
        json.dumps(
            {
                "schemaVersion": 2,
                "owner": PUBLISH_JOURNAL_OWNER,
                "status": "in-progress",
                "transactionId": transaction_id,
                "targets": targets,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    temporary_path = _stage_text(journal_path, journal_text)
    try:
        _staged_file_state(
            temporary_path,
            allow_missing=False,
            error_message="image lock staged publish topology changed",
        )
        os.link(temporary_path.path, journal_path)
        _sync_directory(journal_path.parent)
    except BaseException as journal_error:
        _cleanup_staged_paths_preserving_error((temporary_path,), journal_error)
        raise
    else:
        _cleanup_staged_paths((temporary_path,))
    return journal_path


def _retire_publish_journal(journal_path: Path) -> None:
    journal_path.unlink()
    _sync_directory(journal_path.parent)


def _publish_journal_state_for_transaction(
    journal_path: Path,
    transaction_id: str,
) -> str | None:
    if not os.path.lexists(journal_path) or _is_link_or_reparse(journal_path):
        return None
    try:
        document = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(document, dict)
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 2
        or document.get("owner") != PUBLISH_JOURNAL_OWNER
        or document.get("transactionId") != transaction_id
        or document.get("status") not in {"in-progress", "committed", "rolled-back"}
    ):
        return None
    return str(document["status"])


def _mark_publish_journal_terminal(
    journal_path: Path,
    publish_paths: Sequence[Path],
    *,
    transaction_id: str,
    status: str,
) -> None:
    if status not in {"committed", "rolled-back"}:
        raise ValueError("image lock publish journal terminal status is invalid")
    loaded = _load_publish_journal(
        journal_path,
        publish_paths,
        expected_transaction_id=transaction_id,
        allow_legacy=False,
    )
    if loaded.status != "in-progress":
        raise ValueError("image lock publish journal is not in progress")
    try:
        document = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("image lock publish journal is invalid") from exc
    document["status"] = status
    terminal_text = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    temporary_path = _stage_text(journal_path, terminal_text)
    try:
        _staged_file_state(
            temporary_path,
            allow_missing=False,
            error_message="image lock staged publish topology changed",
        )
        os.replace(temporary_path.path, journal_path)
        _sync_directory(journal_path.parent)
    except BaseException as journal_error:
        _cleanup_staged_paths_preserving_error((temporary_path,), journal_error)
        raise
    else:
        _cleanup_staged_paths((temporary_path,))


def _restore_publish_targets(
    recovered: Sequence[tuple[Path, bytes | None]],
    *,
    topology: Sequence[_PublishTargetBinding],
) -> None:
    for publish_path, content in reversed(recovered):
        _assert_publish_topology_unchanged(topology)
        if content is None:
            publish_path.unlink(missing_ok=True)
        else:
            _restore_bytes(publish_path, content)
        _refresh_published_target(topology, publish_path)
    _assert_publish_topology_unchanged(topology)
    for publish_path, content in recovered:
        if content is None:
            if publish_path.exists():
                raise OSError(f"publish target still exists after recovery: {publish_path}")
        elif publish_path.read_bytes() != content:
            raise OSError(f"publish target recovery mismatch: {publish_path}")
    recovered_parents = {_path_key(path.parent): path.parent for path, _content in recovered}
    for parent in recovered_parents.values():
        _assert_publish_topology_unchanged(topology)
        _sync_directory(parent)
    _assert_publish_topology_unchanged(topology)


def _recover_publish_journal(
    lock_path: Path,
    publish_paths: Sequence[Path],
    *,
    expected_transaction_id: str | None,
    allow_legacy: bool,
    topology: Sequence[_PublishTargetBinding],
) -> None:
    journal_path = _publish_journal_path(lock_path)
    if not os.path.lexists(journal_path):
        return
    loaded = _load_publish_journal(
        journal_path,
        publish_paths,
        expected_transaction_id=expected_transaction_id,
        allow_legacy=allow_legacy,
    )
    try:
        if loaded.status == "in-progress":
            _restore_publish_targets(loaded.targets, topology=topology)
            if loaded.schema_version == 2 and loaded.transaction_id is not None:
                _mark_publish_journal_terminal(
                    journal_path,
                    publish_paths,
                    transaction_id=loaded.transaction_id,
                    status="rolled-back",
                )
        _retire_publish_journal(journal_path)
    except (OSError, ValueError) as exc:
        raise RuntimeError("image lock publish journal recovery failed") from exc


def _reject_in_progress_publish_journal(lock_path: Path) -> None:
    if os.path.lexists(_publish_journal_path(lock_path)):
        raise ValueError("image lock publish journal is in progress")


def write_image_lock(
    output_path: Path = IMAGE_LOCK_PATH,
    *,
    digest_resolver: Callable[[str], str | None] = _registry_digest,
    digest_overrides: Mapping[str, str] | None = None,
    compose_paths: Sequence[Path] = (),
    source_repository: str,
    source_head: str,
    release_tag: str,
    openmaic_head: str,
) -> dict[str, Any]:
    """Record one resolved image set in the lock and selected Compose files."""
    publish_paths = _publish_paths(output_path, compose_paths)
    output_path = publish_paths[-1]
    compose_paths = publish_paths[:-1]
    initial_topology = _capture_publish_topology(publish_paths)
    with _publish_transaction_lease(publish_paths) as transaction_id:
        _assert_publish_topology_unchanged(initial_topology)
        _recover_publish_journal(
            output_path,
            publish_paths,
            expected_transaction_id=None,
            allow_legacy=True,
            topology=initial_topology,
        )
        topology = _capture_publish_topology(publish_paths)
        return _write_image_lock_locked(
            output_path=output_path,
            digest_resolver=digest_resolver,
            digest_overrides=digest_overrides,
            compose_paths=compose_paths,
            source_repository=source_repository,
            source_head=source_head,
            release_tag=release_tag,
            openmaic_head=openmaic_head,
            publish_paths=publish_paths,
            topology=topology,
            transaction_id=transaction_id,
        )


def _write_image_lock_locked(
    *,
    output_path: Path,
    digest_resolver: Callable[[str], str | None],
    digest_overrides: Mapping[str, str] | None,
    compose_paths: Sequence[Path],
    source_repository: str,
    source_head: str,
    release_tag: str,
    openmaic_head: str,
    publish_paths: Sequence[Path],
    topology: list[_PublishTargetBinding],
    transaction_id: str,
) -> dict[str, Any]:
    def resolve_digest(reference: str) -> str | None:
        _assert_publish_topology_unchanged(topology)
        try:
            return digest_resolver(reference)
        finally:
            _assert_publish_topology_unchanged(topology)

    explicit_digests: dict[str, str] | None = None
    if digest_overrides is not None:
        if not isinstance(digest_overrides, Mapping) or set(digest_overrides) != set(
            CANDIDATE_IMAGE_NAMES
        ):
            raise ValueError("all candidate image digests are required")
        explicit_digests = {}
        for name in CANDIDATE_IMAGE_NAMES:
            digest = digest_overrides.get(name)
            if (
                not isinstance(digest, str)
                or DIGEST_PATTERN.fullmatch(digest) is None
                or digest == ZERO_DIGEST
            ):
                raise ValueError(f"candidate image digest is invalid for {name}")
            explicit_digests[name] = digest
        if len(set(explicit_digests.values())) != len(explicit_digests):
            raise ValueError("candidate image digests must be distinct")
    candidate = _candidate_identity(
        source_repository=source_repository,
        source_head=source_head,
        release_tag=release_tag,
        openmaic_head=openmaic_head,
    )
    images: dict[str, dict[str, Any]] = {}
    for name, spec in IMAGE_SPECS.items():
        tag = release_tag if name in CUSTOM_IMAGE_NAMES else str(spec["tag"])
        tagged_reference = _tagged_reference(spec, tag=tag)
        digest = (
            explicit_digests[name]
            if explicit_digests is not None and name in CUSTOM_IMAGE_NAMES
            else resolve_digest(tagged_reference)
        )
        if digest is None or not DIGEST_PATTERN.fullmatch(digest) or digest == ZERO_DIGEST:
            raise ValueError(f"registry digest is unavailable for image {name}")
        record: dict[str, Any] = {
            "repository": spec["repository"],
            "tag": tag,
            "digest": digest,
            "reference": f"{tagged_reference}@{digest}",
        }
        source = spec.get("source")
        if isinstance(source, dict):
            record["source"] = dict(source)
        images[name] = record

    candidate["imageDigests"] = {name: images[name]["digest"] for name in CUSTOM_IMAGE_NAMES}
    if len(set(candidate["imageDigests"].values())) != len(CUSTOM_IMAGE_NAMES):
        raise ValueError("candidate image digests must be distinct")
    signer_tagged_reference = f"{OBSERVER_SIGNER_SPEC['repository']}:{candidate['releaseTag']}"
    signer_digest = (
        explicit_digests[OBSERVER_SIGNER_NAME]
        if explicit_digests is not None
        else resolve_digest(signer_tagged_reference)
    )
    if (
        signer_digest is None
        or DIGEST_PATTERN.fullmatch(signer_digest) is None
        or signer_digest == ZERO_DIGEST
    ):
        raise ValueError(f"registry digest is unavailable for image {OBSERVER_SIGNER_NAME}")
    if signer_digest in candidate["imageDigests"].values():
        raise ValueError("observer signer digest collides with a workload image digest")
    support_images = {
        OBSERVER_SIGNER_NAME: {
            "repository": OBSERVER_SIGNER_SPEC["repository"],
            "tag": candidate["releaseTag"],
            "digest": signer_digest,
            "reference": f"{signer_tagged_reference}@{signer_digest}",
        }
    }
    document = {
        "schemaVersion": 3,
        "candidate": candidate,
        "images": images,
        "supportImages": support_images,
    }
    rendered_compose: dict[Path, str] = {}
    for compose_path in compose_paths:
        path = Path(compose_path)
        _assert_publish_topology_unchanged(topology)
        try:
            rendered_compose[path] = _render_compose_references(
                path,
                images,
                support_images,
            )
        finally:
            _assert_publish_topology_unchanged(topology)
    original_bytes: dict[Path, bytes | None] = {}
    for publish_path in publish_paths:
        _assert_publish_topology_unchanged(topology)
        try:
            original_bytes[publish_path] = publish_path.read_bytes()
        except FileNotFoundError:
            original_bytes[publish_path] = None
        finally:
            _assert_publish_topology_unchanged(topology)
    temporary_paths: dict[Path, _StagedPath] = {}
    journal_path = _publish_journal_path(output_path)
    journal_established = False
    publish_committed = False
    active_error: BaseException | None = None
    try:
        for compose_path, rendered in rendered_compose.items():
            _assert_publish_topology_unchanged(topology)
            try:
                temporary_paths[compose_path] = _stage_text(compose_path, rendered)
            finally:
                _assert_publish_topology_unchanged(topology)
        lock_text = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        _assert_publish_topology_unchanged(topology)
        try:
            temporary_paths[output_path] = _stage_text(output_path, lock_text)
        finally:
            _assert_publish_topology_unchanged(topology)
        _assert_publish_topology_unchanged(topology)
        _create_publish_journal(
            output_path,
            publish_paths,
            original_bytes,
            transaction_id=transaction_id,
        )
        journal_established = True
        for compose_path in rendered_compose:
            _replace_published_target(
                temporary_paths[compose_path],
                compose_path,
                topology,
            )
            _assert_publish_topology_unchanged(topology)
            temporary_paths.pop(compose_path)
        _replace_published_target(
            temporary_paths[output_path],
            output_path,
            topology,
        )
        _assert_publish_topology_unchanged(topology)
        temporary_paths.pop(output_path)
        for parent in {_path_key(binding.parent): binding.parent for binding in topology}.values():
            _assert_publish_topology_unchanged(topology)
            _sync_directory(parent)
        _assert_publish_topology_unchanged(topology)
        _mark_publish_journal_terminal(
            journal_path,
            publish_paths,
            transaction_id=transaction_id,
            status="committed",
        )
        publish_committed = True
        _retire_publish_journal(journal_path)
        journal_established = False
    except BaseException as publish_error:
        try:
            journal_state = _publish_journal_state_for_transaction(journal_path, transaction_id)
            if publish_committed or journal_state in {"committed", "rolled-back"}:
                raise
            if journal_established or journal_state == "in-progress":
                recovered = tuple((path, original_bytes[path]) for path in publish_paths)
                try:
                    _restore_publish_targets(
                        recovered,
                        topology=topology,
                    )
                except (OSError, RuntimeError, ValueError) as rollback_error:
                    cause = BaseExceptionGroup(
                        "image lock publish and rollback failed",
                        [publish_error, rollback_error],
                    )
                    raise RuntimeError(
                        "image lock publish failed and rollback was incomplete"
                    ) from cause
                if journal_state != "in-progress":
                    raise RuntimeError(
                        "image lock publish failed after journal integrity was lost"
                    ) from publish_error
                try:
                    _mark_publish_journal_terminal(
                        journal_path,
                        publish_paths,
                        transaction_id=transaction_id,
                        status="rolled-back",
                    )
                    _retire_publish_journal(journal_path)
                    journal_established = False
                except (OSError, RuntimeError, ValueError) as cleanup_error:
                    cause = BaseExceptionGroup(
                        "image lock publish and rollback journal cleanup failed",
                        [publish_error, cleanup_error],
                    )
                    raise RuntimeError(
                        "image lock publish rollback completed but journal cleanup was incomplete"
                    ) from cause
            raise
        except BaseException as final_error:
            active_error = final_error
            raise
    finally:
        staged_paths = tuple(temporary_paths.values())
        if active_error is None:
            _cleanup_staged_paths(staged_paths)
        else:
            _cleanup_staged_paths_preserving_error(staged_paths, active_error)
    return document


def _validate_image_lock_document(
    document: object,
    *,
    require_candidate: bool = True,
    expected_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("image lock is invalid")
    schema_version = document.get("schemaVersion")
    candidate: dict[str, Any] | None = None
    if schema_version == 3:
        if set(document) != {"schemaVersion", "candidate", "images", "supportImages"}:
            raise ValueError("image lock is invalid")
        raw_candidate = document.get("candidate")
        if not isinstance(raw_candidate, dict) or set(raw_candidate) != {
            "sourceRepository",
            "sourceHead",
            "releaseTag",
            "openmaicHead",
            "imageDigests",
        }:
            raise ValueError("image lock candidate is invalid")
        candidate = _candidate_identity(
            source_repository=raw_candidate.get("sourceRepository"),
            source_head=raw_candidate.get("sourceHead"),
            release_tag=raw_candidate.get("releaseTag"),
            openmaic_head=raw_candidate.get("openmaicHead"),
            image_digests=raw_candidate.get("imageDigests"),
        )
    elif schema_version != 1 or require_candidate or expected_candidate is not None:
        raise ValueError("image lock candidate is required")
    expected_image_names = tuple(
        name for name in IMAGE_SPECS if schema_version != 1 or name != "pocketbase"
    )
    images = document.get("images")
    if not isinstance(images, dict) or set(images) != set(expected_image_names):
        raise ValueError("image lock is invalid")
    for name in expected_image_names:
        spec = IMAGE_SPECS[name]
        record = images.get(name)
        if not isinstance(record, dict):
            raise ValueError(f"image lock is missing {name}")
        digest = record.get("digest")
        expected_tag = (
            str(candidate["releaseTag"])
            if candidate is not None and name in CUSTOM_IMAGE_NAMES
            else str(spec["tag"])
        )
        expected = (
            f"{spec['repository']}:{expected_tag}@{digest}" if isinstance(digest, str) else None
        )
        if (
            not isinstance(digest, str)
            or not DIGEST_PATTERN.fullmatch(digest)
            or digest == ZERO_DIGEST
            or record.get("repository") != spec["repository"]
            or record.get("tag") != expected_tag
            or record.get("reference") != expected
        ):
            raise ValueError(f"image lock entry is invalid for {name}")
    if candidate is not None:
        image_digests = candidate["imageDigests"]
        if any(images[name]["digest"] != image_digests[name] for name in CUSTOM_IMAGE_NAMES):
            raise ValueError("image lock candidate image digests do not match images")
        if len(set(image_digests.values())) != len(CUSTOM_IMAGE_NAMES):
            raise ValueError("image lock candidate image digests must be distinct")
        support_images = document.get("supportImages")
        if not isinstance(support_images, dict) or set(support_images) != {OBSERVER_SIGNER_NAME}:
            raise ValueError("image lock observer signer support image is invalid")
        signer = support_images.get(OBSERVER_SIGNER_NAME)
        if not isinstance(signer, dict):
            raise ValueError("image lock observer signer support image is invalid")
        signer_digest = signer.get("digest")
        expected_signer_reference = (
            f"{OBSERVER_SIGNER_SPEC['repository']}:{candidate['releaseTag']}@{signer_digest}"
            if isinstance(signer_digest, str)
            else None
        )
        if (
            set(signer) != {"repository", "tag", "digest", "reference"}
            or not isinstance(signer_digest, str)
            or DIGEST_PATTERN.fullmatch(signer_digest) is None
            or signer_digest == ZERO_DIGEST
            or signer_digest in image_digests.values()
            or signer.get("repository") != OBSERVER_SIGNER_SPEC["repository"]
            or signer.get("tag") != candidate["releaseTag"]
            or signer.get("reference") != expected_signer_reference
        ):
            raise ValueError("image lock observer signer support image is invalid")
        if expected_candidate is not None and any(
            candidate.get(name) != value for name, value in expected_candidate.items()
        ):
            raise ValueError("image lock candidate does not match expected candidate")
    return document


def load_image_lock_bytes(
    content: bytes,
    *,
    require_candidate: bool = True,
    expected_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an image lock already held by a trusted artifact lease."""
    if not isinstance(content, bytes):
        raise ValueError("image lock could not be read")
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("image lock could not be read") from exc
    return _validate_image_lock_document(
        document,
        require_candidate=require_candidate,
        expected_candidate=expected_candidate,
    )


def load_image_lock(
    lock_path: Any = IMAGE_LOCK_PATH,
    *,
    require_candidate: bool = True,
    expected_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(lock_path, (str, os.PathLike)):
        path = Path(lock_path)
        _reject_in_progress_publish_journal(path)
        snapshot = _read_fixed_file_bytes(
            path,
            error_message="image lock could not be read",
        )
        if snapshot is None:
            raise ValueError("image lock could not be read")
        return load_image_lock_bytes(
            snapshot[0],
            require_candidate=require_candidate,
            expected_candidate=expected_candidate,
        )
    read_bytes = getattr(lock_path, "read_bytes", None)
    try:
        if callable(read_bytes):
            return load_image_lock_bytes(
                read_bytes(),
                require_candidate=require_candidate,
                expected_candidate=expected_candidate,
            )
        read_text = getattr(lock_path, "read_text", None)
        if not callable(read_text):
            raise TypeError("image lock source is not readable")
        document = json.loads(read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("image lock could not be read") from exc
    return _validate_image_lock_document(
        document,
        require_candidate=require_candidate,
        expected_candidate=expected_candidate,
    )


def validate_image_lock_bindings(
    lock_path: Path = IMAGE_LOCK_PATH,
    *,
    compose_paths: Sequence[Path] = (),
    require_candidate: bool = True,
    expected_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lock = load_image_lock(
        lock_path,
        require_candidate=require_candidate,
        expected_candidate=expected_candidate,
    )
    compose_contents: list[tuple[Path, bytes]] = []
    for compose_path in compose_paths:
        path = Path(compose_path)
        snapshot = _read_fixed_file_bytes(
            path,
            error_message=f"production Compose file could not be read: {path.name}",
        )
        if snapshot is None:
            raise ValueError(f"production Compose file could not be read: {path.name}")
        compose_contents.append((path, snapshot[0]))
    _validate_image_lock_binding_bytes(lock, compose_contents)
    return lock


def _validate_image_lock_binding_bytes(
    lock: Mapping[str, Any],
    compose_contents: Sequence[tuple[Path, bytes]],
) -> None:
    images = lock["images"]
    support_images = lock["supportImages"]
    for path, content in compose_contents:
        template_path = (
            PROJECT_ROOT / "deploy" / path.name
            if path.name == "docker-compose.openmaic-shared-ingress-observer.yml"
            else PROJECT_ROOT / path.name
        )
        try:
            current = content.decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(f"production Compose file could not be read: {path.name}") from exc
        expected = _render_compose_references(template_path, images, support_images)
        if current != expected:
            raise ValueError("production Compose image references do not match image lock")


@contextmanager
def validated_candidate_artifact_lease(
    candidate_root: Path,
    *,
    expected_candidate: Mapping[str, Any] | None = None,
) -> Iterator[ValidatedCandidateArtifact]:
    """Keep the complete schema-v3 candidate bound for one operation."""
    paths = candidate_artifact_paths(candidate_root)
    protected_paths = (
        paths.image_lock,
        paths.base_compose,
        paths.platform_compose,
        paths.data_plane_compose,
        paths.observer_overlay,
    )
    body_failed = False
    try:
        _reject_in_progress_publish_journal(paths.image_lock)
        with _candidate_artifact_descriptor_lease(paths.root, protected_paths) as lease:
            contents = lease.contents
            _reject_in_progress_publish_journal(paths.image_lock)
            lock = load_image_lock_bytes(
                contents[_path_key(paths.image_lock)],
                require_candidate=True,
                expected_candidate=expected_candidate,
            )
            compose_bytes = tuple(
                (path, contents[_path_key(path)])
                for path in (
                    paths.base_compose,
                    paths.platform_compose,
                    paths.data_plane_compose,
                    paths.observer_overlay,
                )
            )
            _validate_image_lock_binding_bytes(lock, compose_bytes)
            candidate = lock.get("candidate")
            if not isinstance(candidate, Mapping):
                raise ValueError("image lock candidate is invalid")
            try:
                yield ValidatedCandidateArtifact(
                    paths=paths,
                    image_lock_bytes=contents[_path_key(paths.image_lock)],
                    image_lock=lock,
                    candidate=json.loads(json.dumps(candidate)),
                    compose_bytes=compose_bytes,
                    _check_unchanged=lease.check_unchanged,
                )
            except BaseException:
                body_failed = True
                raise
            _reject_in_progress_publish_journal(paths.image_lock)
    except ValueError as exc:
        if body_failed:
            raise
        raise CandidateArtifactError(str(exc)) from exc


def promotion_image_digests(lock: Mapping[str, Any]) -> dict[str, str]:
    """Return every image digest bound by promotion intent and receipt."""
    images = lock.get("images")
    support_images = lock.get("supportImages")
    if (
        not isinstance(images, Mapping)
        or set(images) != set(IMAGE_SPECS)
        or not isinstance(support_images, Mapping)
        or set(support_images) != {OBSERVER_SIGNER_NAME}
    ):
        raise ValueError("promotion image digests are invalid")
    digests: dict[str, str] = {}
    for name in IMAGE_SPECS:
        record = images.get(name)
        digest = record.get("digest") if isinstance(record, Mapping) else None
        if (
            not isinstance(digest, str)
            or DIGEST_PATTERN.fullmatch(digest) is None
            or digest == ZERO_DIGEST
        ):
            raise ValueError("promotion image digests are invalid")
        digests[name] = digest
    observer_signer = support_images.get(OBSERVER_SIGNER_NAME)
    observer_digest = (
        observer_signer.get("digest") if isinstance(observer_signer, Mapping) else None
    )
    if (
        not isinstance(observer_digest, str)
        or DIGEST_PATTERN.fullmatch(observer_digest) is None
        or observer_digest == ZERO_DIGEST
    ):
        raise ValueError("promotion image digests are invalid")
    digests[OBSERVER_SIGNER_NAME] = observer_digest
    return digests


def _parse_canonical_promotion_document(
    body: bytes,
    *,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if (
        not body
        or len(body) > MAX_PROMOTION_DOCUMENT_BYTES
        or not isinstance(document, dict)
        or (
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        != body
    ):
        raise ValueError(f"{label} is not canonical or changed while being read")
    return document, body


def _parse_pretty_canonical_promotion_document(
    body: bytes,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(body, bytes) or not body or len(body) > MAX_PROMOTION_DOCUMENT_BYTES:
        raise ValueError(f"{label} is invalid")
    try:
        document = json.loads(body)
        canonical = (
            json.dumps(
                document,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(document, dict) or canonical != body:
        raise ValueError(f"{label} is not canonical")
    return document


def _valid_promotion_observed_at(raw: object) -> bool:
    if not isinstance(raw, str) or PROMOTION_OBSERVED_AT_PATTERN.fullmatch(raw) is None:
        return False
    try:
        datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _validate_promotion_evidence_receipt(
    body: bytes,
    *,
    label: str,
    candidate: Mapping[str, Any],
    release_run: Mapping[str, str],
    evidence: str,
    producer: str,
    expected_checks: Mapping[str, bool],
) -> None:
    document = _parse_pretty_canonical_promotion_document(body, label=label)
    if (
        set(document) != {"schemaVersion", "candidate", "releaseRun", "evidence", "receipt"}
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 2
        or document.get("candidate") != candidate
        or document.get("releaseRun") != release_run
        or document.get("evidence") != evidence
    ):
        raise ValueError(f"{label} does not match the promotion candidate")
    receipt = document.get("receipt")
    if not isinstance(receipt, dict) or set(receipt) != {"producer", "observedAt", "result"}:
        raise ValueError(f"{label} is invalid")
    result = receipt.get("result")
    if not isinstance(result, dict) or set(result) != {"outcome", "nativeExit", "checks"}:
        raise ValueError(f"{label} is invalid")
    checks = result.get("checks")
    if (
        receipt.get("producer") != producer
        or not _valid_promotion_observed_at(receipt.get("observedAt"))
        or result.get("outcome") != "pass"
        or type(result.get("nativeExit")) is not int
        or result.get("nativeExit") != 0
        or not isinstance(checks, dict)
        or set(checks) != set(expected_checks)
        or any(checks.get(name) is not True for name in expected_checks)
    ):
        raise ValueError(f"{label} is invalid")


def _read_promotion_archive_members(body: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            entries = archive.infolist()
            names = tuple(entry.filename for entry in entries)
            if (
                len(entries) != len(PROMOTION_ARCHIVE_MEMBERS)
                or len(set(names)) != len(names)
                or set(names) != set(PROMOTION_ARCHIVE_MEMBERS)
            ):
                raise ValueError("promotion archive members are invalid")

            declared_total = 0
            for entry in entries:
                name = entry.filename
                member_path = PurePosixPath(name)
                mode = (entry.external_attr >> 16) & 0xFFFF
                if (
                    entry.orig_filename != name
                    or not name
                    or "\\" in name
                    or member_path.is_absolute()
                    or member_path.as_posix() != name
                    or any(part in {"", ".", ".."} for part in member_path.parts)
                    or entry.is_dir()
                    or entry.create_system != 3
                    or not stat.S_ISREG(mode)
                    or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or entry.flag_bits & 0x1
                    or entry.file_size < 0
                    or entry.file_size > PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
                    or entry.compress_size < 0
                    or (
                        entry.file_size > 0
                        and (
                            entry.compress_size == 0
                            or entry.file_size
                            > entry.compress_size * PROMOTION_ARCHIVE_MAX_COMPRESSION_RATIO
                        )
                    )
                ):
                    raise ValueError(f"promotion archive member is unsafe: {name}")
                declared_total += entry.file_size
                if declared_total > PROMOTION_ARCHIVE_MAX_TOTAL_BYTES:
                    raise ValueError("promotion archive content is too large")

            contents: dict[str, bytes] = {}
            actual_total = 0
            for entry in entries:
                chunks: list[bytes] = []
                member_total = 0
                with archive.open(entry, "r") as member:
                    while True:
                        chunk = member.read(64 * 1024)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise ValueError("promotion archive member is invalid")
                        member_total += len(chunk)
                        actual_total += len(chunk)
                        if (
                            member_total > PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
                            or actual_total > PROMOTION_ARCHIVE_MAX_TOTAL_BYTES
                        ):
                            raise ValueError("promotion archive content is too large")
                        chunks.append(chunk)
                if member_total != entry.file_size:
                    raise ValueError("promotion archive member size changed")
                contents[entry.filename] = b"".join(chunks)
            return contents
    except (EOFError, NotImplementedError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ValueError("promotion archive is invalid") from exc


def _read_canonical_promotion_document(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    snapshot = _read_fixed_file_bytes(
        path,
        error_message=f"{label} is invalid",
        allow_missing=True,
        max_bytes=MAX_PROMOTION_DOCUMENT_BYTES,
    )
    if snapshot is None:
        raise ValueError(f"{label} is unavailable")
    return _parse_canonical_promotion_document(snapshot[0], label=label)


def validate_promotion_receipt(
    candidate_root: Path,
    *,
    expected_github_run_id: str,
    expected_github_run_attempt: int,
) -> dict[str, Any]:
    """Verify the Actions promotion receipt against one fixed candidate artifact.

    The caller supplies the GitHub run identity from the trusted Actions API.
    This function intentionally does not claim to reverify remote registry state.
    """

    root = Path(candidate_root)
    if not root.is_absolute():
        raise ValueError("promotion candidate root must be absolute")
    root = Path(os.path.abspath(root))
    _assert_plain_publish_path(root)
    if not root.is_dir():
        raise ValueError("promotion candidate root is invalid")
    if (
        not isinstance(expected_github_run_id, str)
        or GITHUB_RUN_ID_PATTERN.fullmatch(expected_github_run_id) is None
        or type(expected_github_run_attempt) is not int
        or expected_github_run_attempt != 1
    ):
        raise ValueError("expected GitHub Actions run identity is invalid")

    paths = candidate_artifact_paths(root)
    intent_path = root / "artifacts" / "promotion_intent.json"
    receipt_path = root / "artifacts" / "promotion_receipt.json"
    protected_paths = (
        paths.image_lock,
        paths.base_compose,
        paths.platform_compose,
        paths.data_plane_compose,
        paths.observer_overlay,
        intent_path,
        receipt_path,
    )
    _reject_in_progress_publish_journal(paths.image_lock)
    with _candidate_artifact_descriptor_lease(
        root,
        protected_paths,
        max_bytes={
            _path_key(intent_path): MAX_PROMOTION_DOCUMENT_BYTES,
            _path_key(receipt_path): MAX_PROMOTION_DOCUMENT_BYTES,
        },
    ) as lease:
        contents = lease.contents
        lock = load_image_lock_bytes(
            contents[_path_key(paths.image_lock)],
            require_candidate=True,
        )
        _validate_image_lock_binding_bytes(
            lock,
            (
                (paths.base_compose, contents[_path_key(paths.base_compose)]),
                (paths.platform_compose, contents[_path_key(paths.platform_compose)]),
                (paths.data_plane_compose, contents[_path_key(paths.data_plane_compose)]),
                (paths.observer_overlay, contents[_path_key(paths.observer_overlay)]),
            ),
        )
        candidate = lock["candidate"]
        image_digests = promotion_image_digests(lock)
        expected_intent = {
            "schemaVersion": 1,
            "sourceRepository": candidate["sourceRepository"],
            "sourceHead": candidate["sourceHead"],
            "releaseTag": candidate["releaseTag"],
            "openmaicHead": candidate["openmaicHead"],
            "imageDigests": image_digests,
        }
        intent, intent_body = _parse_canonical_promotion_document(
            contents[_path_key(intent_path)],
            label="promotion intent",
        )
        if intent != expected_intent:
            raise ValueError("promotion intent does not match candidate artifact")

        expected_workflow_ref = (
            f"{candidate['sourceRepository']}/.github/workflows/private-platform-images.yml"
            f"@refs/tags/{candidate['releaseTag']}"
        )
        expected_receipt = {
            "schemaVersion": 2,
            "sourceRepository": candidate["sourceRepository"],
            "sourceHead": candidate["sourceHead"],
            "releaseTag": candidate["releaseTag"],
            "openmaicHead": candidate["openmaicHead"],
            "githubRunId": expected_github_run_id,
            "githubRunAttempt": expected_github_run_attempt,
            "githubWorkflowRef": expected_workflow_ref,
            "promotionIntentSha256": "sha256:" + hashlib.sha256(intent_body).hexdigest(),
            "imageDigests": image_digests,
        }
        receipt, _receipt_body = _parse_canonical_promotion_document(
            contents[_path_key(receipt_path)],
            label="promotion receipt",
        )
        if receipt != expected_receipt:
            raise ValueError(
                "promotion receipt does not match GitHub Actions run or candidate artifact"
            )
        _reject_in_progress_publish_journal(paths.image_lock)
        return receipt


def validate_downloaded_promotion_artifact(
    archive_path: Path,
    *,
    actions_artifact: Mapping[str, Any],
    actions_run: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one downloaded promotion artifact against trusted Actions API records."""
    if not isinstance(actions_run, Mapping):
        raise ValueError("GitHub Actions run metadata is invalid")
    run_id = actions_run.get("id")
    run_attempt = actions_run.get("run_attempt")
    repository = actions_run.get("repository")
    release_tag = actions_run.get("head_branch")
    source_head = actions_run.get("head_sha")
    if (
        type(run_id) is not int
        or run_id <= 0
        or type(run_attempt) is not int
        or run_attempt != 1
        or not isinstance(repository, Mapping)
        or repository.get("full_name") != SOURCE_REPOSITORY
        or actions_run.get("event") != "push"
        or actions_run.get("status") != "completed"
        or actions_run.get("conclusion") != "success"
    ):
        raise ValueError("GitHub Actions run metadata is invalid")
    trusted_candidate = _candidate_identity(
        source_repository=SOURCE_REPOSITORY,
        source_head=source_head,
        release_tag=release_tag,
        openmaic_head=OPENMAIC_HEAD,
    )
    if actions_run.get("path") != ".github/workflows/private-platform-images.yml":
        raise ValueError("GitHub Actions workflow path is invalid")

    if not isinstance(actions_artifact, Mapping):
        raise ValueError("GitHub Actions artifact metadata is invalid")
    artifact_id = actions_artifact.get("id")
    artifact_size = actions_artifact.get("size_in_bytes")
    artifact_workflow_run = actions_artifact.get("workflow_run")
    expected_artifact_name = (
        f"private-platform-image-lock-{trusted_candidate['releaseTag']}-"
        f"{trusted_candidate['sourceHead']}"
    )
    if (
        type(artifact_id) is not int
        or artifact_id <= 0
        or type(artifact_size) is not int
        or artifact_size <= 0
        or actions_artifact.get("name") != expected_artifact_name
        or actions_artifact.get("expired") is not False
        or not isinstance(artifact_workflow_run, Mapping)
        or type(artifact_workflow_run.get("id")) is not int
        or artifact_workflow_run.get("id") != run_id
        or artifact_workflow_run.get("head_branch") != trusted_candidate["releaseTag"]
        or artifact_workflow_run.get("head_sha") != trusted_candidate["sourceHead"]
    ):
        raise ValueError("GitHub Actions artifact metadata is invalid")

    snapshot = _read_fixed_file_bytes(
        Path(archive_path),
        error_message="promotion archive changed or could not be read",
        max_bytes=PROMOTION_ARCHIVE_MAX_BYTES,
    )
    if snapshot is None:
        raise ValueError("promotion archive changed or could not be read")
    archive_body = snapshot[0]
    expected_archive_digest = "sha256:" + hashlib.sha256(archive_body).hexdigest()
    if (
        artifact_size != len(archive_body)
        or actions_artifact.get("digest") != expected_archive_digest
    ):
        raise ValueError("promotion archive does not match GitHub Actions artifact metadata")

    contents = _read_promotion_archive_members(archive_body)
    lock = load_image_lock_bytes(
        contents["deploy/image-lock.json"],
        require_candidate=True,
        expected_candidate=trusted_candidate,
    )
    source_templates = _read_source_compose_templates(trusted_candidate["sourceHead"])
    if (
        not isinstance(source_templates, Mapping)
        or set(source_templates) != set(PROMOTION_COMPOSE_MEMBERS)
        or any(
            not isinstance(source_templates.get(name), bytes)
            or not source_templates[name]
            or len(source_templates[name]) > PROMOTION_ARCHIVE_MAX_MEMBER_BYTES
            for name in PROMOTION_COMPOSE_MEMBERS
        )
        or sum(len(source_templates[name]) for name in PROMOTION_COMPOSE_MEMBERS)
        > PROMOTION_ARCHIVE_MAX_TOTAL_BYTES
    ):
        raise ValueError("promotion source Compose templates are invalid")
    images = lock["images"]
    support_images = lock["supportImages"]
    for name in PROMOTION_COMPOSE_MEMBERS:
        try:
            current = contents[name].decode("utf-8")
            template = source_templates[name].decode("utf-8")
        except UnicodeError as exc:
            raise ValueError(
                f"production Compose file could not be read: {Path(name).name}"
            ) from exc
        expected = _render_compose_references_text(
            Path(name).name,
            template,
            images,
            support_images,
        )
        if current != expected:
            raise ValueError("production Compose image references do not match image lock")
    candidate = lock.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("promotion image lock candidate is invalid")
    image_digests = promotion_image_digests(lock)

    release_run = {
        "runId": f"github-{run_id}-{run_attempt}",
        "environmentId": "ghcr-production",
    }
    _validate_promotion_evidence_receipt(
        contents["artifacts/source_head.json"],
        label="source head receipt",
        candidate=candidate,
        release_run=release_run,
        evidence="source_head",
        producer="git-probe",
        expected_checks={"headMatches": True, "worktreeClean": True},
    )
    _validate_promotion_evidence_receipt(
        contents["artifacts/image_digests.json"],
        label="image digests receipt",
        candidate=candidate,
        release_run=release_run,
        evidence="image_digests",
        producer="image-lock",
        expected_checks={"lockMatches": True, "composeMatches": True},
    )

    expected_intent = {
        "schemaVersion": 1,
        "sourceRepository": candidate["sourceRepository"],
        "sourceHead": candidate["sourceHead"],
        "releaseTag": candidate["releaseTag"],
        "openmaicHead": candidate["openmaicHead"],
        "imageDigests": image_digests,
    }
    intent, intent_body = _parse_canonical_promotion_document(
        contents["artifacts/promotion_intent.json"],
        label="promotion intent",
    )
    if intent != expected_intent:
        raise ValueError("promotion intent does not match candidate artifact")

    expected_receipt = {
        "schemaVersion": 2,
        "sourceRepository": candidate["sourceRepository"],
        "sourceHead": candidate["sourceHead"],
        "releaseTag": candidate["releaseTag"],
        "openmaicHead": candidate["openmaicHead"],
        "githubRunId": str(run_id),
        "githubRunAttempt": run_attempt,
        "githubWorkflowRef": (
            f"{candidate['sourceRepository']}/.github/workflows/private-platform-images.yml"
            f"@refs/tags/{candidate['releaseTag']}"
        ),
        "promotionIntentSha256": "sha256:" + hashlib.sha256(intent_body).hexdigest(),
        "imageDigests": image_digests,
    }
    receipt, _receipt_body = _parse_canonical_promotion_document(
        contents["artifacts/promotion_receipt.json"],
        label="promotion receipt",
    )
    if receipt != expected_receipt:
        raise ValueError(
            "promotion receipt does not match GitHub Actions run or candidate artifact"
        )
    return receipt


def image_reference(name: str, *, lock_path: Path = IMAGE_LOCK_PATH) -> str:
    if name not in IMAGE_SPECS:
        raise ValueError(f"unknown image name: {name}")
    lock = load_image_lock(lock_path, require_candidate=False)
    return str(lock["images"][name]["reference"])


def load_rendered_compose(
    *compose_files: str | Path,
    project_root: Path = PROJECT_ROOT,
    environment: Mapping[str, str] | None = None,
    profiles: Sequence[str] = (),
) -> dict[str, Any]:
    """Return Docker Compose's authoritative merged model as JSON."""
    if not compose_files:
        raise ValueError("at least one Compose file is required")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker was not found on PATH")
    command = [
        docker,
        "compose",
        "--project-name",
        "yfeistai-config-check",
        "--project-directory",
        str(project_root),
    ]
    for compose_file in compose_files:
        path = Path(compose_file)
        if not path.is_absolute():
            path = project_root / path
        command.extend(("-f", str(path)))
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(("config", "--format", "json"))

    process_environment = os.environ.copy()
    process_environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    if environment:
        process_environment.update(environment)
    result = subprocess.run(
        command,
        cwd=str(project_root),
        env=process_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = result.stderr.strip().splitlines()
        detail = diagnostic[-1] if diagnostic else "unknown Compose error"
        raise RuntimeError(f"docker compose config failed: {detail}")
    try:
        rendered = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("docker compose config did not return JSON") from exc
    if not isinstance(rendered, dict):
        raise RuntimeError("docker compose config returned an invalid model")
    return rendered


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write-image-lock", action="store_true")
    action.add_argument("--verify-promotion-receipt", action="store_true")
    action.add_argument("--verify-downloaded-promotion-artifact", type=Path)
    action.add_argument("--print-image", choices=tuple(IMAGE_SPECS))
    parser.add_argument("--lock-path", type=Path, default=IMAGE_LOCK_PATH)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--expected-github-run-id")
    parser.add_argument("--expected-github-run-attempt", type=int)
    parser.add_argument("--expected-github-artifact-id")
    parser.add_argument("--actions-run-metadata", type=Path)
    parser.add_argument("--actions-artifact-metadata", type=Path)
    parser.add_argument("--compose-path", type=Path, action="append", dest="compose_paths")
    parser.add_argument("--source-repository")
    parser.add_argument("--source-head")
    parser.add_argument("--release-tag")
    parser.add_argument("--openmaic-head", default=OPENMAIC_HEAD)
    parser.add_argument("--digest", action="append", dest="digests")
    return parser.parse_args(argv)


def _parse_digest_overrides(values: Sequence[str] | None) -> dict[str, str] | None:
    if not values:
        return None
    digests: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        if (
            not separator
            or name not in CANDIDATE_IMAGE_NAMES
            or name in digests
            or DIGEST_PATTERN.fullmatch(digest) is None
            or digest == ZERO_DIGEST
        ):
            raise ValueError("candidate image digest argument is invalid")
        digests[name] = digest
    if set(digests) != set(CANDIDATE_IMAGE_NAMES):
        raise ValueError("all candidate image digests are required")
    if len(set(digests.values())) != len(digests):
        raise ValueError("candidate image digests must be distinct")
    return digests


def _read_actions_metadata(path: Path, *, label: str) -> dict[str, Any]:
    error_message = f"GitHub Actions {label} metadata is invalid or could not be read"
    snapshot = _read_fixed_file_bytes(
        path,
        error_message=error_message,
        max_bytes=MAX_ACTIONS_METADATA_BYTES,
    )
    if snapshot is None:
        raise ValueError(error_message)
    try:
        document = json.loads(snapshot[0])
    except (ValueError, RecursionError) as exc:
        raise ValueError(error_message) from exc
    if not isinstance(document, dict):
        raise ValueError(error_message)
    return document


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verify_downloaded_promotion_artifact is not None:
        if any(
            value is None
            for value in (
                args.actions_run_metadata,
                args.actions_artifact_metadata,
                args.source_head,
                args.release_tag,
                args.expected_github_run_id,
                args.expected_github_run_attempt,
                args.expected_github_artifact_id,
            )
        ):
            raise ValueError(
                "--actions-run-metadata, --actions-artifact-metadata, --source-head, "
                "--release-tag, --expected-github-run-id, --expected-github-run-attempt, "
                "and --expected-github-artifact-id are required"
            )
        _candidate_identity(
            source_repository=SOURCE_REPOSITORY,
            source_head=args.source_head,
            release_tag=args.release_tag,
            openmaic_head=OPENMAIC_HEAD,
        )
        if (
            GITHUB_RUN_ID_PATTERN.fullmatch(args.expected_github_run_id) is None
            or GITHUB_RUN_ID_PATTERN.fullmatch(args.expected_github_artifact_id) is None
            or args.expected_github_run_attempt != 1
        ):
            raise ValueError("expected GitHub Actions run or artifact identity is invalid")
        actions_run = _read_actions_metadata(args.actions_run_metadata, label="run")
        actions_artifact = _read_actions_metadata(args.actions_artifact_metadata, label="artifact")
        if (
            actions_run.get("head_sha") != args.source_head
            or actions_run.get("head_branch") != args.release_tag
            or str(actions_run.get("id")) != args.expected_github_run_id
            or type(actions_run.get("run_attempt")) is not int
            or actions_run.get("run_attempt") != args.expected_github_run_attempt
            or str(actions_artifact.get("id")) != args.expected_github_artifact_id
        ):
            raise ValueError("GitHub Actions metadata does not match independent selectors")
        receipt = validate_downloaded_promotion_artifact(
            args.verify_downloaded_promotion_artifact,
            actions_artifact=actions_artifact,
            actions_run=actions_run,
        )
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    if args.verify_promotion_receipt:
        if (
            args.candidate_root is None
            or args.expected_github_run_id is None
            or args.expected_github_run_attempt is None
        ):
            raise ValueError(
                "--candidate-root, --expected-github-run-id, and "
                "--expected-github-run-attempt are required"
            )
        validate_promotion_receipt(
            args.candidate_root,
            expected_github_run_id=args.expected_github_run_id,
            expected_github_run_attempt=args.expected_github_run_attempt,
        )
        print(args.candidate_root / "artifacts" / "promotion_receipt.json")
        return 0
    if args.write_image_lock:
        if not args.source_repository or not args.source_head or not args.release_tag:
            raise ValueError("--source-repository, --source-head, and --release-tag are required")
        compose_paths = (
            tuple(args.compose_paths)
            if args.compose_paths
            else (
                PROJECT_ROOT / "docker-compose.yml",
                PROJECT_ROOT / "docker-compose.platform.yml",
                PROJECT_ROOT / "docker-compose.data-plane.yml",
                PROJECT_ROOT / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml",
            )
        )
        if len(compose_paths) != 4 or {path.name for path in compose_paths} != {
            "docker-compose.yml",
            "docker-compose.platform.yml",
            "docker-compose.data-plane.yml",
            "docker-compose.openmaic-shared-ingress-observer.yml",
        }:
            raise ValueError("all four production Compose paths are required")
        write_arguments: dict[str, Any] = {
            "compose_paths": compose_paths,
            "source_repository": args.source_repository,
            "source_head": args.source_head,
            "release_tag": args.release_tag,
            "openmaic_head": args.openmaic_head,
        }
        digest_overrides = _parse_digest_overrides(args.digests)
        if digest_overrides is not None:
            write_arguments["digest_overrides"] = digest_overrides
        write_image_lock(args.lock_path, **write_arguments)
        print(args.lock_path)
        return 0
    print(image_reference(args.print_image, lock_path=args.lock_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
