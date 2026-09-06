from __future__ import annotations

import ast
import asyncio
import base64
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import inspect
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OBSERVER_OVERLAY_PATH = (
    PROJECT_ROOT / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml"
)
OBSERVER_SERVICE_PATH = PROJECT_ROOT / "scripts" / "openmaic_observer_service.py"
OBSERVER_SIGNER_PATH = PROJECT_ROOT / "scripts" / "openmaic_observer_attestation_signer.py"
OBSERVER_CONTRACT_PATH = PROJECT_ROOT / "scripts" / "openmaic_smoke_contract.py"
OUTAGE_PROBE_PATH = PROJECT_ROOT / "scripts" / "openmaic_dedicated_outage_probe.py"
OUTAGE_RUNNER_PATH = PROJECT_ROOT / "scripts" / "openmaic_dedicated_outage_runner.py"
RELEASE_EVIDENCE_PATH = PROJECT_ROOT / "scripts" / "classroom_release_evidence.py"
FINAL_VERIFIER_PATH = PROJECT_ROOT / "scripts" / "verify_classroom_release.py"
COMPOSE_WRAPPER_PATH = PROJECT_ROOT / "scripts" / "docker_compose.py"
IMAGE_LOCK_RENDERER_PATH = PROJECT_ROOT / "scripts" / "render_platform_compose.py"
RUNTIME_ATTESTATION_PATH = PROJECT_ROOT / "scripts" / "classroom_runtime_attestation.py"
PLATFORM_PREFLIGHT_PATH = PROJECT_ROOT / "scripts" / "platform_preflight.py"
BOOTSTRAP_CONTRACT_TEST_PATH = (
    PROJECT_ROOT / "tests" / "scripts" / "test_bootstrap_shared_data_plane.py"
)
TEACHING_HEALTH_PATH = PROJECT_ROOT / "deeptutor" / "api" / "routers" / "teaching_health.py"
EXPORT_INPUT_STAGING_PATH = (
    PROJECT_ROOT
    / "integrations"
    / "openmaic"
    / "overlay"
    / "lib"
    / "yfeistai"
    / "export-input-staging.ts"
)
ARTIFACT_MANIFEST_PATH = (
    PROJECT_ROOT
    / "integrations"
    / "openmaic"
    / "overlay"
    / "lib"
    / "yfeistai"
    / "artifact-manifest.ts"
)
OBSERVER_SERVICE_NAME = "openmaic-shared-ingress-observer"
OBSERVER_HEALTH_PATH = "/api/v1/system/teaching-health/observer-canary"
CANDIDATE_HEALTH_URL = "https://candidate.example.test" + OBSERVER_HEALTH_PATH


def _load_source_module(path: Path, name: str):
    assert path.is_file(), f"required source is missing: {path.relative_to(PROJECT_ROOT)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(name, None)


def _observer_overlay() -> dict[str, Any]:
    assert OBSERVER_OVERLAY_PATH.is_file(), (
        "the run-scoped OpenMAIC shared-ingress observer Compose overlay is missing"
    )
    document = yaml.safe_load(OBSERVER_OVERLAY_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert isinstance(document.get("services"), dict)
    return document


def _tokens(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return tuple(value)


def _service_secret_bindings(service: Mapping[str, object]) -> dict[str, str]:
    bindings: dict[str, str] = {}
    raw_secrets = service.get("secrets", [])
    assert isinstance(raw_secrets, list)
    for raw in raw_secrets:
        if isinstance(raw, str):
            bindings[raw] = raw
            continue
        assert isinstance(raw, dict)
        source = raw.get("source")
        target = raw.get("target", source)
        assert isinstance(source, str) and isinstance(target, str)
        bindings[target] = source
    return bindings


def _function_node(path: Path, function_name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == function_name
    ]
    assert len(matches) == 1
    return matches[0]


def _decorator_dependency_names(path: Path, function_name: str) -> set[str]:
    node = _function_node(path, function_name)
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        if isinstance(function, ast.Name) and function.id == "Depends" and child.args:
            dependency = child.args[0]
            if isinstance(dependency, ast.Name):
                names.add(dependency.id)
            elif isinstance(dependency, ast.Attribute):
                names.add(dependency.attr)
    return names


def _function_parameter_names(path: Path, function_name: str) -> set[str]:
    node = _function_node(path, function_name)
    return {
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
    }


def _call_keyword_sets(
    path: Path,
    function_name: str,
    called_function_name: str,
) -> tuple[set[str], ...]:
    node = _function_node(path, function_name)
    matches: list[set[str]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        function = child.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else None
        )
        if name == called_function_name:
            matches.append({keyword.arg for keyword in child.keywords if keyword.arg is not None})
    assert matches, f"{function_name} must call {called_function_name}"
    return tuple(matches)


def _observer_config(
    module,
    *,
    candidate_binding: Mapping[str, str] | None = None,
    public_key_spki_sha256: str = "c" * 64,
):
    token_fields = {
        name
        for name in module.ObserverServiceConfig.__annotations__
        if name.startswith("candidate_") and name.endswith("_token")
    }
    assert len(token_fields) == 1
    token_field = token_fields.pop()
    values = {
        "auth_token": module.SecretStr("observer-run-token-" + "9" * 48),
        "auth_expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
        token_field: module.SecretStr("candidate-health-token-" + "8" * 48),
        "candidate_health_url": CANDIDATE_HEALTH_URL,
        "candidate_sha256": (
            candidate_binding["candidateSha256"] if candidate_binding is not None else "a" * 64
        ),
        "environment_id": "environment-observer-contract-01",
        "image_lock_sha256": (
            candidate_binding["imageLockSha256"] if candidate_binding is not None else "b" * 64
        ),
        "observer_id": "shared-ingress-observer-contract-01",
        "observer_public_key_spki_sha256": public_key_spki_sha256,
        "observer_url": "https://observer.example.test",
        "release_run_id": "run-observer-contract-01",
        "shared_ingress_control_url": ("https://shared-ingress.example.test/v1/control-canaries"),
        "shared_ingress_upstream_url": "https://openmaic-upstream.example.test",
        "state_limit": 64,
    }
    if "internal_proxy_url" in module.ObserverServiceConfig.__annotations__:
        values["internal_proxy_url"] = f"http://{OBSERVER_SERVICE_NAME}:8000"
    return module.ObserverServiceConfig(**values)


def _signed_headers() -> tuple[tuple[bytes, bytes], ...]:
    return (
        (b"x-yfeistai-tenant-id", b"tenant-observer-contract-01"),
        (b"x-yfeistai-job-id", b"job-observer-contract-01"),
        (b"x-yfeistai-timestamp", b"2026-08-31T00:00:00Z"),
        (b"x-yfeistai-idempotency-key", b"observer-contract-idempotency-01"),
        (b"x-yfeistai-signature", b"a" * 64),
    )


async def _asgi_request(
    app,
    *,
    method: str,
    path: str,
    receive,
    host: bytes = b"shared-ingress.example.test",
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def send(event: dict[str, Any]) -> None:
        events.append(event)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": (
                (b"host", host),
                *_signed_headers(),
            ),
        },
        receive,
        send,
    )
    return events


def test_observer_overlay_mediates_only_the_shared_platform_route_with_fail_stop_singleton() -> (
    None
):
    overlay = _observer_overlay()
    services = overlay["services"]
    assert isinstance(services, dict)
    assert OBSERVER_SERVICE_NAME in services
    assert "shared-data-plane-bootstrap" in services
    assert all("dedicated" not in name for name in services)

    observer = services[OBSERVER_SERVICE_NAME]
    assert isinstance(observer, dict)
    assert observer.get("restart") == "no"
    assert observer.get("scale") == 1
    assert not observer.get("ports")
    assert set(observer.get("networks", [])) == {
        "platform-internal",
        "platform-service-egress",
    }
    assert observer.get("depends_on", {}).get("openmaic", {}).get("condition") == (
        "service_healthy"
    )

    bootstrap = services["shared-data-plane-bootstrap"]
    assert isinstance(bootstrap, dict)
    command = _tokens(bootstrap.get("command"))
    assert "--base-url" in command
    observer_base_url = command[command.index("--base-url") + 1]
    assert observer_base_url == f"http://{OBSERVER_SERVICE_NAME}:8000"
    assert "openmaic:3000" not in " ".join(command)


def test_observer_bootstrap_reads_external_candidate_lock_from_read_only_mount() -> None:
    overlay = _observer_overlay()
    bootstrap = overlay["services"]["shared-data-plane-bootstrap"]
    assert isinstance(bootstrap, dict)

    command = _tokens(bootstrap.get("command"))
    assert "--candidate-root" in command
    candidate_root = command[command.index("--candidate-root") + 1]
    assert candidate_root == "/run/yfeistai-candidate"
    assert candidate_root != "/app"

    candidate_mounts = [
        volume
        for volume in bootstrap.get("volumes", ())
        if isinstance(volume, dict) and volume.get("target") == candidate_root
    ]
    assert candidate_mounts == [
        {
            "type": "bind",
            "source": "${YFEISTAI_CANDIDATE_ROOT:?set candidate root}",
            "target": candidate_root,
            "read_only": True,
        }
    ]
    assert f"{candidate_root}/deploy/image-lock.json" == (
        "/run/yfeistai-candidate/deploy/image-lock.json"
    )


def test_observer_runtime_mounts_distinct_run_scoped_owner_only_secrets_outside_candidate() -> None:
    overlay = _observer_overlay()
    services = overlay["services"]
    observer = services[OBSERVER_SERVICE_NAME]
    assert isinstance(observer, dict)
    environment = observer.get("environment")
    assert isinstance(environment, dict)

    secret_files = {
        "observer": environment.get("YFEISTAI_SHARED_INGRESS_AUTH_TOKEN_FILE"),
        "candidate_health": environment.get("YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE"),
    }
    assert all(
        isinstance(path, str) and path.startswith("/run/secrets/") for path in secret_files.values()
    )
    assert len(set(secret_files.values())) == len(secret_files)

    bindings = _service_secret_bindings(observer)
    sources = {bindings[Path(path).name] for path in secret_files.values()}
    assert len(sources) == len(secret_files)
    definitions = overlay.get("secrets")
    assert isinstance(definitions, dict)
    source_paths = []
    for source in sources:
        definition = definitions.get(source)
        assert isinstance(definition, dict)
        source_path = definition.get("file")
        assert isinstance(source_path, str)
        source_paths.append(source_path)
    assert len(set(source_paths)) == len(source_paths)
    for source_path in source_paths:
        assert "${YFEISTAI_OBSERVER_SECRET_DIR:?" in source_path
        assert "${YFEISTAI_RELEASE_RUN_ID:?" in source_path
        assert not source_path.startswith(".")
        assert "YFEISTAI_CANDIDATE_ROOT" not in source_path
        assert "release-candidate" not in source_path.lower()

    source = OBSERVER_SERVICE_PATH.read_text(encoding="utf-8")
    assert "before.st_uid != os.geteuid()" in source
    assert "stat.S_IMODE(before.st_mode) & 0o077" in source
    assert "O_NOFOLLOW" in source
    assert "_path_has_reparse_component" in source


def test_admin_health_and_observer_canary_use_distinct_noninterchangeable_authorities() -> None:
    observer_source = OBSERVER_SERVICE_PATH.read_text(encoding="utf-8")
    assert "candidate_admin_token" not in observer_source
    assert "YFEISTAI_SHARED_INGRESS_CANDIDATE_TOKEN_FILE" not in observer_source
    assert "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_TOKEN_FILE" in observer_source

    admin_dependencies = _decorator_dependency_names(TEACHING_HEALTH_PATH, "teaching_health")
    observer_dependencies = _decorator_dependency_names(
        TEACHING_HEALTH_PATH,
        "observer_canary_health",
    )
    assert "require_platform_admin" in admin_dependencies
    assert not any("observer" in name.lower() for name in admin_dependencies)
    assert "require_platform_admin" not in observer_dependencies
    assert any(
        "observer" in name.lower()
        and any(label in name.lower() for label in ("canary", "credential", "token"))
        for name in observer_dependencies
    )
    assert "observer_canary" not in _function_parameter_names(
        TEACHING_HEALTH_PATH,
        "teaching_health",
    )
    observer_parameters = _function_parameter_names(
        TEACHING_HEALTH_PATH,
        "observer_canary_health",
    )
    assert "observer_canary" in observer_parameters
    assert "runtime_repository" not in observer_parameters
    assert OBSERVER_HEALTH_PATH in TEACHING_HEALTH_PATH.read_text(encoding="utf-8")
    assert OBSERVER_HEALTH_PATH in observer_source

    overlay = _observer_overlay()
    services = overlay["services"]
    observer = services[OBSERVER_SERVICE_NAME]
    deeptutor = services.get("deeptutor")
    assert isinstance(observer, dict) and isinstance(deeptutor, dict)
    observer_secrets = set(_service_secret_bindings(observer).values())
    deeptutor_secrets = set(_service_secret_bindings(deeptutor).values())
    shared_secrets = observer_secrets & deeptutor_secrets
    assert len(shared_secrets) == 1
    assert "health" in next(iter(shared_secrets)) or "canary" in next(iter(shared_secrets))
    assert all("admin" not in secret.lower() for secret in observer_secrets)


def test_observer_proxy_streams_export_inputs_and_artifacts_at_contract_limits() -> None:
    module = _load_source_module(
        OBSERVER_SERVICE_PATH,
        "openmaic_observer_service_deployment_contract_under_test",
    )
    config = _observer_config(module)

    async def upload_scenario() -> None:
        upstream_started = asyncio.Event()
        receive_count = 0

        async def requester(**kwargs):
            content = kwargs["content"]
            assert not isinstance(content, (bytes, bytearray)), (
                "export input uploads must reach the upstream as an async byte stream"
            )
            upstream_started.set()
            chunks = [chunk async for chunk in content]
            assert chunks == [b"first-", b"second"]
            return module.HTTPResponse(
                status_code=201,
                headers={"content-type": "application/json", "content-length": "3"},
                body=b"{}\n",
            )

        async def receive() -> dict[str, Any]:
            nonlocal receive_count
            receive_count += 1
            if receive_count == 1:
                return {"type": "http.request", "body": b"first-", "more_body": True}
            try:
                await asyncio.wait_for(upstream_started.wait(), timeout=0.25)
            except TimeoutError:
                raise AssertionError(
                    "observer buffered the complete export input before opening the upstream"
                ) from None
            return {"type": "http.request", "body": b"second", "more_body": False}

        app = module.OpenMAICObserverService(config, requester=requester)
        events = await _asgi_request(
            app,
            method="PUT",
            path=(
                "/api/yfeistai/v1/export-inputs/job-observer-contract-01/files/"
                "file-observer-contract-01"
            ),
            receive=receive,
        )
        assert events[-1]["type"] == "http.response.body"

    async def artifact_scenario() -> None:
        async def artifact_body() -> AsyncIterator[bytes]:
            yield b"first-"
            yield b"second"

        async def requester(**_kwargs):
            return module.HTTPResponse(
                status_code=200,
                headers={
                    "content-type": "application/octet-stream",
                    "content-length": "12",
                },
                body=artifact_body(),
            )

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        app = module.OpenMAICObserverService(config, requester=requester)
        events = await _asgi_request(
            app,
            method="GET",
            path=("/api/yfeistai/v1/artifacts/job-observer-contract-01/classroom-export.zip"),
            receive=receive,
        )
        body_events = [event for event in events if event["type"] == "http.response.body"]
        assert [(event["body"], event.get("more_body", False)) for event in body_events] == [
            (b"first-", True),
            (b"second", False),
        ]

    asyncio.run(upload_scenario())
    asyncio.run(artifact_scenario())

    export_source = EXPORT_INPUT_STAGING_PATH.read_text(encoding="utf-8")
    artifact_source = ARTIFACT_MANIFEST_PATH.read_text(encoding="utf-8")
    assert "const MAX_MEDIA_BYTES = 128 * 1024 * 1024" in export_source
    assert "export const MAX_ARTIFACT_BYTES = 256 * 1024 * 1024" in artifact_source
    assert module._MAX_PROXY_REQUEST_BYTES >= 128 * 1024 * 1024
    assert module._MAX_PROXY_RESPONSE_BYTES == 256 * 1024 * 1024


def test_observer_attestation_requires_independent_signature_and_external_public_key_anchor() -> (
    None
):
    contract = _load_source_module(
        OBSERVER_CONTRACT_PATH,
        "openmaic_observer_contract_signature_under_test",
    )
    parser = contract.parse_openmaic_shared_ingress_observer_attestation
    parameters = set(inspect.signature(parser).parameters)
    assert any("public" in name and "key" in name and "path" in name for name in parameters), (
        "observer attestation verification must require an external public-key path"
    )
    assert any("candidate" in name and "root" in name for name in parameters), (
        "the public-key anchor must be proven outside the candidate root"
    )
    parser_source = inspect.getsource(parser)
    assert "signature" in parser_source.lower()
    assert ".verify(" in parser_source

    overlay = _observer_overlay()
    services = overlay["services"]
    observer = services[OBSERVER_SERVICE_NAME]
    observer_secret_sources = set(_service_secret_bindings(observer).values())
    signer_candidates = []
    for name, raw_service in services.items():
        if name == OBSERVER_SERVICE_NAME or not isinstance(raw_service, dict):
            continue
        secret_sources = set(_service_secret_bindings(raw_service).values())
        if any("signing" in source and "key" in source for source in secret_sources):
            signer_candidates.append(raw_service)
    assert len(signer_candidates) == 1
    signer = signer_candidates[0]
    signer_secret_sources = set(_service_secret_bindings(signer).values())
    signing_secrets = {name for name in signer_secret_sources if "sign" in name and "key" in name}
    assert len(signing_secrets) == 1
    assert signing_secrets.isdisjoint(observer_secret_sources)

    definitions = overlay.get("secrets")
    assert isinstance(definitions, dict)
    public_anchors = {
        name: value
        for name, value in definitions.items()
        if "public" in name and "key" in name and isinstance(value, dict)
    }
    assert len(public_anchors) == 1
    public_key_path = next(iter(public_anchors.values())).get("file")
    assert isinstance(public_key_path, str)
    assert "${YFEISTAI_OBSERVER_TRUST_ROOT:?" in public_key_path
    assert not public_key_path.startswith(".")


def test_observer_overlay_is_the_fixed_third_platform_compose_across_candidate_boundaries() -> None:
    renderer = _load_source_module(
        IMAGE_LOCK_RENDERER_PATH,
        "observer_overlay_image_lock_renderer_contract_under_test",
    )
    artifacts = renderer.candidate_artifact_paths(PROJECT_ROOT)
    assert getattr(artifacts, "observer_overlay", None) == OBSERVER_OVERLAY_PATH, (
        "candidate artifact paths must bind the observer overlay"
    )
    assert renderer.COMPOSE_IMAGE_NAMES[OBSERVER_OVERLAY_PATH.name] == ()

    wrapper = _load_source_module(
        COMPOSE_WRAPPER_PATH,
        "observer_overlay_compose_wrapper_contract_under_test",
    )
    arguments = wrapper.platform_compose_topology_arguments(
        deployment_root=PROJECT_ROOT,
        candidate_root=PROJECT_ROOT,
        env_file=PROJECT_ROOT / "data" / "user" / "settings" / "docker.env",
    )
    compose_paths = [
        Path(arguments[index + 1]) for index, value in enumerate(arguments) if value == "-f"
    ]
    assert compose_paths == [
        PROJECT_ROOT / "docker-compose.yml",
        PROJECT_ROOT / "docker-compose.platform.yml",
        OBSERVER_OVERLAY_PATH,
    ]
    with pytest.raises(SystemExit, match="topology overrides are not allowed"):
        wrapper._parse_topology_args(["--platform", "up", "--scale", f"{OBSERVER_SERVICE_NAME}=2"])

    runtime = _load_source_module(
        RUNTIME_ATTESTATION_PATH,
        "observer_overlay_runtime_attestation_contract_under_test",
    )
    assert OBSERVER_OVERLAY_PATH in runtime._CandidateContractLease(PROJECT_ROOT).paths
    image_lock_source = inspect.getsource(wrapper._validate_production_image_lock)
    assert "observer_overlay" in image_lock_source


def test_observer_internal_proxy_is_independent_ready_and_bootstrap_waits_for_signature() -> None:
    module = _load_source_module(
        OBSERVER_SERVICE_PATH,
        "observer_internal_endpoint_contract_under_test",
    )
    assert "internal_proxy_url" in module.ObserverServiceConfig.__annotations__, (
        "observer endpoint model must bind an independent internal proxy authority"
    )
    config = _observer_config(module)
    assert config.internal_proxy_url == f"http://{OBSERVER_SERVICE_NAME}:8000"
    authorities = {
        urlsplit(value).netloc.lower()
        for value in (
            config.internal_proxy_url,
            config.observer_url,
            config.shared_ingress_control_url,
            config.candidate_health_url,
            config.shared_ingress_upstream_url,
        )
    }
    assert len(authorities) == 5

    async def readiness_scenario() -> None:
        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        events = await _asgi_request(
            module.OpenMAICObserverService(config),
            method="GET",
            path="/healthz",
            receive=receive,
            host=f"{OBSERVER_SERVICE_NAME}:8000".encode("ascii"),
        )
        starts = [event for event in events if event["type"] == "http.response.start"]
        assert [event["status"] for event in starts] == [200]

    asyncio.run(readiness_scenario())
    overlay = _observer_overlay()
    services = overlay["services"]
    observer = services[OBSERVER_SERVICE_NAME]
    assert isinstance(observer, dict)
    environment = observer.get("environment")
    assert isinstance(environment, dict)
    assert environment.get("YFEISTAI_SHARED_INGRESS_INTERNAL_PROXY_URL") == (
        f"http://{OBSERVER_SERVICE_NAME}:8000"
    )
    assert isinstance(observer.get("healthcheck"), dict)

    bootstrap = services["shared-data-plane-bootstrap"]
    assert isinstance(bootstrap, dict)
    dependencies = bootstrap.get("depends_on")
    assert isinstance(dependencies, dict)
    assert dependencies.get(OBSERVER_SERVICE_NAME, {}).get("condition") == "service_healthy"
    assert dependencies.get("openmaic-observer-attestation-signer", {}).get("condition") == (
        "service_completed_successfully"
    )


def test_observer_signer_is_workload_independent_networkless_and_candidate_bound() -> None:
    overlay = _observer_overlay()
    signer = overlay["services"]["openmaic-observer-attestation-signer"]
    assert isinstance(signer, dict)
    image = signer.get("image")
    assert isinstance(image, str)
    assert "YFEISTAI_OBSERVER_SIGNER_IMAGE" in image
    assert "YFEISTAI_DEEPTUTOR_IMAGE" not in image
    assert signer.get("network_mode") == "none"
    assert not signer.get("networks")
    assert signer.get("depends_on", {}).get(OBSERVER_SERVICE_NAME, {}).get("condition") == (
        "service_healthy"
    )

    tokens = _tokens(signer.get("command"))
    command = " ".join(tokens)
    assert "until " not in command
    assert "--wait-timeout-seconds" in command
    expected_bindings = {
        "--expected-release-run-id": "${YFEISTAI_RELEASE_RUN_ID:?set release run id}",
        "--expected-environment-id": "${YFEISTAI_ENVIRONMENT_ID:?set environment id}",
        "--expected-observer-id": ("${YFEISTAI_SHARED_INGRESS_OBSERVER_ID:?set observer id}"),
        "--expected-candidate-health-url": (
            "${YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL:?set candidate health URL}"
        ),
        "--expected-candidate-sha256": ("${YFEISTAI_CANDIDATE_SHA256:?set candidate SHA-256}"),
        "--expected-image-lock-sha256": ("${YFEISTAI_IMAGE_LOCK_SHA256:?set image-lock SHA-256}"),
        "--expected-public-key-spki-sha256": (
            "${YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256:?set observer public key SPKI SHA-256}"
        ),
    }
    for option, value in expected_bindings.items():
        index = tokens.index(option)
        assert tokens[index + 1] == value
    signer_source = OBSERVER_SIGNER_PATH.read_text(encoding="utf-8")
    assert '"--wait-timeout-seconds"' in signer_source
    assert "time.monotonic" in signer_source


def test_observer_runtime_is_independent_egress_enabled_and_owns_observer_authority() -> None:
    overlay = _observer_overlay()
    services = overlay["services"]
    observer = services[OBSERVER_SERVICE_NAME]
    signer = services["openmaic-observer-attestation-signer"]
    candidate = services["deeptutor"]
    assert isinstance(observer, dict)
    assert isinstance(signer, dict)
    assert isinstance(candidate, dict)

    independent_image = (
        "${YFEISTAI_OBSERVER_SIGNER_IMAGE:?set the immutable observer signer image reference}"
    )
    assert observer.get("image") == independent_image
    assert signer.get("image") == independent_image
    assert "YFEISTAI_DEEPTUTOR_IMAGE" not in str(observer.get("image"))
    assert set(observer.get("networks", [])) == {
        "platform-internal",
        "platform-service-egress",
    }

    observer_secrets = set(_service_secret_bindings(observer).values())
    candidate_secrets = set(_service_secret_bindings(candidate).values())
    assert "openmaic_observer_auth_token" in observer_secrets
    assert "openmaic_observer_auth_token" not in candidate_secrets

    observer_mounts = observer.get("volumes", [])
    candidate_mounts = candidate.get("volumes", [])
    assert isinstance(observer_mounts, list)
    assert isinstance(candidate_mounts, list)
    observer_attestation_mount = next(
        mount
        for mount in observer_mounts
        if isinstance(mount, dict) and mount.get("target") == "/run/yfeistai-observer"
    )
    assert observer_attestation_mount.get("read_only") is not True
    assert not any(
        isinstance(mount, dict)
        and mount.get("target") == "/run/yfeistai-observer"
        and mount.get("read_only") is not True
        for mount in candidate_mounts
    )


def test_observer_signer_validates_bound_attestation_before_using_the_private_key() -> None:
    source = OBSERVER_SIGNER_PATH.read_text(encoding="utf-8")
    assert "openmaic_smoke_contract" in source
    validation_source = ast.get_source_segment(
        source,
        _function_node(OBSERVER_SIGNER_PATH, "_validate_attestation_binding"),
    )
    assert validation_source is not None
    validation_call = "validate_openmaic_shared_ingress_observer_bound_context("
    assert validation_call in validation_source
    for binding in (
        "expected_candidate_binding={",
        "expected_candidate_sha256",
        "expected_image_lock_sha256",
        "expected_public_key_spki_sha256=expected_public_key_spki_sha256",
        "trusted_now=trusted_now",
    ):
        assert binding in validation_source, (
            f"signer does not pass the bound observer context: {binding}"
        )

    signing_source = ast.get_source_segment(
        source,
        _function_node(OBSERVER_SIGNER_PATH, "sign_observer_attestation"),
    )
    assert signing_source is not None
    assert signing_source.index("_validate_attestation_binding(") < signing_source.index(
        "key_body = _read_regular_file("
    )


def test_observer_signer_digest_reference_is_replayed_as_external_operator_trust() -> None:
    overlay = _observer_overlay()
    signer = overlay["services"]["openmaic-observer-attestation-signer"]
    assert isinstance(signer, dict)
    assert signer.get("image") == (
        "${YFEISTAI_OBSERVER_SIGNER_IMAGE:?set the immutable observer signer image reference}"
    )

    preflight_source = PLATFORM_PREFLIGHT_PATH.read_text(encoding="utf-8")
    assert 'rpartition("@sha256:")' in preflight_source
    assert 'image_digest == "0" * 64' in preflight_source

    replay_contracts = (
        (PLATFORM_PREFLIGHT_PATH, "run_preflight"),
        (RUNTIME_ATTESTATION_PATH, "produce_runtime_attestation"),
        (FINAL_VERIFIER_PATH, "validate_runtime_attestation"),
    )
    for path, function_name in replay_contracts:
        parameters = _function_parameter_names(path, function_name)
        assert any(
            "observer" in name and "signer" in name and "image" in name for name in parameters
        ), f"{function_name} must replay the external operator signer image digest reference"

    verifier_tree = ast.parse(FINAL_VERIFIER_PATH.read_text(encoding="utf-8"))
    binding_assignments = [
        node
        for node in verifier_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "CUSTOM_IMAGE_SERVICE_BINDINGS"
            for target in node.targets
        )
    ]
    assert len(binding_assignments) == 1
    candidate_bindings = ast.literal_eval(binding_assignments[0].value)
    observer_bindings = candidate_bindings[
        "deploy/docker-compose.openmaic-shared-ingress-observer.yml"
    ]
    assert observer_bindings == {}
    assert "observer_signer" not in observer_bindings.values()
    assert "openmaic-observer-attestation-signer" not in observer_bindings


def test_live_outage_chain_archives_and_strictly_verifies_detached_observer_signature() -> None:
    probe = _load_source_module(
        OUTAGE_PROBE_PATH,
        "observer_signed_outage_probe_contract_under_test",
    )
    required_config_fields = {
        "observer_attestation_signature_path",
        "observer_attestation_public_key_path",
        "observer_attestation_public_key_spki_sha256",
    }
    assert required_config_fields <= set(probe.OutageProbeConfig.__annotations__), (
        "live outage config must bind the detached signature and external SPKI anchor"
    )

    required_parser_keywords = {
        "signature",
        "public_key_path",
        "candidate_root",
        "expected_public_key_spki_sha256",
        "trusted_now",
    }
    for path, function_name in (
        (OUTAGE_PROBE_PATH, "_load_config"),
        (RELEASE_EVIDENCE_PATH, "_write_openmaic_plane_receipt_bound"),
    ):
        calls = _call_keyword_sets(
            path,
            function_name,
            "verify_openmaic_shared_ingress_observer_attestation",
        )
        assert all(required_parser_keywords <= keywords for keywords in calls), (
            f"{function_name} must not use the unsigned observer parser seam"
        )
    archive_calls = _call_keyword_sets(
        FINAL_VERIFIER_PATH,
        "derive_openmaic_dedicated_outage_receipt_checks",
        "verify_archived_openmaic_shared_ingress_observer_attestation",
    )
    assert archive_calls
    assert all(required_parser_keywords - {"trusted_now"} <= keywords for keywords in archive_calls)
    archive_replay_source = ast.get_source_segment(
        FINAL_VERIFIER_PATH.read_text(encoding="utf-8"),
        _function_node(FINAL_VERIFIER_PATH, "derive_openmaic_dedicated_outage_receipt_checks"),
    )
    assert archive_replay_source is not None
    assert "verify_archived_openmaic_shared_ingress_observer_attestation" in archive_replay_source
    assert "datetime.now" not in archive_replay_source
    live_receipt_source = ast.get_source_segment(
        RELEASE_EVIDENCE_PATH.read_text(encoding="utf-8"),
        _function_node(RELEASE_EVIDENCE_PATH, "_write_openmaic_plane_receipt_bound"),
    )
    assert live_receipt_source is not None
    assert live_receipt_source.count("trusted_now=_current_trusted_now()") == 2

    archive_source = ast.get_source_segment(
        OUTAGE_RUNNER_PATH.read_text(encoding="utf-8"),
        _function_node(OUTAGE_RUNNER_PATH, "_archive_observer_anchor"),
    )
    assert archive_source is not None
    assert "openmaic-shared-ingress-observer-attestation.sig" in archive_source
    for path, function_name in (
        (RELEASE_EVIDENCE_PATH, "_write_openmaic_plane_receipt_bound"),
        (FINAL_VERIFIER_PATH, "derive_openmaic_dedicated_outage_receipt_checks"),
    ):
        source = ast.get_source_segment(
            path.read_text(encoding="utf-8"),
            _function_node(path, function_name),
        )
        assert source is not None
        assert "signatureArtifact" in source
        assert "publicKeySpkiSha256" in source


def test_observer_signature_is_required_and_bound_to_external_ed25519_spki(tmp_path: Path) -> None:
    contract = _load_source_module(
        OBSERVER_CONTRACT_PATH,
        "observer_real_signature_contract_under_test",
    )
    service = _load_source_module(
        OBSERVER_SERVICE_PATH,
        "observer_real_signature_service_contract_under_test",
    )
    candidate_fixture = _load_source_module(
        BOOTSTRAP_CONTRACT_TEST_PATH,
        "observer_real_signature_candidate_fixture",
    )
    assert hasattr(contract, "verify_openmaic_shared_ingress_observer_attestation"), (
        "a strict signed observer-attestation verifier is required for live evidence"
    )
    verifier = contract.verify_openmaic_shared_ingress_observer_attestation
    archive_verifier = contract.verify_archived_openmaic_shared_ingress_observer_attestation
    parameters = inspect.signature(verifier).parameters
    assert parameters["signature"].default is inspect.Parameter.empty, (
        "live observer attestation parsing must not allow a missing detached signature"
    )
    assert parameters["public_key_path"].default is inspect.Parameter.empty
    assert parameters["candidate_root"].default is inspect.Parameter.empty
    assert parameters["expected_public_key_spki_sha256"].default is inspect.Parameter.empty
    assert "trusted_now" in parameters, (
        "live observer verification must require an explicit trusted clock"
    )
    assert parameters["trusted_now"].default is inspect.Parameter.empty

    candidate_root, _lock = candidate_fixture.write_schema3_candidate_root(tmp_path)
    trust_root = tmp_path / "observer-trust"
    trust_root.mkdir()
    candidate_binding = candidate_fixture._candidate_binding(candidate_root)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_key_path = trust_root / "observer-public-key.pem"
    public_key_path.write_bytes(
        public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_key_spki = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key_spki_sha256 = hashlib.sha256(public_key_spki).hexdigest()
    body = service.canonical_observer_attestation(
        _observer_config(
            service,
            candidate_binding=candidate_binding,
            public_key_spki_sha256=public_key_spki_sha256,
        ),
        observed_at=datetime.now(timezone.utc),
    )
    trusted_now = datetime.now(timezone.utc)
    signature = base64.b64encode(private_key.sign(body)) + b"\n"
    release_run = {
        "runId": "run-observer-contract-01",
        "environmentId": "environment-observer-contract-01",
    }

    assert (
        verifier(
            body,
            release_run=release_run,
            signature=signature,
            public_key_path=public_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now,
        )["releaseRun"]
        == release_run
    )

    with pytest.raises(ValueError, match="auth window"):
        verifier(
            body,
            release_run=release_run,
            signature=signature,
            public_key_path=public_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now + timedelta(minutes=20),
        )

    assert (
        archive_verifier(
            body,
            release_run=release_run,
            signature=signature,
            public_key_path=public_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
        )["releaseRun"]
        == release_run
    )

    with pytest.raises(ValueError, match="signature"):
        verifier(
            body.replace(b"observer-contract-01", b"observer-contract-02", 1),
            release_run=release_run,
            signature=signature,
            public_key_path=public_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now,
        )

    wrong_key = Ed25519PrivateKey.generate().public_key()
    wrong_key_path = trust_root / "wrong-observer-public-key.pem"
    wrong_key_path.write_bytes(
        wrong_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    with pytest.raises(ValueError, match="signature|SPKI"):
        verifier(
            body,
            release_run=release_run,
            signature=signature,
            public_key_path=wrong_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now,
        )

    with pytest.raises((TypeError, ValueError)):
        verifier(
            body,
            release_run=release_run,
            public_key_path=public_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now,
        )

    candidate_anchor = candidate_root / "observer-public-key.pem"
    candidate_anchor.write_bytes(public_key_path.read_bytes())
    with pytest.raises(ValueError, match="signature|anchor|candidate"):
        verifier(
            body,
            release_run=release_run,
            signature=signature,
            public_key_path=candidate_anchor,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256=public_key_spki_sha256,
            trusted_now=trusted_now,
        )
    with pytest.raises(ValueError, match="SPKI|signature"):
        verifier(
            body,
            release_run=release_run,
            signature=signature,
            public_key_path=public_key_path,
            candidate_root=candidate_root,
            expected_public_key_spki_sha256="f" * 64,
            trusted_now=trusted_now,
        )


def test_streaming_upload_oversize_returns_413_without_committing_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_source_module(
        OBSERVER_SERVICE_PATH,
        "observer_streaming_upload_limit_contract_under_test",
    )
    monkeypatch.setattr(module, "_MAX_PROXY_REQUEST_BYTES", 5)

    async def scenario() -> None:
        receive_count = 0

        async def requester(**kwargs):
            async for _chunk in kwargs["content"]:
                pass
            raise AssertionError("oversize upload reached the upstream terminal response")

        async def receive() -> dict[str, Any]:
            nonlocal receive_count
            receive_count += 1
            return (
                {"type": "http.request", "body": b"1234", "more_body": True}
                if receive_count == 1
                else {"type": "http.request", "body": b"56", "more_body": False}
            )

        app = module.OpenMAICObserverService(_observer_config(module), requester=requester)
        events = await _asgi_request(
            app,
            method="PUT",
            path=(
                "/api/yfeistai/v1/export-inputs/job-observer-contract-01/files/"
                "file-observer-contract-01"
            ),
            receive=receive,
        )
        starts = [event for event in events if event["type"] == "http.response.start"]
        assert [event["status"] for event in starts] == [413]
        assert app._request_count == 0

    asyncio.run(scenario())


def test_streaming_response_commits_observation_only_after_complete_success() -> None:
    module = _load_source_module(
        OBSERVER_SERVICE_PATH,
        "observer_streaming_response_commit_contract_under_test",
    )

    async def run_scenario(*, complete: bool) -> tuple[int, list[int]]:
        async def response_body() -> AsyncIterator[bytes]:
            yield b"first-"
            if not complete:
                raise module.OpenMAICObserverServiceError("upstream_invalid")
            yield b"second"

        async def requester(**_kwargs):
            return module.HTTPResponse(
                status_code=200,
                headers={"content-type": "application/octet-stream"},
                body=response_body(),
            )

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        app = module.OpenMAICObserverService(_observer_config(module), requester=requester)
        observed_counts: list[int] = []

        async def send(_event: dict[str, Any]) -> None:
            observed_counts.append(app._request_count)

        try:
            await app(
                {
                    "type": "http",
                    "method": "GET",
                    "path": (
                        "/api/yfeistai/v1/artifacts/job-observer-contract-01/classroom-export.zip"
                    ),
                    "query_string": b"",
                    "headers": (
                        (b"host", b"shared-ingress.example.test"),
                        *_signed_headers(),
                    ),
                },
                receive,
                send,
            )
        except module.OpenMAICObserverServiceError:
            assert not complete
        return app._request_count, observed_counts

    completed_count, completed_observations = asyncio.run(run_scenario(complete=True))
    assert completed_observations and set(completed_observations) == {0}
    assert completed_count == 1

    failed_count, failed_observations = asyncio.run(run_scenario(complete=False))
    assert failed_observations and set(failed_observations) == {0}
    assert failed_count == 0


def test_observer_host_secret_preflight_is_owner_only_run_bound_and_attested() -> None:
    overlay = _observer_overlay()
    definitions = overlay.get("secrets")
    assert isinstance(definitions, dict)
    observer_secret_names = {
        "openmaic_observer_auth_token",
        "openmaic_observer_health_token",
        "openmaic_observer_attestation_signing_key",
    }
    assert observer_secret_names <= set(definitions)
    for name in observer_secret_names:
        definition = definitions[name]
        assert isinstance(definition, dict)
        source_path = definition.get("file")
        assert isinstance(source_path, str)
        assert "${YFEISTAI_OBSERVER_SECRET_DIR:?" in source_path
        assert "${YFEISTAI_RELEASE_RUN_ID:?" in source_path

    preflight_parameters = _function_parameter_names(PLATFORM_PREFLIGHT_PATH, "run_preflight")
    assert {"observer_secret_dir", "release_run_id"} <= preflight_parameters, (
        "host preflight must receive the observer secret root and release-run identity"
    )
    preflight_source = PLATFORM_PREFLIGHT_PATH.read_text(encoding="utf-8")
    for name in observer_secret_names:
        assert name in preflight_source
    assert "observerSecretPreflight" in preflight_source
    assert "ownerOnly" in preflight_source
    assert "releaseRunId" in preflight_source
    assert "st_uid" in preflight_source
    assert "stat.S_IMODE" in preflight_source

    runtime_parameters = _function_parameter_names(
        RUNTIME_ATTESTATION_PATH,
        "produce_runtime_attestation",
    )
    assert "observer_secret_preflight" in runtime_parameters
    runtime_source = RUNTIME_ATTESTATION_PATH.read_text(encoding="utf-8")
    verifier_source = FINAL_VERIFIER_PATH.read_text(encoding="utf-8")
    assert "observerSecretPreflight" in runtime_source
    assert "observerSecretPreflight" in verifier_source
    assert "releaseRunId" in runtime_source
    assert "releaseRunId" in verifier_source
