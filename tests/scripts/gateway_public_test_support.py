from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

BASE_URL = "https://candidate.example.test"
PUBLIC_ADDRESSES = (
    "93.184.216.34",
    "2606:2800:220:1:248:1893:25c8:1946",
)
OBSERVER_ID = "external-observer-01"
OBSERVER_ORIGIN = "https://observer.example.net"
EXPECTED_ATTESTATION_ENV = "YFEISTAI_GATEWAY_OBSERVER_ATTESTATION_SHA256"
EXPECTED_OBSERVER_ID_ENV = "YFEISTAI_GATEWAY_EXPECTED_OBSERVER_ID"
EXPECTED_OBSERVER_ORIGIN_ENV = "YFEISTAI_GATEWAY_EXPECTED_OBSERVER_ORIGIN"
TRUSTED_NOW_ENV = "YFEISTAI_GATEWAY_TRUSTED_NOW"
RUN_STARTED_AT_ENV = "YFEISTAI_GATEWAY_RUN_STARTED_AT"
RUN_ENDED_AT_ENV = "YFEISTAI_GATEWAY_RUN_ENDED_AT"
TRUSTED_NOW = "2026-08-30T04:05:00Z"
RUN_STARTED_AT = "2026-08-30T04:00:00Z"
RUN_ENDED_AT = "2026-08-30T04:10:00Z"
DOCKER_PROJECT = "yfeistai-platform"
DOCKER_CONTEXT = "default"
DOCKER_ENDPOINT = "npipe:////./pipe/dockerDesktopLinuxEngine"
DOCKER_SERVER_ID = "daemon-yfeistai-01"
DOCKER_OS_TYPE = "linux"
DOCKER_HOST_IDENTITY_SHA256 = "7" * 64
GATEWAY_OBSERVER_TRUST_ENVELOPE_ARTIFACT = "runtime/gateway-observer-trust-envelope.json"
GATEWAY_HOST_TRUST_ENVELOPE_ARTIFACT = "runtime/gateway-host-provisioner-trust-envelope.json"
GATEWAY_HOST_PROVISIONING_RECEIPT_ARTIFACT = "runtime/gateway-docker-host-provisioning-receipt.json"
GATEWAY_OBSERVER_ATTESTATION_ARTIFACT = "runtime/gateway-external-observer-attestation.json"
GATEWAY_OBSERVER_CHALLENGE = "8" * 64
GATEWAY_HOST_CHALLENGE = "9" * 64
GATEWAY_TRUST_ISSUED_AT = "2026-08-30T04:00:00Z"
GATEWAY_TRUST_EXPIRES_AT = "2026-08-30T04:10:00Z"
GATEWAY_TRUST_SIGNATURE_DOMAIN = b"yfeistai.gateway-trust-envelope.v1\0"
EXPECTED_DOCKER_HOST_IDENTITY_ENV = "YFEISTAI_GATEWAY_DOCKER_HOST_IDENTITY_SHA256"
DOCKER_LOGICAL_PREFIX = [
    "docker",
    "--config",
    "<isolated-docker-config>",
    "--context",
    "default",
]
DOCKER_PS_ARGUMENTS = [
    "ps",
    "-a",
    "--no-trunc",
    "--filter",
    f"label=com.docker.compose.project={DOCKER_PROJECT}",
    "--format",
    "{{json .ID}}",
]
DOCKER_INSPECT_FORMAT = (
    '{"containerId":{{json .Id}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"networkMode":{{json .HostConfig.NetworkMode}},'
    '"publishedPorts":{{json .NetworkSettings.Ports}}}'
)
DOCKER_NETWORK_INSPECT_FORMAT = (
    '{"containerId":{{json .Id}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"networkMode":{{json .HostConfig.NetworkMode}},'
    '"networks":{{json .NetworkSettings.Networks}},'
    '"publishedPorts":{{json .NetworkSettings.Ports}}}'
)
DOCKER_CONTEXT_ARGUMENTS = [
    "context",
    "inspect",
    DOCKER_CONTEXT,
    "--format",
    "{{json .Endpoints.docker.Host}}",
]
DOCKER_INFO_ARGUMENTS = [
    "info",
    "--format",
    '{"serverId":{{json .ID}},"osType":{{json .OSType}}}',
]


def docker_host_identity(
    *,
    endpoint: str = DOCKER_ENDPOINT,
    server_id: str = DOCKER_SERVER_ID,
    identity_sha256: str = DOCKER_HOST_IDENTITY_SHA256,
) -> dict[str, str]:
    return {
        "context": DOCKER_CONTEXT,
        "endpoint": endpoint,
        "serverId": server_id,
        "dockerHostIdentitySha256": identity_sha256,
    }


def canonical_json(document: object) -> bytes:
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


def _gateway_trust_spki(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _gateway_trust_key_id(private_key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(_gateway_trust_spki(private_key)).hexdigest()


def _gateway_trust_envelope(
    private_key: Ed25519PrivateKey,
    payload_type: str,
    payload: dict[str, object],
) -> bytes:
    signature_input = (
        GATEWAY_TRUST_SIGNATURE_DOMAIN
        + payload_type.encode("ascii")
        + b"\0"
        + canonical_json(payload)
    )
    return canonical_json(
        {
            "schemaVersion": 1,
            "payloadType": payload_type,
            "algorithm": "ed25519",
            "keyId": _gateway_trust_key_id(private_key),
            "payload": payload,
            "signatureBase64": base64.b64encode(private_key.sign(signature_input)).decode("ascii"),
        }
    )


def write_gateway_trust_pair(
    root: Path,
    contract,
    *,
    trusted_root: Path,
    candidate: dict[str, object],
    release_run: dict[str, str],
    runtime_path: Path,
) -> dict[str, object]:
    """Write signed Gateway inputs while keeping the keyring outside the candidate."""

    root = Path(root)
    trusted_root = Path(trusted_root)
    assert root.resolve() not in trusted_root.resolve().parents
    assert trusted_root.resolve() not in root.resolve().parents
    observer_key = Ed25519PrivateKey.generate()
    host_key = Ed25519PrivateKey.generate()
    keyring_path = trusted_root / "gateway-trust-keyring.json"
    keyring_path.parent.mkdir(parents=True, exist_ok=True)
    keyring_body = canonical_json(
        {
            "schemaVersion": 1,
            "keys": [
                {
                    "algorithm": "ed25519",
                    "keyId": _gateway_trust_key_id(observer_key),
                    "payloadTypes": ["gateway-observer"],
                    "publicKeySpkiBase64": base64.b64encode(
                        _gateway_trust_spki(observer_key)
                    ).decode("ascii"),
                },
                {
                    "algorithm": "ed25519",
                    "keyId": _gateway_trust_key_id(host_key),
                    "payloadTypes": ["gateway-host-provisioner"],
                    "publicKeySpkiBase64": base64.b64encode(_gateway_trust_spki(host_key)).decode(
                        "ascii"
                    ),
                },
            ],
        }
    )
    keyring_path.write_bytes(keyring_body)

    host_receipt = {
        "schemaVersion": 1,
        "producer": "gateway-docker-host-provisioner",
        "candidate": copy.deepcopy(candidate),
        "releaseRun": copy.deepcopy(release_run),
        "environmentId": release_run["environmentId"],
        "host": {
            "physicalHostIdSha256": DOCKER_HOST_IDENTITY_SHA256,
            "dockerContext": DOCKER_CONTEXT,
            "dockerEndpoint": DOCKER_ENDPOINT,
            "dockerServerId": DOCKER_SERVER_ID,
            "osType": DOCKER_OS_TYPE,
        },
    }
    host_receipt_body = canonical_json(host_receipt)
    host_receipt_sha256 = hashlib.sha256(host_receipt_body).hexdigest()
    host_receipt_path = root / GATEWAY_HOST_PROVISIONING_RECEIPT_ARTIFACT
    host_receipt_path.parent.mkdir(parents=True, exist_ok=True)
    host_receipt_path.write_bytes(host_receipt_body)

    runtime = json.loads(runtime_path.read_bytes())
    runtime["dockerHostIdentity"] = docker_host_identity(
        identity_sha256=host_receipt_sha256,
    )
    runtime_path.write_text(json.dumps(runtime, sort_keys=True), encoding="utf-8")
    runtime_sha256 = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
    report_path, observer_attestation_path, observer_attestation_sha256 = write_inputs(
        root,
        contract,
        candidate=candidate,
        release_run=release_run,
        runtime_sha256=runtime_sha256,
    )

    observer_payload = {
        "schemaVersion": 1,
        "issuer": "external-observer-authority",
        "issuedAt": GATEWAY_TRUST_ISSUED_AT,
        "expiresAt": GATEWAY_TRUST_EXPIRES_AT,
        "challenge": GATEWAY_OBSERVER_CHALLENGE,
        "candidate": copy.deepcopy(candidate),
        "releaseRun": copy.deepcopy(release_run),
        "environmentId": release_run["environmentId"],
        "claims": {
            "artifact": GATEWAY_OBSERVER_ATTESTATION_ARTIFACT,
            "artifactSha256": observer_attestation_sha256,
        },
    }
    host_payload = {
        "schemaVersion": 1,
        "issuer": "deployment-authority",
        "issuedAt": GATEWAY_TRUST_ISSUED_AT,
        "expiresAt": GATEWAY_TRUST_EXPIRES_AT,
        "challenge": GATEWAY_HOST_CHALLENGE,
        "candidate": copy.deepcopy(candidate),
        "releaseRun": copy.deepcopy(release_run),
        "environmentId": release_run["environmentId"],
        "claims": {
            "artifact": GATEWAY_HOST_PROVISIONING_RECEIPT_ARTIFACT,
            "artifactSha256": host_receipt_sha256,
        },
    }
    observer_envelope_body = _gateway_trust_envelope(
        observer_key,
        "gateway-observer",
        observer_payload,
    )
    host_envelope_body = _gateway_trust_envelope(
        host_key,
        "gateway-host-provisioner",
        host_payload,
    )
    observer_envelope_path = root / GATEWAY_OBSERVER_TRUST_ENVELOPE_ARTIFACT
    host_envelope_path = root / GATEWAY_HOST_TRUST_ENVELOPE_ARTIFACT
    observer_envelope_path.write_bytes(observer_envelope_body)
    host_envelope_path.write_bytes(host_envelope_body)

    return {
        "keyring_path": keyring_path,
        "keyring_sha256": hashlib.sha256(keyring_body).hexdigest(),
        "observer_challenge": GATEWAY_OBSERVER_CHALLENGE,
        "host_challenge": GATEWAY_HOST_CHALLENGE,
        "trusted_now": TRUSTED_NOW,
        "report_path": report_path,
        "observer_attestation_path": observer_attestation_path,
        "observer_attestation_sha256": observer_attestation_sha256,
        "observer_envelope_path": observer_envelope_path,
        "observer_envelope_sha256": hashlib.sha256(observer_envelope_body).hexdigest(),
        "host_envelope_path": host_envelope_path,
        "host_envelope_sha256": hashlib.sha256(host_envelope_body).hexdigest(),
        "host_receipt_path": host_receipt_path,
        "host_receipt": host_receipt,
        "host_receipt_sha256": host_receipt_sha256,
    }


def gateway_trust_arguments(trust_pair: dict[str, object]) -> dict[str, object]:
    return {
        "trusted_keyring_path": trust_pair["keyring_path"],
        "expected_trusted_keyring_sha256": trust_pair["keyring_sha256"],
        "expected_observer_challenge": trust_pair["observer_challenge"],
        "expected_host_challenge": trust_pair["host_challenge"],
        "trusted_now": trust_pair["trusted_now"],
    }


def gateway_trust_references(
    trust_pair: dict[str, object],
) -> dict[str, dict[str, str]]:
    return {
        "observerEnvelope": {
            "artifact": GATEWAY_OBSERVER_TRUST_ENVELOPE_ARTIFACT,
            "sha256": str(trust_pair["observer_envelope_sha256"]),
        },
        "hostProvisionerEnvelope": {
            "artifact": GATEWAY_HOST_TRUST_ENVELOPE_ARTIFACT,
            "sha256": str(trust_pair["host_envelope_sha256"]),
        },
        "hostProvisioningReceipt": {
            "artifact": GATEWAY_HOST_PROVISIONING_RECEIPT_ARTIFACT,
            "sha256": str(trust_pair["host_receipt_sha256"]),
        },
    }


class _ComposeLoader(yaml.SafeLoader):
    pass


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


def expected_service_networks(
    compose_path: Path,
    runtime: dict[str, object],
) -> dict[str, list[str]]:
    document = yaml.load(compose_path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    services = document.get("services") if isinstance(document, dict) else None
    containers = runtime.get("containers")
    assert isinstance(services, dict)
    assert isinstance(containers, list)
    expected: dict[str, list[str]] = {}
    for container in containers:
        assert isinstance(container, dict)
        service = container["service"]
        assert isinstance(service, str)
        definition = services.get(service)
        assert isinstance(definition, dict)
        raw_networks = definition.get("networks")
        if isinstance(raw_networks, dict):
            logical_names = list(raw_networks)
        else:
            assert isinstance(raw_networks, list)
            logical_names = raw_networks
        assert all(isinstance(name, str) and name for name in logical_names)
        expected[service] = [f"{DOCKER_PROJECT}_{name}" for name in logical_names]
    return expected


def _network_attachments(
    service: str,
    expected_networks: dict[str, list[str]],
    *,
    drift: str | None,
) -> dict[str, object]:
    names = list(expected_networks[service])
    if service == "gateway" and drift == "missing-network":
        names.pop()
    elif service == "gateway" and drift == "additional-network":
        names.append(f"{DOCKER_PROJECT}_attacker")
    return {name: {} for name in sorted(names)}


def canonical_inputs(
    contract,
    *,
    candidate: dict[str, object],
    release_run: dict[str, str],
    runtime_sha256: str,
) -> tuple[bytes, bytes, str]:
    observations: list[dict[str, object]] = []
    for port in contract.GATEWAY_ALLOWED_PORTS:
        tls = None
        if port == 443:
            tls = {
                "expectedHostname": "candidate.example.test",
                "hostnameVerified": True,
                "peerCertificateSha256": "5" * 64,
            }
        observations.append(
            {
                "port": port,
                "protocol": "tcp",
                "expected": "open",
                "addressObservations": [
                    {
                        "remoteIp": address,
                        "outcome": "open",
                        "error": None,
                        "tls": tls,
                    }
                    for address in PUBLIC_ADDRESSES
                ],
            }
        )
    observations.extend(
        {
            "port": port,
            "protocol": "tcp",
            "expected": "closed",
            "addressObservations": [
                {
                    "remoteIp": address,
                    "outcome": "closed",
                    "error": "connection-refused",
                    "tls": None,
                }
                for address in PUBLIC_ADDRESSES
            ],
        }
        for port in contract.GATEWAY_DENIED_PORTS
    )
    report = {
        "schemaVersion": contract.GATEWAY_PUBLIC_SCHEMA_VERSION,
        "producer": contract.GATEWAY_PUBLIC_PRODUCER,
        "candidate": candidate,
        "releaseRun": release_run,
        "observedAt": "2026-08-30T04:00:01Z",
        "baseUrl": BASE_URL,
        "target": {
            "hostname": "candidate.example.test",
            "resolvedAddresses": list(PUBLIC_ADDRESSES),
        },
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "observer": {"observerId": OBSERVER_ID, "origin": OBSERVER_ORIGIN},
        "policy": {
            "allowedTcpPorts": list(contract.GATEWAY_ALLOWED_PORTS),
            "deniedTcpPorts": list(contract.GATEWAY_DENIED_PORTS),
        },
        "observations": observations,
    }
    report_body = contract.canonical_gateway_public_document(report)
    report_sha256 = hashlib.sha256(report_body).hexdigest()
    attestation = {
        "schemaVersion": contract.GATEWAY_OBSERVER_ATTESTATION_SCHEMA_VERSION,
        "producer": contract.GATEWAY_OBSERVER_ATTESTATION_PRODUCER,
        "candidate": candidate,
        "releaseRun": release_run,
        "observedAt": "2026-08-30T04:00:02Z",
        "observer": {"observerId": OBSERVER_ID, "origin": OBSERVER_ORIGIN},
        "target": {
            "origin": BASE_URL,
            "expectedTlsHostname": "candidate.example.test",
            "resolvedAddresses": list(PUBLIC_ADDRESSES),
        },
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": runtime_sha256,
        },
        "externalObservation": {
            "artifact": "raw/gateway-public-observation.json",
            "sha256": report_sha256,
        },
        "execution": {
            "command": contract.gateway_public_command_record(),
            "nativeExit": 0,
            "stdoutSha256": report_sha256,
            "stderr": "",
            "stderrSha256": hashlib.sha256(b"").hexdigest(),
        },
    }
    attestation_body = contract.canonical_gateway_public_document(attestation)
    return report_body, attestation_body, hashlib.sha256(attestation_body).hexdigest()


def write_inputs(
    root: Path,
    contract,
    *,
    candidate: dict[str, object],
    release_run: dict[str, str],
    runtime_sha256: str,
) -> tuple[Path, Path, str]:
    report_body, attestation_body, attestation_sha256 = canonical_inputs(
        contract,
        candidate=candidate,
        release_run=release_run,
        runtime_sha256=runtime_sha256,
    )
    report_path = root / "raw" / "gateway-public-observation.json"
    attestation_path = root / "runtime" / "gateway-external-observer-attestation.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    attestation_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(report_body)
    attestation_path.write_bytes(attestation_body)
    return report_path, attestation_path, attestation_sha256


def _published_ports(service: str, *, drift: str | None = None) -> dict[str, object]:
    if service == "gateway":
        ports: dict[str, object] = {
            "80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}],
            "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
        }
        if drift == "gateway-extra-port":
            ports["8001/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "8001"}]
        return ports
    if service == "deeptutor" and drift == "internal-service-port":
        return {"8001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8001"}]}
    return {}


def _network_mode(
    service: str,
    *,
    drift: str | None = None,
    expected_networks: dict[str, list[str]] | None = None,
) -> str:
    if service == "deeptutor" and drift == "network-mode":
        return "host"
    if expected_networks is not None:
        return expected_networks[service][-1]
    return f"{DOCKER_PROJECT}_default"


def normalized_snapshot(
    runtime: dict[str, object],
    *,
    drift: str | None = None,
    compose_path: Path | None = None,
    network_drift: str | None = None,
) -> list[dict[str, object]]:
    containers = runtime["containers"]
    assert isinstance(containers, list)
    expected_networks = (
        expected_service_networks(compose_path, runtime) if compose_path is not None else None
    )
    rows: list[dict[str, object]] = []
    for container in sorted(containers, key=lambda item: str(item["containerId"])):
        service = str(container["service"])
        published: list[dict[str, object]] = []
        for target, bindings in _published_ports(service, drift=drift).items():
            target_port, protocol = target.split("/", 1)
            assert isinstance(bindings, list)
            for binding in bindings:
                published.append(
                    {
                        "containerPort": int(target_port),
                        "hostIp": binding["HostIp"],
                        "hostPort": int(binding["HostPort"]),
                        "protocol": protocol,
                    }
                )
        published.sort(
            key=lambda item: (
                item["containerPort"],
                item["hostIp"],
                item["hostPort"],
                item["protocol"],
            )
        )
        row = {
            "containerId": container["containerId"],
            "project": DOCKER_PROJECT,
            "service": service,
            "networkMode": _network_mode(
                service,
                drift=drift,
                expected_networks=expected_networks,
            ),
            "publishedPorts": published,
        }
        if expected_networks is not None:
            row["networks"] = sorted(
                _network_attachments(
                    service,
                    expected_networks,
                    drift=network_drift,
                )
            )
        rows.append(row)
    return rows


def docker_commands(
    runtime: dict[str, object],
    *,
    drift: str | None = None,
    compose_path: Path | None = None,
    network_drift: str | None = None,
    endpoint: str = DOCKER_ENDPOINT,
    server_id: str = DOCKER_SERVER_ID,
) -> list[dict[str, object]]:
    containers = runtime["containers"]
    assert isinstance(containers, list)
    ordered = sorted(containers, key=lambda item: str(item["containerId"]))
    ps_stdout = "\n".join(json.dumps(item["containerId"]) for item in ordered)
    expected_networks = (
        expected_service_networks(compose_path, runtime) if compose_path is not None else None
    )
    inspect_format = (
        DOCKER_NETWORK_INSPECT_FORMAT if expected_networks is not None else DOCKER_INSPECT_FORMAT
    )

    def record(arguments: list[str], stdout: str) -> dict[str, object]:
        return {
            "argv": [*DOCKER_LOGICAL_PREFIX, *arguments],
            "nativeExit": 0,
            "stdout": stdout,
            "stdoutSha256": hashlib.sha256(stdout.encode()).hexdigest(),
        }

    context_stdout = json.dumps(endpoint)
    info_stdout = json.dumps(
        {"osType": DOCKER_OS_TYPE, "serverId": server_id},
        separators=(",", ":"),
        sort_keys=True,
    )

    def observation_round(active_network_drift: str | None) -> list[dict[str, object]]:
        commands = [
            record(DOCKER_CONTEXT_ARGUMENTS, context_stdout),
            record(DOCKER_INFO_ARGUMENTS, info_stdout),
            record(DOCKER_PS_ARGUMENTS, ps_stdout),
        ]
        for container in ordered:
            service = str(container["service"])
            payload = {
                "containerId": container["containerId"],
                "project": DOCKER_PROJECT,
                "service": container["service"],
                "networkMode": _network_mode(
                    service,
                    drift=drift,
                    expected_networks=expected_networks,
                ),
                "publishedPorts": _published_ports(service, drift=drift),
            }
            if expected_networks is not None:
                payload["networks"] = _network_attachments(
                    service,
                    expected_networks,
                    drift=active_network_drift,
                )
            stdout = json.dumps(payload, separators=(",", ":"), sort_keys=True)
            commands.append(
                record(
                    [
                        "container",
                        "inspect",
                        "--format",
                        inspect_format,
                        str(container["containerId"]),
                    ],
                    stdout,
                )
            )
        commands.extend(
            [
                record(DOCKER_CONTEXT_ARGUMENTS, context_stdout),
                record(DOCKER_INFO_ARGUMENTS, info_stdout),
            ]
        )
        return commands

    return [
        *observation_round(network_drift),
        *observation_round(network_drift),
    ]


def proof_document(
    contract,
    *,
    root: Path,
    candidate: dict[str, object],
    release_run: dict[str, str],
    attestation_sha256: str,
    drift: str | None = None,
    compose_path: Path | None = None,
    network_drift: str | None = None,
    endpoint: str = DOCKER_ENDPOINT,
    server_id: str = DOCKER_SERVER_ID,
    docker_host_identity_sha256: str | None = DOCKER_HOST_IDENTITY_SHA256,
) -> dict[str, object]:
    if compose_path is None:
        compose_path = root / "docker-compose.platform.yml"
    runtime_path = root / "runtime" / "runtime-attestation.json"
    runtime_body = runtime_path.read_bytes()
    runtime = json.loads(runtime_body)
    report_path = root / "raw" / "gateway-public-observation.json"
    report = json.loads(report_path.read_bytes())
    before_snapshot = normalized_snapshot(
        runtime,
        drift=drift,
        compose_path=compose_path,
        network_drift=network_drift,
    )
    after_snapshot = normalized_snapshot(
        runtime,
        drift=drift,
        compose_path=compose_path,
        network_drift=network_drift,
    )
    return {
        "schemaVersion": 1,
        "candidate": candidate,
        "releaseRun": release_run,
        "observedAt": report["observedAt"],
        "baseUrl": BASE_URL,
        "runtimeAttestation": {
            "artifact": "runtime/runtime-attestation.json",
            "sha256": hashlib.sha256(runtime_body).hexdigest(),
        },
        "observerAttestation": {
            "artifact": "runtime/gateway-external-observer-attestation.json",
            "sha256": attestation_sha256,
        },
        "externalObservation": {
            "artifact": "raw/gateway-public-observation.json",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        },
        "docker": {
            "project": DOCKER_PROJECT,
            "daemon": {
                "context": DOCKER_CONTEXT,
                "endpoint": endpoint,
                "serverId": server_id,
                "osType": DOCKER_OS_TYPE,
                **(
                    {"dockerHostIdentitySha256": docker_host_identity_sha256}
                    if docker_host_identity_sha256 is not None
                    else {}
                ),
            },
            "beforeSnapshot": before_snapshot,
            "afterSnapshot": after_snapshot,
            "commands": docker_commands(
                runtime,
                drift=drift,
                compose_path=compose_path,
                network_drift=network_drift,
                endpoint=endpoint,
                server_id=server_id,
            ),
        },
        "summary": {
            "checks": {
                "gatewayPublic": True,
                "internalPortsClosed": drift is None,
            }
        },
    }


def docker_runner(
    runtime_path: Path,
    *,
    drift: str | None = None,
    compose_path: Path | None = None,
    network_drift: str | None = None,
    calls: list[list[str]] | None = None,
    environments: list[dict[str, str]] | None = None,
):
    if compose_path is None:
        compose_path = runtime_path.parent.parent / "docker-compose.platform.yml"
    runtime = json.loads(runtime_path.read_bytes())
    containers = runtime["containers"]
    assert isinstance(containers, list)
    by_id = {str(item["containerId"]): item for item in containers}
    ordered_ids = sorted(by_id)
    expected_networks = (
        expected_service_networks(compose_path, runtime) if compose_path is not None else None
    )
    command_count = 0

    def run(
        arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal command_count
        del cwd, timeout
        logical = arguments[5:]
        command_count += 1
        if calls is not None:
            calls.append([*arguments])
        if environments is not None:
            environments.append(dict(env))
        round_size = len(ordered_ids) + 5
        active_drift = drift if command_count > round_size else None
        active_network_drift = network_drift
        if logical == DOCKER_CONTEXT_ARGUMENTS:
            stdout = json.dumps(
                "npipe:////./pipe/attacker"
                if active_drift == "daemon-endpoint"
                else DOCKER_ENDPOINT
            )
        elif logical == DOCKER_INFO_ARGUMENTS:
            stdout = json.dumps(
                {
                    "osType": DOCKER_OS_TYPE,
                    "serverId": (
                        "daemon-attacker" if active_drift == "server-id" else DOCKER_SERVER_ID
                    ),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        elif logical == DOCKER_PS_ARGUMENTS:
            stdout = "\n".join(json.dumps(container_id) for container_id in ordered_ids)
        else:
            assert logical[:3] == ["container", "inspect", "--format"]
            inspect_format = logical[3]
            assert inspect_format in {
                DOCKER_INSPECT_FORMAT,
                DOCKER_NETWORK_INSPECT_FORMAT,
            }
            observed_networks = (
                expected_networks if inspect_format == DOCKER_NETWORK_INSPECT_FORMAT else None
            )
            container_id = logical[4]
            container = by_id[container_id]
            service = str(container["service"])
            payload = {
                "containerId": container_id,
                "project": DOCKER_PROJECT,
                "service": container["service"],
                "networkMode": _network_mode(
                    service,
                    drift=active_drift,
                    expected_networks=observed_networks,
                ),
                "publishedPorts": _published_ports(
                    service,
                    drift=active_drift,
                ),
            }
            if inspect_format == DOCKER_NETWORK_INSPECT_FORMAT:
                assert expected_networks is not None
                payload["networks"] = _network_attachments(
                    service,
                    expected_networks,
                    drift=active_network_drift,
                )
            stdout = json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            )
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout, stderr="")

    return run


def receipt_provenance(proof_path: Path) -> dict[str, object]:
    return {
        "gatewayOnlyPublicAttestation": {
            "artifact": "runtime/gateway-only-public-attestation.json",
            "sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest(),
        }
    }


def deep_set(document: dict[str, object], path: tuple[str, ...], value: Any) -> None:
    current: dict[str, object] = document
    for name in path[:-1]:
        nested = current[name]
        assert isinstance(nested, dict)
        current = nested
    current[path[-1]] = value
