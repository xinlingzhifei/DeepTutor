"""Canonical contracts for candidate-network platform preflight phases."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

PHASES = ("database-object-store", "openmaic")
PHASE_SERVICES = {
    "database-object-store": "tenant-provisioner",
    "openmaic": "deeptutor",
}
PHASE_CHECKS = {
    "database-object-store": (
        "activeTenantCredentialsValid",
        "databaseConnected",
        "objectStoreRoundTrip",
        "revisionsMatch",
        "tenantCrossPrefixDenied",
        "tenantOwnPrefixAccessible",
    ),
    "openmaic": ("openmaicContractCompatible",),
}
ISOLATED_DOCKER_CONFIG = "<isolated-docker-config>"
MAX_CANDIDATE_NETWORK_REPORT_BYTES = 16 * 1024


def canonical_candidate_network_report(report: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            report,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def failed_candidate_network_report(phase: str, error: str) -> dict[str, object]:
    checks = PHASE_CHECKS.get(phase)
    if checks is None or not isinstance(error, str) or not error:
        raise ValueError("candidate-network preflight failure is invalid")
    return {
        "schemaVersion": 1,
        "producer": "platform-preflight",
        "phase": phase,
        "checks": {name: False for name in checks},
        "errors": [error],
    }


def parse_candidate_network_report(
    body: bytes,
    *,
    expected_phase: str,
) -> dict[str, object]:
    expected_checks = PHASE_CHECKS.get(expected_phase)
    if expected_checks is None:
        raise ValueError("candidate-network preflight phase is invalid")
    if not isinstance(body, bytes) or len(body) > MAX_CANDIDATE_NETWORK_REPORT_BYTES:
        raise ValueError("candidate-network preflight report is too large")
    try:
        report = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate-network preflight report is invalid") from exc
    checks = report.get("checks") if isinstance(report, dict) else None
    errors = report.get("errors") if isinstance(report, dict) else None
    if (
        not isinstance(report, dict)
        or set(report) != {"schemaVersion", "producer", "phase", "checks", "errors"}
        or type(report.get("schemaVersion")) is not int
        or report.get("schemaVersion") != 1
        or report.get("producer") != "platform-preflight"
        or report.get("phase") != expected_phase
        or not isinstance(checks, dict)
        or set(checks) != set(expected_checks)
        or any(not isinstance(checks.get(name), bool) for name in expected_checks)
        or not isinstance(errors, list)
        or any(not isinstance(error, str) or not error for error in errors)
        or len(errors) != len(set(errors))
        or canonical_candidate_network_report(report) != body
    ):
        raise ValueError("candidate-network preflight report is invalid")
    return report


def candidate_network_phase_command(
    phase: str,
    container_id: str,
    *,
    docker_config: str = ISOLATED_DOCKER_CONFIG,
) -> list[str]:
    if phase not in PHASES:
        raise ValueError("candidate-network preflight phase is invalid")
    if (
        not isinstance(container_id, str)
        or not container_id
        or container_id != container_id.strip()
        or any(character.isspace() for character in container_id)
    ):
        raise ValueError("candidate-network preflight container identity is invalid")
    if not isinstance(docker_config, str) or not docker_config:
        raise ValueError("candidate-network Docker config is invalid")
    return [
        "docker",
        "--config",
        docker_config,
        "--context",
        "default",
        "exec",
        "--user",
        "1000:1000",
        container_id,
        "python",
        "/app/scripts/platform_preflight.py",
        "--runtime-phase",
        phase,
        "--config",
        "/app/data/user/settings/platform.json",
        "--secret-dir",
        "/run/secrets",
    ]


def materialize_candidate_network_phase_command(
    phase: str,
    container_id: str,
    *,
    docker_executable: Path,
    docker_config: Path,
) -> tuple[list[str], list[str]]:
    """Bind the canonical logical command to two trusted host-only paths."""

    executable = Path(docker_executable)
    config = Path(docker_config)
    if not executable.is_absolute() or not config.is_absolute():
        raise ValueError("candidate-network host command paths must be absolute")
    logical = candidate_network_phase_command(phase, container_id)
    actual = [str(executable), logical[1], str(config), *logical[3:]]
    return actual, logical
