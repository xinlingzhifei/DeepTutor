"""Verify that one classroom release candidate has every required evidence layer."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Protocol

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

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class LayerEvidence:
    status: str
    detail: str
    artifact: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseVerification:
    layers: dict[str, LayerEvidence]
    missing: tuple[str, ...]
    failed: tuple[str, ...]

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
        self._loaded = False
        self._candidate_head = ""
        self._evidence: dict[str, object] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(document, dict) or document.get("schemaVersion") != 1:
            return
        candidate = document.get("candidate")
        evidence = document.get("evidence")
        if not isinstance(candidate, dict) or not isinstance(evidence, dict):
            return
        source_head = candidate.get("sourceHead")
        if isinstance(source_head, str) and _COMMIT.fullmatch(source_head):
            self._candidate_head = source_head
        self._evidence = evidence

    @staticmethod
    def _parse(raw: object) -> LayerEvidence:
        if not isinstance(raw, dict):
            return LayerEvidence("fail", "evidence entry is invalid")
        status = raw.get("status")
        detail = raw.get("detail")
        artifact = raw.get("artifact")
        if status not in {"pass", "fail"}:
            return LayerEvidence("fail", "evidence status is invalid")
        if not isinstance(detail, str) or not detail.strip():
            return LayerEvidence("fail", "evidence detail is missing")
        if status == "pass" and (not isinstance(artifact, str) or not artifact.strip()):
            return LayerEvidence("fail", "passing evidence artifact is missing")
        return LayerEvidence(
            status=status,
            detail=detail.strip(),
            artifact=artifact.strip() if isinstance(artifact, str) and artifact.strip() else None,
        )

    def result(self, name: str) -> LayerEvidence | None:
        self._load()
        raw = self._evidence.get(name)
        if raw is None:
            return None
        parsed = self._parse(raw)
        if name == "source_head" and (
            not _COMMIT.fullmatch(self._expected_source_head)
            or self._candidate_head != self._expected_source_head
        ):
            return LayerEvidence(
                "fail",
                "evidence candidate source head does not match the checked-out candidate",
                parsed.artifact,
            )
        return parsed


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
        evidence = LayerEvidence(
            status=status if isinstance(status, str) else "fail",
            detail=detail if isinstance(detail, str) else "evidence result is invalid",
            artifact=artifact if isinstance(artifact, str) else None,
        )
        layers[name] = evidence
        if evidence.status != "pass":
            failed.append(name)
    return ReleaseVerification(
        layers=layers,
        missing=tuple(missing),
        failed=tuple(failed),
    )


def report_payload(result: ReleaseVerification) -> dict[str, object]:
    return {
        "status": result.status,
        "ok": result.ok,
        "missing": list(result.missing),
        "failed": list(result.failed),
        "layers": {
            name: {
                "status": evidence.status,
                "detail": evidence.detail,
                "artifact": evidence.artifact,
            }
            for name, evidence in result.layers.items()
        },
    }


def _git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    head = result.stdout.strip() if result.returncode == 0 else ""
    return head if _COMMIT.fullmatch(head) else ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = verify(
        FileReleaseRuntime(
            args.evidence,
            expected_source_head=_git_head(),
        )
    )
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
