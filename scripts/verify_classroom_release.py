"""Verify that one classroom release candidate has every required evidence layer."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Protocol

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from classroom_release_probe_contract import (  # noqa: E402
    LIVE_PROJECT,
    LIVE_SPEC,
    PROBE_RECIPES,
    probe_command_record,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE_PATH = (
    PROJECT_ROOT / "data" / "user" / "release-evidence" / "classroom-first-release.json"
)

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
    "capacity_profile": ("load-classroom", ("thresholdsPassed", "rawSamplesRecorded")),
    "teacher_flow": ("playwright", ("teacherFlowPassed",)),
    "student_micro_flow": ("playwright", ("studentMicroFlowPassed",)),
    "student_full_flow": ("playwright", ("studentFullFlowPassed",)),
    "content_operations_flow": ("playwright", ("contentOperationsFlowPassed",)),
    "classroom_exports": (
        "artifact-inspector",
        ("zipOpened", "pptxOpened", "offlineHtmlOpened", "mp4Opened"),
    ),
    "tenant_isolation": (
        "tenant-isolation-gate",
        ("databaseIsolated", "objectsIsolated", "exportsIsolated", "eventsIsolated"),
    ),
    "learning_event_idempotency": (
        "learning-event-gate",
        ("idempotent", "projectionVisible"),
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

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_RELEASE_TAG = re.compile(r"^yfeistai-first-release-[0-9]{8}-([0-9a-f]{8})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


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


def probe_provenance_error(
    document: Mapping[str, object],
    *,
    evidence: str,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    bundle_root: Path,
) -> str | None:
    """Revalidate raw/execution proof for evidence backed by a fixed probe."""
    recipe = PROBE_RECIPES.get(evidence)
    if recipe is None:
        return None
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "recipe",
        "command",
        "rawReport",
        "execution",
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
            "nativeExit",
            "rawReportSha256",
        }
        or execution.get("schemaVersion") != 1
        or execution.get("candidate") != candidate
        or execution.get("releaseRun") != release_run
        or execution.get("evidence") != evidence
        or execution.get("recipe") != recipe_id
        or execution.get("command") != command
        or not _valid_observed_at_value(execution.get("observedAt"))
        or not isinstance(execution.get("nativeExit"), int)
        or isinstance(execution.get("nativeExit"), bool)
        or execution.get("nativeExit") != 0
        or execution.get("rawReportSha256") != hashlib.sha256(raw_body).hexdigest()
    ):
        return "probe execution record is invalid"
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
            or result.get("outcome") != "pass"
            or not isinstance(native_exit, int)
            or isinstance(native_exit, bool)
            or native_exit != 0
        ):
            return False
        checks = result.get("checks")
        return isinstance(checks, dict) and all(
            checks.get(check) is True for check in required_checks
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
