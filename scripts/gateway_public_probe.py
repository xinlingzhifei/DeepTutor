#!/usr/bin/env python
"""Run the fixed gateway port matrix from an external observer host."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import errno
import hashlib
import inspect
import ipaddress
import json
import math
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from typing import Protocol
from urllib.parse import urlsplit

SCRIPTS_ROOT = Path(__file__).resolve().parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from gateway_public_contract import (
    GATEWAY_ALLOWED_PORTS,
    GATEWAY_DENIED_PORTS,
    GATEWAY_PUBLIC_PRODUCER,
    GATEWAY_PUBLIC_SCHEMA_VERSION,
    canonical_gateway_public_addresses,
    canonical_gateway_public_document,
    validate_gateway_public_report,
)

CONNECT_TIMEOUT_SECONDS = 5.0
_DNS_REAP_TIMEOUT_SECONDS = 0.25
_DNS_RESOLVER_PROGRAM = """
import json
import socket
import sys

answers = socket.getaddrinfo(
    sys.argv[1],
    None,
    family=socket.AF_UNSPEC,
    type=socket.SOCK_STREAM,
)
addresses = [
    answer[4][0]
    for answer in answers
    if answer[0] in {socket.AF_INET, socket.AF_INET6}
    and isinstance(answer[4], tuple)
    and answer[4]
]
sys.stdout.write(json.dumps(addresses, separators=(",", ":")))
"""


class Connector(Protocol):
    def __call__(
        self,
        host: str,
        port: int,
        tls_hostname: str | None,
        *,
        timeout: float,
    ) -> Mapping[str, object]: ...


class Resolver(Protocol):
    def __call__(self, hostname: str, *, timeout: float) -> Sequence[str]: ...


def _connection_error(exc: OSError) -> str:
    error_number = getattr(exc, "errno", None)
    if error_number in {errno.ECONNREFUSED, 10061}:
        return "connection-refused"
    if error_number in {errno.ETIMEDOUT, 10060}:
        return "timeout"
    raise exc


def _connect(
    host: str,
    port: int,
    tls_hostname: str | None,
    *,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return {
            "outcome": "closed",
            "error": _connection_error(exc),
            "tls": None,
        }
    with raw:
        if tls_hostname is None:
            return {"outcome": "open", "error": None, "tls": None}
        context = ssl.create_default_context()
        try:
            with context.wrap_socket(raw, server_hostname=tls_hostname) as secured:
                certificate = secured.getpeercert(binary_form=True)
        except (OSError, ssl.SSLError, ValueError):
            return {
                "outcome": "open",
                "error": None,
                "tls": {
                    "expectedHostname": tls_hostname,
                    "hostnameVerified": False,
                    "peerCertificateSha256": "0" * 64,
                },
            }
        if not isinstance(certificate, bytes) or not certificate:
            raise ValueError("gateway public TLS certificate is unavailable")
        return {
            "outcome": "open",
            "error": None,
            "tls": {
                "expectedHostname": tls_hostname,
                "hostnameVerified": True,
                "peerCertificateSha256": hashlib.sha256(certificate).hexdigest(),
            },
        }


def _resolve(
    hostname: str,
    *,
    timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("gateway public DNS timeout is invalid")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", _DNS_RESOLVER_PROGRAM, hostname],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        error = ValueError("gateway public DNS resolution deadline expired")
        _cleanup_resolver_process(process, error)
        raise error from None
    try:
        stdout, _stderr = process.communicate(timeout=remaining)
    except subprocess.TimeoutExpired:
        error = ValueError("gateway public DNS resolution deadline expired")
        _cleanup_resolver_process(process, error)
        raise error from None
    except BaseException as error:
        _cleanup_resolver_process(process, error)
        raise
    if process.returncode != 0:
        raise ValueError("gateway public DNS resolution failed")
    try:
        addresses = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("gateway public DNS resolution returned invalid data") from None
    if not isinstance(addresses, list) or any(
        not isinstance(address, str) for address in addresses
    ):
        raise ValueError("gateway public DNS resolution returned invalid data")
    return canonical_gateway_public_addresses(addresses)


def _cleanup_resolver_process(
    process: subprocess.Popen[bytes],
    primary_error: BaseException,
) -> None:
    try:
        process.kill()
    except BaseException as cleanup_error:
        primary_error.add_note(
            "gateway public DNS resolver cleanup failed during kill: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
    try:
        process.communicate(timeout=_DNS_REAP_TIMEOUT_SECONDS)
    except BaseException as cleanup_error:
        primary_error.add_note(
            "gateway public DNS resolver cleanup failed during reap: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
    else:
        if process.returncode is None:
            primary_error.add_note(
                "gateway public DNS resolver cleanup failed during reap: "
                "resolver process has no terminal return code"
            )


def _accepts_timeout(operation: object) -> bool:
    try:
        parameters = inspect.signature(operation).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == "timeout" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _remaining_timeout(deadline_monotonic: float, *, maximum: float | None = None) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise ValueError("gateway public probe deadline expired")
    return remaining if maximum is None else min(maximum, remaining)


def _verified_tls(result: Mapping[str, object], *, hostname: str) -> bool:
    tls = result.get("tls")
    if not isinstance(tls, Mapping) or set(tls) != {
        "expectedHostname",
        "hostnameVerified",
        "peerCertificateSha256",
    }:
        return False
    digest = tls.get("peerCertificateSha256")
    return (
        tls.get("expectedHostname") == hostname
        and tls.get("hostnameVerified") is True
        and isinstance(digest, str)
        and len(digest) == 64
        and digest != "0" * 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _observation_for_port(
    *,
    addresses: tuple[str, ...],
    port: int,
    hostname: str,
    connector: Connector,
    deadline_monotonic: float,
) -> dict[str, object]:
    tls_hostname = hostname if port == 443 else None
    results: list[dict[str, object]] = []
    for remote_ip in addresses:
        timeout = _remaining_timeout(
            deadline_monotonic,
            maximum=CONNECT_TIMEOUT_SECONDS,
        )
        if _accepts_timeout(connector):
            connected = connector(remote_ip, port, tls_hostname, timeout=timeout)
        else:
            connected = connector(remote_ip, port, tls_hostname)
        result = dict(connected)
        reported_remote_ip = result.get("remoteIp")
        if reported_remote_ip is not None:
            try:
                reported = canonical_gateway_public_addresses([reported_remote_ip])
            except ValueError as exc:
                raise ValueError(
                    "gateway public connector remote address is outside attested address set"
                ) from exc
            if reported != (remote_ip,):
                raise ValueError(
                    "gateway public connector remote address is outside attested address set"
                )
        results.append(result)

    address_observations = [
        {
            "remoteIp": remote_ip,
            "outcome": result.get("outcome"),
            "error": result.get("error"),
            "tls": result.get("tls"),
        }
        for remote_ip, result in zip(addresses, results, strict=True)
    ]

    expected = "open" if port in GATEWAY_ALLOWED_PORTS else "closed"
    if port in GATEWAY_ALLOWED_PORTS:
        if any(
            result.get("outcome") != "open" or result.get("error") is not None for result in results
        ):
            raise ValueError("gateway public every resolved address must satisfy each allowed port")
        if port == 443 and any(not _verified_tls(result, hostname=hostname) for result in results):
            raise ValueError("gateway public TLS identity is invalid")
        if port != 443 and any(result.get("tls") is not None for result in results):
            raise ValueError("gateway public every resolved address must satisfy each allowed port")
    else:
        if any(
            result.get("outcome") != "closed"
            or result.get("error") not in {"connection-refused", "timeout"}
            or result.get("tls") is not None
            for result in results
        ):
            raise ValueError("gateway public every resolved address must satisfy each denied port")
    return {
        "port": port,
        "protocol": "tcp",
        "expected": expected,
        "addressObservations": address_observations,
    }


def _canonical_base_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("gateway external probe requires an HTTPS candidate origin") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise ValueError("gateway external probe requires an HTTPS candidate origin")
    raw_hostname = parsed.hostname.rstrip(".")
    if "%" in raw_hostname:
        raise ValueError("gateway external probe requires an HTTPS candidate origin")
    try:
        address = ipaddress.ip_address(raw_hostname)
    except ValueError:
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("gateway external probe requires an HTTPS candidate origin") from None
        authority = hostname
    else:
        hostname = address.compressed.lower()
        authority = f"[{hostname}]" if address.version == 6 else hostname
    return f"https://{authority}", hostname


def _observer_origin(value: str, *, candidate_hostname: str) -> str:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("gateway external observer origin is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port not in {None, 443}
    ):
        raise ValueError("gateway external observer origin is invalid")
    raw_hostname = parsed.hostname.rstrip(".")
    if "%" in raw_hostname:
        raise ValueError("gateway external observer origin is invalid")
    try:
        address = ipaddress.ip_address(raw_hostname)
    except ValueError:
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("gateway external observer origin is invalid") from None
        authority = hostname
    else:
        hostname = address.compressed.lower()
        authority = f"[{hostname}]" if address.version == 6 else hostname
    if hostname == candidate_hostname:
        raise ValueError("gateway observer origin must be distinct from candidate")
    return f"https://{authority}"


def probe_gateway_public(
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    base_url: str,
    runtime_attestation_sha256: str,
    observer_id: str,
    observer_origin: str,
    observed_at: str | None = None,
    resolver: Resolver = _resolve,
    connector: Connector = _connect,
    timeout_seconds: float = 60.0,
) -> dict[str, object]:
    """Observe exactly the approved gateway and internal TCP ports."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("gateway public probe timeout is invalid")
    deadline_monotonic = time.monotonic() + float(timeout_seconds)
    canonical_url, hostname = _canonical_base_url(base_url)
    canonical_observer = _observer_origin(
        observer_origin,
        candidate_hostname=hostname,
    )
    resolver_timeout = _remaining_timeout(deadline_monotonic)
    if _accepts_timeout(resolver):
        resolved = resolver(hostname, timeout=resolver_timeout)
    else:
        resolved = resolver(hostname)
    resolved_addresses = canonical_gateway_public_addresses(resolved)
    timestamp = observed_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    observations = [
        _observation_for_port(
            addresses=resolved_addresses,
            port=port,
            hostname=hostname,
            connector=connector,
            deadline_monotonic=deadline_monotonic,
        )
        for port in (*GATEWAY_ALLOWED_PORTS, *GATEWAY_DENIED_PORTS)
    ]
    report: dict[str, object] = {
        "schemaVersion": GATEWAY_PUBLIC_SCHEMA_VERSION,
        "producer": GATEWAY_PUBLIC_PRODUCER,
        "candidate": json.loads(json.dumps(candidate)),
        "releaseRun": json.loads(json.dumps(release_run)),
        "observedAt": timestamp,
        "baseUrl": canonical_url,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_attestation_sha256,
        },
        "observer": {
            "observerId": observer_id,
            "origin": canonical_observer,
        },
        "target": {
            "hostname": hostname,
            "resolvedAddresses": list(resolved_addresses),
        },
        "policy": {
            "allowedTcpPorts": list(GATEWAY_ALLOWED_PORTS),
            "deniedTcpPorts": list(GATEWAY_DENIED_PORTS),
        },
        "observations": observations,
    }
    return validate_gateway_public_report(
        report,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=canonical_url,
        expected_runtime_attestation_sha256=runtime_attestation_sha256,
        expected_observer_id=observer_id,
        expected_observer_origin=canonical_observer,
    )


def _json_file(value: str) -> dict[str, object]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError:
        raise argparse.ArgumentTypeError("value must be a JSON object") from None
    if not isinstance(document, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return document


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("first-release",), required=True)
    parser.add_argument("--candidate-json", type=_json_file, required=True)
    parser.add_argument("--release-run-json", type=_json_file, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--runtime-attestation-sha256", required=True)
    parser.add_argument("--observer-id", required=True)
    parser.add_argument("--observer-origin", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = probe_gateway_public(
        candidate=args.candidate_json,
        release_run=args.release_run_json,
        base_url=args.base_url,
        runtime_attestation_sha256=args.runtime_attestation_sha256,
        observer_id=args.observer_id,
        observer_origin=args.observer_origin,
    )
    sys.stdout.buffer.write(canonical_gateway_public_document(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GATEWAY_ALLOWED_PORTS",
    "GATEWAY_DENIED_PORTS",
    "main",
    "probe_gateway_public",
]
