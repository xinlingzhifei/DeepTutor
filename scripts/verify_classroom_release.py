"""Verify that one classroom release candidate has every required evidence layer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Protocol

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

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

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


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

    def __init__(self, path: Path, *, expected_source_head: str) -> None:
        self._path = Path(path)
        self._expected_source_head = expected_source_head
        self._project_root = PROJECT_ROOT
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
        image_digests = candidate.get("imageDigests")
        digests_are_valid = self._valid_image_digests(image_digests)
        binding_error = self._candidate_binding_error(image_digests) if digests_are_valid else None
        self._candidate_is_valid = (
            self._candidate_head != "" and digests_are_valid and binding_error is None
        )
        if binding_error is not None:
            self._candidate_error = binding_error
        self._candidate = candidate
        self._evidence = evidence

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
        if not isinstance(raw, dict):
            return "evidence candidate image digests are invalid"
        lock_path = self._project_root / "deploy" / "image-lock.json"
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "candidate image lock is unavailable or invalid"
        if not isinstance(lock, dict) or lock.get("schemaVersion") != 1:
            return "candidate image lock is unavailable or invalid"
        images = lock.get("images")
        if not isinstance(images, dict):
            return "candidate image lock is unavailable or invalid"
        references: dict[str, str] = {}
        for name in CUSTOM_IMAGE_NAMES:
            record = images.get(name)
            if not isinstance(record, dict) or record.get("digest") != raw.get(name):
                return "candidate image digests do not match the image lock"
            repository = record.get("repository")
            tag = record.get("tag")
            expected_repository, expected_tag = CUSTOM_IMAGE_SPECS[name]
            if repository != expected_repository or tag != expected_tag:
                return "candidate image lock entry is invalid"
            reference = f"{repository}:{tag}@{raw[name]}"
            if record.get("reference") != reference:
                return "candidate image lock reference is invalid"
            references[name] = reference
        for relative, bindings in CUSTOM_IMAGE_SERVICE_BINDINGS.items():
            try:
                compose = yaml.load(
                    (self._project_root / relative).read_text(encoding="utf-8"),
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
    candidate_source_is_valid = (
        isinstance(candidate, dict)
        and isinstance(candidate.get("sourceHead"), str)
        and _COMMIT.fullmatch(candidate["sourceHead"]) is not None
    )
    candidate_images_are_valid = isinstance(candidate, dict) and (
        FileReleaseRuntime._valid_image_digests(candidate.get("imageDigests"))
    )
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
