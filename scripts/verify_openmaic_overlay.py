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
JAVASCRIPT_SUFFIXES = {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
FORBIDDEN_CLIENT_PARAMETER_PATTERNS = (
    re.compile(r"\bapi[-_\s]*key\b", re.IGNORECASE),
    re.compile(r"\bbase[-_\s]*url\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*id\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*key\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*api[-_\s]*key\b", re.IGNORECASE),
    re.compile(r"\bprovider[-_\s]*base[-_\s]*url\b", re.IGNORECASE),
)
FORBIDDEN_IDENTITY_SURFACE_PATTERNS = (
    re.compile(r"\bsign[-_\s]*in(?:[-_\s]*(?:page|form))?\b", re.IGNORECASE),
    re.compile(r"\blogin(?:[-_\s]*(?:page|form))?\b", re.IGNORECASE),
    re.compile(
        r"\baccount(?:s|[-_\s]*(?:store|table|model|entity|record))?\b",
        re.IGNORECASE,
    ),
)
PRODUCTION_TEST_IMPORT_PATTERN = re.compile(
    r"""(?:(?:\bimport\b|\bfrom\b)[^;\n]*|\brequire\s*\(\s*)["']"""
    r"""(?:tests[/\\][^"']*|[^"']*[/\\]tests(?:[/\\][^"']*)?)["']""",
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


def _strip_javascript_comments(source: str) -> str:
    result: list[str] = []
    index = 0
    quote: str | None = None
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if quote is not None:
            result.append(char)
            if char == "\\" and index + 1 < len(source):
                index += 1
                result.append(source[index])
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {'"', "'", "`"}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(source) and source[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(source) and source[index : index + 2] != "*/":
                if source[index] in "\r\n":
                    result.append(source[index])
                index += 1
            index = min(index + 2, len(source))
            continue
        result.append(char)
        index += 1
    return "".join(result)


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
    commit = EXPECTED_UPSTREAM["commit"]
    short_commit = commit[:12]
    required_tokens = (
        (f'git clone --filter=blob:none --no-checkout "{EXPECTED_UPSTREAM["repository"]}" /src'),
        f'git fetch --depth=1 origin "{commit}"',
        f'git checkout --detach "{commit}"',
        f'test "$(git rev-parse HEAD)" = "{commit}"',
        (
            'test "$(node -p "require(\'./package.json\').version")" = '
            f'"{EXPECTED_UPSTREAM["appVersion"]}"'
        ),
        overlay_copy,
        f'org.opencontainers.image.revision="{commit}"',
        f'org.opencontainers.image.revision-short="{short_commit}"',
        f'org.opencontainers.image.version="{EXPECTED_UPSTREAM["appVersion"]}"',
    )
    missing = [token for token in required_tokens if token not in source]
    if missing:
        raise OverlayVerificationError(
            f"{path} is missing required pin/build tokens: {', '.join(missing)}"
        )
    if re.search(r"^\s*ARG\s+OPENMAIC_", source, re.MULTILINE):
        raise OverlayVerificationError("OpenMAIC release pins cannot be Docker build arguments")
    if "$OPENMAIC_" in source or "${OPENMAIC_" in source:
        raise OverlayVerificationError("OpenMAIC release pins cannot be variable substitutions")
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
        relative_text = relative.as_posix()
        for pattern in FORBIDDEN_IDENTITY_SURFACE_PATTERNS:
            match = pattern.search(relative_text)
            if match:
                violations.append(f"{relative}: forbidden {match.group(0)} surface")
        if path.suffix.lower() not in JAVASCRIPT_SUFFIXES:
            continue
        source = _strip_javascript_comments(path.read_text(encoding="utf-8"))
        for pattern in FORBIDDEN_IDENTITY_SURFACE_PATTERNS:
            match = pattern.search(source)
            if match:
                violations.append(f"{relative}: forbidden {match.group(0)} surface")
        for pattern in FORBIDDEN_CLIENT_PARAMETER_PATTERNS:
            match = pattern.search(source)
            if match:
                violations.append(f"{relative}: forbidden client parameter {match.group(0)}")
        if (
            not relative.parts or relative.parts[0].lower() != "tests"
        ) and PRODUCTION_TEST_IMPORT_PATTERN.search(source):
            violations.append(f"{relative}: production source imports overlay tests")
    return violations


def verify_service_auth_source(source: str) -> None:
    source = _strip_javascript_comments(source)
    required_patterns = {
        "Node crypto imports": (
            r"import\s*\{(?=[^}]*\bcreateHash\b)(?=[^}]*\bcreateHmac\b)"
            r'(?=[^}]*\btimingSafeEqual\b)[^}]*\}\s*from\s*["\']node:crypto["\']'
        ),
        "SHA-256 body digest": (
            r'createHash\(["\']sha256["\']\)\s*\.update\(body\)'
            r'\s*\.digest\(["\']hex["\']\)'
        ),
        "HMAC-SHA256 signer": (
            r'createHmac\(["\']sha256["\'],\s*secret\)\s*'
            r'\.update\(canonicalServiceRequest\(input\),\s*["\']utf8["\']\)'
            r'\s*\.digest\(["\']hex["\']\)'
        ),
        "60-second clock window": (
            r"export\s+const\s+MAX_CLOCK_SKEW_SECONDS\s*=\s*60"
            r"(?:\s+as\s+const)?\s*;"
        ),
        "clock-skew enforcement": (
            r"Math\.abs\(options\.nowSeconds\s*-\s*normalized\.timestamp\)"
            r"\s*>\s*MAX_CLOCK_SKEW_SECONDS"
        ),
        "fixed secret mount": (
            r"export\s+const\s+SERVICE_SECRET_PATH\s*=\s*"
            r'["\']/run/secrets/openmaic_service_secret["\']'
        ),
        "secret file read": (
            r"readFileSync\(SERVICE_SECRET_PATH,\s*"
            r'["\']utf8["\']\)'
        ),
        "write idempotency enforcement": (r"allowEmpty:\s*!requiresIdempotencyKey\(method\)"),
        "canonical field order": (
            r"const\s+canonicalParts\s*=\s*\[\s*"
            r"normalized\.method,\s*"
            r"normalized\.path,\s*"
            r"normalized\.tenantId,\s*"
            r"normalized\.jobId,\s*"
            r"String\(normalized\.timestamp\),\s*"
            r"normalized\.idempotencyKey,\s*"
            r"sha256Body\(input\.body\),?\s*\]"
        ),
        "canonical newline join": (r'return\s+canonicalParts\.join\(["\']\\n["\']\)'),
        "signature shape validation": (r"!SHA256_HEX\.test\(signed\.signature\)"),
        "constant-time signature comparison": (r"timingSafeEqual\(expected,\s*received\)"),
    }
    missing = [
        label
        for label, pattern in required_patterns.items()
        if re.search(pattern, source, re.DOTALL) is None
    ]
    if missing:
        raise OverlayVerificationError(
            "service-auth.ts is missing security-critical structures: " + ", ".join(missing)
        )
    if "process.env" in source:
        raise OverlayVerificationError(
            "service-auth.ts must not read its service secret from process.env"
        )
    if len(re.findall(r"\breadFileSync\s*\(", source)) != 1:
        raise OverlayVerificationError(
            "service-auth.ts must read exactly one fixed service-secret file"
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
