#!/usr/bin/env python3
"""Verify the pinned OpenMAIC overlay and optionally run its focused tests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INTEGRATION_ROOT = ROOT / "integrations" / "openmaic"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
    "commit": "0cf2a330411681190e89f48e20f305345ff99f87",
    "appVersion": "0.3.1",
}
REQUIRED_OVERLAY_FILES = {
    Path("app/api/yfeistai/v1/health/route.ts"),
    Path("lib/yfeistai/contracts.ts"),
    Path("lib/yfeistai/service-auth.ts"),
    Path("tests/yfeistai/service-auth.test.ts"),
}
FORBIDDEN_PATH_SEGMENTS = {"account", "accounts", "login"}
FORBIDDEN_CLIENT_PARAMETER_PATTERNS = (
    re.compile(r"\bapiKey\b", re.IGNORECASE),
    re.compile(r"\bapi_key\b", re.IGNORECASE),
    re.compile(r"\bapi-key\b", re.IGNORECASE),
    re.compile(r"\bbaseUrl\b", re.IGNORECASE),
    re.compile(r"\bbase_url\b", re.IGNORECASE),
    re.compile(r"\bbase-url\b", re.IGNORECASE),
    re.compile(r"\bproviderApiKey\b", re.IGNORECASE),
    re.compile(r"\bprovider_api_key\b", re.IGNORECASE),
    re.compile(r"\bproviderId\b", re.IGNORECASE),
    re.compile(r"\bprovider_id\b", re.IGNORECASE),
    re.compile(r"\bproviderBaseUrl\b", re.IGNORECASE),
    re.compile(r"\bprovider_base_url\b", re.IGNORECASE),
)
FORBIDDEN_IDENTITY_SURFACE_PATTERN = re.compile(
    r"\b(?:LoginPage|LoginForm|AccountTable|AccountModel|AccountEntity)\b",
    re.IGNORECASE,
)
VITEST_PACKAGE = "vitest@4.1.8"


class OverlayVerificationError(RuntimeError):
    """Raised when the committed overlay violates its integration contract."""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OverlayVerificationError(f"required file is missing: {path}") from exc


def _verify_upstream_manifest(integration_root: Path) -> None:
    path = integration_root / "UPSTREAM.json"
    try:
        manifest = json.loads(_read_text(path))
    except json.JSONDecodeError as exc:
        raise OverlayVerificationError(f"invalid JSON in {path}: {exc}") from exc
    if manifest != EXPECTED_UPSTREAM:
        raise OverlayVerificationError(
            f"{path} must exactly pin the verified repository, commit, and app version"
        )


def _verify_dockerfile(integration_root: Path) -> None:
    path = integration_root / "Dockerfile"
    source = _read_text(path)
    overlay_copy = "COPY integrations/openmaic/overlay/"
    required_tokens = (
        EXPECTED_UPSTREAM["repository"],
        EXPECTED_UPSTREAM["commit"],
        EXPECTED_UPSTREAM["appVersion"],
        "git clone",
        "git rev-parse HEAD",
        "require('./package.json').version",
        overlay_copy,
        "org.opencontainers.image.revision",
        "org.opencontainers.image.revision-short",
        "org.opencontainers.image.version",
    )
    missing = [token for token in required_tokens if token not in source]
    if missing:
        raise OverlayVerificationError(
            f"{path} is missing required pin/build tokens: {', '.join(missing)}"
        )
    if source.index("git rev-parse HEAD") > source.index(overlay_copy):
        raise OverlayVerificationError(
            "Dockerfile must verify the checkout before applying overlay"
        )
    runner_marker = " AS runner"
    if runner_marker not in source:
        raise OverlayVerificationError("Dockerfile must contain a final runner stage")
    runner = source[source.index(runner_marker) :]
    if "git clone" in runner or re.search(r"\bapk add\b[^\n]*\bgit\b", runner):
        raise OverlayVerificationError("final image must not clone Git or install Git")


def find_forbidden_overlay_surface(overlay_root: Path) -> list[str]:
    """Return login/account paths and client-controlled provider parameters."""

    violations: list[str] = []
    for path in sorted(overlay_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(overlay_root)
        if relative.parts and relative.parts[0] == "tests":
            continue
        path_tokens = set(re.findall(r"[a-z0-9]+", relative.as_posix().lower()))
        forbidden_parts = path_tokens & FORBIDDEN_PATH_SEGMENTS
        for part in sorted(forbidden_parts):
            violations.append(f"{relative}: forbidden {part} surface")
        if path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx"}:
            continue
        source = path.read_text(encoding="utf-8")
        identity_match = FORBIDDEN_IDENTITY_SURFACE_PATTERN.search(source)
        if identity_match:
            violations.append(f"{relative}: forbidden {identity_match.group(0)} surface")
        for pattern in FORBIDDEN_CLIENT_PARAMETER_PATTERNS:
            match = pattern.search(source)
            if match:
                violations.append(f"{relative}: forbidden client parameter {match.group(0)}")
    return violations


def verify_service_auth_source(source: str) -> None:
    required_tokens = (
        'createHash("sha256")',
        'createHmac("sha256"',
        "timingSafeEqual",
        "MAX_CLOCK_SKEW_SECONDS = 60",
        "/run/secrets/openmaic_service_secret",
        "readFileSync(SERVICE_SECRET_PATH",
        "idempotencyKey",
        "canonicalParts",
    )
    missing = [token for token in required_tokens if token not in source]
    if missing:
        raise OverlayVerificationError(
            "service-auth.ts is missing security-critical primitives: " + ", ".join(missing)
        )
    if "process.env" in source:
        raise OverlayVerificationError(
            "service-auth.ts must not read its service secret from process.env"
        )


def _verify_health_contract(overlay_root: Path) -> None:
    route = _read_text(overlay_root / "app/api/yfeistai/v1/health/route.ts")
    contracts = _read_text(overlay_root / "lib/yfeistai/contracts.ts")
    if "OPENMAIC_HEALTH_RESPONSE" not in route or "Response.json" not in route:
        raise OverlayVerificationError("health route must return the typed health response")
    required_literals = (
        '"openmaic"',
        f'"{EXPECTED_UPSTREAM["commit"]}"',
        f'"{EXPECTED_UPSTREAM["appVersion"]}"',
        '"1.0"',
        '"outline"',
        '"content"',
        '"micro"',
        '"export"',
        '"cancel"',
        '"artifact-manifest"',
        '"classroom_zip"',
        '"pptx"',
        '"offline_html"',
        '"mp4"',
    )
    missing = [token for token in required_literals if token not in contracts]
    if missing:
        raise OverlayVerificationError(
            "contracts.ts is missing health contract literals: " + ", ".join(missing)
        )


def verify_overlay(integration_root: Path = DEFAULT_INTEGRATION_ROOT) -> None:
    integration_root = integration_root.resolve()
    overlay_root = integration_root / "overlay"
    _verify_upstream_manifest(integration_root)
    _verify_dockerfile(integration_root)
    for relative in sorted(REQUIRED_OVERLAY_FILES):
        if not (overlay_root / relative).is_file():
            raise OverlayVerificationError(f"required overlay file is missing: {relative}")
    violations = find_forbidden_overlay_surface(overlay_root)
    if violations:
        raise OverlayVerificationError("\n".join(violations))
    verify_service_auth_source(_read_text(overlay_root / "lib/yfeistai/service-auth.ts"))
    _verify_health_contract(overlay_root)


def resolve_package_runner() -> list[str]:
    corepack_name = "corepack.cmd" if os.name == "nt" else "corepack"
    corepack = shutil.which(corepack_name)
    if corepack:
        return [corepack, "pnpm"]
    pnpm_name = "pnpm.cmd" if os.name == "nt" else "pnpm"
    pnpm = shutil.which(pnpm_name)
    if pnpm:
        return [pnpm]
    raise OverlayVerificationError("corepack or pnpm is required to run overlay tests")


def _run_service_auth_tests(integration_root: Path) -> int:
    try:
        runner = resolve_package_runner()
    except OverlayVerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    command = [
        *runner,
        "dlx",
        VITEST_PACKAGE,
        "run",
        "tests/yfeistai/service-auth.test.ts",
        "--environment",
        "node",
    ]
    completed = subprocess.run(command, cwd=integration_root / "overlay", check=False)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test",
        choices=("service-auth", "static"),
        default="static",
        help="verification surface to run",
    )
    args = parser.parse_args(argv)
    try:
        verify_overlay()
    except OverlayVerificationError as exc:
        print(f"OpenMAIC overlay verification failed: {exc}", file=sys.stderr)
        return 1
    if args.test == "service-auth":
        result = _run_service_auth_tests(DEFAULT_INTEGRATION_ROOT)
        if result:
            return result
    print("OpenMAIC overlay verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
