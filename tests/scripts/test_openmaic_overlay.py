from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = ROOT / "integrations" / "openmaic"
EXPECTED_UPSTREAM = {
    "repository": "https://github.com/xinlingzhifei/OpenMAIC.git",
    "commit": "0cf2a330411681190e89f48e20f305345ff99f87",
    "appVersion": "0.3.1",
}


def _load_verifier():
    module_path = ROOT / "scripts" / "verify_openmaic_overlay.py"
    spec = importlib.util.spec_from_file_location(
        "verify_openmaic_overlay_under_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def test_upstream_manifest_pins_verified_repository_commit_and_version() -> None:
    manifest = json.loads((INTEGRATION_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))

    assert manifest == EXPECTED_UPSTREAM


def test_dockerfile_checks_out_and_verifies_the_pin_before_overlay_copy() -> None:
    dockerfile = (INTEGRATION_ROOT / "Dockerfile").read_text(encoding="utf-8")

    for pin_argument in (
        "OPENMAIC_REPOSITORY",
        "OPENMAIC_COMMIT",
        "OPENMAIC_COMMIT_SHORT",
        "OPENMAIC_APP_VERSION",
    ):
        assert f"ARG {pin_argument}" not in dockerfile
    assert (
        f'git clone --filter=blob:none --no-checkout "{EXPECTED_UPSTREAM["repository"]}" /src'
        in dockerfile
    )
    assert f'git fetch --depth=1 origin "{EXPECTED_UPSTREAM["commit"]}"' in dockerfile
    assert f'test "$(git rev-parse HEAD)" = "{EXPECTED_UPSTREAM["commit"]}"' in dockerfile
    assert (
        f'test "$(node -p "require(\'./package.json\').version")" = '
        f'"{EXPECTED_UPSTREAM["appVersion"]}"'
    ) in dockerfile
    assert "git rev-parse HEAD" in dockerfile
    assert "package.json" in dockerfile
    assert "COPY integrations/openmaic/overlay/" in dockerfile
    assert dockerfile.index("git rev-parse HEAD") < dockerfile.index(
        "COPY integrations/openmaic/overlay/"
    )
    assert "org.opencontainers.image.revision" in dockerfile
    assert "org.opencontainers.image.version" in dockerfile
    assert "org.opencontainers.image.revision-short" in dockerfile
    assert f'org.opencontainers.image.revision="{EXPECTED_UPSTREAM["commit"]}"' in dockerfile
    assert (
        f'org.opencontainers.image.revision-short="{EXPECTED_UPSTREAM["commit"][:12]}"'
        in dockerfile
    )
    assert f'org.opencontainers.image.version="{EXPECTED_UPSTREAM["appVersion"]}"' in dockerfile

    runner = dockerfile[dockerfile.index(" AS runner") :]
    assert "git clone" not in runner
    assert "apk add" in runner
    assert " git" not in runner


def test_static_verifier_accepts_the_committed_overlay() -> None:
    verifier = _load_verifier()

    verifier.verify_overlay(INTEGRATION_ROOT)


@pytest.mark.parametrize(
    ("relative_path", "content", "expected"),
    [
        ("app/login/page.tsx", "export default function Login() {}", "login"),
        ("app/auth/page.tsx", "export default function LoginPage() {}", "login"),
        ("app/auth/page.mts", "export default function SignIn() {}", "signin"),
        ("app/auth/page.tsx", "export default function SignInPage() {}", "SignInPage"),
        ("lib/yfeistai/account.ts", "export interface Account {}", "account"),
        (
            "lib/yfeistai/account-store.ts",
            "export interface StoreRecord {}",
            "account",
        ),
        (
            "lib/yfeistai/request.ts",
            "export const request = { providerApiKey: 'secret' };",
            "providerApiKey",
        ),
        (
            "lib/yfeistai/request.ts",
            "export const request = { providerId: 'client-route' };",
            "providerId",
        ),
        (
            "lib/yfeistai/request.cts",
            "export const request = { 'provider-id': 'client-route' };",
            "provider-id",
        ),
        (
            "lib/yfeistai/request.mjs",
            "export const request = { providerKey: 'secret' };",
            "providerKey",
        ),
        (
            "lib/yfeistai/request.ts",
            "export const request = { providerBaseUrl: 'https://invalid' };",
            "providerBaseUrl",
        ),
        (
            "lib/yfeistai/request.cjs",
            "export const request = { 'provider api key': 'secret' };",
            "provider api key",
        ),
        (
            "tests/yfeistai/negative-fixture.mts",
            "export const request = { 'provider baseURL': 'https://invalid' };",
            "provider baseURL",
        ),
        (
            "lib/yfeistai/request.ts",
            "export const request = { apiKey: 'secret' };",
            "apiKey",
        ),
        (
            "lib/yfeistai/request.ts",
            "export const request = { baseURL: 'https://invalid' };",
            "baseURL",
        ),
    ],
)
def test_verifier_rejects_login_account_and_client_provider_surface(
    tmp_path: Path,
    relative_path: str,
    content: str,
    expected: str,
) -> None:
    verifier = _load_verifier()
    overlay_root = tmp_path / "overlay"
    target = overlay_root / relative_path
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")

    violations = verifier.find_forbidden_overlay_surface(overlay_root)

    assert any(expected.lower() in item.lower() for item in violations)


@pytest.mark.parametrize(
    "content",
    [
        'import { signedFixture } from "../../tests/yfeistai/fixture";\n',
        'const fixture = require("../../tests/yfeistai/fixture");\n',
    ],
)
def test_verifier_rejects_production_imports_from_overlay_tests(
    tmp_path: Path,
    content: str,
) -> None:
    verifier = _load_verifier()
    overlay_root = tmp_path / "overlay"
    target = overlay_root / "lib" / "yfeistai" / "request.ts"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")

    violations = verifier.find_forbidden_overlay_surface(overlay_root)

    assert any("imports overlay tests" in item.lower() for item in violations)


def test_verifier_requires_security_critical_service_auth_primitives() -> None:
    verifier = _load_verifier()
    source = (INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "service-auth.ts").read_text(
        encoding="utf-8"
    )

    verifier.verify_service_auth_source(source)


def test_service_auth_verifier_ignores_security_tokens_inside_comments() -> None:
    verifier = _load_verifier()
    comment_only_source = """
// createHash("sha256")
// createHmac("sha256"
// timingSafeEqual
// MAX_CLOCK_SKEW_SECONDS = 60
// /run/secrets/openmaic_service_secret
// readFileSync(SERVICE_SECRET_PATH
// idempotencyKey
// canonicalParts
"""

    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_service_auth_source(comment_only_source)


def test_service_auth_verifier_rejects_commented_out_constant_time_compare() -> None:
    verifier = _load_verifier()
    source = (INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "service-auth.ts").read_text(
        encoding="utf-8"
    )
    forged = source.replace(
        "timingSafeEqual(expected, received)",
        "true /* timingSafeEqual(expected, received) */",
    )

    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_service_auth_source(forged)


def test_service_auth_verifier_requires_canonical_field_order() -> None:
    verifier = _load_verifier()
    source = (INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "service-auth.ts").read_text(
        encoding="utf-8"
    )
    reordered = source.replace(
        "normalized.tenantId,\n    normalized.jobId,",
        "normalized.jobId,\n    normalized.tenantId,",
    )
    assert reordered != source

    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_service_auth_source(reordered)


def test_outline_contract_hash_matches_the_frozen_json_schema() -> None:
    verifier = _load_verifier()
    source = (
        INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "outline-generation.ts"
    ).read_text(encoding="utf-8")

    verifier.verify_outline_contract_hash(source)


def test_outline_contract_hash_verifier_rejects_a_handwritten_mismatch() -> None:
    verifier = _load_verifier()
    source = (
        INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "outline-generation.ts"
    ).read_text(encoding="utf-8")
    forged = source.replace(
        "f8ddb7c11138f402ed048c4af2010714b2bfd456e5c38122920c689e4a2b3ddf",
        "0" * 64,
    )
    assert forged != source

    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_outline_contract_hash(forged)


def test_cli_dispatches_the_outline_generation_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load_verifier()
    called: list[Path] = []
    monkeypatch.setattr(verifier, "verify_overlay", lambda: None)
    monkeypatch.setattr(
        verifier,
        "_run_outline_generation_tests",
        lambda root: called.append(root) or 0,
    )

    assert verifier.main(["--test", "outline-generation"]) == 0
    assert called == [verifier.DEFAULT_INTEGRATION_ROOT]


def test_verifier_resolves_an_executable_package_runner() -> None:
    verifier = _load_verifier()

    runner = verifier.resolve_package_runner()

    assert runner
    executable = Path(runner[0])
    assert executable.is_file()
    if os.name == "nt":
        assert executable.suffix.lower() in {".cmd", ".exe"}
    completed = subprocess.run(
        [*runner, "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
