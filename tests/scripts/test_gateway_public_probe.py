from __future__ import annotations

import errno
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

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
PUBLIC_ADDRESSES = (
    "93.184.216.34",
    "2606:2800:220:1:248:1893:25c8:1946",
)


def _load_path(name: str, path: Path):
    assert path.is_file(), f"{path.name} is missing"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def _load_probe():
    return _load_path(
        "gateway_public_probe_under_test",
        ROOT / "scripts" / "gateway_public_probe.py",
    )


def test_external_gateway_probe_runs_only_the_fixed_port_matrix() -> None:
    module = _load_probe()
    calls: list[tuple[str, int, str | None]] = []

    def connect(host: str, port: int, tls_hostname: str | None):
        calls.append((host, port, tls_hostname))
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": "5" * 64,
                },
            }
        if port == 80:
            return {"outcome": "open", "error": None, "tls": None}
        return {"outcome": "closed", "error": "connection-refused", "tls": None}

    report = module.probe_gateway_public(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        base_url="https://candidate.example.test",
        runtime_attestation_sha256="4" * 64,
        observer_id="external-observer-01",
        observer_origin="https://observer.example.net",
        observed_at="2026-08-30T04:00:01Z",
        resolver=lambda _hostname: [PUBLIC_ADDRESSES[0]],
        connector=connect,
    )

    expected_ports = [
        *module.GATEWAY_ALLOWED_PORTS,
        *module.GATEWAY_DENIED_PORTS,
    ]
    assert calls == [
        (
            PUBLIC_ADDRESSES[0],
            port,
            "candidate.example.test" if port == 443 else None,
        )
        for port in expected_ports
    ]
    assert [row["port"] for row in report["observations"]] == expected_ports
    assert report["policy"] == {
        "allowedTcpPorts": list(module.GATEWAY_ALLOWED_PORTS),
        "deniedTcpPorts": list(module.GATEWAY_DENIED_PORTS),
    }


def test_external_gateway_probe_rejects_candidate_host_as_observer_before_connect() -> None:
    module = _load_probe()
    calls: list[object] = []

    with pytest.raises(ValueError, match="distinct from candidate"):
        module.probe_gateway_public(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            base_url="https://candidate.example.test",
            runtime_attestation_sha256="4" * 64,
            observer_id="candidate-host",
            observer_origin="https://candidate.example.test",
            observed_at="2026-08-30T04:00:01Z",
            connector=lambda *args: calls.append(args),
        )

    assert calls == []


def test_external_gateway_probe_requires_https_candidate_before_connect() -> None:
    module = _load_probe()
    calls: list[object] = []

    with pytest.raises(ValueError, match="HTTPS"):
        module.probe_gateway_public(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            base_url="http://candidate.example.test",
            runtime_attestation_sha256="4" * 64,
            observer_id="external-observer-01",
            observer_origin="https://observer.example.net",
            observed_at="2026-08-30T04:00:01Z",
            connector=lambda *args: calls.append(args),
        )

    assert calls == []


@pytest.mark.parametrize(
    ("base_url", "canonical_origin", "canonical_hostname"),
    (
        (
            "https://BÜCHER.example./",
            "https://xn--bcher-kva.example",
            "xn--bcher-kva.example",
        ),
        (
            "https://[2606:2800:0220:0001:0248:1893:25c8:1946]/",
            "https://[2606:2800:220:1:248:1893:25c8:1946]",
            "2606:2800:220:1:248:1893:25c8:1946",
        ),
    ),
)
def test_external_gateway_probe_canonicalizes_idna_and_ipv6_origins(
    base_url: str,
    canonical_origin: str,
    canonical_hostname: str,
) -> None:
    module = _load_probe()

    def connect(_host: str, port: int, tls_hostname: str | None):
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": "5" * 64,
                },
            }
        if port == 80:
            return {"outcome": "open", "error": None, "tls": None}
        return {"outcome": "closed", "error": "connection-refused", "tls": None}

    report = module.probe_gateway_public(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        base_url=base_url,
        runtime_attestation_sha256="4" * 64,
        observer_id="external-observer-01",
        observer_origin="https://OBSERVER.EXAMPLE.NET./",
        observed_at="2026-08-30T04:00:01Z",
        resolver=lambda _hostname: [
            canonical_hostname if ":" in canonical_hostname else PUBLIC_ADDRESSES[0]
        ],
        connector=connect,
    )

    assert report["baseUrl"] == canonical_origin
    assert (
        report["observations"][1]["addressObservations"][0]["tls"]["expectedHostname"]
        == canonical_hostname
    )
    assert report["observer"]["origin"] == "https://observer.example.net"


def test_external_gateway_probe_fails_when_tls_identity_is_not_verified() -> None:
    module = _load_probe()

    def connect(_host: str, port: int, tls_hostname: str | None):
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": False,
                    "peerCertificateSha256": "5" * 64,
                },
            }
        if port == 80:
            return {"outcome": "open", "error": None, "tls": None}
        return {"outcome": "closed", "error": "timeout", "tls": None}

    with pytest.raises(ValueError, match="TLS identity"):
        module.probe_gateway_public(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            base_url="https://candidate.example.test",
            runtime_attestation_sha256="4" * 64,
            observer_id="external-observer-01",
            observer_origin="https://observer.example.net",
            observed_at="2026-08-30T04:00:01Z",
            resolver=lambda _hostname: [PUBLIC_ADDRESSES[0]],
            connector=connect,
        )


def test_external_gateway_probe_never_creates_observer_attestation() -> None:
    module = _load_probe()

    source = (ROOT / "scripts" / "gateway_public_probe.py").read_text(encoding="utf-8")

    assert "GATEWAY_OBSERVER_ATTESTATION_PRODUCER" not in source
    assert "observer-attestation-output" not in source
    assert not hasattr(module, "write_observer_attestation")


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (OSError(errno.ECONNREFUSED, "refused"), "connection-refused"),
        (OSError(10061, "refused"), "connection-refused"),
        (OSError(errno.ETIMEDOUT, "timed out"), "timeout"),
        (OSError(10060, "timed out"), "timeout"),
    ),
)
def test_external_gateway_probe_classifies_only_exact_closed_errnos(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    expected: str,
) -> None:
    module = _load_probe()

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(module.socket, "create_connection", fail)

    assert module._connect("candidate.example.test", 8001, None) == {
        "outcome": "closed",
        "error": expected,
        "tls": None,
    }


@pytest.mark.parametrize(
    "error",
    (
        OSError(errno.EACCES, "denied"),
        OSError(errno.EMFILE, "too many files"),
        OSError(errno.ENETUNREACH, "network unreachable"),
        OSError(errno.EHOSTUNREACH, "host unreachable"),
        TimeoutError("timeout without errno"),
        OSError("unclassified"),
    ),
)
def test_external_gateway_probe_propagates_non_connectivity_oserrors(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    module = _load_probe()

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(module.socket, "create_connection", fail)

    with pytest.raises(OSError) as captured:
        module._connect("candidate.example.test", 8001, None)

    assert captured.value is error


def test_external_gateway_probe_resolves_once_and_attests_one_public_address_set() -> None:
    module = _load_probe()
    resolutions: list[str] = []
    calls: list[tuple[str, int, str | None]] = []

    def resolve(hostname: str) -> list[str]:
        resolutions.append(hostname)
        return [
            "2606:2800:0220:0001:0248:1893:25c8:1946",
            "93.184.216.34",
            "93.184.216.34",
        ]

    def connect(remote_ip: str, port: int, tls_hostname: str | None):
        calls.append((remote_ip, port, tls_hostname))
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "remoteIp": remote_ip,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": "5" * 64,
                },
            }
        if port == 80:
            return {
                "outcome": "open",
                "error": None,
                "remoteIp": remote_ip,
                "tls": None,
            }
        return {
            "outcome": "closed",
            "error": "connection-refused",
            "remoteIp": remote_ip,
            "tls": None,
        }

    report = module.probe_gateway_public(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        base_url="https://candidate.example.test",
        runtime_attestation_sha256="4" * 64,
        observer_id="external-observer-01",
        observer_origin="https://observer.example.net",
        observed_at="2026-08-30T04:00:01Z",
        resolver=resolve,
        connector=connect,
    )

    expected_ports = [*module.GATEWAY_ALLOWED_PORTS, *module.GATEWAY_DENIED_PORTS]
    assert resolutions == ["candidate.example.test"]
    assert calls == [
        (
            remote_ip,
            port,
            "candidate.example.test" if port == 443 else None,
        )
        for port in expected_ports
        for remote_ip in PUBLIC_ADDRESSES
    ]
    assert report["target"] == {
        "hostname": "candidate.example.test",
        "resolvedAddresses": list(PUBLIC_ADDRESSES),
    }
    assert all(
        [row["remoteIp"] for row in observation["addressObservations"]] == list(PUBLIC_ADDRESSES)
        for observation in report["observations"]
    )


def test_external_gateway_probe_uses_one_deadline_for_resolution_and_all_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_probe()
    now = [100.0]
    operation_timeouts: list[tuple[str, float]] = []

    def monotonic() -> float:
        return now[0]

    def resolve(_hostname: str, *, timeout: float) -> list[str]:
        operation_timeouts.append(("resolve", timeout))
        now[0] += 0.25
        return list(PUBLIC_ADDRESSES)

    def connect(
        _remote_ip: str,
        port: int,
        tls_hostname: str | None,
        *,
        timeout: float,
    ) -> dict[str, object]:
        operation_timeouts.append(("connect", timeout))
        now[0] += 0.25
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": "5" * 64,
                },
            }
        if port == 80:
            return {"outcome": "open", "error": None, "tls": None}
        return {"outcome": "closed", "error": "connection-refused", "tls": None}

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=monotonic), raising=False)

    module.probe_gateway_public(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        base_url="https://candidate.example.test",
        runtime_attestation_sha256="4" * 64,
        observer_id="external-observer-01",
        observer_origin="https://observer.example.net",
        observed_at="2026-08-30T04:00:01Z",
        resolver=resolve,
        connector=connect,
        timeout_seconds=8,
    )

    expected_connections = len(PUBLIC_ADDRESSES) * (
        len(module.GATEWAY_ALLOWED_PORTS) + len(module.GATEWAY_DENIED_PORTS)
    )
    assert len(operation_timeouts) == 1 + expected_connections
    assert operation_timeouts[0][0] == "resolve"
    assert 0 < operation_timeouts[0][1] <= 8
    connection_timeouts = [
        timeout for operation, timeout in operation_timeouts if operation == "connect"
    ]
    assert all(0 < timeout <= module.CONNECT_TIMEOUT_SECONDS for timeout in connection_timeouts)
    assert connection_timeouts[-1] < connection_timeouts[0]

    now[0] = 200.0
    connection_calls: list[object] = []

    def exhaust_deadline(_hostname: str, *, timeout: float) -> list[str]:
        assert 0 < timeout <= 1
        now[0] += 2.0
        return [PUBLIC_ADDRESSES[0]]

    with pytest.raises(ValueError, match="deadline|budget|timeout"):
        module.probe_gateway_public(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            base_url="https://candidate.example.test",
            runtime_attestation_sha256="4" * 64,
            observer_id="external-observer-01",
            observer_origin="https://observer.example.net",
            observed_at="2026-08-30T04:00:01Z",
            resolver=exhaust_deadline,
            connector=lambda *args, **kwargs: connection_calls.append((args, kwargs)),
            timeout_seconds=1,
        )

    assert connection_calls == []


def test_external_gateway_probe_bounds_blocking_dns_by_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_probe()
    release_blocked_lookup = threading.Event()
    completed = threading.Event()
    failures: list[BaseException] = []
    connector_calls: list[object] = []

    def blocking_getaddrinfo(*_args: object, **_kwargs: object) -> object:
        release_blocked_lookup.wait()
        raise module.socket.gaierror("released blocked DNS test double")

    monkeypatch.setattr(module.socket, "getaddrinfo", blocking_getaddrinfo)
    monkeypatch.setattr(
        module,
        "_DNS_RESOLVER_PROGRAM",
        "import time; time.sleep(30)",
        raising=False,
    )

    def invoke_probe() -> None:
        try:
            module.probe_gateway_public(
                candidate=CANDIDATE,
                release_run=RELEASE_RUN,
                base_url="https://candidate.example.test",
                runtime_attestation_sha256="4" * 64,
                observer_id="external-observer-01",
                observer_origin="https://observer.example.net",
                observed_at="2026-08-30T04:00:01Z",
                connector=lambda *args, **kwargs: connector_calls.append((args, kwargs)),
                timeout_seconds=0.1,
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            completed.set()

    started = time.monotonic()
    probe_thread = threading.Thread(target=invoke_probe, daemon=True)
    probe_thread.start()
    try:
        assert completed.wait(1.0), "blocking DNS exceeded the caller deadline"
    finally:
        release_blocked_lookup.set()
        probe_thread.join(timeout=1.0)

    assert time.monotonic() - started < 1.0
    assert not probe_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "DNS resolution deadline expired" in str(failures[0])
    assert connector_calls == []


def test_external_gateway_probe_dns_communicate_uses_remaining_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_probe()
    now = [100.0]
    communicate_timeouts: list[float | None] = []

    class Process:
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            communicate_timeouts.append(timeout)
            return b'["93.184.216.34"]', b""

        def kill(self) -> None:
            raise AssertionError("successful DNS resolution must not kill the resolver")

    def popen(*_args: object, **_kwargs: object) -> Process:
        now[0] += 0.75
        return Process()

    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.subprocess, "Popen", popen)

    assert module._resolve("candidate.example.test", timeout=1.0) == ("93.184.216.34",)
    assert len(communicate_timeouts) == 1
    assert communicate_timeouts[0] == pytest.approx(0.25)


@pytest.mark.parametrize(
    ("primary_kind", "cleanup_stage"),
    (
        pytest.param("timeout", "kill", id="timeout-kill"),
        pytest.param("timeout", "reap", id="timeout-reap"),
        pytest.param("interruption", "kill", id="interruption-kill"),
        pytest.param("interruption", "reap", id="interruption-reap"),
    ),
)
def test_external_gateway_probe_dns_cleanup_failures_preserve_primary_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    primary_kind: str,
    cleanup_stage: str,
) -> None:
    module = _load_probe()
    primary: BaseException
    if primary_kind == "timeout":
        primary = module.subprocess.TimeoutExpired(["isolated-dns-resolver"], 1.0)
    else:
        primary = KeyboardInterrupt("operator interrupted DNS resolution")
    cleanup_failure = OSError(f"injected resolver {cleanup_stage} cleanup failure")

    class Process:
        returncode = None

        def __init__(self) -> None:
            self.communicate_calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise primary
            if cleanup_stage == "reap":
                raise cleanup_failure
            return b"", b""

        def kill(self) -> None:
            if cleanup_stage == "kill":
                raise cleanup_failure

    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )

    expected_type = ValueError if primary_kind == "timeout" else KeyboardInterrupt
    with pytest.raises(expected_type) as captured:
        module._resolve("candidate.example.test", timeout=1.0)

    if primary_kind == "timeout":
        assert str(captured.value) == "gateway public DNS resolution deadline expired"
    else:
        assert captured.value is primary
    assert any(
        "gateway public DNS resolver cleanup failed" in note
        and cleanup_stage in note
        and str(cleanup_failure) in note
        for note in getattr(captured.value, "__notes__", ())
    )


def test_external_gateway_probe_rejects_more_than_sixteen_resolved_addresses() -> None:
    module = _load_probe()
    resolved_addresses = [f"2606:4700:4700::{index:x}" for index in range(1, 18)]

    def connect(
        _remote_ip: str,
        port: int,
        tls_hostname: str | None,
        **_options: object,
    ) -> dict[str, object]:
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": "5" * 64,
                },
            }
        if port == 80:
            return {"outcome": "open", "error": None, "tls": None}
        return {"outcome": "closed", "error": "connection-refused", "tls": None}

    with pytest.raises(ValueError, match="address.*limit|too many"):
        module.probe_gateway_public(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            base_url="https://candidate.example.test",
            runtime_attestation_sha256="4" * 64,
            observer_id="external-observer-01",
            observer_origin="https://observer.example.net",
            observed_at="2026-08-30T04:00:01Z",
            resolver=lambda _hostname, **_options: resolved_addresses,
            connector=connect,
        )


def test_external_gateway_probe_preserves_each_resolved_address_observation() -> None:
    module = _load_probe()

    def connect(remote_ip: str, port: int, tls_hostname: str | None):
        address_index = PUBLIC_ADDRESSES.index(remote_ip)
        if port == 443:
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": str(5 + address_index) * 64,
                },
            }
        if port == 80:
            return {"outcome": "open", "error": None, "tls": None}
        return {
            "outcome": "closed",
            "error": ("connection-refused", "timeout")[address_index],
            "tls": None,
        }

    report = module.probe_gateway_public(
        candidate=CANDIDATE,
        release_run=RELEASE_RUN,
        base_url="https://candidate.example.test",
        runtime_attestation_sha256="4" * 64,
        observer_id="external-observer-01",
        observer_origin="https://observer.example.net",
        observed_at="2026-08-30T04:00:01Z",
        resolver=lambda _hostname: list(PUBLIC_ADDRESSES),
        connector=connect,
    )

    for observation in report["observations"]:
        port = observation["port"]
        assert observation["addressObservations"] == [
            {
                "remoteIp": remote_ip,
                "outcome": "open" if port in module.GATEWAY_ALLOWED_PORTS else "closed",
                "error": (
                    None
                    if port in module.GATEWAY_ALLOWED_PORTS
                    else ("connection-refused", "timeout")[index]
                ),
                "tls": (
                    {
                        "expectedHostname": "candidate.example.test",
                        "hostnameVerified": True,
                        "peerCertificateSha256": str(5 + index) * 64,
                    }
                    if port == 443
                    else None
                ),
            }
            for index, remote_ip in enumerate(PUBLIC_ADDRESSES)
        ]


def test_external_gateway_probe_rejects_any_non_global_resolved_address_before_connect() -> None:
    module = _load_probe()
    calls: list[object] = []

    for rejected in (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.1.1",
        "0.0.0.0",
        "192.0.2.10",
        "::1",
        "fc00::1",
        "fe80::1",
        "::",
    ):
        with pytest.raises(ValueError, match="globally routable"):
            module.probe_gateway_public(
                candidate=CANDIDATE,
                release_run=RELEASE_RUN,
                base_url="https://candidate.example.test",
                runtime_attestation_sha256="4" * 64,
                observer_id="external-observer-01",
                observer_origin="https://observer.example.net",
                observed_at="2026-08-30T04:00:01Z",
                resolver=lambda _hostname, rejected=rejected: [
                    "93.184.216.34",
                    rejected,
                ],
                connector=lambda *args: calls.append(args),
            )

    assert calls == []


def test_external_gateway_probe_requires_every_address_to_satisfy_each_allowed_port() -> None:
    module = _load_probe()

    def connect(remote_ip: str, port: int, tls_hostname: str | None):
        if remote_ip == PUBLIC_ADDRESSES[1] and port == 443:
            return {
                "outcome": "closed",
                "error": "timeout",
                "remoteIp": remote_ip,
                "tls": None,
            }
        return {
            "outcome": "open" if port in module.GATEWAY_ALLOWED_PORTS else "closed",
            "error": None if port in module.GATEWAY_ALLOWED_PORTS else "connection-refused",
            "remoteIp": remote_ip,
            "tls": (
                {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": True,
                    "peerCertificateSha256": "5" * 64,
                }
                if port == 443
                else None
            ),
        }

    with pytest.raises(ValueError, match="every resolved address"):
        module.probe_gateway_public(
            candidate=CANDIDATE,
            release_run=RELEASE_RUN,
            base_url="https://candidate.example.test",
            runtime_attestation_sha256="4" * 64,
            observer_id="external-observer-01",
            observer_origin="https://observer.example.net",
            observed_at="2026-08-30T04:00:01Z",
            resolver=lambda _hostname: list(PUBLIC_ADDRESSES),
            connector=connect,
        )
