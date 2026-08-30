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
import tempfile
from typing import BinaryIO, Protocol
from urllib.parse import urlsplit

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

SCRIPTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_ROOT.parent
for import_root in (PROJECT_ROOT, SCRIPTS_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from backup_restore_contract import (  # noqa: E402
    MAX_BACKUP_RESTORE_REPORT_BYTES,
    MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
    MAX_TARGET_PROVISIONING_RECEIPT_BYTES,
    derive_backup_restore_checks,
    parse_backup_restore_report,
)
from backup_restore_probe import (  # noqa: E402
    _require_backup_uses_snapshot,
    _snapshot_source_archive,
    _verify_source_archive_snapshot,
)
from backup_teaching import load_verified_backup, reverify_verified_backup  # noqa: E402
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
    validate_playwright_persistence_boundary,
)
from classroom_runtime_attestation import (  # noqa: E402
    CONTAINER_INSPECT_FORMAT as _RUNTIME_CONTAINER_FORMAT,
)
from classroom_runtime_attestation import (
    IMAGE_INSPECT_FORMAT as _RUNTIME_IMAGE_FORMAT,
)
from classroom_runtime_attestation import (
    _CandidateContractLease,
    _close_windows_handle,
    _file_identity,
    _is_link_or_reparse,
    _load_candidate_token,
    _open_windows_directory_handle,
    _open_windows_directory_relative,
    _open_windows_regular_file_relative,
    _read_windows_file_handle,
    _windows_handle_identity,
)
from classroom_runtime_attestation import (
    _compose_hashes as _runtime_compose_hashes,
)
from classroom_runtime_attestation import (
    _container_fact as _producer_runtime_container_fact,
)
from classroom_runtime_attestation import (
    _merged_expected_services as _runtime_merged_expected_services,
)
from classroom_runtime_attestation import (
    _snapshot as _producer_runtime_snapshot,
)
from classroom_runtime_attestation import (
    _validate_container_facts as _validate_runtime_container_facts,
)
from gateway_public_contract import (  # noqa: E402
    GATEWAY_PUBLIC_PRODUCER,
    derive_gateway_public_checks,
    parse_gateway_candidate_networks,
    parse_gateway_public_report,
    signed_gateway_observer_policy,
)
from gateway_trust_contract import verify_gateway_trust_pair  # noqa: E402
from openmaic_smoke_contract import (  # noqa: E402
    MAX_OPENMAIC_SMOKE_REPORT_BYTES,
    derive_openmaic_dedicated_outage_checks,
    derive_openmaic_dedicated_plane_checks,
    derive_openmaic_shared_plane_checks,
    openmaic_dedicated_plane_command_record,
    openmaic_shared_plane_command_record,
    parse_openmaic_dedicated_outage_attempt_marker,
    parse_openmaic_dedicated_outage_attestation,
    parse_openmaic_shared_ingress_observer_attestation,
    parse_openmaic_smoke_report,
    validate_openmaic_shared_ingress_observer_trust_anchor,
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
        ("dedicatedGenerationPassed", "noSharedClientIssued", "noSharedFallback"),
    ),
    "tailwind4_visual_matrix": ("playwright", ("visualMatrixPassed",)),
    "backup_restore": (
        "restore-teaching",
        ("newDatabaseRestored", "distinctVersionedBucketRestored", "receiptsVerified"),
    ),
    "gateway_only_public": (
        GATEWAY_PUBLIC_PRODUCER,
        ("gatewayPublic", "internalPortsClosed"),
    ),
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
_GATEWAY_DOCKER_PROJECT = "yfeistai-platform"
_GATEWAY_DOCKER_CONTEXT = "default"
_GATEWAY_DOCKER_CONTEXT_ARGUMENTS = [
    "context",
    "inspect",
    _GATEWAY_DOCKER_CONTEXT,
    "--format",
    "{{json .Endpoints.docker.Host}}",
]
_GATEWAY_DOCKER_INFO_ARGUMENTS = [
    "info",
    "--format",
    '{"serverId":{{json .ID}},"osType":{{json .OSType}}}',
]
_GATEWAY_DOCKER_PS_ARGUMENTS = [
    "ps",
    "-a",
    "--no-trunc",
    "--filter",
    f"label=com.docker.compose.project={_GATEWAY_DOCKER_PROJECT}",
    "--format",
    "{{json .ID}}",
]
_GATEWAY_DOCKER_INSPECT_FORMAT = (
    '{"containerId":{{json .Id}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"networkMode":{{json .HostConfig.NetworkMode}},'
    '"networks":{{json .NetworkSettings.Networks}},'
    '"publishedPorts":{{json .NetworkSettings.Ports}}}'
)
_GATEWAY_DOCKER_HOST_IDENTITY_ENV = "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256"
_GATEWAY_TRUST_KEYRING_ENV = "YFEISTAI_GATEWAY_TRUST_KEYRING"
_GATEWAY_TRUST_KEYRING_SHA256_ENV = "YFEISTAI_GATEWAY_TRUST_KEYRING_SHA256"
_GATEWAY_OBSERVER_CHALLENGE_ENV = "YFEISTAI_GATEWAY_OBSERVER_CHALLENGE"
_GATEWAY_HOST_CHALLENGE_ENV = "YFEISTAI_GATEWAY_HOST_CHALLENGE"
_GATEWAY_TRUSTED_NOW_ENV = "YFEISTAI_GATEWAY_TRUSTED_NOW"
_OPENMAIC_OBSERVER_ATTESTATION_SHA256_ENV = "YFEISTAI_OPENMAIC_EXPECTED_OBSERVER_ATTESTATION_SHA256"
_OPENMAIC_OBSERVER_ID_ENV = "YFEISTAI_OPENMAIC_EXPECTED_OBSERVER_ID"
_OPENMAIC_OBSERVER_ORIGIN_ENV = "YFEISTAI_OPENMAIC_EXPECTED_OBSERVER_ORIGIN"
_OPENMAIC_CONTROL_ORIGIN_ENV = "YFEISTAI_OPENMAIC_EXPECTED_SHARED_INGRESS_CONTROL_ORIGIN"
_RUNTIME_DOCKER_PREFIX = (
    "docker",
    "--config",
    "<isolated-docker-config>",
    "--context",
    "default",
)
_RUNTIME_COMPOSE_TOPOLOGY = (
    "compose",
    "--env-file",
    "<deployment-root>/data/user/settings/docker.env",
    "--project-directory",
    "<deployment-root>",
    "--project-name",
    _RUNTIME_PROJECT,
    "-f",
    "<candidate-root>/docker-compose.yml",
    "-f",
    "<candidate-root>/docker-compose.platform.yml",
)
_RUNTIME_COMPOSE_CONFIG_ARGUMENTS = (*_RUNTIME_COMPOSE_TOPOLOGY, "config", "--format", "json")
_RUNTIME_COMPOSE_HASH_ARGUMENTS = (*_RUNTIME_COMPOSE_TOPOLOGY, "config", "--hash", "*")
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
    try:
        validate_playwright_persistence_boundary(document)
    except ValueError as exc:
        raise ValueError("probe Playwright persistence boundary is invalid") from exc
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


@dataclass(slots=True)
class _GatewayRawProofLease:
    root: Path
    body: bytes
    windows: bool
    directory_handles: tuple[object | int, object | int]
    directory_identities: tuple[tuple[int, int], tuple[int, int]]
    artifact_handle: object | int
    artifact_identity: tuple[int, int]

    @classmethod
    def open(
        cls,
        bundle_root: Path,
        *,
        expected_sha256: str,
    ) -> _GatewayRawProofLease:
        if _SHA256.fullmatch(expected_sha256) is None:
            raise ValueError("gateway external observation reference is invalid")
        root = Path(os.path.abspath(bundle_root))
        windows = os.name == "nt"
        directory_handles: list[object | int] = []
        directory_identities: list[tuple[int, int]] = []
        artifact_handle: object | int | None = None
        try:
            if windows:
                bundle_handle, bundle_identity = _open_windows_directory_no_follow(root)
                directory_handles.append(bundle_handle)
                directory_identities.append(bundle_identity)
                raw_handle, raw_identity = _open_windows_directory_relative(
                    bundle_handle,
                    "raw",
                )
                directory_handles.append(raw_handle)
                directory_identities.append(raw_identity)
                artifact_handle, artifact_identity = _open_windows_regular_file_relative(
                    raw_handle,
                    "gateway-public-observation.json",
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                )
            else:
                bundle_handle = _open_posix_directory_no_follow(root)
                directory_handles.append(bundle_handle)
                directory_identities.append(_file_identity(os.fstat(bundle_handle)))
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                raw_handle = os.open("raw", directory_flags, dir_fd=bundle_handle)
                directory_handles.append(raw_handle)
                directory_identities.append(_file_identity(os.fstat(raw_handle)))
                file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                artifact_handle = os.open(
                    "gateway-public-observation.json",
                    file_flags,
                    dir_fd=raw_handle,
                )
                details = os.fstat(artifact_handle)
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("gateway external observation is not a regular file")
                artifact_identity = _file_identity(details)
            body = _read_runtime_artifact_handle(artifact_handle, windows=windows)
            if hashlib.sha256(body).hexdigest() != expected_sha256:
                raise ValueError("gateway external observation digest does not match")
            lease = cls(
                root=root,
                body=body,
                windows=windows,
                directory_handles=(directory_handles[0], directory_handles[1]),
                directory_identities=(directory_identities[0], directory_identities[1]),
                artifact_handle=artifact_handle,
                artifact_identity=artifact_identity,
            )
            lease.assert_unchanged()
            return lease
        except BaseException:
            if artifact_handle is not None:
                if windows:
                    _close_windows_handle(artifact_handle)
                else:
                    os.close(int(artifact_handle))
            for handle in reversed(directory_handles):
                if windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            raise

    def __enter__(self) -> _GatewayRawProofLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except BaseException as close_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"gateway raw proof lease cleanup failed: {close_error}")
        return False

    def assert_unchanged(self) -> None:
        bundle_handle, raw_handle = self.directory_handles
        bundle_identity, raw_identity = self.directory_identities
        reopened: list[object | int] = []
        try:
            if self.windows:
                if (
                    _windows_handle_identity(
                        self.artifact_handle,
                        directory=False,
                    )
                    != self.artifact_identity
                ):
                    raise ValueError("gateway external observation handle changed")
                reopened_bundle, current_bundle_identity = _open_windows_directory_no_follow(
                    self.root
                )
                reopened.append(reopened_bundle)
                reopened_raw, current_raw_identity = _open_windows_directory_relative(
                    reopened_bundle,
                    "raw",
                )
                reopened.append(reopened_raw)
                reopened_artifact, current_artifact_identity = _open_windows_regular_file_relative(
                    reopened_raw,
                    "gateway-public-observation.json",
                    share_access=0x00000001 | 0x00000002 | 0x00000004,
                )
                reopened.append(reopened_artifact)
            else:
                if (
                    _file_identity(os.fstat(int(bundle_handle))) != bundle_identity
                    or _file_identity(os.fstat(int(raw_handle))) != raw_identity
                    or _file_identity(os.fstat(int(self.artifact_handle))) != self.artifact_identity
                ):
                    raise ValueError("gateway external observation handle changed")
                reopened_bundle = _open_posix_directory_no_follow(self.root)
                reopened.append(reopened_bundle)
                current_bundle_identity = _file_identity(os.fstat(reopened_bundle))
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                reopened_raw = os.open("raw", directory_flags, dir_fd=reopened_bundle)
                reopened.append(reopened_raw)
                current_raw_identity = _file_identity(os.fstat(reopened_raw))
                file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                reopened_artifact = os.open(
                    "gateway-public-observation.json",
                    file_flags,
                    dir_fd=reopened_raw,
                )
                reopened.append(reopened_artifact)
                details = os.fstat(reopened_artifact)
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("gateway external observation path changed")
                current_artifact_identity = _file_identity(details)
            held_body = _read_runtime_artifact_handle(
                self.artifact_handle,
                windows=self.windows,
            )
            current_body = _read_runtime_artifact_handle(
                reopened_artifact,
                windows=self.windows,
            )
            if (
                current_bundle_identity != bundle_identity
                or current_raw_identity != raw_identity
                or current_artifact_identity != self.artifact_identity
                or held_body != self.body
                or current_body != self.body
            ):
                raise ValueError("gateway external observation boundary changed")
        except (OSError, ValueError) as exc:
            raise ValueError("gateway external observation boundary changed during replay") from exc
        finally:
            for handle in reversed(reopened):
                if self.windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))

    def close(self) -> None:
        errors: list[OSError] = []
        for handle in (self.artifact_handle, *reversed(self.directory_handles)):
            try:
                if self.windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            except OSError as exc:
                errors.append(exc)
        self.directory_handles = ()  # type: ignore[assignment]
        if errors:
            raise errors[0]


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
        "runtime/openmaic-dedicated-plane-attestation.json": (
            "openmaic-dedicated-plane-attestation.json"
        ),
        "runtime/openmaic-dedicated-outage-attestation.json": (
            "openmaic-dedicated-outage-attestation.json"
        ),
        "runtime/gateway-only-public-attestation.json": ("gateway-only-public-attestation.json"),
        "runtime/gateway-external-observer-attestation.json": (
            "gateway-external-observer-attestation.json"
        ),
        "runtime/gateway-observer-trust-envelope.json": ("gateway-observer-trust-envelope.json"),
        "runtime/gateway-host-provisioner-trust-envelope.json": (
            "gateway-host-provisioner-trust-envelope.json"
        ),
        "runtime/gateway-docker-host-provisioning-receipt.json": (
            "gateway-docker-host-provisioning-receipt.json"
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
            if not os.path.lexists(Path(bundle_root) / artifact):
                return f"{label} does not exist"
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
        if _is_link_or_reparse(cursor):
            return f"{label} must not use a symlink"
        cursor = cursor.parent
    try:
        body = resolved.read_bytes()
    except OSError:
        return f"{label} does not exist"
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        return f"{label} digest does not match"
    return body, artifact


_BACKUP_RESTORE_DIRECTORY = Path("runtime/backup-restore")
_BACKUP_RESTORE_REPORT = _BACKUP_RESTORE_DIRECTORY / "backup-restore-report.json"
_BACKUP_RESTORE_SOURCE_ARCHIVE = _BACKUP_RESTORE_DIRECTORY / "source-backup.snapshot"
_BACKUP_RESTORE_JSON_ARTIFACTS = {
    "backup-restore-report.json": MAX_BACKUP_RESTORE_REPORT_BYTES,
    "restore-validation.json": MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
    "source-provenance.json": MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
    "target-config.snapshot.json": MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
    "target-provisioning-receipt.json": MAX_TARGET_PROVISIONING_RECEIPT_BYTES,
}
_BACKUP_RESTORE_ENTRY_NAMES = frozenset(
    (*_BACKUP_RESTORE_JSON_ARTIFACTS, _BACKUP_RESTORE_SOURCE_ARCHIVE.name)
)


@dataclass(slots=True)
class _BackupRestoreArtifactLease:
    root: Path
    windows: bool
    directory_handles: tuple[object | int, object | int, object | int, object | int]
    directory_identities: tuple[
        tuple[int, int],
        tuple[int, int],
        tuple[int, int],
        tuple[int, int],
    ]
    artifact_handles: dict[str, object | int]
    artifact_identities: dict[str, tuple[int, int]]
    artifact_bodies: dict[str, bytes]

    @classmethod
    def open(cls, bundle_root: Path) -> _BackupRestoreArtifactLease:
        root = Path(os.path.abspath(bundle_root))
        windows = os.name == "nt"
        directory_handles: list[object | int] = []
        directory_identities: list[tuple[int, int]] = []
        artifact_handles: dict[str, object | int] = {}
        artifact_identities: dict[str, tuple[int, int]] = {}
        artifact_bodies: dict[str, bytes] = {}
        try:
            if windows:
                bundle_handle, bundle_identity = _open_windows_directory_no_follow(root)
                runtime_handle, runtime_identity = _open_windows_directory_relative(
                    bundle_handle,
                    "runtime",
                )
                evidence_handle, evidence_identity = _open_windows_directory_relative(
                    runtime_handle,
                    "backup-restore",
                )
                archive_handle, archive_identity = _open_windows_directory_relative(
                    evidence_handle,
                    _BACKUP_RESTORE_SOURCE_ARCHIVE.name,
                )
            else:
                bundle_handle = _open_posix_directory_no_follow(root)
                bundle_identity = _file_identity(os.fstat(bundle_handle))
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                runtime_handle = os.open("runtime", directory_flags, dir_fd=bundle_handle)
                runtime_identity = _file_identity(os.fstat(runtime_handle))
                evidence_handle = os.open(
                    "backup-restore",
                    directory_flags,
                    dir_fd=runtime_handle,
                )
                evidence_identity = _file_identity(os.fstat(evidence_handle))
                archive_handle = os.open(
                    _BACKUP_RESTORE_SOURCE_ARCHIVE.name,
                    directory_flags,
                    dir_fd=evidence_handle,
                )
                archive_identity = _file_identity(os.fstat(archive_handle))
            directory_handles.extend(
                (bundle_handle, runtime_handle, evidence_handle, archive_handle)
            )
            directory_identities.extend(
                (bundle_identity, runtime_identity, evidence_identity, archive_identity)
            )
            entry_names = (
                _windows_directory_entry_names(evidence_handle)
                if windows
                else set(os.listdir(int(evidence_handle)))
            )
            if entry_names != _BACKUP_RESTORE_ENTRY_NAMES:
                raise ValueError("backup restore evidence artifact set is invalid")
            for name, max_bytes in _BACKUP_RESTORE_JSON_ARTIFACTS.items():
                if windows:
                    handle, identity = _open_windows_regular_file_relative(
                        evidence_handle,
                        name,
                        share_access=0x00000001 | 0x00000002 | 0x00000004,
                    )
                else:
                    file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                    handle = os.open(name, file_flags, dir_fd=int(evidence_handle))
                    details = os.fstat(handle)
                    if not stat.S_ISREG(details.st_mode):
                        raise ValueError("backup restore evidence artifact is not a regular file")
                    identity = _file_identity(details)
                body = _read_runtime_artifact_handle(handle, windows=windows)
                if not body or len(body) > max_bytes:
                    if windows:
                        _close_windows_handle(handle)
                    else:
                        os.close(int(handle))
                    raise ValueError("backup restore evidence artifact byte length is invalid")
                artifact_handles[name] = handle
                artifact_identities[name] = identity
                artifact_bodies[name] = body
            lease = cls(
                root=root,
                windows=windows,
                directory_handles=(
                    directory_handles[0],
                    directory_handles[1],
                    directory_handles[2],
                    directory_handles[3],
                ),
                directory_identities=(
                    directory_identities[0],
                    directory_identities[1],
                    directory_identities[2],
                    directory_identities[3],
                ),
                artifact_handles=artifact_handles,
                artifact_identities=artifact_identities,
                artifact_bodies=artifact_bodies,
            )
            lease.assert_unchanged()
            return lease
        except BaseException:
            for handle in artifact_handles.values():
                if windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            for handle in reversed(directory_handles):
                if windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            raise

    def __enter__(self) -> _BackupRestoreArtifactLease:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except BaseException as close_error:
            if exc_value is None:
                raise
            if hasattr(exc_value, "add_note"):
                exc_value.add_note(f"backup restore evidence lease cleanup failed: {close_error}")
        return False

    @property
    def source_archive_path(self) -> Path:
        return self.root / _BACKUP_RESTORE_SOURCE_ARCHIVE

    def body(self, name: str) -> bytes:
        try:
            return self.artifact_bodies[name]
        except KeyError:
            raise ValueError("backup restore evidence artifact name is invalid") from None

    def assert_unchanged(self) -> None:
        reopened: list[object | int] = []
        try:
            held_directory_identities = tuple(
                _windows_handle_identity(handle, directory=True)
                if self.windows
                else _file_identity(os.fstat(int(handle)))
                for handle in self.directory_handles
            )
            if held_directory_identities != self.directory_identities:
                raise ValueError("backup restore evidence directory handle changed")
            held_entries = (
                _windows_directory_entry_names(self.directory_handles[2])
                if self.windows
                else set(os.listdir(int(self.directory_handles[2])))
            )
            if held_entries != _BACKUP_RESTORE_ENTRY_NAMES:
                raise ValueError("backup restore evidence artifact set changed")
            for name, handle in self.artifact_handles.items():
                identity = (
                    _windows_handle_identity(handle, directory=False)
                    if self.windows
                    else _file_identity(os.fstat(int(handle)))
                )
                if identity != self.artifact_identities[name] or (
                    _read_runtime_artifact_handle(handle, windows=self.windows)
                    != self.artifact_bodies[name]
                ):
                    raise ValueError("backup restore evidence artifact changed")

            if self.windows:
                bundle_handle, bundle_identity = _open_windows_directory_no_follow(self.root)
                reopened.append(bundle_handle)
                runtime_handle, runtime_identity = _open_windows_directory_relative(
                    bundle_handle,
                    "runtime",
                )
                reopened.append(runtime_handle)
                evidence_handle, evidence_identity = _open_windows_directory_relative(
                    runtime_handle,
                    "backup-restore",
                )
                reopened.append(evidence_handle)
                archive_handle, archive_identity = _open_windows_directory_relative(
                    evidence_handle,
                    _BACKUP_RESTORE_SOURCE_ARCHIVE.name,
                )
                reopened.append(archive_handle)
            else:
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                bundle_handle = _open_posix_directory_no_follow(self.root)
                reopened.append(bundle_handle)
                bundle_identity = _file_identity(os.fstat(bundle_handle))
                runtime_handle = os.open("runtime", directory_flags, dir_fd=bundle_handle)
                reopened.append(runtime_handle)
                runtime_identity = _file_identity(os.fstat(runtime_handle))
                evidence_handle = os.open(
                    "backup-restore",
                    directory_flags,
                    dir_fd=runtime_handle,
                )
                reopened.append(evidence_handle)
                evidence_identity = _file_identity(os.fstat(evidence_handle))
                archive_handle = os.open(
                    _BACKUP_RESTORE_SOURCE_ARCHIVE.name,
                    directory_flags,
                    dir_fd=evidence_handle,
                )
                reopened.append(archive_handle)
                archive_identity = _file_identity(os.fstat(archive_handle))
            if (
                bundle_identity,
                runtime_identity,
                evidence_identity,
                archive_identity,
            ) != self.directory_identities:
                raise ValueError("backup restore evidence directory identity changed")
            current_entries = (
                _windows_directory_entry_names(evidence_handle)
                if self.windows
                else set(os.listdir(int(evidence_handle)))
            )
            if current_entries != _BACKUP_RESTORE_ENTRY_NAMES:
                raise ValueError("backup restore evidence artifact set changed")
            for name in _BACKUP_RESTORE_JSON_ARTIFACTS:
                if self.windows:
                    artifact_handle, identity = _open_windows_regular_file_relative(
                        evidence_handle,
                        name,
                        share_access=0x00000001 | 0x00000002 | 0x00000004,
                    )
                else:
                    file_flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
                    artifact_handle = os.open(name, file_flags, dir_fd=int(evidence_handle))
                    details = os.fstat(artifact_handle)
                    if not stat.S_ISREG(details.st_mode):
                        raise ValueError("backup restore evidence artifact type changed")
                    identity = _file_identity(details)
                reopened.append(artifact_handle)
                if identity != self.artifact_identities[name] or (
                    _read_runtime_artifact_handle(artifact_handle, windows=self.windows)
                    != self.artifact_bodies[name]
                ):
                    raise ValueError("backup restore evidence artifact identity changed")
        except (OSError, ValueError) as exc:
            raise ValueError("backup restore evidence boundary changed during replay") from exc
        finally:
            for handle in reversed(reopened):
                if self.windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))

    def close(self) -> None:
        errors: list[OSError] = []
        for handle in (*self.artifact_handles.values(), *reversed(self.directory_handles)):
            try:
                if self.windows:
                    _close_windows_handle(handle)
                else:
                    os.close(int(handle))
            except OSError as exc:
                errors.append(exc)
        self.artifact_handles.clear()
        self.directory_handles = ()  # type: ignore[assignment]
        if errors:
            raise errors[0]


def _fixed_backup_restore_path(bundle_root: Path, relative: Path) -> Path:
    root = Path(os.path.abspath(bundle_root))
    unresolved = root / relative
    if not root.is_dir():
        raise ValueError("backup restore evidence bundle is unavailable")
    cursor = unresolved
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("backup restore evidence must not use a symlink")
        cursor = cursor.parent
    try:
        resolved_root = root.resolve(strict=True)
        resolved = unresolved.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError):
        raise ValueError("backup restore evidence is outside the fixed bundle path") from None
    if resolved != resolved_root / relative:
        raise ValueError("backup restore evidence is outside the fixed bundle path")
    return resolved


def read_backup_restore_report_artifact(
    path: Path,
    *,
    bundle_root: Path,
) -> tuple[bytes, str]:
    """Read the canonical report from its fixed probe output path."""

    root = Path(os.path.abspath(bundle_root))
    unresolved = Path(path)
    if not unresolved.is_absolute():
        unresolved = root / unresolved
    unresolved = Path(os.path.abspath(unresolved))
    expected = root / _BACKUP_RESTORE_REPORT
    if unresolved != expected:
        raise ValueError("backup restore report is outside the fixed evidence path")
    try:
        with _BackupRestoreArtifactLease.open(root) as lease:
            body = lease.body(_BACKUP_RESTORE_REPORT.name)
            lease.assert_unchanged()
    except (OSError, ValueError):
        raise ValueError("backup restore report is unavailable or invalid") from None
    if not body or len(body) > MAX_BACKUP_RESTORE_REPORT_BYTES:
        raise ValueError("backup restore report is unavailable or invalid")
    return body, hashlib.sha256(body).hexdigest()


def _backup_restore_lease_body(
    lease: _BackupRestoreArtifactLease,
    *,
    name: str,
    expected_sha256: object,
    label: str,
    max_bytes: int,
) -> bytes:
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise ValueError(f"{label} digest is invalid")
    body = lease.body(name)
    if not body or len(body) > max_bytes:
        raise ValueError(f"{label} byte length is invalid")
    if hashlib.sha256(body).hexdigest() != expected_sha256:
        raise ValueError(f"{label} digest does not match")
    return body


def _replay_backup_restore_artifacts(
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    expected_report_sha256: str | None,
    expected_database_ownership: str,
    expected_object_namespace_ownership: str,
) -> tuple[bytes, str, dict[str, bool], str]:
    """Replay one backup/restore proof through a single held no-follow boundary."""

    _runtime_candidate_contract(candidate_root, candidate=candidate)
    root = Path(os.path.abspath(bundle_root))
    try:
        with _BackupRestoreArtifactLease.open(root) as lease:
            report_body = lease.body(_BACKUP_RESTORE_REPORT.name)
            report_sha256 = hashlib.sha256(report_body).hexdigest()
            if expected_report_sha256 is not None and (
                _SHA256.fullmatch(expected_report_sha256) is None
                or report_sha256 != expected_report_sha256
            ):
                raise ValueError("backup restore report digest does not match")
            try:
                untrusted_report = json.loads(report_body)
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError("backup restore report JSON is invalid") from None
            if not isinstance(untrusted_report, dict):
                raise ValueError("backup restore report schema is invalid")
            execution = untrusted_report.get("execution")
            artifact_sha256s = (
                execution.get("artifactSha256s") if isinstance(execution, dict) else None
            )
            if not isinstance(artifact_sha256s, dict):
                raise ValueError("backup restore command artifact hashes are invalid")

            operator_body = _backup_restore_lease_body(
                lease,
                name="restore-validation.json",
                expected_sha256=artifact_sha256s.get("restoreValidation"),
                label="backup restore operator artifact",
                max_bytes=MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
            )
            source_provenance_body = _backup_restore_lease_body(
                lease,
                name="source-provenance.json",
                expected_sha256=artifact_sha256s.get("sourceProvenance"),
                label="backup restore source provenance",
                max_bytes=MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
            )
            target_config_body = _backup_restore_lease_body(
                lease,
                name="target-config.snapshot.json",
                expected_sha256=artifact_sha256s.get("targetConfigSnapshot"),
                label="backup restore target config",
                max_bytes=MAX_RESTORE_VALIDATION_ARTIFACT_BYTES,
            )
            provisioning_receipt_body = _backup_restore_lease_body(
                lease,
                name="target-provisioning-receipt.json",
                expected_sha256=artifact_sha256s.get("targetProvisioningReceipt"),
                label="backup restore target provisioning receipt",
                max_bytes=MAX_TARGET_PROVISIONING_RECEIPT_BYTES,
            )
            try:
                with tempfile.TemporaryDirectory(
                    prefix="yfeistai-backup-restore-verifier-"
                ) as temporary:
                    snapshot_parent = Path(temporary).resolve(strict=True)
                    if snapshot_parent == root or snapshot_parent.is_relative_to(root):
                        raise ValueError("backup restore verifier snapshot boundary is invalid")
                    source_snapshot = _snapshot_source_archive(
                        lease.source_archive_path,
                        snapshot_parent,
                    )
                    lease.assert_unchanged()
                    verified_backup = load_verified_backup(source_snapshot.directory)
                    _require_backup_uses_snapshot(verified_backup, source_snapshot)
                    _verify_source_archive_snapshot(source_snapshot)
                    verified_backup = reverify_verified_backup(verified_backup)
                    _require_backup_uses_snapshot(verified_backup, source_snapshot)
                    _verify_source_archive_snapshot(source_snapshot)

                    parsed = parse_backup_restore_report(
                        report_body,
                        candidate=candidate,
                        release_run=release_run,
                        expected_source_manifest_sha256=verified_backup.manifest_sha256,
                        expected_source_archive_fingerprint_sha256=(
                            verified_backup.archive_fingerprint_sha256
                        ),
                        expected_database_ownership=expected_database_ownership,
                        expected_object_namespace_ownership=(expected_object_namespace_ownership),
                        operator_artifact_body=operator_body,
                        verified_backup=verified_backup,
                        source_provenance_body=source_provenance_body,
                        target_config_body=target_config_body,
                        provisioning_receipt_body=provisioning_receipt_body,
                    )
                    _verify_source_archive_snapshot(source_snapshot)
                    lease.assert_unchanged()
            except ValueError:
                raise
            except OSError as exc:
                raise ValueError("backup restore evidence boundary changed during replay") from exc

            checks = derive_backup_restore_checks(parsed)
            observed_at = parsed.get("observedAt")
            if not isinstance(observed_at, str):
                raise ValueError("backup restore observation time is invalid")
            lease.assert_unchanged()
            return report_body, report_sha256, checks, observed_at
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("backup restore evidence boundary changed during replay") from exc


def derive_backup_restore_receipt_checks(
    report_body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, object],
    expected_database_ownership: str,
    expected_object_namespace_ownership: str,
) -> tuple[dict[str, bool], str]:
    """Replay the fixed backup/restore artifacts and derive the formal receipt."""
    replayed_body, _report_sha256, checks, observed_at = _replay_backup_restore_artifacts(
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
        expected_report_sha256=hashlib.sha256(report_body).hexdigest(),
        expected_database_ownership=expected_database_ownership,
        expected_object_namespace_ownership=(expected_object_namespace_ownership),
    )
    if replayed_body != report_body:
        raise ValueError("backup restore report changed during replay")
    return checks, observed_at


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


def _open_windows_directory_no_follow(
    path: Path,
    *,
    deletable: bool = False,
) -> tuple[object, tuple[int, int]]:
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.anchor:
        raise ValueError("runtime evidence bundle path is invalid")
    current, identity = _open_windows_directory_handle(
        Path(candidate.anchor),
        deletable=deletable,
    )
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


@dataclass(slots=True)
class _EvidenceBundleBoundary:
    root: Path
    windows: bool
    handle: object | int
    identity: tuple[int, int]

    @classmethod
    def open(cls, root: Path) -> _EvidenceBundleBoundary:
        path = Path(os.path.abspath(root))
        try:
            if os.name == "nt":
                handle, identity = _open_windows_directory_no_follow(path, deletable=True)
            else:
                handle = _open_posix_directory_no_follow(path)
                identity = _file_identity(os.fstat(handle))
        except (OSError, ValueError) as exc:
            raise ValueError("evidence bundle boundary cannot be opened") from exc
        return cls(path, os.name == "nt", handle, identity)

    def assert_unchanged(self) -> None:
        reopened: object | int | None = None
        try:
            held_identity = (
                _windows_handle_identity(self.handle, directory=True)
                if self.windows
                else _file_identity(os.fstat(int(self.handle)))
            )
            if self.windows:
                reopened, reopened_identity = _open_windows_directory_no_follow(
                    self.root,
                    deletable=True,
                )
            else:
                reopened = _open_posix_directory_no_follow(self.root)
                reopened_identity = _file_identity(os.fstat(reopened))
            if held_identity != self.identity or reopened_identity != self.identity:
                raise ValueError("evidence bundle boundary changed")
        except (OSError, ValueError) as exc:
            raise ValueError("evidence bundle boundary changed during receipt replay") from exc
        finally:
            if reopened is not None:
                if self.windows:
                    _close_windows_handle(reopened)
                else:
                    os.close(int(reopened))

    def close(self) -> None:
        if self.windows:
            _close_windows_handle(self.handle)
        else:
            os.close(int(self.handle))


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
        "openmaic-dedicated-plane-attestation.json",
        "openmaic-dedicated-outage-attestation.json",
        "gateway-only-public-attestation.json",
        "gateway-external-observer-attestation.json",
        "gateway-observer-trust-envelope.json",
        "gateway-host-provisioner-trust-envelope.json",
        "gateway-docker-host-provisioning-receipt.json",
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


def _runtime_docker_host_observation(endpoint_stdout: str, info_stdout: str) -> dict[str, str]:
    try:
        endpoint = json.loads(endpoint_stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("runtime attestation Docker daemon endpoint is invalid") from exc
    daemon = _runtime_json_object(info_stdout, label="Docker info")
    if (
        not isinstance(endpoint, str)
        or not endpoint
        or endpoint != endpoint.strip()
        or set(daemon) != {"serverId", "osType"}
        or not isinstance(daemon.get("serverId"), str)
        or not daemon["serverId"]
        or daemon["serverId"] != daemon["serverId"].strip()
        or not isinstance(daemon.get("osType"), str)
        or not daemon["osType"]
        or daemon["osType"] != daemon["osType"].strip()
    ):
        raise ValueError("runtime attestation Docker daemon identity is invalid")
    return {
        "context": _GATEWAY_DOCKER_CONTEXT,
        "endpoint": endpoint,
        "serverId": daemon["serverId"],
        "osType": daemon["osType"],
    }


def _runtime_container_fact(stdout: str, *, container_id: str) -> dict[str, object]:
    raw = _runtime_json_object(stdout, label="container inspect")
    try:
        fact = _producer_runtime_container_fact(raw)
    except ValueError as exc:
        raise ValueError("runtime attestation container inspect stdout is invalid") from exc
    if fact.get("containerId") != container_id:
        raise ValueError("runtime attestation container inspect stdout is invalid")
    return fact


def _runtime_image_fact(stdout: str, *, reference: str) -> dict[str, object]:
    raw = _runtime_json_object(stdout, label="image inspect")
    repo_digests = raw.get("repoDigests")
    environment_hashes = raw.get("environmentHashes")
    volumes = raw.get("volumes")
    command = raw.get("command")
    entrypoint = raw.get("entrypoint")
    user = raw.get("user")
    if (
        set(raw)
        != {
            "imageId",
            "repoDigests",
            "command",
            "entrypoint",
            "user",
            "environmentHashes",
            "volumes",
        }
        or not isinstance(raw.get("imageId"), str)
        or not raw["imageId"]
        or not isinstance(repo_digests, list)
        or not all(isinstance(value, str) for value in repo_digests)
        or (command is not None and not isinstance(command, list))
        or isinstance(command, list)
        and not all(isinstance(value, str) for value in command)
        or (entrypoint is not None and not isinstance(entrypoint, list))
        or isinstance(entrypoint, list)
        and not all(isinstance(value, str) for value in entrypoint)
        or not isinstance(user, str)
        or not isinstance(environment_hashes, dict)
        or not all(
            isinstance(name, str)
            and name
            and isinstance(value, str)
            and _SHA256.fullmatch(value) is not None
            for name, value in environment_hashes.items()
        )
        or (volumes is not None and not isinstance(volumes, dict))
    ):
        raise ValueError("runtime attestation image inspect stdout is invalid")
    return {
        "id": raw["imageId"],
        "repoDigests": repo_digests,
        "command": command,
        "entrypoint": entrypoint,
        "user": user,
        "environmentHashes": dict(sorted(environment_hashes.items())),
        "volumes": volumes,
        "reference": reference,
    }


def _runtime_rebuild_containers(
    facts: list[dict[str, object]],
    *,
    expected_services: Mapping[str, Mapping[str, str]],
    image_facts: Mapping[str, Mapping[str, object]],
    compose_hashes: Mapping[str, str],
    compose_security: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    try:
        _validate_runtime_container_facts(
            facts,
            expected_services=expected_services,
            image_facts=image_facts,
            compose_hashes=compose_hashes,
            compose_security=compose_security,
        )
    except ValueError as exc:
        raise ValueError("runtime attestation container facts are invalid") from exc
    containers: list[dict[str, object]] = []
    for fact in sorted(facts, key=lambda item: str(item["service"])):
        service = fact["service"]
        assert isinstance(service, str)
        expected = expected_services[service]
        reference = expected["image"]
        image = image_facts.get(reference)
        if not isinstance(image, Mapping):
            raise ValueError("runtime attestation container identity is invalid")
        containers.append(
            {
                **fact,
                "imageId": image["id"],
                "repoDigests": image["repoDigests"],
            }
        )
    snapshot = _producer_runtime_snapshot(facts)
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
    expected_docker_host_identity_sha256: str | None = None,
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
    document_keys = set(document) if isinstance(document, dict) else set()
    runtime_host_identity = (
        document.get("dockerHostIdentity") if isinstance(document, dict) else None
    )
    host_identity_required = expected_docker_host_identity_sha256 is not None
    if host_identity_required:
        required_keys.add("dockerHostIdentity")
    elif "dockerHostIdentity" in document_keys:
        required_keys.add("dockerHostIdentity")
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
    if "dockerHostIdentity" in required_keys:
        if (
            not isinstance(runtime_host_identity, dict)
            or set(runtime_host_identity)
            != {
                "context",
                "endpoint",
                "serverId",
                "dockerHostIdentitySha256",
            }
            or runtime_host_identity.get("context") != _GATEWAY_DOCKER_CONTEXT
            or not isinstance(runtime_host_identity.get("endpoint"), str)
            or not runtime_host_identity["endpoint"]
            or runtime_host_identity["endpoint"] != runtime_host_identity["endpoint"].strip()
            or not isinstance(runtime_host_identity.get("serverId"), str)
            or not runtime_host_identity["serverId"]
            or runtime_host_identity["serverId"] != runtime_host_identity["serverId"].strip()
            or not isinstance(runtime_host_identity.get("dockerHostIdentitySha256"), str)
            or _SHA256.fullmatch(runtime_host_identity["dockerHostIdentitySha256"]) is None
            or runtime_host_identity["dockerHostIdentitySha256"] == "0" * 64
            or (
                expected_docker_host_identity_sha256 is not None
                and (
                    not isinstance(expected_docker_host_identity_sha256, str)
                    or _SHA256.fullmatch(expected_docker_host_identity_sha256) is None
                    or expected_docker_host_identity_sha256 == "0" * 64
                    or runtime_host_identity["dockerHostIdentitySha256"]
                    != expected_docker_host_identity_sha256
                )
            )
        ):
            raise ValueError("gateway Docker host identity is invalid")
    candidate_services = _runtime_candidate_contract(candidate_root, candidate=candidate)
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

    compose_security = _runtime_json_object(
        consume(list(_RUNTIME_COMPOSE_CONFIG_ARGUMENTS)),
        label="Compose security projection",
    )
    try:
        expected_services = _runtime_merged_expected_services(compose_security, candidate_services)
        compose_hashes = _runtime_compose_hashes(
            consume(list(_RUNTIME_COMPOSE_HASH_ARGUMENTS)).encode("utf-8")
        )
    except ValueError as exc:
        raise ValueError("runtime attestation Compose provenance is invalid") from exc
    if not set(expected_services).issubset(compose_hashes):
        raise ValueError("runtime attestation Compose hashes do not cover candidate services")

    before_host = None
    if "dockerHostIdentity" in required_keys:
        before_host = _runtime_docker_host_observation(
            consume(list(_GATEWAY_DOCKER_CONTEXT_ARGUMENTS)),
            consume(list(_GATEWAY_DOCKER_INFO_ARGUMENTS)),
        )
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
            consume(["image", "inspect", "--format", _RUNTIME_IMAGE_FORMAT, reference]),
            reference=reference,
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
    after_host = None
    if "dockerHostIdentity" in required_keys:
        after_host = _runtime_docker_host_observation(
            consume(list(_GATEWAY_DOCKER_CONTEXT_ARGUMENTS)),
            consume(list(_GATEWAY_DOCKER_INFO_ARGUMENTS)),
        )
    if cursor != len(commands):
        raise ValueError("runtime attestation command records are invalid")
    before_containers, before_snapshot = _runtime_rebuild_containers(
        before_facts,
        expected_services=expected_services,
        image_facts=image_facts,
        compose_hashes=compose_hashes,
        compose_security=compose_security,
    )
    after_containers, after_snapshot = _runtime_rebuild_containers(
        after_facts,
        expected_services=expected_services,
        image_facts=image_facts,
        compose_hashes=compose_hashes,
        compose_security=compose_security,
    )
    if (
        before_containers != after_containers
        or document.get("beforeSnapshot") != before_snapshot
        or document.get("afterSnapshot") != after_snapshot
        or document.get("containers") != after_containers
    ):
        raise ValueError("runtime attestation summaries do not match replayed Docker stdout")
    if before_host != after_host:
        raise ValueError("runtime attestation Docker host identity changed")
    if before_host is not None and (
        not isinstance(runtime_host_identity, dict)
        or runtime_host_identity.get("context") != before_host["context"]
        or runtime_host_identity.get("endpoint") != before_host["endpoint"]
        or runtime_host_identity.get("serverId") != before_host["serverId"]
    ):
        raise ValueError("gateway Docker host identity is invalid")
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


def _derive_openmaic_plane_receipt_checks(
    body: bytes,
    *,
    plane: str,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay one fixed OpenMAIC plane smoke proof into receipt checks."""

    if plane == "shared":
        evidence = "openmaic_shared_plane"
        expected_command = openmaic_shared_plane_command_record()
        derive_checks = derive_openmaic_shared_plane_checks
        expected_check_names = {"sharedGenerationPassed"}
    elif plane == "dedicated":
        evidence = "openmaic_dedicated_plane"
        expected_command = openmaic_dedicated_plane_command_record()
        derive_checks = derive_openmaic_dedicated_plane_checks
        expected_check_names = {"dedicatedGenerationPassed", "noSharedClientIssued"}
    else:
        raise ValueError("OpenMAIC evidence plane is invalid")
    label = f"OpenMAIC {plane} plane"

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} execution proof is invalid") from exc
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
        raise ValueError(f"{label} execution proof is invalid")
    base_url = _runtime_base_url(document.get("baseUrl"))
    if base_url is None:
        raise ValueError(f"{label} execution proof is invalid")

    runtime_proof = document.get("runtimeAttestation")
    if (
        not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
    ):
        raise ValueError(f"{label} runtime attestation proof is invalid")
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
        raise ValueError(f"{label} runtime attestation is invalid: {exc}") from exc

    execution = document.get("execution")
    if (
        not isinstance(execution, dict)
        or set(execution) != {"command", "nativeExit", "stdout", "stdoutSha256", "stderr"}
        or execution.get("command") != expected_command
        or type(execution.get("nativeExit")) is not int
        or execution.get("nativeExit") != 0
        or execution.get("stderr") != ""
        or not isinstance(execution.get("stdout"), str)
        or not isinstance(execution.get("stdoutSha256"), str)
    ):
        raise ValueError(f"{label} execution proof is invalid")
    try:
        stdout = execution["stdout"].encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} execution proof is invalid") from exc
    if (
        len(stdout) > MAX_OPENMAIC_SMOKE_REPORT_BYTES
        or hashlib.sha256(stdout).hexdigest() != execution["stdoutSha256"]
    ):
        raise ValueError(f"{label} execution proof is invalid")
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
            expected_plane=plane,
        )
    except ValueError as exc:
        raise ValueError(f"{label} strict report is invalid: {exc}") from exc
    if report.get("observedAt") != document.get("observedAt"):
        raise ValueError(f"{label} proof timestamp does not match the report")
    checks = derive_checks(report)
    if set(checks) != expected_check_names or any(value is not True for value in checks.values()):
        raise ValueError(f"{label} proof checks did not all pass")
    summary = {
        "fixture": report.get("fixture"),
        "binding": report.get("binding"),
        "generation": report.get("generation"),
        "checks": checks,
    }
    if not exact_json_equal(document.get("summary"), summary):
        raise ValueError(f"{label} proof summary does not match the report")
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

    return _derive_openmaic_plane_receipt_checks(
        body,
        plane="shared",
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
    )


def derive_openmaic_dedicated_plane_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
) -> tuple[dict[str, bool], str]:
    """Replay one fixed dedicated-plane smoke proof into receipt checks."""

    return _derive_openmaic_plane_receipt_checks(
        body,
        plane="dedicated",
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
    )


def _replay_openmaic_dedicated_outage_attempt_marker(
    bundle_root: Path,
    reference: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_observer_attestation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_shared_ingress_control_origin: str,
    expected_tenant_id: str,
    expected_route_id: str,
    return_body: bool = False,
) -> dict[str, object] | tuple[dict[str, object], bytes]:
    """Read and replay the archived marker against caller-supplied trust anchors."""

    marker = _proof_bytes(
        bundle_root,
        reference,
        label="OpenMAIC dedicated outage attempt marker",
    )
    if isinstance(marker, str):
        raise ValueError(marker)
    marker_body = marker[0]
    parsed = parse_openmaic_dedicated_outage_attempt_marker(
        marker_body,
        candidate=candidate,
        release_run=release_run,
        expected_observer_attestation_sha256=expected_observer_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_shared_ingress_control_origin=expected_shared_ingress_control_origin,
        expected_tenant_id=expected_tenant_id,
        expected_route_id=expected_route_id,
    )
    if return_body:
        return parsed, marker_body
    return parsed


def derive_openmaic_dedicated_outage_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_tenant_id: str,
    expected_docker_host_identity_sha256: str,
    expected_openmaic_observer_attestation_sha256: str | None = None,
    expected_openmaic_observer_id: str | None = None,
    expected_openmaic_observer_origin: str | None = None,
    expected_openmaic_shared_ingress_control_origin: str | None = None,
) -> tuple[dict[str, bool], str]:
    """Replay the independent dedicated outage attestation."""

    observer_anchor = validate_openmaic_shared_ingress_observer_trust_anchor(
        expected_observer_attestation_sha256=(expected_openmaic_observer_attestation_sha256),
        expected_observer_id=expected_openmaic_observer_id,
        expected_observer_origin=expected_openmaic_observer_origin,
        expected_shared_ingress_control_origin=(expected_openmaic_shared_ingress_control_origin),
    )

    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("OpenMAIC dedicated outage execution proof is invalid") from exc
    if (
        not isinstance(document, dict)
        or _SHA256.fullmatch(expected_docker_host_identity_sha256) is None
        or expected_docker_host_identity_sha256 == "0" * 64
    ):
        raise ValueError("OpenMAIC dedicated outage execution proof is invalid")
    base_url = _runtime_base_url(document.get("baseUrl"))
    runtime_proof = document.get("runtimeAttestation")
    observer_proof = document.get("observerAttestation")
    if (
        base_url is None
        or not isinstance(runtime_proof, dict)
        or set(runtime_proof) != {"artifact", "sha256"}
        or runtime_proof.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_proof.get("sha256"), str)
        or not isinstance(observer_proof, dict)
        or set(observer_proof)
        != {
            "artifact",
            "sha256",
            "observerId",
            "observerOrigin",
            "sharedIngressControlOrigin",
        }
        or observer_proof.get("artifact")
        != "runtime/openmaic-shared-ingress-observer-attestation.json"
        or observer_proof.get("sha256") != observer_anchor["sha256"]
        or observer_proof.get("observerId") != observer_anchor["observerId"]
        or observer_proof.get("observerOrigin") != observer_anchor["observerOrigin"]
        or observer_proof.get("sharedIngressControlOrigin")
        != observer_anchor["sharedIngressControlOrigin"]
    ):
        raise ValueError("OpenMAIC dedicated outage execution proof is invalid")
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
        raise ValueError("OpenMAIC dedicated outage runtime attestation is invalid") from exc
    observer_body = _proof_bytes(
        bundle_root,
        {"artifact": observer_proof["artifact"], "sha256": observer_proof["sha256"]},
        label="OpenMAIC shared-ingress observer attestation",
    )
    if isinstance(observer_body, str):
        raise ValueError(observer_body)
    try:
        observer = parse_openmaic_shared_ingress_observer_attestation(
            observer_body[0],
            release_run=release_run,
        )
    except ValueError as exc:
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid") from exc
    observer_details = observer.get("observer")
    if not isinstance(observer_details, dict):
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid")
    observer_url = observer_details.get("observerUrl")
    control_url = observer_details.get("sharedIngressControlUrl")
    if not isinstance(observer_url, str) or not isinstance(control_url, str):
        raise ValueError("OpenMAIC shared-ingress observer attestation is invalid")
    observer_origin = urlsplit(observer_url)
    control_origin = urlsplit(control_url)
    observed_observer_origin = f"{observer_origin.scheme}://{observer_origin.netloc}"
    observed_control_origin = f"{control_origin.scheme}://{control_origin.netloc}"
    if (
        observer_proof.get("observerId") != observer_details.get("observerId")
        or observer_details.get("observerId") != observer_anchor["observerId"]
        or observed_observer_origin != observer_anchor["observerOrigin"]
        or observed_control_origin != observer_anchor["sharedIngressControlOrigin"]
    ):
        raise ValueError("OpenMAIC shared-ingress observer reference is invalid")
    fixture = document.get("fixture")
    provenance = document.get("provenance")
    outage = document.get("outage")
    marker_reference = fixture.get("attemptMarker") if isinstance(fixture, dict) else None
    if (
        not isinstance(provenance, dict)
        or provenance.get("attemptMarker") != marker_reference
        or not isinstance(outage, dict)
        or not isinstance(outage.get("routeId"), str)
    ):
        raise ValueError("OpenMAIC dedicated outage attempt marker reference is invalid")
    replayed_marker = _replay_openmaic_dedicated_outage_attempt_marker(
        bundle_root,
        marker_reference,
        candidate=candidate,
        release_run=release_run,
        expected_observer_attestation_sha256=observer_anchor["sha256"],
        expected_observer_id=observer_anchor["observerId"],
        expected_observer_origin=observer_anchor["observerOrigin"],
        expected_shared_ingress_control_origin=(observer_anchor["sharedIngressControlOrigin"]),
        expected_tenant_id=expected_tenant_id,
        expected_route_id=outage["routeId"],
        return_body=True,
    )
    if not isinstance(replayed_marker, tuple):
        raise ValueError("OpenMAIC dedicated outage attempt marker is invalid")
    _marker, marker_body = replayed_marker
    try:
        report = parse_openmaic_dedicated_outage_attestation(
            body,
            candidate=candidate,
            release_run=release_run,
            expected_base_url=base_url,
            expected_runtime_attestation_sha256=runtime_proof["sha256"],
            expected_observer_attestation_sha256=observer_anchor["sha256"],
            expected_observer_id=observer_anchor["observerId"],
            expected_observer_origin=observer_anchor["observerOrigin"],
            expected_shared_ingress_control_origin=(observer_anchor["sharedIngressControlOrigin"]),
            expected_tenant_id=expected_tenant_id,
            attempt_marker_body=marker_body,
            expected_docker_host_identity_sha256=(expected_docker_host_identity_sha256),
        )
    except ValueError as exc:
        raise ValueError(f"OpenMAIC dedicated outage strict report is invalid: {exc}") from exc
    checks = derive_openmaic_dedicated_outage_checks(report)
    if checks != {"noSharedFallback": True}:
        raise ValueError("OpenMAIC dedicated outage proof did not prove no fallback")
    return checks, str(report["observedAt"])


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


def _gateway_docker_host_identity_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None or value == "0" * 64:
        raise ValueError("gateway Docker host identity is unavailable or invalid")
    return value


def _gateway_runtime_host_identity(
    runtime: Mapping[str, object],
    *,
    expected_sha256: str,
) -> dict[str, str]:
    raw = runtime.get("dockerHostIdentity")
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {
            "context",
            "endpoint",
            "serverId",
            "dockerHostIdentitySha256",
        }
        or raw.get("context") != _GATEWAY_DOCKER_CONTEXT
        or not isinstance(raw.get("endpoint"), str)
        or not raw["endpoint"]
        or raw["endpoint"] != raw["endpoint"].strip()
        or not isinstance(raw.get("serverId"), str)
        or not raw["serverId"]
        or raw["serverId"] != raw["serverId"].strip()
        or raw.get("dockerHostIdentitySha256") != expected_sha256
    ):
        raise ValueError("gateway Docker host identity is invalid")
    return {
        "context": raw["context"],
        "endpoint": raw["endpoint"],
        "serverId": raw["serverId"],
        "dockerHostIdentitySha256": raw["dockerHostIdentitySha256"],
    }


def _gateway_runtime_container_map(runtime: Mapping[str, object]) -> dict[str, str]:
    rows = runtime.get("containers")
    if not isinstance(rows, list) or not rows:
        raise ValueError("gateway Docker container identity is invalid")
    containers: dict[str, str] = {}
    for row in rows:
        container_id = row.get("containerId") if isinstance(row, dict) else None
        service = row.get("service") if isinstance(row, dict) else None
        project = row.get("project") if isinstance(row, dict) else None
        if (
            not isinstance(container_id, str)
            or not container_id
            or not isinstance(service, str)
            or not service
            or project != _GATEWAY_DOCKER_PROJECT
            or container_id in containers
        ):
            raise ValueError("gateway Docker container identity is invalid")
        containers[container_id] = service
    if tuple(containers.values()).count("gateway") != 1:
        raise ValueError("gateway Docker container identity is invalid")
    return containers


def _gateway_candidate_service_networks(
    candidate_root: Path,
    *,
    candidate: Mapping[str, object],
    runtime_containers: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    root = Path(os.path.abspath(candidate_root))
    try:
        with _CandidateContractLease.open(root) as lease:
            loaded_candidate, _expected_services = _load_candidate_token(
                lease.token,
                expected_candidate=candidate,
            )
            expected_networks = parse_gateway_candidate_networks(
                lease.token[1],
                docker_project=_GATEWAY_DOCKER_PROJECT,
                expected_services=tuple(runtime_containers.values()),
            )
            lease.assert_unchanged()
    except (OSError, ValueError) as exc:
        raise ValueError("gateway candidate Compose network set is invalid") from exc
    if loaded_candidate != candidate:
        raise ValueError("gateway candidate Compose network set is invalid")
    return expected_networks


def _gateway_replayed_command(
    record: object,
    *,
    arguments: list[str],
) -> str:
    expected_argv = [
        "docker",
        "--config",
        "<isolated-docker-config>",
        "--context",
        _GATEWAY_DOCKER_CONTEXT,
        *arguments,
    ]
    if (
        not isinstance(record, dict)
        or set(record) != {"argv", "nativeExit", "stdout", "stdoutSha256"}
        or record.get("argv") != expected_argv
        or type(record.get("nativeExit")) is not int
        or record.get("nativeExit") != 0
        or not isinstance(record.get("stdout"), str)
    ):
        raise ValueError("gateway Docker observation commands are invalid")
    stdout = record["stdout"]
    try:
        body = stdout.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("gateway Docker observation commands are invalid") from exc
    if (
        len(body) > MAX_RUNTIME_ARTIFACT_BYTES
        or record.get("stdoutSha256") != hashlib.sha256(body).hexdigest()
    ):
        raise ValueError("gateway Docker observation commands are invalid")
    return stdout


def _gateway_replayed_ports(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        raise ValueError("gateway Docker published ports are invalid")
    published: list[dict[str, object]] = []
    for target, bindings in value.items():
        match = re.fullmatch(r"([1-9][0-9]{0,4})/(tcp|udp)", str(target))
        if match is None:
            raise ValueError("gateway Docker published ports are invalid")
        if bindings is None:
            continue
        if not isinstance(bindings, list):
            raise ValueError("gateway Docker published ports are invalid")
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"HostIp", "HostPort"}:
                raise ValueError("gateway Docker published ports are invalid")
            host_ip = binding.get("HostIp")
            host_port = binding.get("HostPort")
            try:
                parsed_host_port = int(host_port)
            except (TypeError, ValueError):
                raise ValueError("gateway Docker published ports are invalid") from None
            if not isinstance(host_ip, str) or not host_ip or not 1 <= parsed_host_port <= 65535:
                raise ValueError("gateway Docker published ports are invalid")
            published.append(
                {
                    "containerPort": int(match.group(1)),
                    "hostIp": host_ip,
                    "hostPort": parsed_host_port,
                    "protocol": match.group(2),
                }
            )
    return sorted(
        published,
        key=lambda row: (
            row["containerPort"],
            row["hostIp"],
            row["hostPort"],
            row["protocol"],
        ),
    )


def _gateway_replayed_networks(value: object) -> list[str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("gateway Docker network set is invalid")
    if any(
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or not isinstance(details, dict)
        for name, details in value.items()
    ):
        raise ValueError("gateway Docker network set is invalid")
    return sorted(value)


def _replay_gateway_docker_round(
    records: list[object],
    *,
    runtime_containers: Mapping[str, str],
    expected_service_networks: Mapping[str, tuple[str, ...]],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    cursor = 0
    try:
        endpoint = json.loads(
            _gateway_replayed_command(
                records[cursor],
                arguments=_GATEWAY_DOCKER_CONTEXT_ARGUMENTS,
            )
        )
        cursor += 1
        daemon = json.loads(
            _gateway_replayed_command(
                records[cursor],
                arguments=_GATEWAY_DOCKER_INFO_ARGUMENTS,
            )
        )
        cursor += 1
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("gateway Docker observation commands are invalid") from exc
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise ValueError("gateway Docker daemon endpoint is invalid")
    if (
        not isinstance(daemon, dict)
        or set(daemon) != {"serverId", "osType"}
        or not isinstance(daemon.get("serverId"), str)
        or not daemon["serverId"]
        or daemon.get("osType") != "linux"
    ):
        raise ValueError("gateway Docker daemon identity is invalid")
    try:
        ps_stdout = _gateway_replayed_command(
            records[cursor],
            arguments=_GATEWAY_DOCKER_PS_ARGUMENTS,
        )
        cursor += 1
        observed_ids = [json.loads(line) for line in ps_stdout.splitlines()]
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("gateway Docker observation commands are invalid") from exc
    expected_ids = sorted(runtime_containers)
    if observed_ids != expected_ids:
        raise ValueError("gateway Docker container identity is invalid")
    snapshot: list[dict[str, object]] = []
    for container_id in expected_ids:
        arguments = [
            "container",
            "inspect",
            "--format",
            _GATEWAY_DOCKER_INSPECT_FORMAT,
            container_id,
        ]
        try:
            row = json.loads(_gateway_replayed_command(records[cursor], arguments=arguments))
        except (IndexError, json.JSONDecodeError) as exc:
            raise ValueError("gateway Docker observation commands are invalid") from exc
        cursor += 1
        service = runtime_containers[container_id]
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "containerId",
                "project",
                "service",
                "networkMode",
                "networks",
                "publishedPorts",
            }
            or row.get("containerId") != container_id
            or row.get("project") != _GATEWAY_DOCKER_PROJECT
            or row.get("service") != service
        ):
            raise ValueError("gateway Docker container identity is invalid")
        expected_networks = expected_service_networks.get(service)
        if not isinstance(expected_networks, tuple) or not expected_networks:
            raise ValueError("gateway candidate Compose network set is invalid")
        network_mode = row.get("networkMode")
        if not isinstance(network_mode, str) or network_mode not in expected_networks:
            raise ValueError("gateway Docker network mode is invalid")
        networks = _gateway_replayed_networks(row.get("networks"))
        if networks != sorted(expected_networks):
            raise ValueError("gateway Docker network set is invalid")
        ports = _gateway_replayed_ports(row.get("publishedPorts"))
        expected_ports = (
            [
                {
                    "containerPort": port,
                    "hostIp": "0.0.0.0",
                    "hostPort": port,
                    "protocol": "tcp",
                }
                for port in (80, 443)
            ]
            if service == "gateway"
            else []
        )
        if ports != expected_ports:
            raise ValueError("gateway Docker published ports are invalid")
        snapshot.append(
            {
                "containerId": container_id,
                "project": _GATEWAY_DOCKER_PROJECT,
                "service": service,
                "networkMode": network_mode,
                "networks": networks,
                "publishedPorts": ports,
            }
        )
    try:
        endpoint_after = json.loads(
            _gateway_replayed_command(
                records[cursor],
                arguments=_GATEWAY_DOCKER_CONTEXT_ARGUMENTS,
            )
        )
        cursor += 1
        daemon_after = json.loads(
            _gateway_replayed_command(
                records[cursor],
                arguments=_GATEWAY_DOCKER_INFO_ARGUMENTS,
            )
        )
        cursor += 1
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("gateway Docker observation commands are invalid") from exc
    if endpoint_after != endpoint or daemon_after != daemon:
        raise ValueError("gateway Docker daemon identity changed during observation round")
    if cursor != len(records):
        raise ValueError("gateway Docker observation commands are invalid")
    return (
        {
            "context": _GATEWAY_DOCKER_CONTEXT,
            "endpoint": endpoint,
            "serverId": daemon["serverId"],
            "osType": daemon["osType"],
        },
        snapshot,
    )


def _validate_gateway_stored_snapshot(value: object, expected: list[dict[str, object]]) -> None:
    if not isinstance(value, list) or len(value) != len(expected):
        raise ValueError("gateway Docker container identity is invalid")
    for stored, replayed in zip(value, expected, strict=True):
        if not isinstance(stored, dict):
            raise ValueError("gateway Docker container identity is invalid")
        identity_keys = {"containerId", "project", "service"}
        if any(stored.get(key) != replayed[key] for key in identity_keys):
            raise ValueError("gateway Docker container identity is invalid")
        if stored.get("networkMode") != replayed["networkMode"]:
            raise ValueError("gateway Docker network mode is invalid")
        if stored.get("networks") != replayed["networks"]:
            raise ValueError("gateway Docker network set is invalid")
        if stored.get("publishedPorts") != replayed["publishedPorts"]:
            raise ValueError("gateway Docker published ports are invalid")
        if set(stored) != {
            "containerId",
            "project",
            "service",
            "networkMode",
            "networks",
            "publishedPorts",
        }:
            raise ValueError("gateway Docker container identity is invalid")


def derive_gateway_only_public_receipt_checks(
    body: bytes,
    *,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_docker_host_identity_sha256: str,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str,
    expected_observer_challenge: str,
    expected_host_challenge: str,
    trusted_now: str,
) -> tuple[dict[str, bool], str]:
    """Replay the external observation and both Docker rounds from fixed proof."""

    expected_host_identity_sha256 = _gateway_docker_host_identity_sha256(
        expected_docker_host_identity_sha256
    )
    try:
        proof = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("gateway-only-public execution proof is missing or invalid") from exc
    expected_fields = {
        "schemaVersion",
        "candidate",
        "releaseRun",
        "observedAt",
        "baseUrl",
        "runtimeAttestation",
        "observerAttestation",
        "trustPair",
        "externalObservation",
        "docker",
        "summary",
    }
    if (
        not isinstance(proof, dict)
        or set(proof) != expected_fields
        or type(proof.get("schemaVersion")) is not int
        or proof.get("schemaVersion") != 1
        or not exact_json_equal(proof.get("candidate"), dict(candidate))
    ):
        raise ValueError("gateway public candidate binding is invalid")
    if not exact_json_equal(proof.get("releaseRun"), dict(release_run)):
        raise ValueError("gateway public run binding is invalid")
    observed_at = proof.get("observedAt")
    base_url = proof.get("baseUrl")
    if not isinstance(observed_at, str) or not FileReleaseRuntime._valid_observed_at(observed_at):
        raise ValueError("gateway public observation timestamp is invalid")
    if _runtime_base_url(base_url) is None:
        raise ValueError("gateway public candidate origin binding is invalid")

    runtime_reference = proof.get("runtimeAttestation")
    if (
        not isinstance(runtime_reference, dict)
        or set(runtime_reference) != {"artifact", "sha256"}
        or runtime_reference.get("artifact") != "runtime/runtime-attestation.json"
        or not isinstance(runtime_reference.get("sha256"), str)
    ):
        raise ValueError("gateway public runtime attestation binding is invalid")
    observer_reference = proof.get("observerAttestation")
    if (
        not isinstance(observer_reference, dict)
        or set(observer_reference) != {"artifact", "sha256"}
        or observer_reference.get("artifact")
        != "runtime/gateway-external-observer-attestation.json"
    ):
        raise ValueError("gateway external observer attestation reference is invalid")
    observer_body = _proof_bytes(
        bundle_root,
        observer_reference,
        label="gateway external observer attestation",
    )
    if isinstance(observer_body, str):
        raise ValueError(observer_body)
    trust_references = proof.get("trustPair")
    fixed_trust_artifacts = {
        "observerEnvelope": "runtime/gateway-observer-trust-envelope.json",
        "hostProvisionerEnvelope": ("runtime/gateway-host-provisioner-trust-envelope.json"),
        "hostProvisioningReceipt": ("runtime/gateway-docker-host-provisioning-receipt.json"),
    }
    if not isinstance(trust_references, dict) or set(trust_references) != set(
        fixed_trust_artifacts
    ):
        raise ValueError("gateway trust pair references are invalid")
    trust_bodies: dict[str, bytes] = {}
    for name, artifact in fixed_trust_artifacts.items():
        reference = trust_references.get(name)
        if not isinstance(reference, dict) or reference.get("artifact") != artifact:
            raise ValueError("gateway trust pair reference is invalid")
        loaded = _proof_bytes(
            bundle_root,
            reference,
            label=f"gateway trust {name}",
        )
        if isinstance(loaded, str):
            raise ValueError(loaded)
        trust_bodies[name] = loaded[0]
    external_reference = proof.get("externalObservation")
    if (
        not isinstance(external_reference, dict)
        or set(external_reference) != {"artifact", "sha256"}
        or external_reference.get("artifact") != "raw/gateway-public-observation.json"
        or not isinstance(external_reference.get("sha256"), str)
    ):
        raise ValueError("gateway external observation reference is invalid")
    with _GatewayRawProofLease.open(
        bundle_root,
        expected_sha256=external_reference["sha256"],
    ) as external_lease:
        return _derive_gateway_only_public_receipt_checks_bound(
            external_lease,
            proof=proof,
            observer_body=observer_body[0],
            observer_envelope_body=trust_bodies["observerEnvelope"],
            host_envelope_body=trust_bodies["hostProvisionerEnvelope"],
            host_receipt_body=trust_bodies["hostProvisioningReceipt"],
            runtime_reference=runtime_reference,
            observed_at=observed_at,
            base_url=base_url,
            bundle_root=bundle_root,
            candidate_root=candidate_root,
            candidate=candidate,
            release_run=release_run,
            expected_host_identity_sha256=expected_host_identity_sha256,
            trusted_keyring_path=trusted_keyring_path,
            expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
            expected_observer_challenge=expected_observer_challenge,
            expected_host_challenge=expected_host_challenge,
            trusted_now=trusted_now,
        )


def _derive_gateway_only_public_receipt_checks_bound(
    external_lease: _GatewayRawProofLease,
    *,
    proof: Mapping[str, object],
    observer_body: bytes,
    observer_envelope_body: bytes,
    host_envelope_body: bytes,
    host_receipt_body: bytes,
    runtime_reference: Mapping[str, object],
    observed_at: str,
    base_url: str,
    bundle_root: Path,
    candidate_root: Path,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_host_identity_sha256: str,
    trusted_keyring_path: Path,
    expected_trusted_keyring_sha256: str,
    expected_observer_challenge: str,
    expected_host_challenge: str,
    trusted_now: str,
) -> tuple[dict[str, bool], str]:
    environment_id = release_run.get("environmentId")
    if not isinstance(environment_id, str) or not environment_id:
        raise ValueError("gateway trust release environment is invalid")
    trust_pair = verify_gateway_trust_pair(
        observer_envelope_body=observer_envelope_body,
        host_envelope_body=host_envelope_body,
        observer_artifact_body=observer_body,
        host_receipt_body=host_receipt_body,
        trusted_keyring_path=trusted_keyring_path,
        expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
        expected_environment_id=environment_id,
        expected_observer_challenge=expected_observer_challenge,
        expected_host_challenge=expected_host_challenge,
        trusted_now=trusted_now,
    )
    host_receipt_sha256 = hashlib.sha256(host_receipt_body).hexdigest()
    host_receipt = trust_pair.get("hostReceipt")
    host = host_receipt.get("host") if isinstance(host_receipt, dict) else None
    if host_receipt_sha256 != expected_host_identity_sha256 or not isinstance(host, dict):
        raise ValueError("gateway trust host receipt does not match Docker host identity")
    runtime = validate_runtime_attestation(
        Path(bundle_root) / str(runtime_reference["artifact"]),
        bundle_root=bundle_root,
        candidate_root=candidate_root,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
        expected_sha256=runtime_reference["sha256"],
        expected_docker_host_identity_sha256=expected_host_identity_sha256,
    )
    runtime_host_identity = _gateway_runtime_host_identity(
        runtime,
        expected_sha256=expected_host_identity_sha256,
    )
    if runtime_host_identity["dockerHostIdentitySha256"] != host_receipt_sha256:
        raise ValueError("gateway trust host receipt does not match runtime identity")
    observer_policy = signed_gateway_observer_policy(
        observer_body,
        trust_pair["observer"],
    )
    report = parse_gateway_public_report(
        external_lease.body,
        observer_attestation_body=observer_body,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=base_url,
        expected_runtime_attestation_sha256=runtime_reference["sha256"],
        expected_observer_id=observer_policy["expected_observer_id"],
        expected_observer_origin=observer_policy["expected_observer_origin"],
        expected_attestation_sha256=observer_policy["expected_attestation_sha256"],
        trusted_now=trusted_now,
        run_started_at=observer_policy["run_started_at"],
        run_ended_at=observer_policy["run_ended_at"],
    )
    external_lease.assert_unchanged()
    if report.get("observedAt") != observed_at:
        raise ValueError("gateway external observation timestamp binding is invalid")
    checks = derive_gateway_public_checks(report)
    if checks != {"gatewayPublic": True, "internalPortsClosed": True}:
        raise ValueError("gateway external observation did not prove the fixed policy")

    docker = proof.get("docker")
    if not isinstance(docker, dict) or set(docker) != {
        "project",
        "daemon",
        "beforeSnapshot",
        "afterSnapshot",
        "commands",
    }:
        raise ValueError("gateway Docker observation proof is invalid")
    if docker.get("project") != _GATEWAY_DOCKER_PROJECT:
        raise ValueError("gateway Docker container identity is invalid")
    runtime_containers = _gateway_runtime_container_map(runtime)
    expected_service_networks = _gateway_candidate_service_networks(
        candidate_root,
        candidate=candidate,
        runtime_containers=runtime_containers,
    )
    commands = docker.get("commands")
    round_size = len(runtime_containers) + 5
    if not isinstance(commands, list) or len(commands) != round_size * 2:
        raise ValueError("gateway Docker observation commands are invalid")
    before_daemon, before_snapshot = _replay_gateway_docker_round(
        commands[:round_size],
        runtime_containers=runtime_containers,
        expected_service_networks=expected_service_networks,
    )
    after_daemon, after_snapshot = _replay_gateway_docker_round(
        commands[round_size:],
        runtime_containers=runtime_containers,
        expected_service_networks=expected_service_networks,
    )
    if (
        host.get("dockerContext") != runtime_host_identity["context"]
        or host.get("dockerContext") != before_daemon["context"]
        or host.get("dockerContext") != after_daemon["context"]
        or host.get("dockerEndpoint") != runtime_host_identity["endpoint"]
        or host.get("dockerEndpoint") != before_daemon["endpoint"]
        or host.get("dockerEndpoint") != after_daemon["endpoint"]
        or host.get("dockerServerId") != runtime_host_identity["serverId"]
        or host.get("dockerServerId") != before_daemon["serverId"]
        or host.get("dockerServerId") != after_daemon["serverId"]
        or host.get("osType") != before_daemon["osType"]
        or host.get("osType") != after_daemon["osType"]
    ):
        raise ValueError("gateway trust host receipt does not match replayed Docker host")
    stored_daemon = docker.get("daemon")
    if not isinstance(stored_daemon, dict):
        raise ValueError("gateway Docker daemon identity is invalid")
    if (
        stored_daemon.get("context") != _GATEWAY_DOCKER_CONTEXT
        or stored_daemon.get("context") != runtime_host_identity["context"]
        or stored_daemon.get("endpoint") != runtime_host_identity["endpoint"]
        or stored_daemon.get("endpoint") != before_daemon["endpoint"]
        or stored_daemon.get("serverId") != runtime_host_identity["serverId"]
        or stored_daemon.get("serverId") != before_daemon["serverId"]
        or stored_daemon.get("dockerHostIdentitySha256") != expected_host_identity_sha256
        or stored_daemon.get("osType") != before_daemon["osType"]
        or set(stored_daemon)
        != {
            "context",
            "endpoint",
            "serverId",
            "dockerHostIdentitySha256",
            "osType",
        }
    ):
        raise ValueError("gateway Docker host identity is invalid")
    if before_daemon["endpoint"] != after_daemon["endpoint"]:
        raise ValueError("gateway Docker daemon endpoint changed")
    if (
        before_daemon["serverId"] != after_daemon["serverId"]
        or before_daemon["osType"] != after_daemon["osType"]
    ):
        raise ValueError("gateway Docker daemon identity changed")
    _validate_gateway_stored_snapshot(docker.get("beforeSnapshot"), before_snapshot)
    _validate_gateway_stored_snapshot(docker.get("afterSnapshot"), after_snapshot)
    if before_snapshot != after_snapshot:
        raise ValueError("gateway Docker container identity changed")
    summary = proof.get("summary")
    if (
        not isinstance(summary, dict)
        or set(summary) != {"checks"}
        or not exact_json_equal(summary.get("checks"), checks)
    ):
        raise ValueError("gateway-only-public execution proof summary is invalid")
    external_lease.assert_unchanged()
    return checks, observed_at


def probe_provenance_error(
    document: Mapping[str, object],
    *,
    evidence: str,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    bundle_root: Path,
    candidate_root: Path,
    expected_outage_docker_host_identity_sha256: str | None = None,
    expected_gateway_docker_host_identity_sha256: str | None = None,
    trusted_keyring_path: Path | None = None,
    expected_trusted_keyring_sha256: str | None = None,
    expected_observer_challenge: str | None = None,
    expected_host_challenge: str | None = None,
    trusted_now: str | None = None,
    expected_openmaic_observer_attestation_sha256: str | None = None,
    expected_openmaic_observer_id: str | None = None,
    expected_openmaic_observer_origin: str | None = None,
    expected_openmaic_shared_ingress_control_origin: str | None = None,
) -> str | None:
    """Revalidate raw/execution proof for evidence backed by a fixed probe."""
    if evidence == "backup_restore":
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"backupRestoreReport"}:
            return "backup restore execution proof is missing or invalid"
        proof = provenance.get("backupRestoreReport")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != _BACKUP_RESTORE_REPORT.as_posix()
            or not isinstance(proof.get("sha256"), str)
        ):
            return "backup restore execution proof is missing or invalid"
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if not isinstance(receipt, dict) or not isinstance(result, dict):
            return "backup restore receipt is invalid"
        checks = result.get("checks")
        try:
            _report_body, _report_sha256, derived, observed_at = _replay_backup_restore_artifacts(
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
                expected_report_sha256=proof["sha256"],
                expected_database_ownership="runner-owned-disposable",
                expected_object_namespace_ownership="runner-owned-disposable",
            )
        except ValueError as exc:
            return str(exc)
        if receipt.get("observedAt") != observed_at or not exact_json_equal(checks, derived):
            return "backup restore receipt does not match execution proof"
        return None
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
    if evidence == "gateway_only_public":
        if (
            expected_gateway_docker_host_identity_sha256 is None
            or trusted_keyring_path is None
            or expected_trusted_keyring_sha256 is None
            or expected_observer_challenge is None
            or expected_host_challenge is None
            or trusted_now is None
        ):
            return "gateway external trust inputs are required"
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or set(provenance) != {"gatewayOnlyPublicAttestation"}:
            return "gateway-only-public execution proof is missing or invalid"
        proof = provenance.get("gatewayOnlyPublicAttestation")
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != "runtime/gateway-only-public-attestation.json"
        ):
            return "gateway-only-public execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label="gateway-only-public execution proof",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            checks, _observed_at = derive_gateway_only_public_receipt_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
                expected_docker_host_identity_sha256=(expected_gateway_docker_host_identity_sha256),
                trusted_keyring_path=trusted_keyring_path,
                expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
                expected_observer_challenge=expected_observer_challenge,
                expected_host_challenge=expected_host_challenge,
                trusted_now=trusted_now,
            )
        except ValueError as exc:
            return str(exc)
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or not isinstance(result, dict)
            or not exact_json_equal(result.get("checks"), checks)
        ):
            return "gateway-only-public receipt does not match execution proof"
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
    if evidence in {"openmaic_shared_plane", "openmaic_dedicated_plane"}:
        if evidence == "openmaic_shared_plane":
            plane = "shared"
            provenance_key = "openmaicSharedPlaneAttestation"
            proof_artifact = "runtime/openmaic-shared-plane-attestation.json"
            derive_checks = derive_openmaic_shared_plane_receipt_checks
        else:
            plane = "dedicated"
            provenance_key = "openmaicDedicatedPlaneAttestation"
            proof_artifact = "runtime/openmaic-dedicated-plane-attestation.json"
            derive_checks = derive_openmaic_dedicated_plane_receipt_checks
        label = f"OpenMAIC {plane} plane"
        provenance = document.get("provenance")
        if not isinstance(provenance, dict) or provenance_key not in provenance:
            return f"{label} execution proof is missing or invalid"
        expected_provenance = {provenance_key}
        outage_provenance_key = "openmaicDedicatedOutageAttestation"
        observer_provenance_key = "openmaicSharedIngressObserverAttestation"
        if plane == "dedicated":
            if outage_provenance_key not in provenance:
                return "OpenMAIC dedicated outage execution proof is missing or invalid"
            expected_provenance.add(outage_provenance_key)
            if observer_provenance_key not in provenance:
                return "OpenMAIC shared-ingress observer execution proof is missing or invalid"
            expected_provenance.add(observer_provenance_key)
        if set(provenance) != expected_provenance:
            return f"{label} execution proof is missing or invalid"
        proof = provenance.get(provenance_key)
        if (
            not isinstance(proof, dict)
            or set(proof) != {"artifact", "sha256"}
            or proof.get("artifact") != proof_artifact
        ):
            return f"{label} execution proof is missing or invalid"
        proof_body = _proof_bytes(
            bundle_root,
            proof,
            label=f"{label} attestation",
        )
        if isinstance(proof_body, str):
            return proof_body
        try:
            checks, observed_at = derive_checks(
                proof_body[0],
                bundle_root=bundle_root,
                candidate_root=candidate_root,
                candidate=candidate,
                release_run=release_run,
            )
        except ValueError as exc:
            return str(exc)
        if plane == "dedicated":
            if (
                not isinstance(expected_outage_docker_host_identity_sha256, str)
                or _SHA256.fullmatch(expected_outage_docker_host_identity_sha256) is None
                or expected_outage_docker_host_identity_sha256 == "0" * 64
            ):
                return "OpenMAIC dedicated outage Docker host anchor is unavailable"
            try:
                success_document = json.loads(proof_body[0])
            except (UnicodeError, json.JSONDecodeError):
                return f"{label} execution proof is missing or invalid"
            summary = (
                success_document.get("summary") if isinstance(success_document, dict) else None
            )
            fixture = summary.get("fixture") if isinstance(summary, dict) else None
            tenant_id = fixture.get("tenantId") if isinstance(fixture, dict) else None
            if not isinstance(tenant_id, str):
                return f"{label} execution proof is missing or invalid"
            outage_proof = provenance.get(outage_provenance_key)
            if (
                not isinstance(outage_proof, dict)
                or set(outage_proof) != {"artifact", "sha256"}
                or outage_proof.get("artifact")
                != "runtime/openmaic-dedicated-outage-attestation.json"
            ):
                return "OpenMAIC dedicated outage execution proof is missing or invalid"
            outage_body = _proof_bytes(
                bundle_root,
                outage_proof,
                label="OpenMAIC dedicated outage attestation",
            )
            if isinstance(outage_body, str):
                return outage_body
            observer_proof = provenance.get(observer_provenance_key)
            if (
                not isinstance(observer_proof, dict)
                or set(observer_proof) != {"artifact", "sha256"}
                or observer_proof.get("artifact")
                != "runtime/openmaic-shared-ingress-observer-attestation.json"
            ):
                return "OpenMAIC shared-ingress observer execution proof is missing or invalid"
            observer_body = _proof_bytes(
                bundle_root,
                observer_proof,
                label="OpenMAIC shared-ingress observer attestation",
            )
            if isinstance(observer_body, str):
                return observer_body
            try:
                outage_checks, _outage_observed_at = (
                    derive_openmaic_dedicated_outage_receipt_checks(
                        outage_body[0],
                        bundle_root=bundle_root,
                        candidate_root=candidate_root,
                        candidate=candidate,
                        release_run=release_run,
                        expected_tenant_id=tenant_id,
                        expected_docker_host_identity_sha256=(
                            expected_outage_docker_host_identity_sha256
                        ),
                        expected_openmaic_observer_attestation_sha256=(
                            expected_openmaic_observer_attestation_sha256
                        ),
                        expected_openmaic_observer_id=expected_openmaic_observer_id,
                        expected_openmaic_observer_origin=(expected_openmaic_observer_origin),
                        expected_openmaic_shared_ingress_control_origin=(
                            expected_openmaic_shared_ingress_control_origin
                        ),
                    )
                )
            except ValueError as exc:
                return str(exc)
            checks = {**checks, **outage_checks}
        receipt = document.get("receipt")
        result = receipt.get("result") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("observedAt") != observed_at
            or not isinstance(result, dict)
            or not exact_json_equal(result.get("checks"), checks)
        ):
            return f"{label} receipt does not match execution proof"
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
        expected_outage_docker_host_identity_sha256: str | None = None,
        expected_gateway_docker_host_identity_sha256: str | None = None,
        trusted_keyring_path: Path | None = None,
        expected_trusted_keyring_sha256: str | None = None,
        expected_observer_challenge: str | None = None,
        expected_host_challenge: str | None = None,
        trusted_now: str | None = None,
        expected_openmaic_observer_attestation_sha256: str | None = None,
        expected_openmaic_observer_id: str | None = None,
        expected_openmaic_observer_origin: str | None = None,
        expected_openmaic_shared_ingress_control_origin: str | None = None,
    ) -> None:
        self._path = Path(path)
        self._expected_source_head = expected_source_head
        self._candidate_root = Path(candidate_root) if candidate_root is not None else PROJECT_ROOT
        self._expected_outage_docker_host_identity_sha256 = (
            expected_outage_docker_host_identity_sha256
        )
        self._expected_gateway_docker_host_identity_sha256 = (
            expected_gateway_docker_host_identity_sha256
            if expected_gateway_docker_host_identity_sha256 is not None
            else os.environ.get(_GATEWAY_DOCKER_HOST_IDENTITY_ENV)
        )
        self._trusted_keyring_path = (
            Path(trusted_keyring_path) if trusted_keyring_path is not None else None
        )
        self._expected_trusted_keyring_sha256 = expected_trusted_keyring_sha256
        self._expected_observer_challenge = expected_observer_challenge
        self._expected_host_challenge = expected_host_challenge
        self._trusted_now = trusted_now
        self._expected_openmaic_observer_attestation_sha256 = (
            expected_openmaic_observer_attestation_sha256
        )
        self._expected_openmaic_observer_id = expected_openmaic_observer_id
        self._expected_openmaic_observer_origin = expected_openmaic_observer_origin
        self._expected_openmaic_shared_ingress_control_origin = (
            expected_openmaic_shared_ingress_control_origin
        )
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
        try:
            bundle_boundary = _EvidenceBundleBoundary.open(self._path.parent)
        except ValueError:
            return LayerEvidence("fail", "evidence bundle boundary cannot be opened")
        try:
            try:
                bundle_boundary.assert_unchanged()
            except ValueError:
                return LayerEvidence(
                    "fail",
                    "evidence bundle boundary changed during receipt replay",
                )
            provenance_error = probe_provenance_error(
                artifact_document,
                evidence=name,
                candidate=self._candidate,
                release_run=self._release_run,
                bundle_root=self._path.parent,
                candidate_root=self._candidate_root,
                expected_outage_docker_host_identity_sha256=(
                    self._expected_outage_docker_host_identity_sha256
                ),
                expected_gateway_docker_host_identity_sha256=(
                    self._expected_gateway_docker_host_identity_sha256
                ),
                trusted_keyring_path=self._trusted_keyring_path,
                expected_trusted_keyring_sha256=(self._expected_trusted_keyring_sha256),
                expected_observer_challenge=self._expected_observer_challenge,
                expected_host_challenge=self._expected_host_challenge,
                trusted_now=self._trusted_now,
                expected_openmaic_observer_attestation_sha256=(
                    self._expected_openmaic_observer_attestation_sha256
                ),
                expected_openmaic_observer_id=self._expected_openmaic_observer_id,
                expected_openmaic_observer_origin=self._expected_openmaic_observer_origin,
                expected_openmaic_shared_ingress_control_origin=(
                    self._expected_openmaic_shared_ingress_control_origin
                ),
            )
            try:
                bundle_boundary.assert_unchanged()
            except ValueError:
                return LayerEvidence(
                    "fail",
                    "evidence bundle boundary changed during receipt replay",
                )
        finally:
            try:
                bundle_boundary.close()
            except OSError:
                pass
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
    parser.add_argument("--outage-docker-host-identity-sha256")
    parser.add_argument("--gateway-docker-host-identity-sha256")
    parser.add_argument("--gateway-trust-keyring", type=Path)
    parser.add_argument("--gateway-trust-keyring-sha256")
    parser.add_argument("--gateway-observer-challenge")
    parser.add_argument("--gateway-host-challenge")
    parser.add_argument("--gateway-trusted-now")
    parser.add_argument("--openmaic-observer-attestation-sha256")
    parser.add_argument("--openmaic-observer-id")
    parser.add_argument("--openmaic-observer-origin")
    parser.add_argument("--openmaic-shared-ingress-control-origin")
    parser.add_argument("--json", action="store_true")
    return parser


def _resolve_gateway_docker_host_identity_sha256(
    cli_value: object,
    environment: Mapping[str, str],
) -> str:
    by_upper = {name.upper(): value for name, value in environment.items()}
    environment_value = by_upper.get(_GATEWAY_DOCKER_HOST_IDENTITY_ENV)
    supplied = [value for value in (cli_value, environment_value) if value is not None]
    if not supplied:
        raise ValueError("gateway Docker host identity is unavailable or invalid")
    validated = [_gateway_docker_host_identity_sha256(value) for value in supplied]
    if len(set(validated)) != 1:
        raise ValueError("gateway Docker host identity inputs do not match")
    return validated[0]


def _resolve_gateway_trust_keyring(
    cli_value: object,
    environment: Mapping[str, str],
) -> Path:
    by_upper = {name.upper(): value for name, value in environment.items()}
    environment_value = by_upper.get(_GATEWAY_TRUST_KEYRING_ENV)
    supplied = [value for value in (cli_value, environment_value) if value is not None]
    if not supplied:
        raise ValueError("gateway trust keyring is unavailable or invalid")
    resolved: list[Path] = []
    for value in supplied:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("gateway trust keyring is unavailable or invalid")
        raw = os.fspath(value)
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise ValueError("gateway trust keyring is unavailable or invalid")
        resolved.append(Path(os.path.abspath(raw)))
    if len(set(resolved)) != 1:
        raise ValueError("gateway trust keyring inputs do not match")
    return resolved[0]


def _resolve_gateway_trust_value(
    cli_value: object,
    environment: Mapping[str, str],
    *,
    environment_name: str,
    label: str,
) -> str:
    by_upper = {name.upper(): value for name, value in environment.items()}
    environment_value = by_upper.get(environment_name)
    supplied = [value for value in (cli_value, environment_value) if value is not None]
    if not supplied or any(
        not isinstance(value, str) or not value or value != value.strip() for value in supplied
    ):
        raise ValueError(f"{label} is unavailable or invalid")
    if len(set(supplied)) != 1:
        raise ValueError(f"{label} inputs do not match")
    return supplied[0]


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
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        expected_gateway_docker_host_identity_sha256 = _resolve_gateway_docker_host_identity_sha256(
            args.gateway_docker_host_identity_sha256,
            os.environ,
        )
        trusted_keyring_path = _resolve_gateway_trust_keyring(
            args.gateway_trust_keyring,
            os.environ,
        )
        expected_trusted_keyring_sha256 = _resolve_gateway_trust_value(
            args.gateway_trust_keyring_sha256,
            os.environ,
            environment_name=_GATEWAY_TRUST_KEYRING_SHA256_ENV,
            label="gateway trust keyring SHA-256",
        )
        expected_observer_challenge = _resolve_gateway_trust_value(
            args.gateway_observer_challenge,
            os.environ,
            environment_name=_GATEWAY_OBSERVER_CHALLENGE_ENV,
            label="gateway observer challenge",
        )
        expected_host_challenge = _resolve_gateway_trust_value(
            args.gateway_host_challenge,
            os.environ,
            environment_name=_GATEWAY_HOST_CHALLENGE_ENV,
            label="gateway host challenge",
        )
        trusted_now = _resolve_gateway_trust_value(
            args.gateway_trusted_now,
            os.environ,
            environment_name=_GATEWAY_TRUSTED_NOW_ENV,
            label="gateway trusted time",
        )
        expected_openmaic_observer_attestation_sha256 = _resolve_gateway_trust_value(
            args.openmaic_observer_attestation_sha256,
            os.environ,
            environment_name=_OPENMAIC_OBSERVER_ATTESTATION_SHA256_ENV,
            label="OpenMAIC observer attestation SHA-256",
        )
        expected_openmaic_observer_id = _resolve_gateway_trust_value(
            args.openmaic_observer_id,
            os.environ,
            environment_name=_OPENMAIC_OBSERVER_ID_ENV,
            label="OpenMAIC observer ID",
        )
        expected_openmaic_observer_origin = _resolve_gateway_trust_value(
            args.openmaic_observer_origin,
            os.environ,
            environment_name=_OPENMAIC_OBSERVER_ORIGIN_ENV,
            label="OpenMAIC observer origin",
        )
        expected_openmaic_shared_ingress_control_origin = _resolve_gateway_trust_value(
            args.openmaic_shared_ingress_control_origin,
            os.environ,
            environment_name=_OPENMAIC_CONTROL_ORIGIN_ENV,
            label="OpenMAIC shared-ingress control origin",
        )
        validate_openmaic_shared_ingress_observer_trust_anchor(
            expected_observer_attestation_sha256=(expected_openmaic_observer_attestation_sha256),
            expected_observer_id=expected_openmaic_observer_id,
            expected_observer_origin=expected_openmaic_observer_origin,
            expected_shared_ingress_control_origin=(
                expected_openmaic_shared_ingress_control_origin
            ),
        )
    except ValueError as exc:
        parser.error(str(exc))
    expected_source_head = _git_head()
    result = verify(
        FileReleaseRuntime(
            args.evidence,
            expected_source_head=expected_source_head,
            candidate_root=args.candidate_root,
            expected_outage_docker_host_identity_sha256=(args.outage_docker_host_identity_sha256),
            expected_gateway_docker_host_identity_sha256=(
                expected_gateway_docker_host_identity_sha256
            ),
            trusted_keyring_path=trusted_keyring_path,
            expected_trusted_keyring_sha256=expected_trusted_keyring_sha256,
            expected_observer_challenge=expected_observer_challenge,
            expected_host_challenge=expected_host_challenge,
            trusted_now=trusted_now,
            expected_openmaic_observer_attestation_sha256=(
                expected_openmaic_observer_attestation_sha256
            ),
            expected_openmaic_observer_id=expected_openmaic_observer_id,
            expected_openmaic_observer_origin=expected_openmaic_observer_origin,
            expected_openmaic_shared_ingress_control_origin=(
                expected_openmaic_shared_ingress_control_origin
            ),
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
