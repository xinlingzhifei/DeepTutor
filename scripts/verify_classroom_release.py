"""Verify that one classroom release candidate has every required evidence layer."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import BinaryIO, Protocol
from urllib.parse import urlsplit

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from capacity_profile_contract import (  # noqa: E402
    CAPACITY_PRODUCER,
    MAX_CAPACITY_REPORT_BYTES,
    capacity_profile_command_record,
    derive_capacity_profile_summary,
    derive_learning_event_idempotency_checks,
    exact_json_equal,
    parse_capacity_profile_report,
)
from classroom_export_contract import (  # noqa: E402
    CLASSROOM_EXPORT_PATHS,
    CLASSROOM_EXPORT_PRODUCER,
    MAX_CLASSROOM_EXPORT_REPORT_BYTES,
    MAX_EXPORT_BYTES,
    MAX_TOTAL_EXPORT_BYTES,
    classroom_export_archive_contains_forbidden_bytes,
    classroom_exports_command_record,
    derive_classroom_export_checks,
    parse_classroom_export_report,
)
from classroom_release_probe_contract import (  # noqa: E402
    LIVE_PROJECT,
    LIVE_SPEC,
    PROBE_RECIPES,
    probe_command_record,
)
from classroom_runtime_attestation import (  # noqa: E402
    _CandidateContractLease,
    _close_windows_handle,
    _file_identity,
    _load_candidate_token,
    _open_windows_directory_handle,
    _open_windows_directory_relative,
    _open_windows_regular_file_relative,
    _read_windows_file_handle,
)
from openmaic_smoke_contract import (  # noqa: E402
    MAX_OPENMAIC_SMOKE_REPORT_BYTES,
    derive_openmaic_shared_plane_checks,
    openmaic_shared_plane_command_record,
    parse_openmaic_smoke_report,
)
from platform_preflight_contract import (
    PHASE_SERVICES as PREFLIGHT_PHASE_SERVICES,
)
from platform_preflight_contract import (  # noqa: E402
    PHASES as PREFLIGHT_PHASES,
)
from platform_preflight_contract import (
    candidate_network_phase_command,
    parse_candidate_network_report,
)
from tenant_isolation_contract import (  # noqa: E402
    MAX_TENANT_ISOLATION_REPORT_BYTES,
    TENANT_ISOLATION_PRODUCER,
    derive_tenant_isolation_checks,
    parse_tenant_isolation_report,
    tenant_isolation_command_record,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_PATH = (
    PROJECT_ROOT / "data" / "user" / "release-evidence" / "classroom-first-release.json"
)
MAX_RUNTIME_ARTIFACT_BYTES = 1024 * 1024

REQUIRED_OPERATIONAL_LAYERS = (
    "source_head",
    "image_digests",
    "database_revisions",
    "running_containers",
    "service_health",
    "capacity_profile",
)

REQUIRED_ACCEPTANCE_EVIDENCE = (
    "teacher_flow",
    "student_micro_flow",
    "student_full_flow",
    "content_operations_flow",
    "classroom_exports",
    "tenant_isolation",
    "learning_event_idempotency",
    "openmaic_shared_plane",
    "openmaic_dedicated_plane",
    "tailwind4_visual_matrix",
    "backup_restore",
    "gateway_only_public",
)

REQUIRED_LAYERS = REQUIRED_OPERATIONAL_LAYERS + REQUIRED_ACCEPTANCE_EVIDENCE

EVIDENCE_SCHEMA_VERSION = 3
ARTIFACT_SCHEMA_VERSION = 2
SOURCE_REPOSITORY = "xinlingzhifei/DeepTutor"
OPENMAIC_HEAD = "0cf2a330411681190e89f48e20f305345ff99f87"
CUSTOM_IMAGE_SPECS = {
    "deeptutor": ("ghcr.io/xinlingzhifei/deeptutor", "first-release"),
    "openmaic": ("ghcr.io/xinlingzhifei/openmaic", "0.3.1-0cf2a330"),
    "openmaic_render": (
        "ghcr.io/xinlingzhifei/openmaic-render",
        "0.3.1-0cf2a330",
    ),
}
CUSTOM_IMAGE_NAMES = tuple(CUSTOM_IMAGE_SPECS)
CUSTOM_IMAGE_SERVICE_BINDINGS = {
    "docker-compose.platform.yml": {
        "deeptutor": "deeptutor",
        "teaching-migrate": "deeptutor",
        "tenant-provisioner": "deeptutor",
        "shared-data-plane-bootstrap": "deeptutor",
        "teaching-dispatcher": "deeptutor",
        "teaching-worker": "deeptutor",
        "teaching-export-worker": "deeptutor",
        "teaching-reaper": "deeptutor",
        "learning-projector": "deeptutor",
        "openmaic": "openmaic",
        "openmaic-render": "openmaic_render",
    },
    "docker-compose.data-plane.yml": {
        "openmaic": "openmaic",
        "openmaic-render": "openmaic_render",
    },
}
RECEIPT_CONTRACTS = {
    "source_head": ("git-probe", ("headMatches", "worktreeClean")),
    "image_digests": ("image-lock", ("lockMatches", "composeMatches")),
    "database_revisions": ("platform-preflight", ("revisionsMatch",)),
    "running_containers": ("docker-compose", ("stableContainerSet",)),
    "service_health": ("platform-preflight", ("allServicesHealthy",)),
    "capacity_profile": (
        CAPACITY_PRODUCER,
        (
            "thresholdsPassed",
            "rawSamplesRecorded",
            "resourceObservationsRecorded",
            "resourceAccountingComplete",
            "resourceBoundaryStable",
        ),
    ),
    "teacher_flow": ("playwright", ("teacherFlowPassed",)),
    "student_micro_flow": ("playwright", ("studentMicroFlowPassed",)),
    "student_full_flow": ("playwright", ("studentFullFlowPassed",)),
    "content_operations_flow": ("playwright", ("contentOperationsFlowPassed",)),
    "classroom_exports": (
        CLASSROOM_EXPORT_PRODUCER,
        ("zipOpened", "pptxOpened", "offlineHtmlOpened", "mp4Opened"),
    ),
    "tenant_isolation": (
        TENANT_ISOLATION_PRODUCER,
        ("databaseIsolated", "objectsIsolated", "exportsIsolated", "eventsIsolated"),
    ),
    "learning_event_idempotency": (
        CAPACITY_PRODUCER,
        ("duplicateCountedOnce", "ticketReplayRejected", "projectionVisible"),
    ),
    "openmaic_shared_plane": ("openmaic-smoke", ("sharedGenerationPassed",)),
    "openmaic_dedicated_plane": (
        "openmaic-smoke",
        ("dedicatedGenerationPassed", "noSharedFallback"),
    ),
    "tailwind4_visual_matrix": ("playwright", ("visualMatrixPassed",)),
    "backup_restore": (
        "restore-teaching",
        ("newDatabaseRestored", "distinctVersionedBucketRestored", "receiptsVerified"),
    ),
    "gateway_only_public": ("gateway-probe", ("gatewayPublic", "internalPortsClosed")),
}

PROBE_TITLE_PATTERNS = {
    evidence: re.compile(rf"^\[release-evidence:{re.escape(evidence)}\] .+$")
    for evidence in PROBE_RECIPES
}
_TAILWIND_MATRIX_TITLES = frozenset(
    "[release-evidence:tailwind4_visual_matrix] "
    f"route={route} viewport={viewport} appearance={appearance}"
    for route in (
        "/login",
        "/home",
        "/knowledge",
        "/settings/appearance",
        "/settings/llm",
        "/space/learning",
    )
    for viewport in ("desktop", "mobile")
    for appearance in ("snow", "light", "dark", "glass")
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_RELEASE_TAG = re.compile(r"^yfeistai-first-release-[0-9]{8}-([0-9a-f]{8})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_RUNTIME_PROJECT = "yfeistai-platform"
_RUNTIME_HEALTH_SERVICES = frozenset(
    ("deeptutor", "postgres", "minio", "openmaic", "openmaic-render")
)
_RUNTIME_DOCKER_PREFIX = (
    "docker",
    "--config",
    "<isolated-docker-config>",
    "--context",
    "default",
)
_RUNTIME_CONTAINER_FORMAT = (
    '{"containerId":{{json .Id}},"localImageId":{{json .Image}},'
    '"configImage":{{json .Config.Image}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"state":{{json .State.Status}},"running":{{json .State.Running}},'
    '"restarting":{{json .State.Restarting}},"exitCode":{{json .State.ExitCode}},'
    '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}}}'
)
_RUNTIME_IMAGE_FORMAT = '{"imageId":{{json .Id}},"repoDigests":{{json .RepoDigests}}}'
_RUNTIME_PS_ARGUMENTS = (
    "ps",
    "-a",
    "--no-trunc",
    "--filter",
    f"label=com.docker.compose.project={_RUNTIME_PROJECT}",
    "--format",
    "{{json .ID}}",
)


def _valid_observed_at_value(raw: object) -> bool:
    if not isinstance(raw, str) or _OBSERVED_AT.fullmatch(raw) is None:
        return False
    try:
        datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def derive_probe_checks(
    evidence: str,
    *,
    raw_report: bytes,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> dict[str, bool]:
    """Derive checks by recounting a native Playwright JSON reporter document."""
    del candidate, release_run  # Bound independently by the hashed execution record.
    recipe = PROBE_RECIPES.get(evidence)
    if recipe is None:
        raise ValueError("probe recipe is not implemented for this evidence layer")
    _recipe_id, expected_count = recipe
    try:
        document = json.loads(raw_report)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("probe raw report is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "config",
        "suites",
        "errors",
        "stats",
    }:
        raise ValueError("probe raw report is not native Playwright JSON")
    config = document.get("config")
    projects = config.get("projects") if isinstance(config, dict) else None
    if not isinstance(projects, list) or len(projects) != 1:
        raise ValueError("probe Playwright config does not match the fixed project")
    project = projects[0]
    retries = project.get("retries") if isinstance(project, dict) else None
    if (
        not isinstance(project, dict)
        or project.get("id") != LIVE_PROJECT
        or project.get("name") != LIVE_PROJECT
        or not isinstance(retries, int)
        or isinstance(retries, bool)
        or retries != 0
    ):
        raise ValueError("probe Playwright config does not match the fixed project")
    errors = document.get("errors")
    if errors != []:
        raise ValueError("probe Playwright report contains global errors")
    stats = document.get("stats")
    if not isinstance(stats, dict) or set(stats) != {
        "startTime",
        "duration",
        "expected",
        "unexpected",
        "flaky",
        "skipped",
    }:
        raise ValueError("probe Playwright stats are invalid")
    if not _valid_observed_at_value(stats.get("startTime")):
        raise ValueError("probe Playwright stats are invalid")
    duration = stats.get("duration")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ValueError("probe Playwright stats are invalid")
    outcome_names = ("expected", "unexpected", "flaky", "skipped")
    if any(
        not isinstance(stats.get(name), int) or isinstance(stats.get(name), bool) or stats[name] < 0
        for name in outcome_names
    ):
        raise ValueError("probe Playwright stats are invalid")

    suites = document.get("suites")
    if not isinstance(suites, list):
        raise ValueError("probe Playwright suites are invalid")
    specs: list[dict[str, object]] = []
    pending: list[object] = list(suites)
    while pending:
        suite = pending.pop()
        if not isinstance(suite, dict) or not isinstance(suite.get("specs"), list):
            raise ValueError("probe Playwright suites are invalid")
        nested = suite.get("suites", [])
        if not isinstance(nested, list):
            raise ValueError("probe Playwright suites are invalid")
        pending.extend(nested)
        for spec in suite["specs"]:
            if not isinstance(spec, dict):
                raise ValueError("probe Playwright suites are invalid")
            specs.append(spec)
    if len(specs) != expected_count:
        raise ValueError("probe selected test count does not match the fixed recipe")

    recounted = {name: 0 for name in outcome_names}
    observed_titles: list[str] = []
    title_pattern = PROBE_TITLE_PATTERNS[evidence]
    for spec in specs:
        title = spec.get("title")
        tests = spec.get("tests")
        if (
            spec.get("file") != LIVE_SPEC
            or not isinstance(title, str)
            or title_pattern.fullmatch(title) is None
            or spec.get("ok") is not True
            or not isinstance(tests, list)
            or len(tests) != 1
        ):
            raise ValueError("probe selected spec does not match the fixed recipe")
        observed_titles.append(title)
        test = tests[0]
        if not isinstance(test, dict):
            raise ValueError("probe selected test is invalid")
        outcome = test.get("status")
        if outcome not in recounted:
            raise ValueError("probe selected test outcome is invalid")
        recounted[outcome] += 1
        results = test.get("results")
        if (
            test.get("projectId") != LIVE_PROJECT
            or test.get("projectName") != LIVE_PROJECT
            or test.get("expectedStatus") != "passed"
            or outcome != "expected"
            or not isinstance(results, list)
            or len(results) != 1
        ):
            raise ValueError("probe selected test does not prove a clean pass")
        result = results[0]
        result_duration = result.get("duration") if isinstance(result, dict) else None
        retry = result.get("retry") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or result.get("status") != "passed"
            or not isinstance(result_duration, (int, float))
            or isinstance(result_duration, bool)
            or not math.isfinite(result_duration)
            or result_duration < 0
            or not isinstance(retry, int)
            or isinstance(retry, bool)
            or retry != 0
            or result.get("errors") != []
            or result.get("error") not in (None, {})
        ):
            raise ValueError("probe selected test does not prove a clean pass")
    if evidence == "tailwind4_visual_matrix" and frozenset(observed_titles) != (
        _TAILWIND_MATRIX_TITLES
    ):
        raise ValueError("probe selected spec does not match the fixed recipe")
    if any(stats[name] != recounted[name] for name in outcome_names):
        raise ValueError("probe Playwright stats do not match selected test results")
    if recounted != {
        "expected": expected_count,
        "unexpected": 0,
        "flaky": 0,
        "skipped": 0,
    }:
        raise ValueError("probe selected tests do not prove passing evidence")
    required_checks = RECEIPT_CONTRACTS[evidence][1]
    if len(required_checks) != 1:
        raise ValueError("probe recipe does not match the evidence contract")
    return {required_checks[0]: True}


def _proof_bytes(
    bundle_root: Path,
    raw: object,
    *,
    label: str,
) -> tuple[bytes, str] | str:
    if not isinstance(raw, dict) or set(raw) != {"artifact", "sha256"}:
        return f"{label} reference is invalid"
    artifact = raw.get("artifact")
    expected_sha256 = raw.get("sha256")
    if (
        not isinstance(artifact, str)
        or not artifact.strip()
        or Path(artifact).is_absolute()
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        return f"{label} reference is invalid"
    fixed_runtime_artifacts = {
        "runtime/platform-preflight-attestation.json": "platform-preflight-attestation.json",
        "runtime/capacity-profile-attestation.json": "capacity-profile-attestation.json",
        "runtime/classroom-exports-attestation.json": "classroom-exports-attestation.json",
        "runtime/tenant-isolation-attestation.json": "tenant-isolation-attestation.json",
        "runtime/openmaic-shared-plane-attestation.json": (
            "openmaic-shared-plane-attestation.json"
        ),
    }
    fixed_name = fixed_runtime_artifacts.get(artifact)
    if fixed_name is not None:
        try:
            body = _runtime_artifact_body(
                Path(artifact),
                bundle_root=bundle_root,
                expected_sha256=expected_sha256,
                artifact_name=fixed_name,
            )
        except ValueError:
            return f"{label} cannot be read from its fixed boundary"
        return body, artifact
    root = Path(bundle_root).resolve()
    unresolved = root / artifact
    try:
        resolved = unresolved.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return f"{label} is outside the evidence bundle"
    cursor = unresolved
    while cursor != root:
        if cursor.is_symlink():
            return f"{label} must not use a symlink"
        cursor = cursor.parent
    try:
        body = resolved.read_bytes()
    except OSError:
        return f"{label} does not exist"
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        return f"{label} digest does not match"
    return body, artifact


def _runtime_base_url(raw: object) -> str | None:
    if not isinstance(raw, str) or raw != raw.rstrip("/"):
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return raw


def _runtime_candidate_contract(
    candidate_root: Path,
    *,
    candidate: Mapping[str, object],
) -> dict[str, dict[str, str]]:
    root = Path(os.path.abspath(candidate_root))
    try:
        with _CandidateContractLease.open(root) as lease:
            loaded_candidate, expected = _load_candidate_token(
                lease.token,
                expected_candidate=candidate,
            )
            lease.assert_unchanged()
    except (OSError, ValueError) as exc:
        raise ValueError("candidate runtime contract is unavailable or invalid") from exc
    if loaded_candidate != candidate:
        raise ValueError("candidate runtime contract is unavailable or invalid")
    return expected


def _normalized_runtime_repo_digest(reference: str) -> str:
    tagged, separator, digest = reference.rpartition("@")
    if not separator:
        raise ValueError("candidate runtime image reference is invalid")
    last_slash = tagged.rfind("/")
    tag_separator = tagged.rfind(":")
    repository = tagged[:tag_separator] if tag_separator > last_slash else tagged
    return f"{repository}@{digest}"


def _read_runtime_artifact_handle(handle: object | int, *, windows: bool) -> bytes:
    if windows:
        import ctypes
        from ctypes import wintypes

        size = ctypes.c_longlong()
        get_size = ctypes.WinDLL("kernel32", use_last_error=True).GetFileSizeEx
        get_size.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong))
        get_size.restype = wintypes.BOOL
        if not get_size(handle, ctypes.byref(size)):
            error = ctypes.get_last_error()
            raise OSError(error, "cannot inspect runtime artifact size")
        if size.value < 0 or size.value > MAX_RUNTIME_ARTIFACT_BYTES:
            raise ValueError("runtime artifact is too large")
        return _read_windows_file_handle(handle)
    file_descriptor = int(handle)
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        requested = min(64 * 1024, MAX_RUNTIME_ARTIFACT_BYTES - total + 1)
        chunk = os.read(file_descriptor, requested)
        if not chunk:
            return b"".join(chunks)
        if total + len(chunk) > MAX_RUNTIME_ARTIFACT_BYTES:
            raise ValueError("runtime artifact is too large")
        chunks.append(chunk)
        total += len(chunk)


def _open_posix_directory_no_follow(path: Path) -> int:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.anchor:
        raise ValueError("runtime evidence bundle path is invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open(candidate.anchor, flags)
    try:
        for component in candidate.relative_to(candidate.anchor).parts:
            opened = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = opened
        return current
    except BaseException:
        os.close(current)
        raise


def _open_windows_directory_no_follow(path: Path) -> tuple[object, tuple[int, int]]:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.anchor:
        raise ValueError("runtime evidence bundle path is invalid")
    current, identity = _open_windows_directory_handle(Path(candidate.anchor))
    try:
        for component in candidate.relative_to(candidate.anchor).parts:
            opened, opened_identity = _open_windows_directory_relative(current, component)
            _close_windows_handle(current)
            current = opened
            identity = opened_identity
        return current, identity
    except BaseException:
        _close_windows_handle(current)
        raise


def _runtime_artifact_body(
    path: Path,
    *,
    bundle_root: Path,
    expected_sha256: str | None,
    artifact_name: str = "runtime-attestation.json",
) -> bytes:
    if artifact_name not in {
        "runtime-attestation.json",
        "platform-preflight-attestation.json",
        "capacity-profile-attestation.json",
        "classroom-exports-attestation.json",
        "tenant-isolation-attestation.json",
        "openmaic-shared-plane-attestation.json",
    }:
        raise ValueError("runtime artifact name is invalid")
    root = Path(os.path.abspath(bundle_root))
    unresolved = Path(path)
    if not unresolved.is_absolute():
        unresolved = root / unresolved
    unresolved = Path(os.path.abspath(unresolved))
    expected_path = root / "runtime" / artifact_name
    if unresolved != expected_path:
        raise ValueError("runtime attestation is outside the fixed evidence bundle path")
    directory_handle: object | int | None = None
    runtime_handle: object | int | None = None
    artifact_handle: object | int | None = None
    windows = os.name == "nt"
    try:
        if windows:
            directory_handle, bundle_identity = _open_windows_directory_no_follow(root)
            runtime_handle, runtime_identity = _open_windows_directory_relative(
                directory_handle,
                "runtime",
            )
            artifact_handle, artifact_identity = _open_windows_regular_file_relative(
                runtime_handle,
                artifact_name,
                share_access=0x00000001,
            )
        else:
            directory_handle = _open_posix_directory_no_follow(root)
            bundle_details = os.fstat(directory_handle)
            bundle_identity = (bundle_details.st_dev, bundle_details.st_ino)
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            runtime_handle = os.open("runtime", directory_flags, dir_fd=directory_handle)
            runtime_details = os.fstat(runtime_handle)
            runtime_identity = (runtime_details.st_dev, runtime_details.st_ino)
            file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            artifact_handle = os.open(
                artifact_name,
                file_flags,
                dir_fd=runtime_handle,
            )
            details = os.fstat(artifact_handle)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError("runtime attestation is not a regular file")
            artifact_identity = (details.st_dev, details.st_ino)
        body = _read_runtime_artifact_handle(artifact_handle, windows=windows)
        reopened_directory: object | int | None = None
        reopened_runtime: object | int | None = None
        reopened_artifact: object | int | None = None
        try:
            if windows:
                reopened_directory, reopened_bundle_identity = _open_windows_directory_no_follow(
                    root
                )
                reopened_runtime, reopened_runtime_identity = _open_windows_directory_relative(
                    reopened_directory, "runtime"
                )
                reopened_artifact, reopened_artifact_identity = _open_windows_regular_file_relative(
                    reopened_runtime,
                    artifact_name,
                    share_access=0x00000001,
                )
            else:
                reopened_directory = _open_posix_directory_no_follow(root)
                reopened_bundle_details = os.fstat(reopened_directory)
                reopened_bundle_identity = (
                    reopened_bundle_details.st_dev,
                    reopened_bundle_details.st_ino,
                )
                reopened_runtime = os.open(
                    "runtime",
                    directory_flags,
                    dir_fd=reopened_directory,
                )
                reopened_runtime_details = os.fstat(reopened_runtime)
                reopened_runtime_identity = (
                    reopened_runtime_details.st_dev,
                    reopened_runtime_details.st_ino,
                )
                reopened_artifact = os.open(
                    artifact_name,
                    file_flags,
                    dir_fd=reopened_runtime,
                )
                reopened_artifact_details = os.fstat(reopened_artifact)
                if not stat.S_ISREG(reopened_artifact_details.st_mode):
                    raise ValueError("runtime attestation replacement is not a regular file")
                reopened_artifact_identity = (
                    reopened_artifact_details.st_dev,
                    reopened_artifact_details.st_ino,
                )
            if (
                reopened_bundle_identity != bundle_identity
                or reopened_runtime_identity != runtime_identity
                or reopened_artifact_identity != artifact_identity
            ):
                raise ValueError("runtime attestation boundary changed while it was read")
        finally:
            for handle in (reopened_artifact, reopened_runtime, reopened_directory):
                if handle is None:
                    continue
                if windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
    except (OSError, ValueError) as exc:
        raise ValueError("runtime attestation cannot be read from its fixed boundary") from exc
    finally:
        for handle in (artifact_handle, runtime_handle, directory_handle):
            if handle is None:
                continue
            if windows:
                _close_windows_handle(handle)
            else:
                os.close(int(handle))
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if expected_sha256 is not None and (
        _SHA256.fullmatch(expected_sha256) is None or actual_sha256 != expected_sha256
    ):
        raise ValueError("runtime attestation digest does not match")
    return body


def read_runtime_attestation_artifact(
    path: Path,
    *,
    bundle_root: Path,
) -> tuple[bytes, str]:
    """Read the fixed runtime attestation through one stable no-follow boundary."""

    body = _runtime_artifact_body(
        path,
        bundle_root=bundle_root,
        expected_sha256=None,
    )
    return body, hashlib.sha256(body).hexdigest()


def read_capacity_profile_attestation_artifact(
    path: Path,
    *,
    bundle_root: Path,
) -> tuple[bytes, str]:
    """Read the fixed capacity proof through the runtime no-follow boundary."""

    body = _runtime_artifact_body(
        path,
        bundle_root=bundle_root,
        expected_sha256=None,
        artifact_name="capacity-profile-attestation.json",
    )
    return body, hashlib.sha256(body).hexdigest()


def _runtime_command_stdout(raw: object, *, argv: list[str]) -> str:
    if (
        not isinstance(raw, dict)
        or set(raw) != {"argv", "nativeExit", "stdout", "stdoutSha256"}
        or raw.get("argv") != argv
        or raw.get("nativeExit") != 0
        or isinstance(raw.get("nativeExit"), bool)
        or not isinstance(raw.get("stdout"), str)
        or not isinstance(raw.get("stdoutSha256"), str)
        or _SHA256.fullmatch(raw["stdoutSha256"]) is None
    ):
        raise ValueError("runtime attestation command records are invalid")
    stdout = raw["stdout"]
    try:
        stdout_bytes = stdout.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("runtime attestation command stdout is invalid") from exc
    if hashlib.sha256(stdout_bytes).hexdigest() != raw["stdoutSha256"]:
        raise ValueError("runtime attestation command stdout digest does not match")
    return stdout


def _runtime_ps_ids(stdout: str) -> list[str]:
    ids: list[str] = []
    try:
        for line in stdout.splitlines():
            container_id = json.loads(line)
            if not isinstance(container_id, str) or not container_id:
                raise ValueError
            ids.append(container_id)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("runtime attestation Docker ps stdout is invalid") from exc
    if len(ids) != len(set(ids)):
        raise ValueError("runtime attestation Docker ps identities are duplicated")
    return sorted(ids)


def _runtime_json_object(stdout: str, *, label: str) -> dict[str, object]:
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"runtime attestation {label} stdout is invalid") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"runtime attestation {label} stdout is invalid")
    return raw


def _runtime_container_fact(stdout: str, *, container_id: str) -> dict[str, object]:
    raw = _runtime_json_object(stdout, label="container inspect")
    keys = {
        "containerId",
        "localImageId",
        "configImage",
        "project",
        "service",
        "state",
        "running",
        "restarting",
        "exitCode",
        "health",
    }
    exit_code = raw.get("exitCode")
    if (
        set(raw) != keys
        or raw.get("containerId") != container_id
        or any(
            not isinstance(raw.get(name), str) or not raw[name]
            for name in (
                "containerId",
                "localImageId",
                "configImage",
                "project",
                "service",
                "state",
                "health",
            )
        )
        or not isinstance(raw.get("running"), bool)
        or not isinstance(raw.get("restarting"), bool)
        or not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
    ):
        raise ValueError("runtime attestation container inspect stdout is invalid")
    return raw


def _runtime_image_fact(stdout: str) -> dict[str, object]:
    raw = _runtime_json_object(stdout, label="image inspect")
    repo_digests = raw.get("repoDigests")
    if (
        set(raw) != {"imageId", "repoDigests"}
        or not isinstance(raw.get("imageId"), str)
        or not raw["imageId"]
        or not isinstance(repo_digests, list)
        or not all(isinstance(value, str) for value in repo_digests)
    ):
        raise ValueError("runtime attestation image inspect stdout is invalid")
    return raw


def _runtime_rebuild_containers(
    facts: list[dict[str, object]],
    *,
    expected_services: Mapping[str, Mapping[str, str]],
    image_facts: Mapping[str, Mapping[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    services = [fact["service"] for fact in facts]
    if (
        any(not isinstance(service, str) for service in services)
        or len(services) != len(set(services))
        or set(services) != set(expected_services)
    ):
        raise ValueError("runtime attestation service set does not match candidate Compose")
    containers: list[dict[str, object]] = []
    for fact in sorted(facts, key=lambda item: str(item["service"])):
        service = fact["service"]
        assert isinstance(service, str)
        expected = expected_services[service]
        reference = expected["image"]
        image = image_facts.get(reference)
        exit_code = fact["exitCode"]
        if (
            fact["project"] != _RUNTIME_PROJECT
            or fact["configImage"] != reference
            or not isinstance(image, Mapping)
            or fact["localImageId"] != image.get("imageId")
            or _normalized_runtime_repo_digest(reference) not in image.get("repoDigests", [])
        ):
            raise ValueError("runtime attestation container identity is invalid")
        one_shot = expected["restart"] == "no"
        if one_shot:
            valid_state = (
                fact["state"] == "exited"
                and fact["running"] is False
                and fact["restarting"] is False
                and exit_code == 0
            )
        else:
            valid_state = (
                fact["state"] == "running"
                and fact["running"] is True
                and fact["restarting"] is False
            )
        if not valid_state or (service in _RUNTIME_HEALTH_SERVICES and fact["health"] != "healthy"):
            raise ValueError("runtime attestation container state is invalid")
        containers.append(
            {
                **fact,
                "imageId": image["imageId"],
                "repoDigests": image["repoDigests"],
            }
        )
    snapshot = [
        {
            "containerId": container["containerId"],
            "service": container["service"],
            "image": container["configImage"],
            "state": container["state"],
            "health": container["health"],
            "exitCode": container["exitCode"],
        }
        for container in containers
    ]
    return containers, snapshot


def validate_runtime_attestation(
    path: Path,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    """Strictly rederive runtime truth from one hashed attestation artifact."""
    body = _runtime_artifact_body(
        path,
        bundle_root=bundle_root,
        expected_sha256=expected_sha256,
    )
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime attestation is not valid JSON") from exc
    required_keys = {
        "schemaVersion",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "project",
        "beforeSnapshot",
        "afterSnapshot",
        "containers",
        "commands",
    }
    bound_base_url = _runtime_base_url(
        document.get("baseUrl") if isinstance(document, dict) else None
    )
    if (
        not isinstance(document, dict)
        or set(document) != required_keys
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or document.get("candidate") != candidate
        or document.get("releaseRun") != release_run
        or not _valid_observed_at_value(document.get("observedAt"))
        or bound_base_url is None
        or (expected_base_url is not None and bound_base_url != expected_base_url)
        or document.get("project") != _RUNTIME_PROJECT
    ):
        raise ValueError("runtime attestation envelope does not match the release")
    expected_services = _runtime_candidate_contract(candidate_root, candidate=candidate)
    commands = document.get("commands")
    if not isinstance(commands, list):
        raise ValueError("runtime attestation command records are invalid")

    cursor = 0

    def consume(arguments: list[str]) -> str:
        nonlocal cursor
        if cursor >= len(commands):
            raise ValueError("runtime attestation command records are invalid")
        stdout = _runtime_command_stdout(
            commands[cursor],
            argv=[*_RUNTIME_DOCKER_PREFIX, *arguments],
        )
        cursor += 1
        return stdout

    before_ids = _runtime_ps_ids(consume(list(_RUNTIME_PS_ARGUMENTS)))
    before_facts = [
        _runtime_container_fact(
            consume(
                [
                    "container",
                    "inspect",
                    "--format",
                    _RUNTIME_CONTAINER_FORMAT,
                    container_id,
                ]
            ),
            container_id=container_id,
        )
        for container_id in before_ids
    ]
    image_facts = {
        reference: _runtime_image_fact(
            consume(["image", "inspect", "--format", _RUNTIME_IMAGE_FORMAT, reference])
        )
        for reference in sorted({service["image"] for service in expected_services.values()})
    }
    after_ids = _runtime_ps_ids(consume(list(_RUNTIME_PS_ARGUMENTS)))
    after_facts = [
        _runtime_container_fact(
            consume(
                [
                    "container",
                    "inspect",
                    "--format",
                    _RUNTIME_CONTAINER_FORMAT,
                    container_id,
                ]
            ),
            container_id=container_id,
        )
        for container_id in after_ids
    ]
    if cursor != len(commands):
        raise ValueError("runtime attestation command records are invalid")
    before_containers, before_snapshot = _runtime_rebuild_containers(
        before_facts,
        expected_services=expected_services,
        image_facts=image_facts,
    )
    after_containers, after_snapshot = _runtime_rebuild_containers(
        after_facts,
        expected_services=expected_services,
        image_facts=image_facts,
    )
    if (
        before_containers != after_containers
        or document.get("beforeSnapshot") != before_snapshot
        or document.get("afterSnapshot") != after_snapshot
        or document.get("containers") != after_containers
    ):
        raise ValueError("runtime attestation summaries do not match replayed Docker stdout")
    return document


def derive_platform_preflight_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, dict[str, bool]], str]:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("platform preflight attestation is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schemaVersion",
            "candidate",
            "releaseRun",
            "observedAt",
            "baseUrl",
            "runtimeAttestation",
            "executions",
        }
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or document.get("candidate") != candidate
        or document.get("releaseRun") != release_run
        or not _valid_observed_at_value(document.get("observedAt"))
        or _runtime_base_url(document.get("baseUrl")) is None
    ):
        raise ValueError("platform preflight attestation does not match the release")
    runtime_proof = document.get("runtimeAttestation")
    if (
        not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
    ):
        raise ValueError("platform preflight runtime proof is invalid")
    runtime = validate_runtime_attestation(
        Path(runtime_proof["artifact"]),
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=document["baseUrl"],
        expected_sha256=runtime_proof["sha256"],
    )
    containers = runtime.get("containers")
    if not isinstance(containers, list):
        raise ValueError("platform preflight runtime containers are invalid")
    container_ids = {
        container.get("service"): container.get("containerId")
        for container in containers
        if isinstance(container, dict)
    }
    executions = document.get("executions")
    if not isinstance(executions, list) or len(executions) != len(PREFLIGHT_PHASES):
        raise ValueError("platform preflight execution records are invalid")
    phase_reports: dict[str, dict[str, object]] = {}
    for phase, execution in zip(PREFLIGHT_PHASES, executions, strict=True):
        service = PREFLIGHT_PHASE_SERVICES[phase]
        container_id = container_ids.get(service)
        if not isinstance(container_id, str):
            raise ValueError("platform preflight container identity is invalid")
        expected_command = candidate_network_phase_command(phase, container_id)
        if (
            not isinstance(execution, dict)
            or set(execution)
            != {
                "phase",
                "service",
                "containerId",
                "command",
                "nativeExit",
                "stdout",
                "stdoutSha256",
            }
            or execution.get("phase") != phase
            or execution.get("service") != service
            or execution.get("containerId") != container_id
            or execution.get("command") != expected_command
            or execution.get("nativeExit") != 0
            or isinstance(execution.get("nativeExit"), bool)
            or not isinstance(execution.get("stdout"), str)
            or not isinstance(execution.get("stdoutSha256"), str)
        ):
            raise ValueError("platform preflight execution records are invalid")
        stdout = execution["stdout"]
        try:
            stdout_body = stdout.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("platform preflight execution stdout is invalid") from exc
        if hashlib.sha256(stdout_body).hexdigest() != execution["stdoutSha256"]:
            raise ValueError("platform preflight execution stdout digest does not match")
        report = parse_candidate_network_report(stdout_body, expected_phase=phase)
        checks = report["checks"]
        errors = report["errors"]
        assert isinstance(checks, dict) and isinstance(errors, list)
        if errors or any(value is not True for value in checks.values()):
            raise ValueError("platform preflight phase does not prove passing evidence")
        phase_reports[phase] = report
    database_checks = phase_reports["database-object-store"]["checks"]
    openmaic_checks = phase_reports["openmaic"]["checks"]
    assert isinstance(database_checks, dict) and isinstance(openmaic_checks, dict)
    return (
        {
            "database_revisions": {
                "revisionsMatch": database_checks["revisionsMatch"] is True,
            },
            "service_health": {
                "allServicesHealthy": all(
                    value is True
                    for value in (*database_checks.values(), *openmaic_checks.values())
                ),
            },
        },
        document["observedAt"],
    )


def derive_capacity_profile_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay one fixed live-capacity attestation into receipt checks."""

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capacity execution proof is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schemaVersion",
            "candidate",
            "releaseRun",
            "observedAt",
            "baseUrl",
            "runtimeAttestation",
            "execution",
            "summary",
        }
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or document.get("candidate") != candidate
        or document.get("releaseRun") != release_run
        or not _valid_observed_at_value(document.get("observedAt"))
    ):
        raise ValueError("capacity execution proof is invalid")
    base_url = _runtime_base_url(document.get("baseUrl"))
    if base_url is None:
        raise ValueError("capacity execution proof is invalid")
    runtime_proof = document.get("runtimeAttestation")
    if (
        not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
    ):
        raise ValueError("capacity runtime attestation proof is invalid")
    try:
        validate_runtime_attestation(
            Path("runtime/runtime-attestation.json"),
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
            expected_sha256=runtime_proof["sha256"],
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    execution = document.get("execution")
    if (
        not isinstance(execution, dict)
        or set(execution) != {"command", "nativeExit", "stdout", "stdoutSha256", "stderr"}
        or execution.get("command") != capacity_profile_command_record()
        or type(execution.get("nativeExit")) is not int
        or execution.get("nativeExit") != 0
        or execution.get("stderr") != ""
        or not isinstance(execution.get("stdout"), str)
        or not isinstance(execution.get("stdoutSha256"), str)
    ):
        raise ValueError("capacity execution proof is invalid")
    try:
        stdout = execution["stdout"].encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("capacity execution proof is invalid") from exc
    if (
        len(stdout) > MAX_CAPACITY_REPORT_BYTES
        or hashlib.sha256(stdout).hexdigest() != execution["stdoutSha256"]
    ):
        raise ValueError("capacity execution proof is invalid")
    report = parse_capacity_profile_report(
        stdout,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
    )
    if report.get("observedAt") != document.get("observedAt"):
        raise ValueError("capacity execution proof timestamp does not match the report")
    summary = derive_capacity_profile_summary(report)
    if not exact_json_equal(document.get("summary"), summary):
        raise ValueError("capacity execution proof summary does not match raw samples")
    checks = summary.get("checks")
    if not isinstance(checks, dict) or set(checks) != {
        "thresholdsPassed",
        "rawSamplesRecorded",
        "resourceObservationsRecorded",
        "resourceAccountingComplete",
        "resourceBoundaryStable",
    }:
        raise ValueError("capacity execution proof checks are invalid")
    return {name: checks[name] is True for name in checks}, str(document["observedAt"])


def derive_capacity_profile_tenant_id(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> str:
    """Return the existing tenant bound into one validated capacity proof."""

    derive_capacity_profile_receipt_checks(
        body,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
    )
    document = json.loads(body)
    execution = document.get("execution") if isinstance(document, dict) else None
    base_url = document.get("baseUrl") if isinstance(document, dict) else None
    stdout = execution.get("stdout") if isinstance(execution, dict) else None
    if not isinstance(base_url, str) or not isinstance(stdout, str):
        raise ValueError("capacity execution proof is invalid")
    report = parse_capacity_profile_report(
        stdout.encode("utf-8", errors="strict"),
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
    )
    observation = report.get("idempotencyObservation")
    tenant_id = observation.get("tenantId") if isinstance(observation, dict) else None
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("capacity execution proof tenant is invalid")
    return tenant_id


def derive_capacity_profile_tenant_ids(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[str, ...]:
    """Return every executed tenant from one passing, fully replayed capacity proof."""

    checks, _observed_at = derive_capacity_profile_receipt_checks(
        body,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
    )
    if any(value is not True for value in checks.values()):
        raise ValueError("capacity execution proof checks did not all pass")

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("capacity execution proof is invalid") from exc
    execution = document.get("execution") if isinstance(document, dict) else None
    base_url = document.get("baseUrl") if isinstance(document, dict) else None
    stdout = execution.get("stdout") if isinstance(execution, dict) else None
    if not isinstance(base_url, str) or not isinstance(stdout, str):
        raise ValueError("capacity execution proof is invalid")
    report = parse_capacity_profile_report(
        stdout.encode("utf-8", errors="strict"),
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
    )
    profile = report.get("profile")
    completions = report.get("sessionCompletions")
    expected_tenants = profile.get("executedTenants") if isinstance(profile, dict) else None
    if type(expected_tenants) is not int or not isinstance(completions, list):
        raise ValueError("capacity execution proof tenant inventory is invalid")
    tenant_ids = tuple(
        sorted(
            {
                tenant_id
                for completion in completions
                if isinstance(completion, dict)
                and isinstance((tenant_id := completion.get("tenantId")), str)
                and tenant_id
            }
        )
    )
    if len(tenant_ids) != expected_tenants:
        raise ValueError("capacity execution proof tenant inventory is invalid")
    return tenant_ids


def derive_tenant_isolation_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay one tenant-isolation proof and both of its bound dependencies."""

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("tenant isolation execution proof is invalid") from exc
    required_keys = {
        "schemaVersion",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "runtimeAttestation",
        "capacityAttestation",
        "execution",
    }
    if (
        not isinstance(document, dict)
        or frozenset(document)
        not in {frozenset(required_keys), frozenset((*required_keys, "summary"))}
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or not exact_json_equal(document.get("candidate"), dict(candidate))
        or not exact_json_equal(document.get("releaseRun"), dict(release_run))
        or not _valid_observed_at_value(document.get("observedAt"))
    ):
        raise ValueError("tenant isolation execution proof is invalid")
    base_url = _runtime_base_url(document.get("baseUrl"))
    if base_url is None:
        raise ValueError("tenant isolation execution proof is invalid")

    runtime_proof = document.get("runtimeAttestation")
    if (
        not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
    ):
        raise ValueError("tenant isolation runtime attestation proof is invalid")
    try:
        validate_runtime_attestation(
            Path("runtime/runtime-attestation.json"),
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
            expected_sha256=runtime_proof["sha256"],
        )
    except ValueError as exc:
        raise ValueError(f"tenant isolation runtime attestation is invalid: {exc}") from exc

    capacity_proof = document.get("capacityAttestation")
    if (
        not isinstance(capacity_proof, dict)
        or set(capacity_proof) != {"artifact", "sha256"}
        or capacity_proof.get("artifact") != "runtime/capacity-profile-attestation.json"
    ):
        raise ValueError("tenant isolation capacity attestation proof is invalid")
    capacity_body = _proof_bytes(
        bundle_root,
        capacity_proof,
        label="tenant isolation capacity attestation",
    )
    if isinstance(capacity_body, str):
        raise ValueError(capacity_body)
    try:
        capacity_tenant_ids = derive_capacity_profile_tenant_ids(
            capacity_body[0],
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=release_run,
        )
    except ValueError as exc:
        raise ValueError(f"tenant isolation capacity attestation is invalid: {exc}") from exc
    selected_tenant_ids = capacity_tenant_ids[:2]
    if (
        len(selected_tenant_ids) != 2
        or len(set(selected_tenant_ids)) != 2
        or selected_tenant_ids != tuple(sorted(selected_tenant_ids))
    ):
        raise ValueError("tenant isolation capacity tenant pair is invalid")

    execution = document.get("execution")
    if (
        not isinstance(execution, dict)
        or set(execution) != {"command", "nativeExit", "stdout", "stdoutSha256", "stderr"}
        or execution.get("command") != tenant_isolation_command_record()
        or type(execution.get("nativeExit")) is not int
        or execution.get("nativeExit") != 0
        or execution.get("stderr") != ""
        or not isinstance(execution.get("stdout"), str)
        or not isinstance(execution.get("stdoutSha256"), str)
    ):
        raise ValueError("tenant isolation execution proof is invalid")
    try:
        stdout = execution["stdout"].encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("tenant isolation execution proof is invalid") from exc
    if (
        len(stdout) > MAX_TENANT_ISOLATION_REPORT_BYTES
        or hashlib.sha256(stdout).hexdigest() != execution["stdoutSha256"]
    ):
        raise ValueError("tenant isolation execution proof is invalid")
    try:
        report = parse_tenant_isolation_report(
            stdout,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
            expected_capacity_report_sha256=capacity_proof["sha256"],
            expected_capacity_tenant_ids=selected_tenant_ids,
            forbidden_secret_values=(),
        )
    except ValueError as exc:
        raise ValueError(f"tenant isolation strict report is invalid: {exc}") from exc
    if report.get("observedAt") != document.get("observedAt"):
        raise ValueError("tenant isolation execution proof timestamp does not match the report")
    checks = derive_tenant_isolation_checks(report)
    if set(checks) != set(RECEIPT_CONTRACTS["tenant_isolation"][1]) or any(
        value is not True for value in checks.values()
    ):
        raise ValueError("tenant isolation execution proof checks did not all pass")
    if "summary" in document and not exact_json_equal(
        document["summary"],
        {"checks": checks},
    ):
        raise ValueError("tenant isolation execution proof summary does not match the report")
    return checks, str(document["observedAt"])


def derive_openmaic_shared_plane_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay one fixed shared-plane smoke proof into receipt checks."""

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC shared plane execution proof is invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "schemaVersion",
            "candidate",
            "releaseRun",
            "observedAt",
            "baseUrl",
            "runtimeAttestation",
            "execution",
            "summary",
        }
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or not exact_json_equal(document.get("candidate"), dict(candidate))
        or not exact_json_equal(document.get("releaseRun"), dict(release_run))
        or not _valid_observed_at_value(document.get("observedAt"))
    ):
        raise ValueError("OpenMAIC shared plane execution proof is invalid")
    base_url = _runtime_base_url(document.get("baseUrl"))
    if base_url is None:
        raise ValueError("OpenMAIC shared plane execution proof is invalid")

    runtime_proof = document.get("runtimeAttestation")
    if (
        not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
    ):
        raise ValueError("OpenMAIC shared plane runtime attestation proof is invalid")
    try:
        validate_runtime_attestation(
            Path("runtime/runtime-attestation.json"),
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
            expected_sha256=runtime_proof["sha256"],
        )
    except ValueError as exc:
        raise ValueError(f"OpenMAIC shared plane runtime attestation is invalid: {exc}") from exc

    execution = document.get("execution")
    if (
        not isinstance(execution, dict)
        or set(execution) != {"command", "nativeExit", "stdout", "stdoutSha256", "stderr"}
        or execution.get("command") != openmaic_shared_plane_command_record()
        or type(execution.get("nativeExit")) is not int
        or execution.get("nativeExit") != 0
        or execution.get("stderr") != ""
        or not isinstance(execution.get("stdout"), str)
        or not isinstance(execution.get("stdoutSha256"), str)
    ):
        raise ValueError("OpenMAIC shared plane execution proof is invalid")
    try:
        stdout = execution["stdout"].encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("OpenMAIC shared plane execution proof is invalid") from exc
    if (
        len(stdout) > MAX_OPENMAIC_SMOKE_REPORT_BYTES
        or hashlib.sha256(stdout).hexdigest() != execution["stdoutSha256"]
    ):
        raise ValueError("OpenMAIC shared plane execution proof is invalid")
    live_token = os.environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    forbidden_secrets = tuple(
        value.encode("utf-8", errors="strict")
        for value in ({live_token, live_token.strip()} if isinstance(live_token, str) else set())
        if value
    )
    try:
        report = parse_openmaic_smoke_report(
            stdout,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
            expected_runtime_attestation_sha256=runtime_proof["sha256"],
            forbidden_secret_values=forbidden_secrets,
        )
    except ValueError as exc:
        raise ValueError(f"OpenMAIC shared plane strict report is invalid: {exc}") from exc
    if report.get("observedAt") != document.get("observedAt"):
        raise ValueError("OpenMAIC shared plane proof timestamp does not match the report")
    checks = derive_openmaic_shared_plane_checks(report)
    if set(checks) != set(RECEIPT_CONTRACTS["openmaic_shared_plane"][1]) or any(
        value is not True for value in checks.values()
    ):
        raise ValueError("OpenMAIC shared plane proof checks did not all pass")
    summary = {
        "fixture": report.get("fixture"),
        "binding": report.get("binding"),
        "generation": report.get("generation"),
        "checks": checks,
    }
    if not exact_json_equal(document.get("summary"), summary):
        raise ValueError("OpenMAIC shared plane proof summary does not match the report")
    return checks, str(document["observedAt"])


def _classroom_fixture_secret_bytes() -> set[bytes]:
    token = os.environ.get("YFEISTAI_LIVE_FIXTURE_TOKEN")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("classroom exports live fixture token is unavailable")
    return {value.encode("utf-8", errors="strict") for value in {token, token.strip()} if value}


def _windows_directory_entry_names(directory_handle: object) -> set[str]:
    import ctypes
    from ctypes import wintypes

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = (("status", wintypes.LONG), ("information", ctypes.c_size_t))

    ntdll = ctypes.WinDLL("ntdll")
    query_directory = ntdll.NtQueryDirectoryFile
    query_directory.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPVOID,
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.BOOLEAN,
        wintypes.LPVOID,
        wintypes.BOOLEAN,
    )
    query_directory.restype = wintypes.LONG
    status_no_more_files = ctypes.c_long(0x80000006).value
    status_buffer_overflow = ctypes.c_long(0x80000005).value
    names: set[str] = set()
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(64 * 1024)
        io_status = _IoStatusBlock()
        status = query_directory(
            directory_handle,
            None,
            None,
            None,
            ctypes.byref(io_status),
            buffer,
            len(buffer),
            37,
            False,
            None,
            restart,
        )
        restart = False
        if status == status_no_more_files:
            return names
        if status < 0 and status != status_buffer_overflow:
            status_to_error = ntdll.RtlNtStatusToDosError
            status_to_error.argtypes = (wintypes.LONG,)
            status_to_error.restype = wintypes.ULONG
            error = status_to_error(status)
            raise OSError(error, "cannot enumerate classroom export directory")
        used = int(io_status.information)
        if used <= 0 or used > len(buffer):
            raise ValueError("classroom export raw boundary enumeration is invalid")
        offset = 0
        raw = buffer.raw
        while offset < used:
            if offset + 104 > used:
                raise ValueError("classroom export raw boundary enumeration is invalid")
            next_offset = int.from_bytes(raw[offset : offset + 4], "little")
            name_length = int.from_bytes(raw[offset + 60 : offset + 64], "little")
            name_end = offset + 104 + name_length
            if name_length % 2 or name_end > used:
                raise ValueError("classroom export raw boundary enumeration is invalid")
            try:
                name = raw[offset + 104 : name_end].decode("utf-16-le", errors="strict")
            except UnicodeError as exc:
                raise ValueError("classroom export raw boundary enumeration is invalid") from exc
            if name not in {".", ".."}:
                if name in names:
                    raise ValueError("classroom export raw boundary enumeration is invalid")
                names.add(name)
            if next_offset == 0:
                break
            if next_offset % 8 or next_offset < 104 or offset + next_offset >= used:
                raise ValueError("classroom export raw boundary enumeration is invalid")
            offset += next_offset


def _read_classroom_artifact_handle(
    handle: BinaryIO,
    *,
    kind: str,
    max_bytes: int,
    forbidden_secrets: set[bytes] | None = None,
) -> tuple[int, str]:
    details = os.fstat(handle.fileno())
    if not stat.S_ISREG(details.st_mode) or details.st_size <= 0 or details.st_size > max_bytes:
        raise ValueError("classroom export raw artifact size is invalid")
    secrets = forbidden_secrets or set()
    overlap = max((len(secret) for secret in secrets), default=1) - 1
    previous = b""
    digest = hashlib.sha256()
    size = 0
    handle.seek(0)
    try:
        while chunk := handle.read(1024 * 1024):
            window = previous + chunk
            if any(secret in window for secret in secrets):
                raise ValueError("classroom export raw artifact contains a live fixture token")
            digest.update(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ValueError("classroom export raw artifact size is invalid")
            previous = window[-overlap:] if overlap > 0 else b""
    finally:
        handle.seek(0)
    if size != details.st_size:
        raise ValueError("classroom export raw artifact size changed")
    if secrets and classroom_export_archive_contains_forbidden_bytes(
        handle,
        kind=kind,
        forbidden=secrets,
    ):
        raise ValueError("classroom export raw artifact contains a live fixture token")
    return size, digest.hexdigest()


@dataclass
class _ClassroomRawSnapshot:
    raw_root: Path
    windows: bool
    directory_handles: tuple[object | int, object | int, object | int]
    directory_identities: tuple[tuple[int, int], tuple[int, int], tuple[int, int]]
    artifact_handles: dict[str, BinaryIO]
    artifact_path_identities: dict[str, tuple[int, int]]
    artifact_file_identities: dict[str, tuple[int, int]]
    records: dict[str, dict[str, object]]

    def _entry_names(self) -> set[str]:
        exports_handle = self.directory_handles[2]
        if self.windows:
            return _windows_directory_entry_names(exports_handle)
        return set(os.listdir(int(exports_handle)))

    def assert_unchanged(self) -> None:
        expected_names = set(CLASSROOM_EXPORT_PATHS.values())
        if self._entry_names() != expected_names:
            raise ValueError("classroom export raw artifact set changed")
        for kind, handle in self.artifact_handles.items():
            details = os.fstat(handle.fileno())
            size, digest = _read_classroom_artifact_handle(
                handle,
                kind=kind,
                max_bytes=MAX_EXPORT_BYTES[kind],
            )
            if (
                not stat.S_ISREG(details.st_mode)
                or _file_identity(details) != self.artifact_file_identities[kind]
                or size != self.records[kind]["sizeBytes"]
                or digest != self.records[kind]["sha256"]
            ):
                raise ValueError("classroom export raw artifact changed")

        bundle_handle, raw_handle, exports_handle = self.directory_handles
        reopened: list[object | int] = []
        try:
            if self.windows:
                reopened_raw, raw_identity = _open_windows_directory_relative(
                    bundle_handle,
                    "raw",
                )
                reopened.append(reopened_raw)
                reopened_exports, exports_identity = _open_windows_directory_relative(
                    raw_handle,
                    "classroom-exports",
                )
                reopened.append(reopened_exports)
                if (
                    raw_identity != self.directory_identities[1]
                    or exports_identity != self.directory_identities[2]
                ):
                    raise ValueError("classroom export raw boundary changed")
                for kind, name in CLASSROOM_EXPORT_PATHS.items():
                    reopened_file, identity = _open_windows_regular_file_relative(
                        exports_handle,
                        name,
                        share_access=0x00000001 | 0x00000002 | 0x00000004,
                    )
                    reopened.append(reopened_file)
                    if identity != self.artifact_path_identities[kind]:
                        raise ValueError("classroom export raw artifact identity changed")
            else:
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                reopened_raw = os.open("raw", directory_flags, dir_fd=int(bundle_handle))
                reopened.append(reopened_raw)
                reopened_exports = os.open(
                    "classroom-exports",
                    directory_flags,
                    dir_fd=int(raw_handle),
                )
                reopened.append(reopened_exports)
                if (
                    _file_identity(os.fstat(reopened_raw)) != self.directory_identities[1]
                    or _file_identity(os.fstat(reopened_exports)) != self.directory_identities[2]
                ):
                    raise ValueError("classroom export raw boundary changed")
                file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                for kind, name in CLASSROOM_EXPORT_PATHS.items():
                    reopened_file = os.open(name, file_flags, dir_fd=int(exports_handle))
                    reopened.append(reopened_file)
                    if (
                        _file_identity(os.fstat(reopened_file))
                        != self.artifact_path_identities[kind]
                    ):
                        raise ValueError("classroom export raw artifact identity changed")
        except OSError as exc:
            raise ValueError("classroom export raw boundary changed") from exc
        finally:
            for handle in reversed(reopened):
                if self.windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))

    def close(self) -> None:
        for handle in self.artifact_handles.values():
            handle.close()
        self.artifact_handles.clear()
        for handle in reversed(self.directory_handles):
            if self.windows:
                _close_windows_handle(handle)
            else:
                os.close(int(handle))
        self.directory_handles = ()  # type: ignore[assignment]


def _classroom_raw_artifact_records(
    bundle_root: Path,
    *,
    forbidden_secrets: set[bytes],
) -> _ClassroomRawSnapshot:
    root = Path(os.path.abspath(bundle_root))
    raw_root = root / "raw" / "classroom-exports"
    windows = os.name == "nt"
    directory_handles: list[object | int] = []
    directory_identities: list[tuple[int, int]] = []
    artifact_handles: dict[str, BinaryIO] = {}
    artifact_path_identities: dict[str, tuple[int, int]] = {}
    artifact_file_identities: dict[str, tuple[int, int]] = {}
    try:
        if windows:
            bundle_handle, bundle_identity = _open_windows_directory_no_follow(root)
            directory_handles.append(bundle_handle)
            directory_identities.append(bundle_identity)
            raw_handle, raw_identity = _open_windows_directory_relative(bundle_handle, "raw")
            directory_handles.append(raw_handle)
            directory_identities.append(raw_identity)
            exports_handle, exports_identity = _open_windows_directory_relative(
                raw_handle,
                "classroom-exports",
            )
            directory_handles.append(exports_handle)
            directory_identities.append(exports_identity)
            entry_names = _windows_directory_entry_names(exports_handle)
        else:
            bundle_handle = _open_posix_directory_no_follow(root)
            directory_handles.append(bundle_handle)
            directory_identities.append(_file_identity(os.fstat(bundle_handle)))
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            raw_handle = os.open("raw", directory_flags, dir_fd=bundle_handle)
            directory_handles.append(raw_handle)
            directory_identities.append(_file_identity(os.fstat(raw_handle)))
            exports_handle = os.open(
                "classroom-exports",
                directory_flags,
                dir_fd=raw_handle,
            )
            directory_handles.append(exports_handle)
            directory_identities.append(_file_identity(os.fstat(exports_handle)))
            entry_names = set(os.listdir(exports_handle))
        if entry_names != set(CLASSROOM_EXPORT_PATHS.values()):
            raise ValueError("classroom export raw artifact set is invalid")

        records: dict[str, dict[str, object]] = {}
        total_size = 0
        for kind, name in CLASSROOM_EXPORT_PATHS.items():
            if windows:
                import msvcrt

                native_handle, native_identity = _open_windows_regular_file_relative(
                    directory_handles[2],
                    name,
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                )
                descriptor: int | None = None
                try:
                    handle_value = getattr(native_handle, "value", native_handle)
                    descriptor = msvcrt.open_osfhandle(
                        int(handle_value),
                        os.O_RDONLY | os.O_BINARY,
                    )
                    handle = os.fdopen(descriptor, "rb")
                except BaseException:
                    if descriptor is None:
                        _close_windows_handle(native_handle)
                    else:
                        os.close(descriptor)
                    raise
                artifact_path_identities[kind] = native_identity
            else:
                file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                descriptor = os.open(name, file_flags, dir_fd=int(directory_handles[2]))
                try:
                    handle = os.fdopen(descriptor, "rb")
                except BaseException:
                    os.close(descriptor)
                    raise
                artifact_path_identities[kind] = _file_identity(os.fstat(handle.fileno()))
            artifact_handles[kind] = handle
            details = os.fstat(handle.fileno())
            artifact_file_identities[kind] = _file_identity(details)
            size, digest = _read_classroom_artifact_handle(
                handle,
                kind=kind,
                max_bytes=MAX_EXPORT_BYTES[kind],
                forbidden_secrets=forbidden_secrets,
            )
            total_size += size
            if total_size > MAX_TOTAL_EXPORT_BYTES:
                raise ValueError("classroom export raw artifact set is too large")
            records[kind] = {
                "artifact": f"raw/classroom-exports/{name}",
                "sha256": digest,
                "sizeBytes": size,
            }
        snapshot = _ClassroomRawSnapshot(
            raw_root=raw_root,
            windows=windows,
            directory_handles=(
                directory_handles[0],
                directory_handles[1],
                directory_handles[2],
            ),
            directory_identities=(
                directory_identities[0],
                directory_identities[1],
                directory_identities[2],
            ),
            artifact_handles=artifact_handles,
            artifact_path_identities=artifact_path_identities,
            artifact_file_identities=artifact_file_identities,
            records=records,
        )
        snapshot.assert_unchanged()
        return snapshot
    except OSError as exc:
        for handle in artifact_handles.values():
            handle.close()
        for handle in reversed(directory_handles):
            if windows:
                _close_windows_handle(handle)
            else:
                os.close(int(handle))
        raise ValueError("classroom export raw boundary is unavailable") from exc
    except ValueError as exc:
        for handle in artifact_handles.values():
            handle.close()
        for handle in reversed(directory_handles):
            if windows:
                _close_windows_handle(handle)
            else:
                os.close(int(handle))
        if str(exc).startswith("classroom export"):
            raise
        raise ValueError("classroom export raw boundary is unavailable") from exc
    except BaseException:
        for handle in artifact_handles.values():
            handle.close()
        for handle in reversed(directory_handles):
            if windows:
                _close_windows_handle(handle)
            else:
                os.close(int(handle))
        raise


def derive_classroom_exports_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay the fixed classroom export proof and all four raw artifacts."""

    forbidden_secrets = _classroom_fixture_secret_bytes()
    if any(secret in body for secret in forbidden_secrets):
        raise ValueError("classroom exports proof contains a live fixture token")

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("classroom exports execution proof is invalid") from exc
    expected_keys = {
        "schemaVersion",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "tenantId",
        "runtimeAttestation",
        "capacityAttestation",
        "execution",
        "rawArtifacts",
    }
    if (
        not isinstance(document, dict)
        or set(document) != expected_keys
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != 1
        or not exact_json_equal(document.get("candidate"), dict(candidate))
        or not exact_json_equal(document.get("releaseRun"), dict(release_run))
        or not _valid_observed_at_value(document.get("observedAt"))
    ):
        raise ValueError("classroom exports execution proof is invalid")
    base_url = _runtime_base_url(document.get("baseUrl"))
    tenant_id = document.get("tenantId")
    if base_url is None or not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("classroom exports execution proof is invalid")

    runtime_proof = document.get("runtimeAttestation")
    if (
        not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
    ):
        raise ValueError("classroom exports runtime attestation proof is invalid")
    validate_runtime_attestation(
        Path("runtime/runtime-attestation.json"),
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
        expected_sha256=runtime_proof["sha256"],
    )

    capacity_proof = document.get("capacityAttestation")
    if (
        not isinstance(capacity_proof, dict)
        or set(capacity_proof) != {"artifact", "sha256"}
        or capacity_proof.get("artifact") != "runtime/capacity-profile-attestation.json"
    ):
        raise ValueError("classroom exports capacity attestation proof is invalid")
    capacity_body = _proof_bytes(
        Path(bundle_root),
        capacity_proof,
        label="classroom exports capacity attestation",
    )
    if isinstance(capacity_body, str):
        raise ValueError(capacity_body)
    capacity_tenant = derive_capacity_profile_tenant_id(
        capacity_body[0],
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
    )
    if tenant_id != capacity_tenant:
        raise ValueError("classroom exports tenant does not match capacity proof")

    execution = document.get("execution")
    if (
        not isinstance(execution, dict)
        or set(execution) != {"command", "nativeExit", "stdout", "stdoutSha256", "stderr"}
        or execution.get("command") != classroom_exports_command_record()
        or type(execution.get("nativeExit")) is not int
        or execution.get("nativeExit") != 0
        or execution.get("stderr") != ""
        or not isinstance(execution.get("stdout"), str)
        or not isinstance(execution.get("stdoutSha256"), str)
    ):
        raise ValueError("classroom exports execution proof is invalid")
    try:
        stdout = execution["stdout"].encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("classroom exports execution proof is invalid") from exc
    if (
        len(stdout) > MAX_CLASSROOM_EXPORT_REPORT_BYTES
        or hashlib.sha256(stdout).hexdigest() != execution["stdoutSha256"]
    ):
        raise ValueError("classroom exports execution proof is invalid")

    raw_snapshot = _classroom_raw_artifact_records(
        Path(bundle_root),
        forbidden_secrets=forbidden_secrets,
    )
    try:
        raw_root = raw_snapshot.raw_root
        actual_records = raw_snapshot.records
        artifact_handles = raw_snapshot.artifact_handles
        raw_records = document.get("rawArtifacts")
        if not isinstance(raw_records, dict) or set(raw_records) != set(CLASSROOM_EXPORT_PATHS):
            raise ValueError("classroom exports raw artifact proof is invalid")
        for kind in CLASSROOM_EXPORT_PATHS:
            record = raw_records.get(kind)
            if (
                not isinstance(record, dict)
                or type(record.get("sizeBytes")) is not int
                or record != actual_records[kind]
            ):
                raise ValueError("classroom exports raw artifact proof does not match")

        report = parse_classroom_export_report(
            stdout,
            artifact_root=raw_root,
            artifact_handles=artifact_handles,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
        )
        if (
            report.get("observedAt") != document.get("observedAt")
            or report.get("tenantId") != tenant_id
        ):
            raise ValueError("classroom exports report binding is invalid")
        checks = derive_classroom_export_checks(
            stdout,
            artifact_root=raw_root,
            artifact_handles=artifact_handles,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
        )
        raw_snapshot.assert_unchanged()
        replayed_snapshot: _ClassroomRawSnapshot | None = None
        try:
            replayed_snapshot = _classroom_raw_artifact_records(
                Path(bundle_root),
                forbidden_secrets=forbidden_secrets,
            )
            replayed_snapshot.assert_unchanged()
            if (
                replayed_snapshot.raw_root != raw_root
                or replayed_snapshot.records != actual_records
            ):
                raise ValueError("classroom export raw artifact set changed during replay")
        except ValueError as exc:
            raise ValueError("classroom export raw artifact set changed during replay") from exc
        finally:
            if replayed_snapshot is not None:
                replayed_snapshot.close()
        raw_snapshot.assert_unchanged()
        return checks, str(document["observedAt"])
    finally:
        raw_snapshot.close()


def derive_learning_event_idempotency_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay the capacity proof stdout into learning-event checks."""

    _capacity_checks, observed_at = derive_capacity_profile_receipt_checks(
        body,
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
    )
    document = json.loads(body)
    execution = document["execution"]
    base_url = document["baseUrl"]
    if not isinstance(execution, dict) or not isinstance(base_url, str):
        raise ValueError("capacity execution proof is invalid")
    stdout = execution.get("stdout")
    if not isinstance(stdout, str):
        raise ValueError("capacity execution proof is invalid")
    report = parse_capacity_profile_report(
        stdout.encode("utf-8", errors="strict"),
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
    )
    checks = derive_learning_event_idempotency_checks(report)
    return checks, observed_at


def probe_provenance_error(
    document: Mapping[str, object],
    *,
    evidence: str,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    bundle_root: Path,
    candidate_root: Path,
) -> str | None:
    """Revalidate raw/execution proof for evidence backed by a fixed probe."""
    if evidence == "running_containers":
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"runtimeAttestation"}:
            return "runtime attestation proof is missing or invalid"
        attestation_proof = provenance.get("runtimeAttestation")
        if (
            not isinstance(attestation_proof, dict)
            or set(attestation_proof) != {"artifact", "sha256"}
            or attestation_proof.get("artifact") != "runtime/runtime-attestation.json"
            or not isinstance(attestation_proof.get("sha256"), str)
        ):
            return "runtime attestation proof is missing or invalid"
        try:
            attestation = validate_runtime_attestation(
                Path(attestation_proof["artifact"]),
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
                expected_sha256=attestation_proof["sha256"],
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        if not isinstance(receipt, dict) or receipt.get("observedAt") != attestation.get(
            "observedAt"
        ):
            return "running containers receipt does not match runtime attestation"
        return None
    if evidence in {"database_revisions", "service_health"}:
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"platformPreflightAttestation"}:
            return "preflight execution proof is missing or invalid"
        proof = provenance.get("platformPreflightAttestation")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != "runtime/platform-preflight-attestation.json"
        ):
            return "preflight execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label="platform preflight attestation",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            derived, observed_at = derive_platform_preflight_receipt_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        checks = result.get("checks") if isinstance(result, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("observedAt") != observed_at
            or checks != derived[evidence]
        ):
            return "platform preflight receipt does not match execution proof"
        return None
    if evidence == "classroom_exports":
        try:
            forbidden_secrets = _classroom_fixture_secret_bytes()
            receipt_body = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", errors="strict")
        except (UnicodeEncodeError, ValueError) as exc:
            return str(exc)
        if any(secret in receipt_body for secret in forbidden_secrets):
            return "classroom exports receipt contains a live fixture token"
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"classroomExportsAttestation"}:
            return "classroom exports execution proof is missing or invalid"
        proof = provenance.get("classroomExportsAttestation")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != "runtime/classroom-exports-attestation.json"
        ):
            return "classroom exports execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label="classroom exports attestation",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            checks, observed_at = derive_classroom_exports_receipt_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("observedAt") != observed_at
            or not isinstance(result, dict)
            or result.get("checks") != checks
        ):
            return "classroom exports receipt does not match execution proof"
        return None
    if evidence == "tenant_isolation":
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"tenantIsolationAttestation"}:
            return "tenant isolation execution proof is missing or invalid"
        proof = provenance.get("tenantIsolationAttestation")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != "runtime/tenant-isolation-attestation.json"
        ):
            return "tenant isolation execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label="tenant isolation attestation",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            checks, observed_at = derive_tenant_isolation_receipt_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("observedAt") != observed_at
            or not isinstance(result, dict)
            or result.get("checks") != checks
        ):
            return "tenant isolation receipt does not match execution proof"
        return None
    if evidence == "openmaic_shared_plane":
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {
            "openmaicSharedPlaneAttestation"
        }:
            return "OpenMAIC shared plane execution proof is missing or invalid"
        proof = provenance.get("openmaicSharedPlaneAttestation")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != "runtime/openmaic-shared-plane-attestation.json"
        ):
            return "OpenMAIC shared plane execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label="OpenMAIC shared plane attestation",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            checks, observed_at = derive_openmaic_shared_plane_receipt_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("observedAt") != observed_at
            or not isinstance(result, dict)
            or not exact_json_equal(result.get("checks"), checks)
        ):
            return "OpenMAIC shared plane receipt does not match execution proof"
        return None
    if evidence in {"capacity_profile", "learning_event_idempotency"}:
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"capacityAttestation"}:
            return "capacity execution proof is missing or invalid"
        proof = provenance.get("capacityAttestation")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != "runtime/capacity-profile-attestation.json"
        ):
            return "capacity execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label="capacity execution attestation",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            derive_checks = (
                derive_capacity_profile_receipt_checks
                if evidence == "capacity_profile"
                else derive_learning_event_idempotency_receipt_checks
            )
            checks, observed_at = derive_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("observedAt") != observed_at
            or not isinstance(result, dict)
            or result.get("checks") != checks
        ):
            return f"{evidence} receipt does not match capacity execution proof"
        return None
    recipe = PROBE_RECIPES.get(evidence)
    if recipe is None:
        return None
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "recipe",
        "command",
        "rawReport",
        "execution",
        "runtimeAttestation",
    }:
        return "evidence execution proof is missing or invalid"
    recipe_id, _expected_count = recipe
    command = provenance.get("command")
    expected_command = probe_command_record(evidence)
    if (
        provenance.get("recipe") != recipe_id
        or not isinstance(command, dict)
        or command != expected_command
    ):
        return "evidence execution proof is invalid"
    raw_proof = _proof_bytes(bundle_root, provenance.get("rawReport"), label="probe raw report")
    if isinstance(raw_proof, str):
        return raw_proof
    raw_body, _raw_artifact = raw_proof
    execution_proof = _proof_bytes(
        bundle_root,
        provenance.get("execution"),
        label="probe execution record",
    )
    if isinstance(execution_proof, str):
        return execution_proof
    execution_body, _execution_artifact = execution_proof
    try:
        execution = json.loads(execution_body)
    except (UnicodeError, json.JSONDecodeError):
        return "probe execution record is invalid"
    if (
        not isinstance(execution, dict)
        or set(execution)
        != {
            "schemaVersion",
            "candidate",
            "releaseRun",
            "evidence",
            "recipe",
            "command",
            "observedAt",
            "baseUrl",
            "nativeExit",
            "rawReportSha256",
            "runtimeAttestation",
        }
        or execution.get("schemaVersion") != 1
        or execution.get("candidate") != candidate
        or execution.get("releaseRun") != release_run
        or execution.get("evidence") != evidence
        or execution.get("recipe") != recipe_id
        or execution.get("command") != command
        or not _valid_observed_at_value(execution.get("observedAt"))
        or _runtime_base_url(execution.get("baseUrl")) is None
        or not isinstance(execution.get("nativeExit"), int)
        or isinstance(execution.get("nativeExit"), bool)
        or execution.get("nativeExit") != 0
        or execution.get("rawReportSha256") != hashlib.sha256(raw_body).hexdigest()
        or execution.get("runtimeAttestation") != provenance.get("runtimeAttestation")
    ):
        return "probe execution record is invalid"
    attestation_proof = provenance.get("runtimeAttestation")
    if (
        not isinstance(attestation_proof, dict)
        or set(attestation_proof) != {"artifact", "sha256"}
        or attestation_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(attestation_proof.get("sha256"), str)
    ):
        return "runtime attestation proof is invalid"
    try:
        validate_runtime_attestation(
            Path(attestation_proof["artifact"]),
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=execution["baseUrl"],
            expected_sha256=attestation_proof["sha256"],
        )
    except ValueError as exc:
        return str(exc)
    try:
        derived_checks = derive_probe_checks(
            evidence,
            raw_report=raw_body,
            candidate=candidate,
            release_run=release_run,
        )
    except ValueError as exc:
        return str(exc)
    receipt = document.get("receipt")
    result = receipt.get("result") if isinstance(receipt, dict) else None
    checks = result.get("checks") if isinstance(result, dict) else None
    if checks != derived_checks:
        return "receipt checks do not match the probe raw report"
    return None


class _ComposeLoader(yaml.SafeLoader):
    """Safe YAML loader that unwraps Docker Compose's value tags."""


def _construct_compose_value(
    loader: yaml.SafeLoader,
    node: ScalarNode | SequenceNode | MappingNode,
) -> object:
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
        return None if value in {"null", "~"} else value
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for _compose_tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_compose_tag, _construct_compose_value)


@dataclass(frozen=True, slots=True)
class LayerEvidence:
    status: str
    detail: str
    artifact: str | None = None
    artifact_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseVerification:
    layers: dict[str, LayerEvidence]
    missing: tuple[str, ...]
    failed: tuple[str, ...]
    candidate: dict[str, object] | None = None
    evidence_bundle_sha256: str | None = None
    release_run: dict[str, str] | None = None

    @property
    def ok(self) -> bool:
        return not self.missing and not self.failed

    @property
    def status(self) -> str:
        return "ready" if self.ok else "not_ready"


class ReleaseRuntime(Protocol):
    def result(self, name: str) -> LayerEvidence | None: ...


class FileReleaseRuntime:
    """Read explicit evidence for one immutable source candidate."""

    def __init__(
        self,
        path: Path,
        *,
        expected_source_head: str,
        candidate_root: Path | None = None,
    ) -> None:
        self._path = Path(path)
        self._expected_source_head = expected_source_head
        self._candidate_root = Path(candidate_root) if candidate_root is not None else PROJECT_ROOT
        self._loaded = False
        self._candidate_head = ""
        self._candidate: dict[str, object] = {}
        self._candidate_is_valid = False
        self._candidate_error = "evidence candidate image digests are invalid"
        self._bundle_sha256 = ""
        self._release_run: dict[str, str] = {}
        self._release_run_is_valid = False
        self._evidence: dict[str, object] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            body = self._path.read_bytes()
            document = json.loads(body)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        self._bundle_sha256 = hashlib.sha256(body).hexdigest()
        if (
            not isinstance(document, dict)
            or document.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION
        ):
            return
        candidate = document.get("candidate")
        evidence = document.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence, dict):
            return
        release_run = document.get("releaseRun")
        if isinstance(release_run, dict) and self._valid_release_run(release_run):
            self._release_run = {
                "runId": release_run["runId"],
                "environmentId": release_run["environmentId"],
            }
            self._release_run_is_valid = True
        source_head = candidate.get("sourceHead")
        if isinstance(source_head, str) and _COMMIT.fullmatch(source_head):
            self._candidate_head = source_head
        metadata_error = self._candidate_metadata_error(candidate)
        binding_error = self._candidate_binding_error(candidate) if metadata_error is None else None
        self._candidate_is_valid = metadata_error is None and binding_error is None
        if metadata_error is not None:
            self._candidate_error = metadata_error
        elif binding_error is not None:
            self._candidate_error = binding_error
        self._candidate = candidate
        self._evidence = evidence

    @staticmethod
    def _candidate_metadata_error(raw: object) -> str | None:
        if not isinstance(raw, dict) or set(raw) != {
            "sourceRepository",
            "sourceHead",
            "releaseTag",
            "openmaicHead",
            "imageDigests",
        }:
            return "evidence candidate metadata is invalid"
        if raw.get("sourceRepository") != SOURCE_REPOSITORY:
            return "evidence candidate source repository is invalid"
        source_head = raw.get("sourceHead")
        if not isinstance(source_head, str) or _COMMIT.fullmatch(source_head) is None:
            return "evidence candidate source head is invalid"
        release_tag = raw.get("releaseTag")
        release_match = (
            _RELEASE_TAG.fullmatch(release_tag) if isinstance(release_tag, str) else None
        )
        if release_match is None or release_match.group(1) != source_head[:8]:
            return "evidence candidate release tag is invalid"
        if raw.get("openmaicHead") != OPENMAIC_HEAD:
            return "evidence candidate OpenMAIC head is invalid"
        if not FileReleaseRuntime._valid_image_digests(raw.get("imageDigests")):
            return "evidence candidate image digests are invalid"
        return None

    @staticmethod
    def _valid_image_digests(raw: object) -> bool:
        if not isinstance(raw, dict) or set(raw) != set(CUSTOM_IMAGE_NAMES):
            return False
        for name in CUSTOM_IMAGE_NAMES:
            digest = raw.get(name)
            match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
            if match is None or match.group(1) == "0" * 64:
                return False
        return True

    @staticmethod
    def _valid_release_run(raw: object) -> bool:
        if not isinstance(raw, dict) or set(raw) != {"runId", "environmentId"}:
            return False
        return all(
            isinstance(raw.get(name), str) and _RELEASE_ID.fullmatch(raw[name]) is not None
            for name in ("runId", "environmentId")
        )

    @staticmethod
    def _valid_receipt(name: str, raw: object) -> bool:
        if not isinstance(raw, dict) or set(raw) != {"producer", "observedAt", "result"}:
            return False
        contract = RECEIPT_CONTRACTS.get(name)
        if contract is None:
            return False
        expected_producer, required_checks = contract
        producer = raw.get("producer")
        observed_at = raw.get("observedAt")
        result = raw.get("result")
        native_exit = result.get("nativeExit") if isinstance(result, dict) else None
        if (
            producer != expected_producer
            or not isinstance(observed_at, str)
            or not FileReleaseRuntime._valid_observed_at(observed_at)
            or not isinstance(result, dict)
            or set(result) != {"outcome", "nativeExit", "checks"}
            or result.get("outcome") != "pass"
            or not isinstance(native_exit, int)
            or isinstance(native_exit, bool)
            or native_exit != 0
        ):
            return False
        checks = result.get("checks")
        return (
            isinstance(checks, dict)
            and set(checks) == set(required_checks)
            and all(checks.get(check) is True for check in required_checks)
        )

    @staticmethod
    def _valid_observed_at(raw: str) -> bool:
        if _OBSERVED_AT.fullmatch(raw) is None:
            return False
        try:
            datetime.fromisoformat(raw.removesuffix("Z") + "+00:00")
        except ValueError:
            return False
        return True

    def _candidate_binding_error(self, raw: object) -> str | None:
        metadata_error = self._candidate_metadata_error(raw)
        if metadata_error is not None:
            return metadata_error
        assert isinstance(raw, dict)
        lock_path = self._candidate_root / "deploy" / "image-lock.json"
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "candidate image lock is unavailable or invalid"
        if not isinstance(lock, dict) or lock.get("schemaVersion") != 2:
            return "candidate image lock is unavailable or invalid"
        lock_candidate = lock.get("candidate")
        if self._candidate_metadata_error(lock_candidate) is not None:
            return "candidate image lock is unavailable or invalid"
        assert isinstance(lock_candidate, dict)
        for field, detail in (
            ("sourceRepository", "source repository"),
            ("sourceHead", "source head"),
            ("releaseTag", "release tag"),
            ("openmaicHead", "OpenMAIC head"),
            ("imageDigests", "image digests"),
        ):
            if lock_candidate.get(field) != raw.get(field):
                return f"candidate {detail} does not match the image lock"
        images = lock.get("images")
        if not isinstance(images, dict):
            return "candidate image lock is unavailable or invalid"
        image_digests = raw["imageDigests"]
        release_tag = raw["releaseTag"]
        assert isinstance(image_digests, dict)
        assert isinstance(release_tag, str)
        references: dict[str, str] = {}
        for name in CUSTOM_IMAGE_NAMES:
            record = images.get(name)
            if not isinstance(record, dict) or record.get("digest") != image_digests.get(name):
                return "candidate image digests do not match the image lock"
            repository = record.get("repository")
            tag = record.get("tag")
            expected_repository, _compatibility_tag = CUSTOM_IMAGE_SPECS[name]
            if repository != expected_repository or tag != release_tag:
                return "candidate image lock entry is invalid"
            reference = f"{repository}:{tag}@{image_digests[name]}"
            if record.get("reference") != reference:
                return "candidate image lock reference is invalid"
            references[name] = reference
        for relative, bindings in CUSTOM_IMAGE_SERVICE_BINDINGS.items():
            try:
                compose = yaml.load(
                    (self._candidate_root / relative).read_text(encoding="utf-8"),
                    Loader=_ComposeLoader,
                )
            except (OSError, UnicodeError, yaml.YAMLError):
                return "candidate production Compose is unavailable or invalid"
            services = compose.get("services") if isinstance(compose, dict) else None
            if not isinstance(services, dict):
                return "candidate production Compose is unavailable or invalid"
            for service_name, image_name in bindings.items():
                service = services.get(service_name)
                if not isinstance(service, dict) or service.get("image") != references[image_name]:
                    return (
                        "candidate images are not bound to every required production "
                        "Compose service"
                    )
        return None

    @property
    def candidate(self) -> dict[str, object] | None:
        self._load()
        return self._candidate or None

    @property
    def evidence_bundle_sha256(self) -> str | None:
        self._load()
        return self._bundle_sha256 or None

    @property
    def release_run(self) -> dict[str, str] | None:
        self._load()
        return self._release_run or None

    def _artifact_path(self, reference: str) -> Path | None:
        relative = Path(reference)
        if relative.is_absolute():
            return None
        root = self._path.parent.resolve()
        resolved = (root / relative).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    def _parse(self, name: str, raw: object) -> LayerEvidence:
        if not isinstance(raw, dict):
            return LayerEvidence("fail", "evidence entry is invalid")
        status = raw.get("status")
        detail = raw.get("detail")
        artifact = raw.get("artifact")
        artifact_sha256 = raw.get("artifactSha256")
        if status not in {"pass", "fail"}:
            return LayerEvidence("fail", "evidence status is invalid")
        if not isinstance(detail, str) or not detail.strip():
            return LayerEvidence("fail", "evidence detail is missing")
        if status != "pass":
            return LayerEvidence(
                status=status,
                detail=detail.strip(),
                artifact=(
                    artifact.strip() if isinstance(artifact, str) and artifact.strip() else None
                ),
            )
        if not isinstance(artifact, str) or not artifact.strip():
            return LayerEvidence("fail", "passing evidence artifact is missing")
        if not isinstance(artifact_sha256, str) or not _SHA256.fullmatch(artifact_sha256):
            return LayerEvidence("fail", "passing evidence artifact digest is invalid")
        if not self._candidate_is_valid:
            return LayerEvidence("fail", self._candidate_error)
        if not self._release_run_is_valid:
            return LayerEvidence("fail", "evidence release run identity is invalid")
        artifact_reference = artifact.strip()
        artifact_path = self._artifact_path(artifact_reference)
        if artifact_path is None:
            return LayerEvidence("fail", "evidence artifact is outside the evidence bundle")
        try:
            artifact_body = artifact_path.read_bytes()
        except OSError:
            return LayerEvidence("fail", "evidence artifact does not exist")
        actual_sha256 = hashlib.sha256(artifact_body).hexdigest()
        if actual_sha256 != artifact_sha256:
            return LayerEvidence("fail", "evidence artifact digest does not match")
        try:
            artifact_document = json.loads(artifact_body)
        except (UnicodeError, json.JSONDecodeError):
            return LayerEvidence("fail", "evidence artifact is not valid JSON")
        if (
            not isinstance(artifact_document, dict)
            or type(artifact_document.get("schemaVersion")) is not int
            or artifact_document.get("schemaVersion") != ARTIFACT_SCHEMA_VERSION
            or artifact_document.get("evidence") != name
        ):
            return LayerEvidence("fail", "evidence artifact envelope is invalid")
        if artifact_document.get("candidate") != self._candidate:
            return LayerEvidence("fail", "evidence artifact candidate does not match")
        if artifact_document.get("releaseRun") != self._release_run:
            return LayerEvidence("fail", "evidence artifact release run does not match")
        if not self._valid_receipt(name, artifact_document.get("receipt")):
            return LayerEvidence("fail", "evidence artifact receipt is invalid")
        assert isinstance(self._candidate, dict)
        assert isinstance(self._release_run, dict)
        provenance_error = probe_provenance_error(
            artifact_document,
            evidence=name,
            candidate=self._candidate,
            release_run=self._release_run,
            bundle_root=self._path.parent,
            candidate_root=self._candidate_root,
        )
        if provenance_error is not None:
            return LayerEvidence("fail", provenance_error)
        return LayerEvidence(
            status=status,
            detail=detail.strip(),
            artifact=artifact_reference,
            artifact_sha256=artifact_sha256,
        )

    def result(self, name: str) -> LayerEvidence | None:
        self._load()
        raw = self._evidence.get(name)
        if raw is None:
            return None
        parsed = self._parse(name, raw)
        if name == "source_head" and (
            not _COMMIT.fullmatch(self._expected_source_head)
            or self._candidate_head != self._expected_source_head
        ):
            return LayerEvidence(
                "fail",
                "evidence candidate source head does not match the checked-out candidate",
                parsed.artifact,
                parsed.artifact_sha256,
            )
        return parsed


def _runtime_metadata(runtime: ReleaseRuntime, name: str) -> object:
    try:
        return getattr(runtime, name, None)
    except Exception:
        return None


def verify(runtime: ReleaseRuntime) -> ReleaseVerification:
    layers: dict[str, LayerEvidence] = {}
    missing: list[str] = []
    failed: list[str] = []
    for name in REQUIRED_LAYERS:
        try:
            raw = runtime.result(name)
        except Exception:
            raw = LayerEvidence("fail", "evidence probe failed")
        if raw is None:
            layers[name] = LayerEvidence("missing", "evidence was not recorded")
            missing.append(name)
            continue
        status = getattr(raw, "status", "fail")
        detail = getattr(raw, "detail", "evidence result is invalid")
        artifact = getattr(raw, "artifact", None)
        artifact_sha256 = getattr(raw, "artifact_sha256", None)
        evidence = LayerEvidence(
            status=status if isinstance(status, str) else "fail",
            detail=detail if isinstance(detail, str) else "evidence result is invalid",
            artifact=artifact if isinstance(artifact, str) else None,
            artifact_sha256=(artifact_sha256 if isinstance(artifact_sha256, str) else None),
        )
        layers[name] = evidence
        if evidence.status != "pass":
            failed.append(name)
    candidate = _runtime_metadata(runtime, "candidate")
    evidence_bundle_sha256 = _runtime_metadata(runtime, "evidence_bundle_sha256")
    release_run = _runtime_metadata(runtime, "release_run")
    candidate_is_valid = FileReleaseRuntime._candidate_metadata_error(candidate) is None
    candidate_source_is_valid = candidate_is_valid
    candidate_images_are_valid = candidate_is_valid
    bundle_is_valid = (
        isinstance(evidence_bundle_sha256, str)
        and _SHA256.fullmatch(evidence_bundle_sha256) is not None
    )
    release_run_is_valid = isinstance(release_run, dict) and (
        FileReleaseRuntime._valid_release_run(release_run)
    )
    shared_binding_is_valid = bundle_is_valid and release_run_is_valid
    for name, binding_is_valid in (
        ("source_head", candidate_source_is_valid and shared_binding_is_valid),
        ("image_digests", candidate_images_are_valid and shared_binding_is_valid),
    ):
        if not binding_is_valid and layers[name].status == "pass":
            layers[name] = LayerEvidence("fail", "release candidate binding is invalid")
            if name not in failed:
                failed.append(name)
    return ReleaseVerification(
        layers=layers,
        missing=tuple(missing),
        failed=tuple(failed),
        candidate=(candidate if candidate_source_is_valid and candidate_images_are_valid else None),
        evidence_bundle_sha256=(evidence_bundle_sha256 if bundle_is_valid else None),
        release_run=release_run if release_run_is_valid else None,
    )


def report_payload(result: ReleaseVerification) -> dict[str, object]:
    return {
        "status": result.status,
        "ok": result.ok,
        "candidate": result.candidate,
        "releaseRun": result.release_run,
        "evidenceBundleSha256": result.evidence_bundle_sha256,
        "missing": list(result.missing),
        "failed": list(result.failed),
        "layers": {
            name: {
                "status": evidence.status,
                "detail": evidence.detail,
                "artifact": evidence.artifact,
                "artifactSha256": evidence.artifact_sha256,
            }
            for name, evidence in result.layers.items()
        },
    }


def _git_head() -> str:
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    head = head_result.stdout.strip() if head_result.returncode == 0 else ""
    if (
        _COMMIT.fullmatch(head) is None
        or status_result.returncode != 0
        or status_result.stdout.strip()
    ):
        return ""
    return head


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def _reject_source_tree_change(result: ReleaseVerification) -> ReleaseVerification:
    current = result.layers["source_head"]
    if current.status != "pass":
        return result
    layers = dict(result.layers)
    layers["source_head"] = LayerEvidence(
        "fail",
        "checked-out source changed during verification",
        current.artifact,
        current.artifact_sha256,
    )
    failed = result.failed
    if "source_head" not in failed:
        failed = (*failed, "source_head")
    return replace(result, layers=layers, failed=failed)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    expected_source_head = _git_head()
    result = verify(
        FileReleaseRuntime(
            args.evidence,
            expected_source_head=expected_source_head,
            candidate_root=args.candidate_root,
        )
    )
    if not expected_source_head or _git_head() != expected_source_head:
        result = _reject_source_tree_change(result)
    payload = report_payload(result)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"classroom first-release: {result.status}")
        for name, evidence in result.layers.items():
            print(f"{name}: {evidence.status} - {evidence.detail}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
