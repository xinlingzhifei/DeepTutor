from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAYWRIGHT_CONFIG = ROOT / "web" / "playwright.config.ts"
LIVE_SPEC = ROOT / "web" / "tests" / "e2e" / "classroom-first-release.live.spec.ts"
LIVE_FIXTURE = (
    ROOT
    / "web"
    / "tests"
    / "e2e"
    / "support"
    / "classroom-first-release-live-fixture.ts"
)
LIVE_FLOWS = (
    ROOT
    / "web"
    / "tests"
    / "e2e"
    / "support"
    / "classroom-first-release-live-flows.ts"
)
LIVE_SOURCE_IMPORT_ALLOWLIST = {
    LIVE_SPEC: frozenset(
        {
            "@playwright/test",
            "./support/classroom-first-release-live-fixture",
            "./support/classroom-first-release-live-flows",
        }
    ),
    LIVE_FIXTURE: frozenset({"node:crypto"}),
    LIVE_FLOWS: frozenset(
        {
            "@playwright/test",
            "./classroom-first-release-live-fixture",
        }
    ),
}


def _playwright_config_source() -> str:
    return PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")


def _compact_typescript(source: str) -> str:
    return re.sub(r"\s+", "", source)


def _typescript_static_module_sources(source: str) -> list[str]:
    statements = re.findall(r"(?m)^\s*import\b[^;]*;", source)
    modules: list[str] = []
    for statement in statements:
        match = re.search(r'(?:\bfrom\s+)?["\']([^"\']+)["\']\s*;', statement)
        assert match is not None, f"unrecognized TypeScript module statement: {statement}"
        modules.append(match.group(1))
    modules.extend(
        re.findall(
            r'(?m)^\s*export\b[^;]*\bfrom\s+["\']([^"\']+)["\']\s*;',
            source,
        )
    )
    return modules


def _balanced_brace_segment(source: str, opening: int) -> str:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(source)):
        character = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'", "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening : index + 1]
    raise AssertionError("TypeScript brace segment is not balanced")


def _object_containing(source: str, marker: str) -> str:
    marker_index = source.find(marker)
    assert marker_index >= 0, f"missing TypeScript marker: {marker}"
    opening = source.rfind("{", 0, marker_index)
    assert opening >= 0, f"missing object for TypeScript marker: {marker}"
    return _balanced_brace_segment(source, opening)


def _object_after(source: str, marker: str) -> str:
    marker_index = source.find(marker)
    assert marker_index >= 0, f"missing TypeScript marker: {marker}"
    opening = source.find("{", marker_index)
    assert opening >= 0, f"missing object after TypeScript marker: {marker}"
    return _balanced_brace_segment(source, opening)


def test_playwright_live_project_exposes_one_exact_spec_only_in_live_mode() -> None:
    source = _playwright_config_source()
    marker = 'name: "first-release-live"'
    live_project = _object_containing(source, marker)

    assert source.count(marker) == 1
    assert len(re.findall(r"\btestMatch\s*:", live_project)) == 1
    assert re.search(
        r'\btestMatch:\s*LIVE_PROJECT_SELECTED\s*\?\s*'
        r'"\*\*/e2e/classroom-first-release\.live\.spec\.ts"\s*:\s*\[\]\s*,',
        live_project,
    )
    assert live_project.count("classroom-first-release.live.spec.ts") == 1


def test_playwright_config_wires_live_policy_and_server_boundary() -> None:
    source = _playwright_config_source()
    compact = _compact_typescript(source)

    assert re.search(
        r'import\s*\{\s*isLivePlaywrightSelected\s*,\s*resolveLiveBaseUrl\s*,?\s*\}'
        r'\s*from\s*"\./playwright\.live-policy"\s*;',
        source,
    )
    assert re.search(
        r"\bconst\s+LIVE_PROJECT_SELECTED\s*=\s*isLivePlaywrightSelected\("
        r"\s*process\.argv\s*,\s*process\.env\.YFEISTAI_EVIDENCE\s*,?\s*\)\s*;",
        source,
    )
    assert re.search(
        r"\bconst\s+LIVE_BASE_URL\s*=\s*resolveLiveBaseUrl\("
        r"\s*LIVE_PROJECT_SELECTED\s*,\s*process\.env\.WEB_BASE_URL\s*,?\s*\)\s*;",
        source,
    )
    assert "functionresolveLiveBaseUrl(" not in compact
    assert (
        "constBASE_URL=LIVE_PROJECT_SELECTED?LIVE_BASE_URL:"
        "process.env.WEB_BASE_URL||LOCAL_BASE_URL;"
        in compact
    )
    assert (
        "constSTART_LOCAL_WEB_SERVER=!LIVE_PROJECT_SELECTED&&"
        "!process.env.WEB_BASE_URL;"
        in compact
    )
    assert (
        "globalSetup:START_LOCAL_WEB_SERVER?path.join(WEB_ROOT,"
        '"tests","e2e","support","managed-web-server.ts"):undefined,'
        in compact
    )


def test_playwright_live_project_is_fixed_serial_chromium_without_artifacts() -> None:
    source = _playwright_config_source()
    live_project = _object_containing(source, 'name: "first-release-live"')
    live_use = _object_after(live_project, 'use:')
    project_compact = _compact_typescript(live_project)
    use_compact = _compact_typescript(live_use)

    assert "fullyParallel:false," in project_compact
    assert "workers:1," in project_compact
    assert "retries:0," in project_compact
    assert re.search(r'\.\.\.devices\["Desktop Chrome"\]\s*,', live_use)
    assert 'channel:"chromium",' in use_compact
    assert 'locale:"en-US",' in use_compact
    assert 'timezoneId:"UTC",' in use_compact
    assert 'trace:"off",' in use_compact
    assert 'screenshot:"off",' in use_compact
    assert 'video:"off",' in use_compact


def test_live_spec_uses_only_real_sources_and_declares_task4_markers_once() -> None:
    live_sources: dict[Path, str] = {}
    for path in LIVE_SOURCE_IMPORT_ALLOWLIST:
        relative = path.relative_to(ROOT).as_posix()
        assert path.is_file(), f"fixed live source is missing: {relative}"
        live_sources[path] = path.read_text(encoding="utf-8")

    for path, allowed_imports in LIVE_SOURCE_IMPORT_ALLOWLIST.items():
        source = live_sources[path]
        imports = _typescript_static_module_sources(source)
        relative = path.relative_to(ROOT).as_posix()
        assert len(imports) == len(set(imports)), f"duplicate imports in {relative}"
        assert set(imports) == allowed_imports, (
            f"unexpected live import boundary in {relative}: {sorted(imports)}"
        )
        assert not re.search(r"\b(?:import|require)\s*\(", source)
        for forbidden_import in (
            "teaching-flow-test",
            "baseline",
            "teacher-classroom-flow",
            "content-operations-flow",
            "student-classroom-flow",
            "classroom-learning-loop",
            "classroom-visual-support",
        ):
            assert all(forbidden_import not in imported for imported in imports)
        for forbidden_source_path in (
            r"\bpage\s*\.\s*route\s*\(",
            r"\bbrowserContext\s*\.\s*route\s*\(",
            r"\broute\s*\.\s*fulfill\s*\(",
            r"\bscreenshots?\b",
            r"\btrace\b",
            r"\bvideos?\b",
            r"\battachments?\b",
            r"\bJSON\s*\.\s*stringify\s*\(",
            r"\bserialize\w*\s*\(",
            r"\btoJSON\s*\(",
        ):
            assert not re.search(forbidden_source_path, source, flags=re.IGNORECASE), (
                f"forbidden live source path in {relative}: {forbidden_source_path}"
            )

    for path in (LIVE_SPEC, LIVE_FLOWS):
        source = live_sources[path]
        relative = path.relative_to(ROOT).as_posix()
        for forbidden_live_operation in (
            r"\.\s*route(?:FromHAR)?\s*\(",
            r"\.\s*(?:post|put|patch|delete)\s*\(",
        ):
            assert not re.search(
                forbidden_live_operation,
                source,
                flags=re.IGNORECASE,
            ), f"forbidden live operation in {relative}: {forbidden_live_operation}"

    spec_imports = _typescript_static_module_sources(live_sources[LIVE_SPEC])
    assert "@playwright/test" in spec_imports
    combined_source = "\n".join(live_sources.values())
    assert combined_source.count("[release-evidence:teacher_flow]") == 1
    assert combined_source.count("[release-evidence:content_operations_flow]") == 1


def _load_probe():
    path = ROOT / "scripts" / "classroom_release_probe.py"
    assert path.is_file(), "fixed classroom release probe is missing"
    spec = importlib.util.spec_from_file_location("classroom_release_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _trusted_node_runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "trusted-node"
    runtime.mkdir()
    npm = runtime / "npm.cmd"
    npm.write_bytes(b"trusted npm launcher")
    (runtime / "node.exe").write_bytes(b"trusted node executable")
    return npm


def _probe_environment(tmp_path: Path, report: Path, evidence: str) -> dict[str, str]:
    return {
        "YFEISTAI_EVIDENCE_REPORT": str(report),
        "YFEISTAI_CANDIDATE_ROOT": str(tmp_path),
        "YFEISTAI_RELEASE_RUN_ID": "release-run-1",
        "YFEISTAI_ENVIRONMENT_ID": "staging-1",
        "YFEISTAI_EVIDENCE": evidence,
        "YFEISTAI_PROBE_TIMEOUT_SECONDS": "20",
        "YFEISTAI_LIVE_FIXTURE_TOKEN": "test-fixture-token-placeholder",
        "WEB_BASE_URL": "https://candidate.example.test",
    }


def test_probe_runs_only_the_fixed_teacher_recipe_and_writes_native_stdout(
    tmp_path: Path,
) -> None:
    module = _load_probe()
    npm = _trusted_node_runtime(tmp_path)
    report = tmp_path / "raw" / "teacher.json"
    native_stdout = b'{"config":{},"suites":[],"errors":[],"stats":{}}\n'
    captured: dict[str, object] = {}

    def run(arguments: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(arguments=arguments, options=options)
        return subprocess.CompletedProcess(arguments, 0, stdout=native_stdout)

    exit_code = module.main(
        ["teacher_flow"],
        environ=_probe_environment(tmp_path, report, "teacher_flow"),
        runner=run,
        runtime_resolver=lambda: module.resolve_fixed_node_runtime(
            platform="win32", trusted_roots=(npm.parent,)
        ),
    )

    assert exit_code == 0
    assert captured["arguments"] == [
        str(npm),
        "--prefix",
        "web",
        "exec",
        "playwright",
        "--",
        "test",
        "tests/e2e/classroom-first-release.live.spec.ts",
        "--project=first-release-live",
        "--grep",
        r"\[release-evidence:teacher_flow\]",
        "--reporter=json",
        "--workers=1",
        "--retries=0",
    ]
    options = captured["options"]
    assert isinstance(options, dict)
    assert options["cwd"] == ROOT
    assert options["stdout"] == subprocess.PIPE
    assert options["check"] is False
    child_environment = options["env"]
    assert isinstance(child_environment, dict)
    assert child_environment["WEB_BASE_URL"] == "https://candidate.example.test"
    assert child_environment["YFEISTAI_RELEASE_RUN_ID"] == "release-run-1"
    assert report.read_bytes() == native_stdout


def test_probe_command_is_built_from_the_canonical_descriptor(tmp_path: Path) -> None:
    module = _load_probe()
    npm = _trusted_node_runtime(tmp_path)

    descriptor = module.probe_command_descriptor("teacher_flow")

    assert descriptor == {
        "commandId": "yfeistai.classroom-release.playwright",
        "version": 1,
        "evidence": "teacher_flow",
        "innerNpmArgv": module._command("teacher_flow", str(npm))[1:],
        "liveSpec": "tests/e2e/classroom-first-release.live.spec.ts",
        "project": "first-release-live",
        "grep": r"\[release-evidence:teacher_flow\]",
        "reporter": "json",
        "workers": 1,
        "retries": 0,
        "reportFormat": "playwright-json-reporter",
        "environmentPolicyVersion": 2,
    }


@pytest.mark.parametrize(
    "fixture_token",
    (
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param(" \t ", id="blank"),
    ),
)
def test_probe_rejects_missing_or_blank_fixture_token_before_execution(
    tmp_path: Path,
    fixture_token: str | None,
) -> None:
    module = _load_probe()
    report = tmp_path / "probe.json"
    environment = _probe_environment(tmp_path, report, "teacher_flow")
    if fixture_token is None:
        environment.pop("YFEISTAI_LIVE_FIXTURE_TOKEN")
    else:
        environment["YFEISTAI_LIVE_FIXTURE_TOKEN"] = fixture_token
    calls: list[str] = []

    def resolve_runtime() -> tuple[str, Path]:
        calls.append("runtime_resolver")
        return "C:/trusted/npm.cmd", Path("C:/trusted")

    def run(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        calls.append("runner")
        return subprocess.CompletedProcess(arguments, 0, stdout=b"{}")

    error: ValueError | None = None
    try:
        module.main(
            ["teacher_flow"],
            environ=environment,
            runner=run,
            runtime_resolver=resolve_runtime,
        )
    except ValueError as exc:
        error = exc

    assert calls == []
    assert error is not None
    assert str(error) == "probe environment is incomplete"


def test_probe_forwards_only_the_exact_live_fixture_secret(tmp_path: Path) -> None:
    module = _load_probe()
    npm = _trusted_node_runtime(tmp_path)
    report = tmp_path / "probe.json"
    fixture_token = "focused-test-fixture-token"
    captured: dict[str, object] = {}

    def run(arguments: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(arguments=arguments, environment=options["env"])
        return subprocess.CompletedProcess(arguments, 0, stdout=b"{}")

    module.main(
        ["teacher_flow"],
        environ={
            **_probe_environment(tmp_path, report, "teacher_flow"),
            "YFEISTAI_LIVE_FIXTURE_TOKEN": fixture_token,
            "YFEISTAI_LIVE_PLATFORM_ADMIN_TOKEN": "must-not-pass",
            "YFEISTAI_LIVE_FIXTURE_TOKEN_BACKUP": "must-not-pass",
        },
        runner=run,
        runtime_resolver=lambda: module.resolve_fixed_node_runtime(
            platform="win32", trusted_roots=(npm.parent,)
        ),
    )

    child_environment = captured["environment"]
    arguments = captured["arguments"]
    assert isinstance(child_environment, dict)
    assert isinstance(arguments, list)
    assert child_environment["YFEISTAI_LIVE_FIXTURE_TOKEN"] == fixture_token
    assert "YFEISTAI_LIVE_PLATFORM_ADMIN_TOKEN" not in child_environment
    assert "YFEISTAI_LIVE_FIXTURE_TOKEN_BACKUP" not in child_environment
    assert all(fixture_token not in argument for argument in arguments)
    assert fixture_token not in json.dumps(module.probe_command_descriptor("teacher_flow"))


@pytest.mark.parametrize(
    "forbidden",
    (
        "--command",
        "--test-file",
        "--project",
        "--grep",
        "--reporter",
        "--workers",
        "--retries",
    ),
)
def test_probe_cli_rejects_command_injection_inputs(forbidden: str) -> None:
    module = _load_probe()

    with pytest.raises(SystemExit):
        module._parse_args(["teacher_flow", forbidden, "attacker-controlled"])


def test_probe_preserves_non_json_stdout_and_returns_native_failure(
    tmp_path: Path,
) -> None:
    module = _load_probe()
    npm = _trusted_node_runtime(tmp_path)
    report = tmp_path / "probe.json"
    native_stdout = b"playwright failed before producing JSON\r\n"

    def run(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(arguments, 7, stdout=native_stdout)

    exit_code = module.main(
        ["student_micro_flow"],
        environ={
            **_probe_environment(tmp_path, report, "student_micro_flow"),
            "YFEISTAI_EVIDENCE_PASS": "true",
            "YFEISTAI_EVIDENCE_CHECKS": '{"studentMicroFlowPassed":true}',
        },
        runner=run,
        runtime_resolver=lambda: module.resolve_fixed_node_runtime(
            platform="win32", trusted_roots=(npm.parent,)
        ),
    )

    assert exit_code == 7
    assert report.read_bytes() == native_stdout


def test_probe_filters_hostile_host_environment_before_npm(
    tmp_path: Path,
) -> None:
    module = _load_probe()
    npm = _trusted_node_runtime(tmp_path)
    report = tmp_path / "probe.json"
    captured: dict[str, str] = {}
    hostile = {
        "NODE_OPTIONS": "--require=attacker.js",
        "node_path": "C:/attacker/modules",
        "PW_TEST_HTML_REPORT_OPEN": "always",
        "playwright_json_output_name": "attacker.json",
        "PLAYWRIGHT_CONNECT_WS_ENDPOINT": "ws://attacker.invalid",
        "npm_config_registry": "https://attacker.invalid",
        "NPM_CONFIG_USERCONFIG": "C:/attacker/.npmrc",
        "BABEL_ENV": "attacker",
        "RANDOM_HOST_SECRET": "must-not-pass",
    }

    def run(arguments: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        child = options["env"]
        assert isinstance(child, dict)
        captured.update(child)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"{}")

    module.main(
        ["teacher_flow"],
        environ={
            **_probe_environment(tmp_path, report, "teacher_flow"),
            **hostile,
            "SystemRoot": "C:/Windows",
            "TEMP": str(tmp_path / "temp"),
            "YFEISTAI_LIVE_FIXTURE_TOKEN": "fixture-token",
        },
        runner=run,
        runtime_resolver=lambda: module.resolve_fixed_node_runtime(
            platform="win32", trusted_roots=(npm.parent,)
        ),
    )

    upper_child = {name.upper() for name in captured}
    assert not upper_child.intersection(
        {
            "NODE_OPTIONS",
            "NODE_PATH",
            "PW_TEST_HTML_REPORT_OPEN",
            "PLAYWRIGHT_JSON_OUTPUT_NAME",
            "PLAYWRIGHT_CONNECT_WS_ENDPOINT",
            "NPM_CONFIG_REGISTRY",
            "NPM_CONFIG_USERCONFIG",
            "BABEL_ENV",
            "RANDOM_HOST_SECRET",
        }
    )
    assert captured["PATH"] == str(npm.parent)
    assert captured["WEB_BASE_URL"] == "https://candidate.example.test"
    assert captured["YFEISTAI_LIVE_FIXTURE_TOKEN"] == "fixture-token"


def test_default_runtime_resolver_ignores_path_hijack_and_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_probe()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "npm.cmd").write_bytes(b"attacker npm")
    (attacker / "node.exe").write_bytes(b"attacker node")
    missing_trusted_root = tmp_path / "trusted-system-nodejs"
    monkeypatch.setenv("PATH", str(attacker))
    monkeypatch.setattr(module, "WINDOWS_RUNTIME_ROOTS", (missing_trusted_root,))

    with pytest.raises(ValueError, match="trusted.*runtime"):
        module.resolve_fixed_node_runtime(platform="win32")


def test_fixed_runtime_resolver_accepts_only_a_trusted_root(tmp_path: Path) -> None:
    module = _load_probe()
    trusted_root = tmp_path / "Program Files" / "nodejs"
    trusted_root.mkdir(parents=True)
    npm = trusted_root / "npm.cmd"
    node = trusted_root / "node.exe"
    npm.write_bytes(b"trusted npm")
    node.write_bytes(b"trusted node")

    resolved_npm, resolved_root = module.resolve_fixed_node_runtime(
        platform="win32",
        trusted_roots=(trusted_root,),
    )

    assert resolved_npm == str(npm)
    assert resolved_root == trusted_root


def test_fixed_runtime_resolver_rejects_symlink_resolution_outside_trusted_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_probe()
    trusted_root = tmp_path / "Program Files" / "nodejs"
    trusted_root.mkdir(parents=True)
    npm = trusted_root / "npm.cmd"
    node = trusted_root / "node.exe"
    npm.write_bytes(b"npm symlink placeholder")
    node.write_bytes(b"trusted node")
    outside = tmp_path / "attacker" / "npm.cmd"
    outside.parent.mkdir()
    outside.write_bytes(b"attacker npm")
    real_resolve = module.Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == npm:
            return outside
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(module.Path, "resolve", resolve)
    with pytest.raises(ValueError, match="trusted.*runtime"):
        module.resolve_fixed_node_runtime(
            platform="win32",
            trusted_roots=(trusted_root,),
        )


@pytest.mark.parametrize("case", ("relative-root", "missing-npm", "missing-node", "npm-directory"))
def test_probe_rejects_untrusted_npm_or_node_boundary(tmp_path: Path, case: str) -> None:
    module = _load_probe()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    npm = runtime / "npm.cmd"
    if case == "npm-directory":
        npm.mkdir()
    elif case != "missing-npm":
        npm.write_bytes(b"npm")
    if case not in {"missing-node", "relative-root"}:
        (runtime / "node.exe").write_bytes(b"node")
    root = Path("relative-runtime") if case == "relative-root" else runtime

    with pytest.raises(ValueError, match="trusted.*runtime"):
        module.resolve_fixed_node_runtime(
            platform="win32",
            trusted_roots=(root,),
        )


def test_timeout_terminates_only_the_owned_tree_and_waits() -> None:
    module = _load_probe()
    events: list[str] = []

    class OwnedProcess:
        pid = 4242
        returncode = 124
        calls = 0

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                events.append(f"communicate:{timeout}")
                raise subprocess.TimeoutExpired(["npm"], timeout)
            events.append("communicate:final")
            return b"partial timeout diagnostics", None

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            return self.returncode

    owned = OwnedProcess()

    def terminate(process, *, platform: str) -> None:
        assert process is owned
        events.append(f"terminate:{platform}:{process.pid}")
        process.wait(timeout=5)

    completed = module._run(
        ["C:/trusted/npm.cmd"],
        cwd=ROOT,
        env={},
        stdout=subprocess.PIPE,
        check=False,
        timeout_seconds=3,
        popen_factory=lambda *_args, **_options: owned,
        tree_terminator=terminate,
        platform="win32",
    )

    assert completed.returncode == module.TIMEOUT_EXIT
    assert completed.stdout == b"partial timeout diagnostics"
    assert events == [
        "communicate:3",
        "terminate:win32:4242",
        "wait:5",
        "communicate:final",
    ]


def test_windows_tree_helper_targets_only_the_owned_pid_and_waits(tmp_path: Path) -> None:
    module = _load_probe()
    system_root = tmp_path / "Windows"
    taskkill = system_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b"trusted taskkill")
    calls: list[list[str]] = []
    waits: list[int] = []

    class OwnedProcess:
        pid = 4242

        def wait(self, timeout: int) -> int:
            waits.append(timeout)
            return 1

    def run(arguments: list[str], **options: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        assert options["check"] is False
        return subprocess.CompletedProcess(arguments, 0)

    module._terminate_owned_process_tree(
        OwnedProcess(),
        platform="win32",
        environment={"SystemRoot": str(system_root)},
        command_runner=run,
    )

    assert calls == [[str(taskkill), "/PID", "4242", "/T", "/F"]]
    assert waits == [module.TERMINATION_WAIT_SECONDS]


def test_atomic_raw_write_cleans_only_its_temp_when_replace_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_probe()
    report = tmp_path / "probe.json"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        module._atomic_write(report, b"raw")

    assert not report.exists()
    assert list(tmp_path.glob(".probe.json.*.tmp")) == []
