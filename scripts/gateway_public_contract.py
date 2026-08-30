"""Strict contract for externally observed gateway-only-public evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

GATEWAY_PUBLIC_SCHEMA_VERSION = 1
GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION = 1
GATEWAY_PUBLIC_PRODUCER = "gateway-external-probe"
GATEWAY_OBSERVER_ATTESTATION_PRODUCER = "gateway-external-observer-attestation"
GATEWAY_ALLOWED_PORTS = (80, 443)
GATEWAY_DENIED_PORTS = (3000, 3782, 5432, 8001, 8090, 9000, 9001)
MAX_GATEWAY_PUBLIC_ADDRESSES = 16
MAX_GATEWAY_PUBLIC_REPORT_BYTES = 128 * 1024
MAX_GATEWAY_OBSERVER_ATTESTATION_BYTES = 64 * 1024
MAX_GATEWAY_OBSERVATION_AGE_SECONDS = 600
MAX_GATEWAY_ATTESTATION_DELAY_SECONDS = 30
MAX_GATEWAY_FUTURE_SKEW_SECONDS = 5

_CANDIDATE_FIELDS = {
    "sourceRepository",
    "sourceHead",
    "releaseTag",
    "openmaicHead",
    "imageDigests",
}
_REPORT_FIELDS = {
    "schemaVersion",
    "producer",
    "candidate",
    "releaseRun",
    "observedAt",
    "baseUrl",
    "runtimeAttestation",
    "observer",
    "target",
    "policy",
    "observations",
}
_ATTESTATION_FIELDS = {
    "schemaVersion",
    "producer",
    "candidate",
    "releaseRun",
    "observedAt",
    "observer",
    "target",
    "runtimeAttestation",
    "externalObservation",
    "execution",
}
_EXECUTION_FIELDS = {
    "command",
    "nativeExit",
    "stdoutSha256",
    "stderr",
    "stderrSha256",
}
_OBSERVATION_FIELDS = {
    "port",
    "protocol",
    "expected",
    "addressObservations",
}
_ADDRESS_OBSERVATION_FIELDS = {"remoteIp", "outcome", "error", "tls"}
_TLS_FIELDS = {
    "expectedHostname",
    "hostnameVerified",
    "peerCertificateSha256",
}
_CLOSED_ERRORS = {
    "connection-refused",
    "timeout",
}
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "ticket",
    "token",
)

_OBSERVED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _ComposeLoader(yaml.SafeLoader):
    """Safe loader for Docker Compose value tags."""


def _construct_compose_value(
    loader: yaml.SafeLoader,
    node: ScalarNode | SequenceNode | MappingNode,
) -> object:
    if isinstance(node, ScalarNode):
        value = loader.construct_scalar(node)
        return None if value in {"null", "~"} else value
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for _compose_tag in ("!reset", "!override"):
    _ComposeLoader.add_constructor(_compose_tag, _construct_compose_value)


def parse_gateway_candidate_networks(
    compose_body: bytes,
    *,
    docker_project: str,
    expected_services: Sequence[str],
) -> dict[str, tuple[str, ...]]:
    """Derive exact Docker network names for runtime services from fixed Compose bytes."""

    if (
        not isinstance(compose_body, bytes)
        or not isinstance(docker_project, str)
        or not docker_project
        or docker_project != docker_project.strip()
    ):
        raise ValueError("gateway candidate Compose network set is invalid")
    service_names = frozenset(expected_services)
    if not service_names or any(
        not isinstance(service, str) or not service or service != service.strip()
        for service in service_names
    ):
        raise ValueError("gateway candidate Compose network set is invalid")
    try:
        document = yaml.load(compose_body.decode("utf-8"), Loader=_ComposeLoader)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("gateway candidate Compose network set is invalid") from exc
    services = document.get("services") if isinstance(document, dict) else None
    network_definitions = document.get("networks") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not isinstance(network_definitions, dict):
        raise ValueError("gateway candidate Compose network set is invalid")

    expected: dict[str, tuple[str, ...]] = {}
    for service_name in sorted(service_names):
        service = services.get(service_name)
        raw_networks = service.get("networks") if isinstance(service, dict) else None
        if isinstance(raw_networks, dict):
            logical_names = list(raw_networks)
        elif isinstance(raw_networks, list):
            logical_names = raw_networks
        else:
            raise ValueError("gateway candidate Compose network set is invalid")
        if (
            not logical_names
            or any(
                not isinstance(name, str) or not name or name != name.strip()
                for name in logical_names
            )
            or len(logical_names) != len(set(logical_names))
        ):
            raise ValueError("gateway candidate Compose network set is invalid")

        docker_names: list[str] = []
        for logical_name in logical_names:
            if logical_name not in network_definitions:
                raise ValueError("gateway candidate Compose network set is invalid")
            raw_definition = network_definitions[logical_name]
            if raw_definition is None:
                definition: Mapping[str, object] = {}
            elif isinstance(raw_definition, dict):
                definition = raw_definition
            else:
                raise ValueError("gateway candidate Compose network set is invalid")
            configured_name = definition.get("name")
            external = definition.get("external", False)
            if type(external) is not bool:
                raise ValueError("gateway candidate Compose network set is invalid")
            if configured_name is not None:
                if (
                    not isinstance(configured_name, str)
                    or not configured_name
                    or configured_name != configured_name.strip()
                    or "${" in configured_name
                ):
                    raise ValueError("gateway candidate Compose network set is invalid")
                docker_name = configured_name
            elif external:
                docker_name = logical_name
            else:
                docker_name = f"{docker_project}_{logical_name}"
            if docker_name in docker_names:
                raise ValueError("gateway candidate Compose network set is invalid")
            docker_names.append(docker_name)
        expected[service_name] = tuple(docker_names)
    return expected


def gateway_public_command_record() -> dict[str, object]:
    """Return the fixed, secret-free external probe command identity."""

    return {
        "runner": "python",
        "script": "scripts/gateway_public_probe.py",
        "arguments": ["--profile", "first-release"],
    }


def canonical_gateway_public_document(document: Mapping[str, object]) -> bytes:
    """Serialize a gateway document in the only accepted byte representation."""

    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _exact_json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(
            _exact_json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _exact_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None and value != "0" * 64


def _valid_observed_at(value: object) -> bool:
    if not isinstance(value, str) or _OBSERVED_AT.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


def _timestamp(value: object, *, label: str) -> datetime:
    if not _valid_observed_at(value):
        raise ValueError(f"gateway public {label} is invalid")
    return datetime.fromisoformat(str(value).removesuffix("Z") + "+00:00")


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_field(item) for item in value)
    return False


def _validate_candidate(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _CANDIDATE_FIELDS:
        raise ValueError("gateway public candidate binding is invalid")
    if (
        not isinstance(value.get("sourceRepository"), str)
        or _REPOSITORY.fullmatch(value["sourceRepository"]) is None
        or not isinstance(value.get("sourceHead"), str)
        or _GIT_SHA.fullmatch(value["sourceHead"]) is None
        or not isinstance(value.get("openmaicHead"), str)
        or _GIT_SHA.fullmatch(value["openmaicHead"]) is None
        or not isinstance(value.get("releaseTag"), str)
        or _PUBLIC_ID.fullmatch(value["releaseTag"]) is None
    ):
        raise ValueError("gateway public candidate binding is invalid")
    digests = value.get("imageDigests")
    if (
        not isinstance(digests, dict)
        or set(digests) != {"deeptutor", "openmaic", "openmaic_render"}
        or any(
            not isinstance(digest, str) or _IMAGE_DIGEST.fullmatch(digest) is None
            for digest in digests.values()
        )
    ):
        raise ValueError("gateway public candidate binding is invalid")
    if any(digest == "sha256:" + "0" * 64 for digest in digests.values()):
        raise ValueError("gateway public image digest is invalid")
    return value


def _validate_release_run(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"runId", "environmentId"}
        or any(
            not isinstance(item, str) or _PUBLIC_ID.fullmatch(item) is None
            for item in value.values()
        )
    ):
        raise ValueError("gateway public run binding is invalid")
    return value


def _https_origin(value: object, *, label: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError(f"gateway public {label} is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"gateway public {label} is invalid") from None
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
        raise ValueError(f"gateway public {label} is invalid")
    raw_hostname = parsed.hostname.rstrip(".")
    if "%" in raw_hostname:
        raise ValueError(f"gateway public {label} is invalid")
    try:
        address = ipaddress.ip_address(raw_hostname)
    except ValueError:
        try:
            hostname = raw_hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError(f"gateway public {label} is invalid") from None
        authority = hostname
    else:
        hostname = address.compressed.lower()
        authority = f"[{hostname}]" if address.version == 6 else hostname
    return f"https://{authority}", hostname


def canonical_gateway_public_addresses(addresses: object) -> tuple[str, ...]:
    """Normalize one non-empty set of globally routable IP addresses."""

    if not isinstance(addresses, Sequence) or isinstance(addresses, (str, bytes)) or not addresses:
        raise ValueError("gateway public resolved address set is invalid")
    parsed: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for value in addresses:
        if not isinstance(value, str) or not value or value != value.strip() or "%" in value:
            raise ValueError("gateway public resolved address set is invalid")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ValueError("gateway public resolved address set is invalid") from None
        if (
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        ):
            raise ValueError("gateway public resolved addresses must be globally routable")
        parsed.add(address)
    if len(parsed) > MAX_GATEWAY_PUBLIC_ADDRESSES:
        raise ValueError("gateway public resolved address set exceeds address limit")
    return tuple(
        address.compressed.lower()
        for address in sorted(parsed, key=lambda item: (item.version, item.packed))
    )


def _validate_canonical_address_set(value: object) -> tuple[str, ...]:
    canonical = canonical_gateway_public_addresses(value)
    if not isinstance(value, list) or value != list(canonical):
        raise ValueError("gateway public resolved address set is invalid")
    return canonical


def _validate_runtime_binding(
    value: object,
    *,
    expected_sha256: str,
) -> dict[str, str]:
    if (
        not _valid_sha256(expected_sha256)
        or not isinstance(value, dict)
        or set(value) != {"artifact", "sha256"}
        or value.get("artifact") != "runtime/runtime-attestation.json"
        or value.get("sha256") != expected_sha256
    ):
        raise ValueError("gateway public runtime attestation binding is invalid")
    return value


def _canonical_json_object(body: bytes, *, label: str, limit: int) -> dict[str, object]:
    if not isinstance(body, bytes) or not body or len(body) > limit:
        raise ValueError(f"gateway public {label} is invalid")
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError(f"gateway public {label} is invalid") from None
    if (
        not isinstance(document, dict)
        or _contains_sensitive_field(document)
        or canonical_gateway_public_document(document) != body
    ):
        raise ValueError(f"gateway public {label} is invalid")
    return document


def _validate_observer_execution(
    value: object,
    *,
    expected_stdout_sha256: str,
) -> dict[str, object]:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    if (
        not isinstance(value, dict)
        or set(value) != _EXECUTION_FIELDS
        or not _exact_json_equal(value.get("command"), gateway_public_command_record())
        or type(value.get("nativeExit")) is not int
        or value.get("nativeExit") != 0
        or value.get("stdoutSha256") != expected_stdout_sha256
        or value.get("stderr") != ""
        or value.get("stderrSha256") != empty_sha256
    ):
        raise ValueError("gateway public observer execution is invalid")
    return value


def parse_gateway_observer_attestation(
    body: bytes,
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    expected_external_observation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_attestation_sha256: str,
) -> dict[str, object]:
    """Parse the externally anchored observer identity and observation binding."""

    if not _valid_sha256(expected_attestation_sha256):
        raise ValueError("gateway observer out-of-band attestation digest is invalid")
    if hashlib.sha256(body).hexdigest() != expected_attestation_sha256:
        raise ValueError("gateway observer out-of-band attestation digest does not match")
    document = _canonical_json_object(
        body,
        label="observer attestation",
        limit=MAX_GATEWAY_OBSERVER_ATTESTATION_BYTES,
    )
    if (
        set(document) != _ATTESTATION_FIELDS
        or type(document.get("schemaVersion")) is not int
        or document.get("schemaVersion") != GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION
        or document.get("producer") != GATEWAY_OBSERVER_ATTESTATION_PRODUCER
        or not _valid_observed_at(document.get("observedAt"))
    ):
        raise ValueError("gateway public observer attestation is invalid")
    _validate_candidate(document.get("candidate"))
    if not _exact_json_equal(document.get("candidate"), dict(candidate)):
        raise ValueError("gateway public candidate binding is invalid")
    _validate_release_run(document.get("releaseRun"))
    if not _exact_json_equal(document.get("releaseRun"), dict(release_run)):
        raise ValueError("gateway public run binding is invalid")
    candidate_origin, candidate_hostname = _https_origin(
        expected_base_url,
        label="candidate HTTPS origin",
    )
    observer = document.get("observer")
    if (
        not isinstance(expected_observer_id, str)
        or _PUBLIC_ID.fullmatch(expected_observer_id) is None
        or not isinstance(observer, dict)
        or set(observer) != {"observerId", "origin"}
        or observer.get("observerId") != expected_observer_id
    ):
        raise ValueError("gateway public observer identity is invalid")
    expected_observer_origin, _expected_observer_hostname = _https_origin(
        expected_observer_origin,
        label="expected observer origin",
    )
    observer_origin, observer_hostname = _https_origin(
        observer.get("origin"),
        label="observer origin",
    )
    if observer_hostname == candidate_hostname:
        raise ValueError("gateway observer origin must be distinct from candidate")
    target = document.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"origin", "expectedTlsHostname", "resolvedAddresses"}
        or target.get("origin") != candidate_origin
        or target.get("expectedTlsHostname") != candidate_hostname
        or observer.get("origin") != observer_origin
        or observer_origin != expected_observer_origin
    ):
        raise ValueError("gateway public observer identity or target is invalid")
    _validate_canonical_address_set(target.get("resolvedAddresses"))
    _validate_runtime_binding(
        document.get("runtimeAttestation"),
        expected_sha256=expected_runtime_attestation_sha256,
    )
    observation = document.get("externalObservation")
    if (
        not _valid_sha256(expected_external_observation_sha256)
        or not isinstance(observation, dict)
        or set(observation) != {"artifact", "sha256"}
        or observation.get("artifact") != "raw/gateway-public-observation.json"
        or observation.get("sha256") != expected_external_observation_sha256
    ):
        raise ValueError("gateway public external observation binding is invalid")
    _validate_observer_execution(
        document.get("execution"),
        expected_stdout_sha256=expected_external_observation_sha256,
    )
    return document


def _validate_tls(value: object, *, expected_hostname: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != _TLS_FIELDS
        or value.get("expectedHostname") != expected_hostname
        or value.get("hostnameVerified") is not True
        or not _valid_sha256(value.get("peerCertificateSha256"))
    ):
        raise ValueError("gateway public TLS identity is invalid")
    return value


def validate_gateway_public_report(
    report: Mapping[str, object],
    *,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
) -> dict[str, object]:
    """Validate one already-decoded fixed external observation."""

    if (
        not isinstance(report, dict)
        or set(report) != _REPORT_FIELDS
        or type(report.get("schemaVersion")) is not int
        or report.get("schemaVersion") != GATEWAY_PUBLIC_SCHEMA_VERSION
        or report.get("producer") != GATEWAY_PUBLIC_PRODUCER
        or not _valid_observed_at(report.get("observedAt"))
    ):
        raise ValueError("gateway public report is invalid")
    _validate_candidate(report.get("candidate"))
    if not _exact_json_equal(report.get("candidate"), dict(candidate)):
        raise ValueError("gateway public candidate binding is invalid")
    _validate_release_run(report.get("releaseRun"))
    if not _exact_json_equal(report.get("releaseRun"), dict(release_run)):
        raise ValueError("gateway public run binding is invalid")
    candidate_origin, candidate_hostname = _https_origin(
        expected_base_url,
        label="candidate HTTPS origin",
    )
    if report.get("baseUrl") != candidate_origin:
        raise ValueError("gateway public candidate origin binding is invalid")
    target = report.get("target")
    if (
        not isinstance(target, dict)
        or set(target) != {"hostname", "resolvedAddresses"}
        or target.get("hostname") != candidate_hostname
    ):
        raise ValueError("gateway public resolved address set is invalid")
    resolved_addresses = _validate_canonical_address_set(target.get("resolvedAddresses"))
    _validate_runtime_binding(
        report.get("runtimeAttestation"),
        expected_sha256=expected_runtime_attestation_sha256,
    )
    canonical_observer_origin, _expected_observer_hostname = _https_origin(
        expected_observer_origin,
        label="expected observer origin",
    )
    observer = report.get("observer")
    if (
        not isinstance(expected_observer_id, str)
        or _PUBLIC_ID.fullmatch(expected_observer_id) is None
        or not isinstance(observer, dict)
        or set(observer) != {"observerId", "origin"}
        or observer.get("observerId") != expected_observer_id
    ):
        raise ValueError("gateway public observer identity is invalid")
    observer_origin, observer_hostname = _https_origin(
        observer.get("origin"),
        label="observer origin",
    )
    if observer.get("origin") != observer_origin or observer_origin != canonical_observer_origin:
        raise ValueError("gateway public observer identity is invalid")
    if observer_hostname == candidate_hostname:
        raise ValueError("gateway observer origin must be distinct from candidate")
    policy = report.get("policy")
    if (
        not isinstance(policy, dict)
        or set(policy) != {"allowedTcpPorts", "deniedTcpPorts"}
        or policy.get("allowedTcpPorts") != list(GATEWAY_ALLOWED_PORTS)
        or policy.get("deniedTcpPorts") != list(GATEWAY_DENIED_PORTS)
    ):
        raise ValueError("gateway public fixed port policy is invalid")
    observations = report.get("observations")
    expected_ports = (*GATEWAY_ALLOWED_PORTS, *GATEWAY_DENIED_PORTS)
    if (
        not isinstance(observations, list)
        or len(observations) != len(expected_ports)
        or [row.get("port") if isinstance(row, dict) else None for row in observations]
        != list(expected_ports)
    ):
        raise ValueError("gateway public observation matrix is invalid")
    for port, observation in zip(expected_ports, observations, strict=True):
        expected = "open" if port in GATEWAY_ALLOWED_PORTS else "closed"
        if (
            not isinstance(observation, dict)
            or set(observation) != _OBSERVATION_FIELDS
            or type(observation.get("port")) is not int
            or observation.get("port") != port
            or observation.get("protocol") != "tcp"
            or observation.get("expected") != expected
        ):
            raise ValueError("gateway public observation matrix is invalid")
        address_observations = observation.get("addressObservations")
        if (
            not isinstance(address_observations, list)
            or len(address_observations) != len(resolved_addresses)
            or [
                row.get("remoteIp") if isinstance(row, dict) else None
                for row in address_observations
            ]
            != list(resolved_addresses)
        ):
            raise ValueError("gateway public per-address observation set is invalid")
        for remote_ip, address_observation in zip(
            resolved_addresses,
            address_observations,
            strict=True,
        ):
            if (
                not isinstance(address_observation, dict)
                or set(address_observation) != _ADDRESS_OBSERVATION_FIELDS
                or address_observation.get("remoteIp") != remote_ip
            ):
                raise ValueError("gateway public per-address observation set is invalid")
            if port in GATEWAY_ALLOWED_PORTS:
                if (
                    address_observation.get("outcome") != "open"
                    or address_observation.get("error") is not None
                ):
                    raise ValueError("gateway public gateway port observation is invalid")
                if port == 443:
                    _validate_tls(
                        address_observation.get("tls"),
                        expected_hostname=candidate_hostname,
                    )
                elif address_observation.get("tls") is not None:
                    raise ValueError("gateway public gateway port observation is invalid")
            elif (
                address_observation.get("outcome") != "closed"
                or address_observation.get("error") not in _CLOSED_ERRORS
                or address_observation.get("tls") is not None
            ):
                raise ValueError("gateway public internal port observation is invalid")
    return report


def parse_gateway_public_report(
    body: bytes,
    *,
    observer_attestation_body: bytes,
    candidate: Mapping[str, object],
    release_run: Mapping[str, str],
    expected_base_url: str,
    expected_runtime_attestation_sha256: str,
    expected_observer_id: str,
    expected_observer_origin: str,
    expected_attestation_sha256: str,
    trusted_now: str,
    run_started_at: str,
    run_ended_at: str,
) -> dict[str, object]:
    """Parse a canonical report bound by the external observer attestation."""

    report = _canonical_json_object(
        body,
        label="external observation",
        limit=MAX_GATEWAY_PUBLIC_REPORT_BYTES,
    )
    observer_attestation = parse_gateway_observer_attestation(
        observer_attestation_body,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=expected_base_url,
        expected_runtime_attestation_sha256=expected_runtime_attestation_sha256,
        expected_external_observation_sha256=hashlib.sha256(body).hexdigest(),
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
        expected_attestation_sha256=expected_attestation_sha256,
    )
    parsed = validate_gateway_public_report(
        report,
        candidate=candidate,
        release_run=release_run,
        expected_base_url=expected_base_url,
        expected_runtime_attestation_sha256=expected_runtime_attestation_sha256,
        expected_observer_id=expected_observer_id,
        expected_observer_origin=expected_observer_origin,
    )
    report_target = parsed.get("target")
    attestation_target = observer_attestation.get("target")
    if (
        not isinstance(report_target, dict)
        or not isinstance(attestation_target, dict)
        or not _exact_json_equal(
            report_target.get("resolvedAddresses"),
            attestation_target.get("resolvedAddresses"),
        )
    ):
        raise ValueError("gateway public observer address set does not match external observation")
    report_time = _timestamp(parsed.get("observedAt"), label="observation timestamp")
    attestation_time = _timestamp(
        observer_attestation.get("observedAt"),
        label="observer attestation timestamp",
    )
    now = _timestamp(trusted_now, label="trusted current time")
    run_started = _timestamp(run_started_at, label="run window start")
    run_ended = _timestamp(run_ended_at, label="run window end")
    if run_ended < run_started:
        raise ValueError("gateway public run window is invalid")
    if now - report_time > timedelta(seconds=MAX_GATEWAY_OBSERVATION_AGE_SECONDS):
        raise ValueError("gateway public observation is too old")
    future_limit = now + timedelta(seconds=MAX_GATEWAY_FUTURE_SKEW_SECONDS)
    if report_time > future_limit or attestation_time > future_limit:
        raise ValueError("gateway public evidence timestamp is in the future")
    if attestation_time < report_time:
        raise ValueError("gateway public observer attestation predates observation")
    if attestation_time - report_time > timedelta(seconds=MAX_GATEWAY_ATTESTATION_DELAY_SECONDS):
        raise ValueError("gateway public observer attestation delay is too large")
    if not (
        run_started <= report_time <= run_ended and run_started <= attestation_time <= run_ended
    ):
        raise ValueError("gateway public evidence is outside the run window")
    return parsed


def signed_gateway_observer_policy(
    observer_attestation_body: bytes,
    observer_trust_payload: Mapping[str, object],
) -> dict[str, str]:
    """Derive observer policy only from a verified signed payload and its artifact."""

    document = _canonical_json_object(
        observer_attestation_body,
        label="observer attestation",
        limit=MAX_GATEWAY_OBSERVER_ATTESTATION_BYTES,
    )
    observer = document.get("observer")
    claims = observer_trust_payload.get("claims")
    issued_at = observer_trust_payload.get("issuedAt")
    expires_at = observer_trust_payload.get("expiresAt")
    if (
        not isinstance(observer, dict)
        or set(observer) != {"observerId", "origin"}
        or not isinstance(observer.get("observerId"), str)
        or _PUBLIC_ID.fullmatch(observer["observerId"]) is None
        or not isinstance(claims, Mapping)
        or claims.get("artifact") != "runtime/gateway-external-observer-attestation.json"
        or not isinstance(claims.get("artifactSha256"), str)
        or not _valid_sha256(claims["artifactSha256"])
        or claims["artifactSha256"] != hashlib.sha256(observer_attestation_body).hexdigest()
        or not isinstance(issued_at, str)
        or not isinstance(expires_at, str)
    ):
        raise ValueError("gateway signed observer policy is invalid")
    observer_origin, _observer_hostname = _https_origin(
        observer.get("origin"),
        label="signed observer origin",
    )
    return {
        "expected_observer_id": observer["observerId"],
        "expected_observer_origin": observer_origin,
        "expected_attestation_sha256": claims["artifactSha256"],
        "run_started_at": issued_at,
        "run_ended_at": expires_at,
    }


def derive_gateway_public_checks(report: Mapping[str, object]) -> dict[str, bool]:
    """Derive the two release checks from a validated fixed observation matrix."""

    failed = {"gatewayPublic": False, "internalPortsClosed": False}
    observations = report.get("observations") if isinstance(report, Mapping) else None
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        return failed
    by_port = {
        row.get("port"): row
        for row in observations
        if isinstance(row, Mapping) and type(row.get("port")) is int
    }

    def address_rows(port: int) -> Sequence[object] | None:
        observation = by_port.get(port)
        rows = observation.get("addressObservations") if isinstance(observation, Mapping) else None
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
            return None
        return rows

    gateway_public = True
    for port in GATEWAY_ALLOWED_PORTS:
        rows = address_rows(port)
        if rows is None:
            gateway_public = False
            break
        for row in rows:
            if (
                not isinstance(row, Mapping)
                or row.get("outcome") != "open"
                or row.get("error") is not None
            ):
                gateway_public = False
                break
            tls = row.get("tls")
            if port == 443:
                if (
                    not isinstance(tls, Mapping)
                    or tls.get("hostnameVerified") is not True
                    or not _valid_sha256(tls.get("peerCertificateSha256"))
                ):
                    gateway_public = False
                    break
            elif tls is not None:
                gateway_public = False
                break
        if not gateway_public:
            break

    internal_closed = True
    for port in GATEWAY_DENIED_PORTS:
        rows = address_rows(port)
        if rows is None or not all(
            isinstance(row, Mapping)
            and row.get("outcome") == "closed"
            and row.get("error") in _CLOSED_ERRORS
            and row.get("tls") is None
            for row in rows
        ):
            internal_closed = False
            break
    return {
        "gatewayPublic": gateway_public,
        "internalPortsClosed": internal_closed,
    }


__all__ = [
    "GATEWAY_ALLOWED_PORTS",
    "GATEWAY_DENIED_PORTS",
    "GATEWAY_OBSERVER_ATTESTATION_PRODUCER",
    "GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION",
    "GATEWAY_PUBLIC_PRODUCER",
    "GATEWAY_PUBLIC_SCHEMA_VERSION",
    "MAX_GATEWAY_PUBLIC_ADDRESSES",
    "MAX_GATEWAY_ATTESTATION_DELAY_SECONDS",
    "MAX_GATEWAY_FUTURE_SKEW_SECONDS",
    "MAX_GATEWAY_OBSERVER_ATTESTATION_BYTES",
    "MAX_GATEWAY_OBSERVATION_AGE_SECONDS",
    "MAX_GATEWAY_PUBLIC_REPORT_BYTES",
    "canonical_gateway_public_addresses",
    "canonical_gateway_public_document",
    "derive_gateway_public_checks",
    "gateway_public_command_record",
    "parse_gateway_candidate_networks",
    "parse_gateway_observer_attestation",
    "parse_gateway_public_report",
    "signed_gateway_observer_policy",
    "validate_gateway_public_report",
]
