"""Canonical fixed-command contract for classroom release Playwright probes."""

from __future__ import annotations

import hashlib
import json

COMMAND_ID = "yfeistai.classroom-release.playwright"
COMMAND_VERSION = 1
ENVIRONMENT_POLICY_VERSION = 1
LIVE_SPEC = "tests/e2e/classroom-first-release.live.spec.ts"
LIVE_PROJECT = "first-release-live"
REPORTER = "json"
WORKERS = 1
RETRIES = 0
REPORT_FORMAT = "playwright-json-reporter"

PROBE_RECIPES = {
    "teacher_flow": ("teacher_flow", 1),
    "student_micro_flow": ("student_micro_flow", 1),
    "student_full_flow": ("student_full_flow", 1),
    "content_operations_flow": ("content_operations_flow", 1),
    "tailwind4_visual_matrix": ("tailwind4_visual_matrix", 48),
}
EVIDENCE_GREP = {evidence: rf"\[release-evidence:{evidence}\]" for evidence in PROBE_RECIPES}


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def probe_command_descriptor(evidence: str) -> dict[str, object]:
    if evidence not in PROBE_RECIPES:
        raise ValueError("probe evidence is invalid")
    grep = EVIDENCE_GREP[evidence]
    inner_argv = [
        "--prefix",
        "web",
        "exec",
        "playwright",
        "--",
        "test",
        LIVE_SPEC,
        f"--project={LIVE_PROJECT}",
        "--grep",
        grep,
        f"--reporter={REPORTER}",
        f"--workers={WORKERS}",
        f"--retries={RETRIES}",
    ]
    return {
        "commandId": COMMAND_ID,
        "version": COMMAND_VERSION,
        "evidence": evidence,
        "innerNpmArgv": inner_argv,
        "liveSpec": LIVE_SPEC,
        "project": LIVE_PROJECT,
        "grep": grep,
        "reporter": REPORTER,
        "workers": WORKERS,
        "retries": RETRIES,
        "reportFormat": REPORT_FORMAT,
        "environmentPolicyVersion": ENVIRONMENT_POLICY_VERSION,
    }


def probe_command_record(
    evidence: str,
) -> dict[str, object]:
    descriptor = probe_command_descriptor(evidence)
    logical_launcher = ["python", "scripts/classroom_release_probe.py", evidence]
    return {
        "logicalLauncher": logical_launcher,
        "argvSha256": canonical_sha256(logical_launcher),
        "descriptor": descriptor,
        "descriptorSha256": canonical_sha256(descriptor),
    }
