from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import subprocess

import pytest

from tests.scripts import test_platform_preflight as support

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
OBSERVER = "openmaic-shared-ingress-observer"
PREFIX = "YFEISTAI_SHARED_INGRESS_"
FIELDS = (
    "YFEISTAI_ENVIRONMENT_ID",
    *(
        PREFIX + suffix
        for suffix in (
            "OBSERVER_ID",
            "OBSERVER_URL",
            "CONTROL_URL",
            "UPSTREAM_URL",
            "CANDIDATE_HEALTH_URL",
            "AUTH_EXPIRES_AT",
        )
    ),
)
ERROR = "observer deployment configuration"


def _case(tmp_path, monkeypatch, *, seconds=600, captured_environments=None):
    module = support._module()
    root = support._write_preflight_candidate_root(tmp_path)
    admission = support._stub_admitted_observer_for_compose_unit_probe()
    environment = support._observer_compose_environment(admission)
    environment[PREFIX + "AUTH_EXPIRES_AT"] = (
        (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    )
    admission["environment"].update({name: environment[name] for name in FIELDS})
    lock = json.loads((root / "deploy/image-lock.json").read_bytes())
    services = {
        name: {"image": lock["images"][image]["reference"]}
        for name, image in (
            ("gateway", "nginx"),
            ("deeptutor", "deeptutor"),
            ("minio", "minio"),
            ("minio-bootstrap", "minio_client"),
            ("openmaic", "openmaic"),
            ("openmaic-render", "openmaic_render"),
            ("pocketbase", "pocketbase"),
            ("postgres", "postgres"),
        )
    }
    services["gateway"]["ports"] = [{"published": "80"}, {"published": "443"}]
    for name in (OBSERVER, "openmaic-observer-attestation-signer"):
        services[name] = {"image": lock["supportImages"]["observer_signer"]["reference"]}
    services[OBSERVER]["environment"] = environment
    calls = []

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return NOW

    monkeypatch.setattr(module, "datetime", FixedClock)
    # Valid host values must never substitute for missing actual Compose values.
    for name in FIELDS:
        monkeypatch.setenv(name, environment[name])

    def runner(command, **_kwargs):
        calls.append(command)
        if captured_environments is not None:
            captured_environments.append(_kwargs["env"].copy())
        stdout = (
            "2.24.4\n"
            if command[-2:] == ["version", "--short"]
            else json.dumps(
                {
                    "services": services,
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def inspect():
        result = module._inspect_compose_runtime(
            root, candidate_root=root, observer_admission=admission, runner=runner
        )
        assert len(calls) == 2
        return result

    return environment, services, inspect


@pytest.mark.parametrize("field", FIELDS)
def test_compose_preflight_rejects_missing_observer_deployment_field(tmp_path, monkeypatch, field):
    environment, _services, inspect = _case(tmp_path, monkeypatch)
    environment.pop(field)
    assert inspect() == (ERROR,)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("YFEISTAI_ENVIRONMENT_ID", "bad/environment"),
        (PREFIX + "OBSERVER_ID", ""),
        (PREFIX + "OBSERVER_URL", "http://observer.example.test"),
        (PREFIX + "OBSERVER_URL", "https://user:must-not-echo@observer.example.test"),
        (PREFIX + "CONTROL_URL", "https://control.example.test/wrong"),
        (PREFIX + "UPSTREAM_URL", "https://upstream.example.test?secret=must-not-echo"),
        (PREFIX + "CANDIDATE_HEALTH_URL", "https://candidate.example.test/health"),
        (PREFIX + "UPSTREAM_URL", "https://OBSERVER.example.test.:443/"),
        (PREFIX + "INTERNAL_PROXY_URL", "http://observer.example.test:443"),
        (PREFIX + "OBSERVER_URL", "${UNRESOLVED_OBSERVER_URL}"),
        (PREFIX + "OBSERVER_ID", None),
        (PREFIX + "CONTROL_URL", 123),
        (PREFIX + "AUTH_EXPIRES_AT", "2026-09-06T12:00:00Z"),
        (PREFIX + "AUTH_EXPIRES_AT", "2026-09-06T12:00:29Z"),
        (PREFIX + "AUTH_EXPIRES_AT", "2026-09-06T14:00:01Z"),
        (PREFIX + "AUTH_EXPIRES_AT", "2026-09-06T12:10:00"),
        (PREFIX + "AUTH_EXPIRES_AT", "not-a-date"),
    ),
)
def test_compose_preflight_rejects_invalid_observer_deployment_value(
    tmp_path, monkeypatch, field, value
):
    environment, _services, inspect = _case(tmp_path, monkeypatch)
    environment[field] = value
    assert inspect() == (ERROR,)


@pytest.mark.parametrize(
    "field",
    (
        "YFEISTAI_RELEASE_RUN_ID",
        "YFEISTAI_CANDIDATE_SHA256",
        "YFEISTAI_IMAGE_LOCK_SHA256",
        "YFEISTAI_OBSERVER_PUBLIC_KEY_SPKI_SHA256",
    ),
)
def test_compose_preflight_binds_actual_observer_to_admitted_release(tmp_path, monkeypatch, field):
    environment, _services, inspect = _case(tmp_path, monkeypatch)
    environment[field] = "different-valid-id" if field.endswith("RUN_ID") else "e" * 64
    assert inspect() == (ERROR,)


@pytest.mark.parametrize("environment", (None, [], "not-a-mapping"))
def test_compose_preflight_rejects_missing_observer_environment(tmp_path, monkeypatch, environment):
    _environment, services, inspect = _case(tmp_path, monkeypatch)
    services[OBSERVER]["environment"] = environment
    assert inspect() == (ERROR,)


@pytest.mark.parametrize("seconds", (30, 600, 7200))
def test_compose_preflight_accepts_valid_interpolated_observer_environment(
    tmp_path, monkeypatch, seconds
):
    environment, _services, inspect = _case(tmp_path, monkeypatch, seconds=seconds)
    for name in FIELDS:
        monkeypatch.setenv(name, "invalid-host-value-not-used-as-config")
    assert inspect() == ()
