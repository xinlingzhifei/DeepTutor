from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

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


def test_service_auth_normalizes_only_fields_shared_by_body_and_digest_requests() -> None:
    source = (
        INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "service-auth.ts"
    ).read_text(encoding="utf-8")
    compact_source = "".join(source.split())

    assert (
        'typeServiceRequestIdentityParts=Omit<ServiceRequestParts,"body">;'
        in compact_source
    )
    assert (
        "functionnormalizeRequestParts(input:ServiceRequestIdentityParts)"
        in compact_source
    )
    assert compact_source.count("normalizeRequestParts(signed)") == 2
    assert "normalizeRequestParts({...signed,body:" not in compact_source


def test_outline_contract_hash_matches_the_frozen_json_schema() -> None:
    verifier = _load_verifier()
    source = (
        INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "outline-generation.ts"
    ).read_text(encoding="utf-8")

    verifier.verify_outline_contract_hash(source)


def test_outline_route_resolves_research_before_sync_generation_callback() -> None:
    source = (
        INTEGRATION_ROOT / "overlay" / "app" / "api" / "yfeistai" / "v1" / "outlines" / "route.ts"
    ).read_text(encoding="utf-8")

    resolved = "const researchContext = await resolveResearchContext(request);"
    adapter = "const generated = await runOutlineRouteAdapter({"
    assert resolved in source
    assert source.index(resolved) < source.index(adapter)
    assert "researchContext: await resolveResearchContext(request)" not in source
    assert "researchContext," in source[source.index(adapter) :]


def test_classroom_route_types_json_cloned_actions_at_serialization_boundary() -> None:
    source = (
        INTEGRATION_ROOT
        / "overlay"
        / "app"
        / "api"
        / "yfeistai"
        / "v1"
        / "classrooms"
        / "route.ts"
    ).read_text(encoding="utf-8")

    compact_source = "".join(source.split())
    assert "functionportableClone<T>(value:unknown):T" in compact_source
    assert "portableClone<Array<Record<string,JsonValue>>>(actions" in compact_source


def test_pptx_export_binds_document_digest_in_visible_slide_text() -> None:
    source = (
        INTEGRATION_ROOT / "overlay" / "app" / "api" / "yfeistai" / "v1" / "exports" / "route.ts"
    ).read_text(encoding="utf-8")

    assert "slide.addText(`Document SHA-256: ${request.classroomDocumentSha256}`" in source


def test_outline_contract_hash_verifier_rejects_a_handwritten_mismatch() -> None:
    verifier = _load_verifier()
    source = (
        INTEGRATION_ROOT / "overlay" / "lib" / "yfeistai" / "outline-generation.ts"
    ).read_text(encoding="utf-8")
    expected_hash = hashlib.sha256(
        (ROOT / "contracts" / "classroom" / "outline-bundle.schema.json").read_bytes()
    ).hexdigest()
    forged = source.replace(
        expected_hash,
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


def test_task4_files_are_mandatory_overlay_inputs() -> None:
    verifier = _load_verifier()
    expected = {
        Path("lib/yfeistai/service-boundary.ts"),
        Path("lib/yfeistai/content-generation.ts"),
        Path("lib/yfeistai/export-generation.ts"),
        Path("lib/yfeistai/artifact-manifest.ts"),
        Path("app/api/yfeistai/v1/classrooms/route.ts"),
        Path("app/api/yfeistai/v1/classrooms/[jobId]/route.ts"),
        Path("app/api/yfeistai/v1/exports/route.ts"),
        Path("app/api/yfeistai/v1/exports/[jobId]/route.ts"),
        Path("app/api/yfeistai/v1/jobs/[jobId]/cancel/route.ts"),
        Path("app/api/yfeistai/v1/artifacts/[jobId]/[...path]/route.ts"),
        Path("tests/yfeistai/content-generation.test.ts"),
        Path("tests/yfeistai/export-generation.test.ts"),
        Path("tests/yfeistai/cancel.test.ts"),
        Path("tests/yfeistai/artifact-manifest.test.ts"),
    }

    assert expected <= verifier.REQUIRED_OVERLAY_FILES


def test_task6_staging_files_are_mandatory_overlay_inputs() -> None:
    verifier = _load_verifier()
    expected = {
        Path("lib/yfeistai/export-input-staging.ts"),
        Path("app/api/yfeistai/v1/export-inputs/[jobId]/route.ts"),
        Path("app/api/yfeistai/v1/export-inputs/[jobId]/commit/route.ts"),
        Path("app/api/yfeistai/v1/export-inputs/[jobId]/files/[fileId]/route.ts"),
        Path("tests/yfeistai/export-input-staging.test.ts"),
    }

    assert expected <= verifier.REQUIRED_OVERLAY_FILES


def test_task6_static_verifier_requires_streamed_hash_only_staging(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    source_root = INTEGRATION_ROOT / "overlay"
    overlay_root = tmp_path / "overlay"
    for relative in verifier.TASK6_SOURCE_FILES:
        source = source_root / relative
        target = overlay_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    verifier.verify_task6_staging(overlay_root)

    staging = overlay_root / "lib/yfeistai/export-input-staging.ts"
    source = staging.read_text(encoding="utf-8")
    staging.write_text(source.replace("sourceManifestSha256", "objectKey", 1), encoding="utf-8")
    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_task6_staging(overlay_root)


def test_task4_static_verifier_requires_security_and_lifecycle_controls(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    source_root = INTEGRATION_ROOT / "overlay"
    overlay_root = tmp_path / "overlay"
    for relative in verifier.TASK4_SOURCE_FILES:
        source = source_root / relative
        target = overlay_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    verifier.verify_task4_sources(overlay_root)

    export_library = overlay_root / "lib/yfeistai/export-generation.ts"
    export_source = export_library.read_text(encoding="utf-8")
    weakened_csp = export_source.replace("connect-src 'none'", "connect-src *")
    assert weakened_csp != export_source
    export_library.write_text(weakened_csp, encoding="utf-8")
    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_task4_sources(overlay_root)
    export_library.write_text(export_source, encoding="utf-8")

    artifact = overlay_root / "lib/yfeistai/artifact-manifest.ts"
    source = artifact.read_text(encoding="utf-8")
    forged = source.replace(
        "if (stat.isSymbolicLink())",
        "if (false && stat.isSymbolicLink())",
    )
    assert forged != source
    artifact.write_text(forged, encoding="utf-8")
    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_task4_sources(overlay_root)

    artifact.write_text(source, encoding="utf-8")
    export_route = overlay_root / "app/api/yfeistai/v1/exports/route.ts"
    route_source = export_route.read_text(encoding="utf-8")
    unsafe_redirects = route_source.replace('redirect: "error"', 'redirect: "follow"')
    assert unsafe_redirects != route_source
    export_route.write_text(unsafe_redirects, encoding="utf-8")
    with pytest.raises(verifier.OverlayVerificationError):
        verifier.verify_task4_sources(overlay_root)


def test_cli_dispatches_each_task4_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load_verifier()
    called: list[tuple[Path, str]] = []
    monkeypatch.setattr(verifier, "verify_overlay", lambda: None)
    monkeypatch.setattr(
        verifier,
        "_run_task4_tests",
        lambda root, name: called.append((root, name)) or 0,
    )

    for name in (
        "content-generation",
        "cancel",
        "artifact-manifest",
        "export-generation",
    ):
        assert verifier.main(["--test", name]) == 0

    assert called == [
        (verifier.DEFAULT_INTEGRATION_ROOT, "content-generation"),
        (verifier.DEFAULT_INTEGRATION_ROOT, "cancel"),
        (verifier.DEFAULT_INTEGRATION_ROOT, "artifact-manifest"),
        (verifier.DEFAULT_INTEGRATION_ROOT, "export-generation"),
    ]


def test_cli_dispatches_export_input_staging_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier()
    called: list[tuple[Path, str]] = []
    monkeypatch.setattr(verifier, "verify_overlay", lambda: None)
    monkeypatch.setattr(
        verifier,
        "_run_task4_tests",
        lambda root, name: called.append((root, name)) or 0,
    )

    assert verifier.main(["--test", "export-input-staging"]) == 0
    assert called == [
        (verifier.DEFAULT_INTEGRATION_ROOT, "export-input-staging")
    ]


def test_pinned_image_build_uses_repository_context_and_fixed_dockerfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier()
    calls: list[tuple[list[str], Path, bool]] = []
    monkeypatch.setattr(verifier.shutil, "which", lambda name: "docker")

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    assert verifier._run_pinned_image_build(INTEGRATION_ROOT) == 0
    assert calls == [
        (
            [
                "docker",
                "build",
                "--file",
                str(INTEGRATION_ROOT / "Dockerfile"),
                "--tag",
                f"yfeistai/openmaic:verify-{EXPECTED_UPSTREAM['commit'][:12]}",
                str(ROOT),
            ],
            ROOT,
            False,
        )
    ]


def test_cli_build_gate_propagates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = _load_verifier()
    called: list[Path] = []
    monkeypatch.setattr(verifier, "verify_overlay", lambda: None)
    monkeypatch.setattr(
        verifier,
        "_run_pinned_image_build",
        lambda root: called.append(root) or 23,
    )

    assert verifier.main(["--build"]) == 23
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
