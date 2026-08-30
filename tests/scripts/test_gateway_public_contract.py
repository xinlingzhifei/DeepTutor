from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_HEAD = "a" * 40
CANDIDATE = {
    "sourceRepository": "xinlingzhifei/DeepTutor",
    "sourceHead": SOURCE_HEAD,
    "releaseTag": f"yfeistai-first-release-20260830-{SOURCE_HEAD[:8]}",
    "openmaicHead": "0cf2a330411681190e89f48e20f305345ff99f87",
    "imageDigests": {
        "deeptutor": "sha256:" + "1" * 64,
        "openmaic": "sha256:" + "2" * 64,
        "openmaic_render": "sha256:" + "3" * 64,
    },
}
RELEASE_RUN = {
    "runId": "first-release-run-20260830",
    "environmentId": "acceptance-external-01",
}
BASE_URL = "https://candidate.example.test"
RUNTIME_SHA256 = "4" * 64
OBSERVER_ID = "external-observer-01"
OBSERVER_ORIGIN = "https://observer.example.net"
TRUSTED_NOW = "2026-08-30T04:05:00Z"
RUN_STARTED_AT = "2026-08-30T04:00:00Z"
RUN_ENDED_AT = "2026-08-30T04:10:00Z"
PUBLIC_ADDRESSES = (
    "93.184.216.34",
    "2606:2800:220:1:248:1893:25c8:1946",
)


def _load_module():
    path = ROOT / "scripts" / "gateway_public_contract.py"
    assert path.is_file(), "gateway public contract is missing"
    spec = importlib.util.spec_from_file_location("gateway_public_contract_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _canonical(module, document: dict[str, object]) -> bytes:
    return module.canonical_gateway_public_document(document)


def _observations(module) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for port in module.GATEWAY_ALLOWED_PORTS:
        tls = None
        if port == 443:
            tls = {
                "expectedHostname": "candidate.example.test",
                "hostnameVerified": True,
                "peerCertificateSha256": "5" * 64,
            }
        rows.append(
            {
                "port": port,
                "protocol": "tcp",
                "expected": "open",
                "addressObservations": [
                    {
                        "remoteIp": remote_ip,
                        "outcome": "open",
                        "error": None,
                        "tls": tls,
                    }
                    for remote_ip in PUBLIC_ADDRESSES
                ],
            }
        )
    for port in module.GATEWAY_DENIED_PORTS:
        rows.append(
            {
                "port": port,
                "protocol": "tcp",
                "expected": "closed",
                "addressObservations": [
                    {
                        "remoteIp": remote_ip,
                        "outcome": "closed",
                        "error": "connection-refused",
                        "tls": None,
                    }
                    for remote_ip in PUBLIC_ADDRESSES
                ],
            }
        )
    return rows


def _report(module) -> dict[str, object]:
    return {
        "schemaVersion": module.GATEWAY_PUBLIC_SCHEMA_VERSION,
        "producer": module.GATEWAY_PUBLIC_PRODUCER,
        "candidate": CANDIDATE,
        "releaseRun": RELEASE_RUN,
        "observedAt": "2026-08-30T04:00:01Z",
        "baseUrl": BASE_URL,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": RUNTIME_SHA256,
        },
        "observer": {
            "observerId": OBSERVER_ID,
            "origin": OBSERVER_ORIGIN,
        },
        "target": {
            "hostname": "candidate.example.test",
            "resolvedAddresses": list(PUBLIC_ADDRESSES),
        },
        "policy": {
            "allowedTcpPorts": list(module.GATEWAY_ALLOWED_PORTS),
            "deniedTcpPorts": list(module.GATEWAY_DENIED_PORTS),
        },
        "observations": _observations(module),
    }


def _attestation(module, observation_sha256: str) -> dict[str, object]:
    return {
        "schemaVersion": module.GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION,
        "producer": module.GATEWAY_OBSERVER_ATTESTATION_PRODUCER,
        "candidate": CANDIDATE,
        "releaseRun": RELEASE_RUN,
        "observedAt": "2026-08-30T04:00:02Z",
        "observer": {
            "observerId": OBSERVER_ID,
            "origin": OBSERVER_ORIGIN,
        },
        "target": {
            "origin": BASE_URL,
            "expectedTlsHostname": "candidate.example.test",
            "resolvedAddresses": list(PUBLIC_ADDRESSES),
        },
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": RUNTIME_SHA256,
        },
        "externalObservation": {
            "artifact": "raw/gateway-public-observation.json",
            "sha256": observation_sha256,
        },
        "execution": {
            "command": module.gateway_public_command_record(),
            "nativeExit": 0,
            "stdoutSha256": observation_sha256,
            "stderr": "",
            "stderrSha256": hashlib.sha256(b"").hexdigest(),
        },
    }


def _bound_documents(module):
    report = _report(module)
    report_body = _canonical(module, report)
    attestation = _attestation(module, hashlib.sha256(report_body).hexdigest())
    attestation_body = _canonical(module, attestation)
    return report, report_body, attestation, attestation_body


def _address_bound_documents(module):
    report = _report(module)
    report["target"] = {
        "hostname": "candidate.example.test",
        "resolvedAddresses": list(PUBLIC_ADDRESSES),
    }
    report_body = _canonical(module, report)
    attestation = _attestation(module, hashlib.sha256(report_body).hexdigest())
    attestation["target"] = {
        "origin": BASE_URL,
        "expectedTlsHostname": "candidate.example.test",
        "resolvedAddresses": list(PUBLIC_ADDRESSES),
    }
    attestation_body = _canonical(module, attestation)
    return report, report_body, attestation, attestation_body


def _parse_bound(
    module,
    report_body: bytes,
    attestation_body: bytes,
    *,
    candidate: dict[str, object] = CANDIDATE,
    expected_base_url: str = BASE_URL,
    expected_observer_id: str = OBSERVER_ID,
    expected_observer_origin: str = OBSERVER_ORIGIN,
    expected_attestation_sha256: str | None = None,
    trusted_now: str = TRUSTED_NOW,
    run_started_at: str = RUN_STARTED_AT,
    run_ended_at: str = RUN_ENDED_AT,
):
    return module.parse_gateway_public_report(
        report_body,
        observer_attestation_body=attestation_body,
        candidate=candidate,
        release_run=RELEASE_RUN,
        expected_base_url=expected_base_url,
        expected_runtime_attestation_sha256=RUNTIME_SHA256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_attestation_sha256=(
            expected_attestation_sha256
            if expected_attestation_sha256 is not None
            else hashlib.sha256(attestation_body).hexdigest()
        ),
        trusted_now=trusted_now,
        run_started_at=run_started_at,
        run_ended_at=run_ended_at,
    )


def test_gateway_public_contract_binds_external_observation_and_attestation() -> None:
    module = _load_module()
    report, report_body, _attestation, attestation_body = _bound_documents(module)
    parsed_report = _parse_bound(module, report_body, attestation_body)

    assert parsed_report == report
    assert module.derive_gateway_public_checks(parsed_report) == {
        "gatewayPublic": True,
        "internalPortsClosed": True,
    }
    assert module.gateway_public_command_record() == {
        "runner": "python",
        "script": "scripts/gateway_public_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def test_gateway_public_contract_accepts_attested_canonical_remote_address_set() -> None:
    module = _load_module()
    report, report_body, _attestation, attestation_body = _address_bound_documents(module)

    assert _parse_bound(module, report_body, attestation_body) == report


def test_gateway_public_contract_requires_exact_per_address_observation_set() -> None:
    module = _load_module()
    report = _report(module)
    per_port_observations: list[dict[str, object]] = []
    for observation in report["observations"]:
        port = observation["port"]
        address_observations = []
        for index, remote_ip in enumerate(PUBLIC_ADDRESSES):
            tls = None
            if port == 443:
                tls = {
                    "expectedHostname": "candidate.example.test",
                    "hostnameVerified": True,
                    "peerCertificateSha256": str(5 + index) * 64,
                }
            address_observations.append(
                {
                    "remoteIp": remote_ip,
                    "outcome": ("open" if observation["expected"] == "open" else "closed"),
                    "error": (
                        ("connection-refused", "timeout")[index]
                        if observation["expected"] == "closed"
                        else None
                    ),
                    "tls": tls,
                }
            )
        per_port_observations.append(
            {
                "port": port,
                "protocol": "tcp",
                "expected": observation["expected"],
                "addressObservations": address_observations,
            }
        )
    report["observations"] = per_port_observations
    report_body = _canonical(module, report)
    attestation = _attestation(module, hashlib.sha256(report_body).hexdigest())
    attestation_body = _canonical(module, attestation)

    assert _parse_bound(module, report_body, attestation_body) == report

    for mutation in ("duplicate", "missing", "extra"):
        changed = copy.deepcopy(report)
        address_observations = changed["observations"][0]["addressObservations"]
        if mutation == "duplicate":
            address_observations.append(copy.deepcopy(address_observations[0]))
        elif mutation == "missing":
            address_observations.pop()
        else:
            address_observations.append(
                {
                    "remoteIp": "1.1.1.1",
                    "outcome": "open",
                    "error": None,
                    "tls": None,
                }
            )
        changed_body = _canonical(module, changed)
        changed_sha256 = hashlib.sha256(changed_body).hexdigest()
        changed_attestation = copy.deepcopy(attestation)
        changed_attestation["externalObservation"]["sha256"] = changed_sha256
        changed_attestation["execution"]["stdoutSha256"] = changed_sha256

        with pytest.raises(ValueError, match="address"):
            _parse_bound(
                module,
                changed_body,
                _canonical(module, changed_attestation),
            )


@pytest.mark.parametrize(
    "change",
    ("observation-set", "private-address", "attestation-set"),
)
def test_gateway_public_contract_rejects_remote_address_binding_drift(
    change: str,
) -> None:
    module = _load_module()
    report, _report_body, attestation, _attestation_body = _address_bound_documents(module)
    changed_report = copy.deepcopy(report)
    changed_attestation = copy.deepcopy(attestation)
    message = "address set"
    if change == "observation-set":
        changed_report["observations"][0]["addressObservations"].pop()
        message = "address"
    elif change == "private-address":
        private_addresses = [PUBLIC_ADDRESSES[0], "10.0.0.1"]
        changed_report["target"]["resolvedAddresses"] = private_addresses
        changed_attestation["target"]["resolvedAddresses"] = private_addresses
        message = "globally routable"
    else:
        changed_attestation["target"]["resolvedAddresses"] = [PUBLIC_ADDRESSES[0]]

    changed_body = _canonical(module, changed_report)
    changed_sha256 = hashlib.sha256(changed_body).hexdigest()
    changed_attestation["externalObservation"]["sha256"] = changed_sha256
    changed_attestation["execution"]["stdoutSha256"] = changed_sha256
    changed_attestation_body = _canonical(module, changed_attestation)

    with pytest.raises(ValueError, match=message):
        _parse_bound(module, changed_body, changed_attestation_body)


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda report: report["policy"]["allowedTcpPorts"].append(8443),
            "fixed port policy",
        ),
        (
            lambda report: report["observations"].append(
                {
                    "port": 22,
                    "protocol": "tcp",
                    "expected": "closed",
                    "outcome": "closed",
                    "error": "connection-refused",
                    "tls": None,
                }
            ),
            "observation matrix",
        ),
        (
            lambda report: report["observations"][1]["addressObservations"][0]["tls"].update(
                {"expectedHostname": "other.example.test"}
            ),
            "TLS identity",
        ),
        (
            lambda report: report["observations"][1]["addressObservations"][0]["tls"].update(
                {"peerCertificateSha256": "0" * 64}
            ),
            "TLS identity",
        ),
        (
            lambda report: report["observations"][2]["addressObservations"][0].update(
                {"outcome": "open", "error": None}
            ),
            "internal port observation",
        ),
    ),
)
def test_gateway_public_contract_rejects_policy_or_observation_drift(
    mutate,
    message: str,
) -> None:
    module = _load_module()
    report, _report_body, attestation, _attestation_body = _bound_documents(module)
    changed = copy.deepcopy(report)
    mutate(changed)
    changed_body = _canonical(module, changed)
    changed_attestation = copy.deepcopy(attestation)
    changed_sha256 = hashlib.sha256(changed_body).hexdigest()
    changed_attestation["externalObservation"]["sha256"] = changed_sha256
    changed_attestation["execution"]["stdoutSha256"] = changed_sha256
    changed_attestation_body = _canonical(module, changed_attestation)

    with pytest.raises(ValueError, match=message):
        _parse_bound(module, changed_body, changed_attestation_body)


@pytest.mark.parametrize(
    ("change", "message"),
    (
        ("candidate", "candidate binding"),
        ("run", "run binding"),
        ("environment", "run binding"),
        ("runtime", "runtime attestation"),
        ("observation", "external observation"),
        ("observer", "observer identity"),
        ("self-origin", "distinct from candidate"),
    ),
)
def test_gateway_observer_attestation_fails_closed_on_binding_drift(
    change: str,
    message: str,
) -> None:
    module = _load_module()
    _report, report_body, attestation, _attestation_body = _bound_documents(module)
    changed = copy.deepcopy(attestation)
    if change == "candidate":
        changed["candidate"]["sourceHead"] = "b" * 40
    elif change == "run":
        changed["releaseRun"]["runId"] = "other-run"
    elif change == "environment":
        changed["releaseRun"]["environmentId"] = "other-environment"
    elif change == "runtime":
        changed["runtimeAttestation"]["sha256"] = "6" * 64
    elif change == "observation":
        changed["externalObservation"]["sha256"] = "7" * 64
    elif change == "observer":
        changed["observer"]["observerId"] = "other-observer"
    else:
        changed["observer"]["origin"] = BASE_URL
    changed_body = _canonical(module, changed)

    with pytest.raises(ValueError, match=message):
        _parse_bound(module, report_body, changed_body)


def test_gateway_observer_attestation_requires_out_of_band_digest() -> None:
    module = _load_module()
    _report, _report_body, _attestation, attestation_body = _bound_documents(module)

    with pytest.raises(ValueError, match="out-of-band"):
        _parse_bound(
            module,
            _report_body,
            attestation_body,
            expected_attestation_sha256="0" * 64,
        )


def test_gateway_public_documents_are_canonical_json() -> None:
    module = _load_module()
    report, report_body, _attestation, attestation_body = _bound_documents(module)

    assert report_body.endswith(b"\n")
    assert attestation_body.endswith(b"\n")
    assert json.loads(report_body) == report


def test_gateway_report_parser_rejects_predecoded_observer_attestation() -> None:
    module = _load_module()
    _report, report_body, attestation, _attestation_body = _bound_documents(module)

    with pytest.raises((TypeError, ValueError)):
        module.parse_gateway_public_report(
            report_body,
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            expected_base_url=BASE_URL,
            expected_runtime_attestation_sha256=RUNTIME_SHA256,
            observer_attestation=attestation,
        )


@pytest.mark.parametrize(
    ("field", "expected_value"),
    (
        ("observer-id", "other-observer"),
        ("observer-origin", "https://other-observer.example.net"),
    ),
)
def test_gateway_observer_identity_must_match_out_of_band_anchor(
    field: str,
    expected_value: str,
) -> None:
    module = _load_module()
    _report, report_body, _attestation, attestation_body = _bound_documents(module)
    arguments = {
        "expected_observer_id": OBSERVER_ID,
        "expected_observer_origin": OBSERVER_ORIGIN,
    }
    anchor = "expected_observer_id" if field == "observer-id" else "expected_observer_origin"
    arguments[anchor] = expected_value

    with pytest.raises(ValueError, match="observer identity"):
        _parse_bound(module, report_body, attestation_body, **arguments)


def test_gateway_observer_rejects_unverified_public_key_metadata() -> None:
    module = _load_module()
    _report, report_body, attestation, _attestation_body = _bound_documents(module)
    attestation["observer"]["publicKeySha256"] = "9" * 64
    attestation_body = _canonical(module, attestation)

    with pytest.raises(ValueError, match="observer identity"):
        _parse_bound(module, report_body, attestation_body)


@pytest.mark.parametrize(
    ("expected_base_url", "canonical_origin", "canonical_hostname"),
    (
        (
            "https://BÜCHER.example./",
            "https://xn--bcher-kva.example",
            "xn--bcher-kva.example",
        ),
        (
            "https://[2001:0db8:0:0:0:0:0:1]/",
            "https://[2001:db8::1]",
            "2001:db8::1",
        ),
    ),
)
def test_gateway_origins_are_canonical_for_idna_and_ipv6(
    expected_base_url: str,
    canonical_origin: str,
    canonical_hostname: str,
) -> None:
    module = _load_module()
    report = _report(module)
    report["baseUrl"] = canonical_origin
    report["target"]["hostname"] = canonical_hostname
    for address_observation in report["observations"][1]["addressObservations"]:
        address_observation["tls"]["expectedHostname"] = canonical_hostname
    report_body = _canonical(module, report)
    attestation = _attestation(module, hashlib.sha256(report_body).hexdigest())
    attestation["target"] = {
        "origin": canonical_origin,
        "expectedTlsHostname": canonical_hostname,
        "resolvedAddresses": list(PUBLIC_ADDRESSES),
    }
    attestation_body = _canonical(module, attestation)

    assert (
        _parse_bound(
            module,
            report_body,
            attestation_body,
            expected_base_url=expected_base_url,
            expected_observer_origin="https://OBSERVER.EXAMPLE.NET./",
        )
        == report
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "command",
        "native-exit",
        "stdout-digest",
        "stderr",
        "stderr-digest",
    ),
)
def test_gateway_observer_attestation_binds_fixed_execution(mutation: str) -> None:
    module = _load_module()
    _report, report_body, attestation, _attestation_body = _bound_documents(module)
    execution = attestation["execution"]
    if mutation == "command":
        execution["command"]["arguments"] = ["--profile", "other"]
    elif mutation == "native-exit":
        execution["nativeExit"] = 1
    elif mutation == "stdout-digest":
        execution["stdoutSha256"] = "7" * 64
    elif mutation == "stderr":
        execution["stderr"] = "unexpected warning"
    else:
        execution["stderrSha256"] = "7" * 64
    attestation_body = _canonical(module, attestation)

    with pytest.raises(ValueError, match="execution"):
        _parse_bound(module, report_body, attestation_body)


def test_gateway_attestation_anchor_covers_execution() -> None:
    module = _load_module()
    _report, report_body, attestation, attestation_body = _bound_documents(module)
    trusted_sha256 = hashlib.sha256(attestation_body).hexdigest()
    attestation["execution"]["nativeExit"] = 1
    changed_body = _canonical(module, attestation)

    with pytest.raises(ValueError, match="out-of-band"):
        _parse_bound(
            module,
            report_body,
            changed_body,
            expected_attestation_sha256=trusted_sha256,
        )


def test_gateway_freshness_limits_are_fixed() -> None:
    module = _load_module()

    assert module.MAX_GATEWAY_OBSERVATION_AGE_SECONDS == 600
    assert module.MAX_GATEWAY_ATTESTATION_DELAY_SECONDS == 30
    assert module.MAX_GATEWAY_FUTURE_SKEW_SECONDS == 5


@pytest.mark.parametrize(
    (
        "report_time",
        "attestation_time",
        "run_started_at",
        "run_ended_at",
        "message",
    ),
    (
        (
            "2026-08-30T03:54:59Z",
            "2026-08-30T03:55:00Z",
            "2026-08-30T03:00:00Z",
            "2026-08-30T05:00:00Z",
            "too old",
        ),
        (
            "2026-08-30T04:05:06Z",
            "2026-08-30T04:05:07Z",
            "2026-08-30T03:00:00Z",
            "2026-08-30T05:00:00Z",
            "future",
        ),
        (
            "2026-08-30T04:00:01Z",
            "2026-08-30T04:00:32Z",
            RUN_STARTED_AT,
            RUN_ENDED_AT,
            "delay",
        ),
        (
            "2026-08-30T04:00:00Z",
            "2026-08-30T04:00:01Z",
            "2026-08-30T04:00:01Z",
            RUN_ENDED_AT,
            "run window",
        ),
        (
            "2026-08-30T04:00:01Z",
            "2026-08-30T04:00:02Z",
            RUN_STARTED_AT,
            "2026-08-30T04:00:01Z",
            "run window",
        ),
    ),
)
def test_gateway_freshness_rejects_stale_delayed_future_or_out_of_run_evidence(
    report_time: str,
    attestation_time: str,
    run_started_at: str,
    run_ended_at: str,
    message: str,
) -> None:
    module = _load_module()
    report = _report(module)
    report["observedAt"] = report_time
    report_body = _canonical(module, report)
    attestation = _attestation(module, hashlib.sha256(report_body).hexdigest())
    attestation["observedAt"] = attestation_time
    attestation_body = _canonical(module, attestation)

    with pytest.raises(ValueError, match=message):
        _parse_bound(
            module,
            report_body,
            attestation_body,
            run_started_at=run_started_at,
            run_ended_at=run_ended_at,
        )


@pytest.mark.parametrize("image", ("deeptutor", "openmaic", "openmaic_render"))
def test_gateway_candidate_rejects_zero_image_digest(image: str) -> None:
    module = _load_module()
    candidate = copy.deepcopy(CANDIDATE)
    candidate["imageDigests"][image] = "sha256:" + "0" * 64
    report = _report(module)
    report["candidate"] = candidate
    report_body = _canonical(module, report)
    attestation = _attestation(module, hashlib.sha256(report_body).hexdigest())
    attestation["candidate"] = candidate
    attestation_body = _canonical(module, attestation)

    with pytest.raises(ValueError, match="image digest"):
        _parse_bound(
            module,
            report_body,
            attestation_body,
            candidate=candidate,
        )
