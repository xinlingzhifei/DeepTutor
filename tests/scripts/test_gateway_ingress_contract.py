from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "gateway_ingress_contract_under_test", ROOT / "scripts" / "gateway_public_contract.py"
)
assert SPEC is not None and SPEC.loader is not None
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def _compose(*, shared: bool = True) -> dict:
    return {
        "services": {
            "gateway": {
                "ports": [
                    {
                        "target": target,
                        "published": str(published),
                        "host_ip": "127.0.0.1" if shared else "0.0.0.0",
                        "protocol": "tcp",
                    }
                    for target, published in (
                        ((80, 13780), (443, 13783)) if shared else ((80, 80), (443, 443))
                    )
                ]
            },
            "deeptutor": {"ports": []},
        }
    }


def _body(compose: dict) -> bytes:
    return yaml.safe_dump(compose).encode("utf-8")


def test_shared_candidate_ports_are_bound_to_loopback_without_host_overrides() -> None:
    expected = [
        {"containerPort": 80, "hostIp": "127.0.0.1", "hostPort": 13780, "protocol": "tcp"},
        {"containerPort": 443, "hostIp": "127.0.0.1", "hostPort": 13783, "protocol": "tcp"},
    ]
    assert contract.parse_gateway_candidate_ports(_body(_compose())) == expected


def test_historical_direct_candidate_ports_remain_verifiable() -> None:
    compose = _compose(shared=False)
    for port in compose["services"]["gateway"]["ports"]:
        port.pop("host_ip")
    assert contract.parse_gateway_candidate_ports(_body(compose)) == [
        {"containerPort": port, "hostIp": "0.0.0.0", "hostPort": port, "protocol": "tcp"}
        for port in (80, 443)
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("host_ip", "0.0.0.0"),
        ("host_ip", "::"),
        ("host_ip", "localhost"),
        ("published", "443"),
        ("published", "13780-13790"),
        ("target", 8001),
        ("target", True),
        ("protocol", "udp"),
        ("mode", "host"),
    ],
)
def test_gateway_candidate_rejects_unapproved_binding(key: str, value: object) -> None:
    compose = _compose()
    compose["services"]["gateway"]["ports"][0][key] = value
    with pytest.raises(ValueError, match="gateway candidate.*ports"):
        contract.parse_gateway_candidate_ports(_body(compose))


def test_gateway_candidate_rejects_non_gateway_publish_and_overlay_drift() -> None:
    compose = _compose()
    for service in ("gateway", "deeptutor"):
        overlay = {"services": {service: {"ports": ["8001:8001"]}}}
        with pytest.raises(ValueError, match="gateway candidate.*ports"):
            contract.parse_gateway_candidate_ports(_body(compose), overlay_bodies=(_body(overlay),))
    host_network = deepcopy(compose)
    host_network["services"]["deeptutor"]["network_mode"] = "host"
    with pytest.raises(ValueError, match="gateway candidate.*ports"):
        contract.parse_gateway_candidate_ports(_body(host_network))


def test_production_candidate_uses_the_approved_shared_host_bindings() -> None:
    assert contract.parse_gateway_candidate_ports(
        (ROOT / "docker-compose.platform.yml").read_bytes()
    ) == contract.parse_gateway_candidate_ports(_body(_compose()))
