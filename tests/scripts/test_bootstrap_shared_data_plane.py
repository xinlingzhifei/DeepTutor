from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.scripts.platform_candidate_test_support import write_schema3_candidate_root

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "bootstrap_shared_data_plane.py"
CONTRACT_PATH = ROOT / "scripts" / "openmaic_smoke_contract.py"


@pytest.mark.parametrize("launch_directory", ("repository", "outside-repository"))
def test_bootstrap_cli_starts_directly_without_pythonpath(
    tmp_path: Path,
    launch_directory: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["DEEPTUTOR_HOME"] = str(tmp_path / "runtime")

    completed = subprocess.run(
        [sys.executable, "-B", str(SCRIPT_PATH), "--help"],
        cwd=ROOT if launch_directory == "repository" else tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
    assert "--candidate-root" in completed.stdout
    assert "--observer-attestation" in completed.stdout
    assert "--release-run-id" in completed.stdout


def _candidate_binding(candidate_root: Path) -> dict[str, str]:
    body = (candidate_root / "deploy" / "image-lock.json").read_bytes()
    candidate = json.loads(body)["candidate"]
    candidate_body = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    return {
        "candidateSha256": hashlib.sha256(candidate_body).hexdigest(),
        "imageLockSha256": hashlib.sha256(body).hexdigest(),
    }


def _module():
    name = "bootstrap_shared_data_plane_signed_observer_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_registration_uses_real_provider_descriptor_and_preserves_old_conflict():
    module = _module()
    registration = module.SharedDataPlaneRegistration(
        provider_type="openai",
        model_name="vendor/model:version",
        provider_api_base_url="https://llm.example.test/v1",
    )
    assert registration.model_name == "vendor/model:version"
    assert registration.provider_api_base_url == "https://llm.example.test/v1"
    profile = module.ProviderProfile(
        id="platform-default",
        scope="shared",
        tenant_id=None,
        owner_key="shared",
        provider_type="openmaic-server-configured",
        model_name="server-selected-model",
        api_base_url=None,
        secret_ref="shared/providers/platform-default",
        status="active",
    )
    assert module._profile_matches(profile, registration) is False
    assert profile.model_name == "server-selected-model"


def _contract_module():
    name = "openmaic_smoke_contract_for_bootstrap_test"
    spec = importlib.util.spec_from_file_location(name, CONTRACT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _arguments(tmp_path: Path) -> tuple[list[str], Path, Path, Path, Path]:
    candidate_root, _lock = write_schema3_candidate_root(tmp_path)
    attestation = tmp_path / "observer-attestation.json"
    signature = tmp_path / "observer-attestation.sig"
    public_key = tmp_path / "observer-public-key.pem"
    attestation.write_bytes(b"signed-observer-attestation\n")
    signature.write_bytes(b"signed-observer-signature\n")
    return (
        [
            "--provider-id",
            "openai",
            "--provider-model-name",
            "configured-model",
            "--provider-api-base-url",
            "https://llm.example.test/v1",
            "--base-url",
            "http://openmaic-shared-ingress-observer:8000",
            "--observer-attestation",
            str(attestation),
            "--observer-attestation-signature",
            str(signature),
            "--observer-attestation-public-key",
            str(public_key),
            "--observer-attestation-public-key-spki-sha256",
            "a" * 64,
            "--candidate-root",
            str(candidate_root),
            "--release-run-id",
            "release-run-01",
            "--environment-id",
            "environment-01",
        ],
        candidate_root,
        attestation,
        signature,
        public_key,
    )


def test_bootstrap_strictly_verifies_signed_observer_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    contract = _contract_module()
    argv, candidate_root, attestation, signature, public_key = _arguments(tmp_path)
    candidate_binding = _candidate_binding(candidate_root)
    calls: list[str] = []

    def verify(body: bytes, **kwargs):
        calls.append("verify")
        assert body == attestation.read_bytes()
        trusted_now = kwargs.pop("trusted_now")
        assert trusted_now.tzinfo is not None
        assert kwargs == {
            "release_run": {
                "runId": "release-run-01",
                "environmentId": "environment-01",
            },
            "signature": signature.read_bytes(),
            "public_key_path": public_key,
            "candidate_root": candidate_root,
            "expected_public_key_spki_sha256": "a" * 64,
        }
        return {
            "releaseRun": kwargs["release_run"],
            "candidateBinding": candidate_binding,
            "attestationChallenge": contract.openmaic_observer_attestation_challenge("a" * 64),
            "observedAt": "2026-09-01T00:00:00Z",
            "observer": {
                "internalProxyUrl": "http://openmaic-shared-ingress-observer:8000",
                "authExpiresAt": "2099-09-01T00:00:00Z",
            },
            "schemaVersion": 3,
        }

    async def run(registration, *, assert_candidate_unchanged):
        calls.append("runtime")
        assert_candidate_unchanged()
        assert registration.base_url == "http://openmaic-shared-ingress-observer:8000"

    monkeypatch.setattr(
        module,
        "verify_openmaic_shared_ingress_observer_attestation",
        verify,
    )
    monkeypatch.setattr(module, "_run", run)

    assert module.main(argv) == 0
    assert calls == ["verify", "runtime"]


@pytest.mark.parametrize(
    ("invalid_artifact", "error_message"),
    (
        ("missing-pocketbase", "image lock is invalid"),
        ("missing-signer", "observer signer support image"),
        ("docker-compose.yml", "production Compose image references"),
        ("docker-compose.platform.yml", "production Compose image references"),
        ("docker-compose.data-plane.yml", "production Compose image references"),
        (
            "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
            "production Compose image references",
        ),
    ),
)
def test_bootstrap_rejects_invalid_candidate_before_observer_or_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_artifact: str,
    error_message: str,
) -> None:
    module = _module()
    argv, candidate_root, _, _, _ = _arguments(tmp_path)
    if invalid_artifact.startswith("missing-"):
        lock_path = candidate_root / "deploy" / "image-lock.json"
        lock = json.loads(lock_path.read_bytes())
        if invalid_artifact == "missing-pocketbase":
            del lock["images"]["pocketbase"]
        else:
            del lock["supportImages"]["observer_signer"]
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    else:
        (candidate_root / invalid_artifact).write_bytes(b"services: {}\n")
    calls: list[str] = []

    def verify_observer(*_args, **_kwargs):
        calls.append("observer")
        return {}

    async def run(_registration, *, assert_candidate_unchanged):
        calls.append("runtime")
        assert_candidate_unchanged()

    monkeypatch.setattr(module, "verify_shared_data_plane_observer_binding", verify_observer)
    monkeypatch.setattr(module, "_run", run)

    failure = None
    try:
        module.main(argv)
    except ValueError as exc:
        failure = exc

    assert calls == [], "invalid candidate reached observer or runtime effects"
    assert isinstance(failure, ValueError)
    assert error_message in str(failure)


def test_bootstrap_rejects_signed_proxy_mismatch_before_health_or_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    contract = _contract_module()
    argv, candidate_root, _, _, _ = _arguments(tmp_path)
    candidate_binding = _candidate_binding(candidate_root)
    runtime_started = False

    def verify(*_args, **kwargs):
        return {
            "releaseRun": kwargs["release_run"],
            "candidateBinding": candidate_binding,
            "attestationChallenge": contract.openmaic_observer_attestation_challenge("a" * 64),
            "observedAt": "2026-09-01T00:00:00Z",
            "observer": {
                "internalProxyUrl": "http://foreign-proxy:8000",
                "authExpiresAt": "2099-09-01T00:00:00Z",
            },
            "schemaVersion": 3,
        }

    async def run(_registration, *, assert_candidate_unchanged):
        nonlocal runtime_started
        assert_candidate_unchanged()
        runtime_started = True

    monkeypatch.setattr(
        module,
        "verify_openmaic_shared_ingress_observer_attestation",
        verify,
    )
    monkeypatch.setattr(module, "_run", run)

    with pytest.raises(ValueError, match="observer proxy binding"):
        module.main(argv)

    assert runtime_started is False


def test_bootstrap_rejects_attestation_for_a_different_current_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    _argv, candidate_root, attestation, signature, public_key = _arguments(tmp_path)
    current_binding = _candidate_binding(candidate_root)
    foreign_binding = {
        **current_binding,
        "candidateSha256": "9" * 64,
    }

    monkeypatch.setattr(
        module,
        "verify_openmaic_shared_ingress_observer_attestation",
        lambda *_args, **kwargs: {
            "releaseRun": kwargs["release_run"],
            "candidateBinding": foreign_binding,
            "observer": {"internalProxyUrl": "http://openmaic-shared-ingress-observer:8000"},
        },
    )

    with pytest.raises(ValueError, match="observer candidate binding"):
        module.verify_shared_data_plane_observer_binding(
            module.SharedDataPlaneRegistration(
                base_url="http://openmaic-shared-ingress-observer:8000"
            ),
            attestation_path=attestation,
            signature_path=signature,
            public_key_path=public_key,
            candidate_root=candidate_root,
            release_run={
                "runId": "release-run-01",
                "environmentId": "environment-01",
            },
            public_key_spki_sha256="a" * 64,
        )


def test_bootstrap_holds_candidate_lease_across_observer_verification_and_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    argv, candidate_root, _, _, _ = _arguments(tmp_path)
    events: list[str] = []
    opened_leases: list[object] = []

    class FakeCandidateLease:
        def __init__(self, root: Path) -> None:
            self.candidate_root = root
            self.active = False
            events.append("open")
            opened_leases.append(self)

        def __enter__(self):
            self.active = True
            events.append("enter")
            return self

        def __exit__(self, _exc_type, _exc_value, _traceback) -> bool:
            events.append("exit")
            self.active = False
            return False

        def check_unchanged(self) -> None:
            assert self.active, "candidate assertion escaped the active lease"
            events.append("assert")

    def verify_observer(_registration, **kwargs):
        assert opened_leases, "observer verification ran outside candidate lease"
        lease = opened_leases[0]
        assert lease.active, "observer verification ran outside candidate lease"
        assert kwargs["candidate_root"] == candidate_root
        events.append("observer")
        return {}

    async def run(_registration, *, assert_candidate_unchanged) -> None:
        lease = opened_leases[0]
        assert lease.active, "runtime ran outside candidate lease"
        events.append("health")
        assert_candidate_unchanged()
        events.append("transaction")
        assert_candidate_unchanged()

    monkeypatch.setattr(
        module,
        "validated_candidate_artifact_lease",
        FakeCandidateLease,
    )
    monkeypatch.setattr(module, "verify_shared_data_plane_observer_binding", verify_observer)
    monkeypatch.setattr(module, "_run", run)

    assert module.main(argv) == 0

    assert len(opened_leases) == 1
    assert opened_leases[0].candidate_root == candidate_root
    assert opened_leases[0].active is False
    assert events == [
        "open",
        "enter",
        "assert",
        "observer",
        "assert",
        "health",
        "assert",
        "transaction",
        "assert",
        "assert",
        "exit",
    ]


def test_registration_rejects_candidate_drift_after_health_before_persistence() -> None:
    module = _module()
    registration = module.SharedDataPlaneRegistration()
    drifted = False
    assertion_count = 0
    persisted = False

    def assert_candidate_unchanged() -> None:
        nonlocal assertion_count
        assertion_count += 1
        if drifted:
            raise ValueError("candidate runtime contract lease changed")

    async def verify_health(candidate):
        nonlocal drifted
        assert candidate == registration
        assert assertion_count == 1
        drifted = True
        return module.VerifiedDataPlaneHealth(
            service="openmaic",
            upstream_commit=module.EXPECTED_UPSTREAM_COMMIT,
            app_version=module.EXPECTED_APP_VERSION,
            contract_versions=(module.SUPPORTED_CONTRACT_VERSION,),
            capabilities=tuple(module.REQUIRED_CAPABILITIES),
            export_formats=tuple(module.REQUIRED_EXPORT_FORMATS),
        )

    async def persist(_candidate, *, assert_candidate_unchanged) -> None:
        nonlocal persisted
        assert_candidate_unchanged()
        persisted = True

    with pytest.raises(ValueError, match="candidate runtime contract lease changed"):
        asyncio.run(
            module.register_shared_data_plane(
                registration,
                verify_health=verify_health,
                persist=persist,
                assert_candidate_unchanged=assert_candidate_unchanged,
            )
        )

    assert assertion_count == 2
    assert persisted is False


def test_persistence_rolls_back_when_candidate_drifts_inside_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    drifted = False

    class FakeTransaction:
        def __init__(self) -> None:
            self.committed = False
            self.rolled_back = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, _exc_value, _traceback) -> bool:
            self.rolled_back = exc_type is not None
            self.committed = exc_type is None
            return False

    class FakeSession:
        def __init__(self) -> None:
            self.transaction = FakeTransaction()
            self.flush_count = 0
            self.added: list[object] = []

        def begin(self):
            return self.transaction

        async def scalar(self, _statement):
            return None

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            nonlocal drifted
            self.flush_count += 1
            if self.flush_count == 2:
                drifted = True

    class FakePlatformSession:
        def __init__(self, session: FakeSession) -> None:
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, _exc_type, _exc_value, _traceback) -> bool:
            return False

    session = FakeSession()

    def assert_candidate_unchanged() -> None:
        if drifted:
            raise ValueError("candidate runtime contract lease changed")

    monkeypatch.setattr(
        module,
        "platform_session",
        lambda: FakePlatformSession(session),
    )

    with pytest.raises(ValueError, match="candidate runtime contract lease changed"):
        asyncio.run(
            module.persist_shared_data_plane(
                module.SharedDataPlaneRegistration(),
                assert_candidate_unchanged=assert_candidate_unchanged,
            )
        )

    assert session.flush_count == 2
    assert len(session.added) == 2
    assert session.transaction.rolled_back is True
    assert session.transaction.committed is False


def test_empty_model_restart_preserves_existing_profile_for_exports():
    from types import SimpleNamespace
    module = _module()
    registration = module.SharedDataPlaneRegistration()
    profile = SimpleNamespace(id="platform-default", scope="shared", tenant_id=None,
        owner_key="shared", provider_type="openai", model_name="previous-model",
        api_base_url="https://provider.invalid/v1",
        secret_ref="shared/providers/platform-default", status="active")
    assert module._profile_matches(profile, registration)
    profile.tenant_id = "private-tenant"
    assert not module._profile_matches(profile, registration)
