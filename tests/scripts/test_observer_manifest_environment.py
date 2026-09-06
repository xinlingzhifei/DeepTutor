from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from tests.scripts import test_docker_compose as launcher_support
from tests.scripts import test_observer_preflight_environment as preflight_support
from tests.scripts import test_provision_openmaic_observer_secrets as support


def _freeze(module, monkeypatch, current):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return current

    monkeypatch.setattr(module, "datetime", FixedClock, raising=False)


@pytest.mark.parametrize("field", tuple(support.DEPLOYMENT_INPUTS))
def test_provisioning_requires_each_explicit_cli_input(tmp_path, monkeypatch, field):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    arguments = support._cli_arguments(roots)
    index = arguments.index("--" + field.replace("_", "-"))
    arguments[index : index + 2] = []
    for name, value in support._deployment_environment().items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SystemExit) as failure:
        module.main(arguments)
    assert failure.value.code == 2
    assert all(not (root / support.RUN_ID).exists() for root in roots[1:])


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("environment_id", "bad/environment"),
        ("observer_id", ""),
        ("observer_url", "https://user:must-not-echo@observer.example.test"),
        ("control_url", "https://control.example.test/wrong"),
        ("upstream_url", "https://OBSERVER.example.test.:443/"),
        ("candidate_health_url", "https://candidate.example.test/health"),
        ("auth_expires_at", "2026-09-06T12:00:29Z"),
        ("auth_expires_at", "2026-09-06T14:00:01Z"),
        ("auth_expires_at", "2026-09-06T12:10:00"),
        ("auth_expires_at", "not-a-date"),
    ),
)
def test_invalid_deployment_input_is_refused_before_material_generation(
    tmp_path, monkeypatch, field, value
):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    _freeze(module, monkeypatch, preflight_support.NOW)
    inputs = {"auth_expires_at": "2026-09-06T12:10:00Z", field: value}
    generated = []
    monkeypatch.setattr(module, "_new_material", lambda: generated.append("material"))

    with pytest.raises(ValueError, match="observer deployment configuration") as failure:
        support._provision(module, roots, **inputs)
    assert "must-not-echo" not in str(failure.value)
    assert generated == []
    assert all(not (root / support.RUN_ID).exists() for root in roots[1:])


@pytest.mark.parametrize("seconds", (30, 600, 7200))
def test_provisioning_accepts_exact_startup_expiry_window(tmp_path, monkeypatch, seconds):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    _freeze(module, monkeypatch, preflight_support.NOW)
    expiry = (preflight_support.NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
    result = support._provision(module, roots, auth_expires_at=expiry)
    assert result["environment"]["YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT"] == expiry


def test_expired_batch_is_not_renewed_by_host_override_or_same_run_retry(tmp_path, monkeypatch):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    _freeze(module, monkeypatch, preflight_support.NOW)
    support._provision(module, roots, auth_expires_at="2026-09-06T12:10:00Z")
    before = {
        path: path.read_bytes() for root in roots[1:] for path in root.rglob("*") if path.is_file()
    }
    _freeze(module, monkeypatch, preflight_support.NOW + timedelta(minutes=11))
    monkeypatch.setenv("YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT", "2026-09-06T12:30:00Z")

    with pytest.raises(ValueError, match="observer deployment configuration"):
        module.load_committed_observer_release_manifest(
            manifest_path=support._manifest_path(roots[2]),
            candidate_root=roots[0],
            observer_signer_image=support.SIGNER_IMAGE,
        )
    arguments = support._cli_arguments(roots)
    arguments[arguments.index("--auth-expires-at") + 1] = "2026-09-06T12:30:00Z"
    with pytest.raises(ValueError, match="observer release batch is committed"):
        module.main(arguments)
    assert {path: path.read_bytes() for path in before} == before
    arguments[arguments.index("--release-run-id") + 1] = support.RUN_ID + "-02"
    assert module.main(arguments) == 0
    next_manifest = json.loads(
        (roots[2] / (support.RUN_ID + "-02") / support.MANIFEST_FILE).read_bytes()
    )
    assert next_manifest["releaseRunId"] == support.RUN_ID + "-02"
    assert next_manifest["deploymentEnvironment"]["YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT"] == (
        "2026-09-06T12:30:00Z"
    )
    assert {path: path.read_bytes() for path in before} == before


def test_expired_uncommitted_batch_can_still_be_recovered(tmp_path, monkeypatch):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    support._write_recovery_batch(roots, status="uncommitted", owner_markers=True)
    _freeze(module, monkeypatch, datetime.now(UTC) + timedelta(days=1))
    recovered = support._recover(module, roots)
    assert set(recovered) == {"secret", "trust", "attestation"}
    assert all(not (root / support.RUN_ID).exists() for root in roots[1:])


def test_launcher_rechecks_absolute_expiry_before_subprocess(tmp_path, monkeypatch):
    module = launcher_support._load_module()
    validate_lock = module._validate_production_image_lock
    subprocess_calls = launcher_support._isolate_production_wrapper_subprocess(
        module, monkeypatch, platform=False
    )
    monkeypatch.setattr(module, "_validate_production_image_lock", validate_lock)
    candidate, environment = launcher_support._configure_real_committed_observer_release(
        module, tmp_path, monkeypatch
    )
    expiry = datetime.fromisoformat(
        environment["YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT"].replace("Z", "+00:00")
    )
    provisioner = module._load_neighbor_script("provision_openmaic_observer_secrets.py")
    load_admission = module._load_observer_release_admission

    def expire_after_admission(**kwargs):
        admission = load_admission(**kwargs)
        _freeze(provisioner, monkeypatch, expiry - timedelta(seconds=29))
        return admission

    monkeypatch.setattr(module, "_load_observer_release_admission", expire_after_admission)
    with pytest.raises(SystemExit, match="observer release manifest changed"):
        module.main(["--platform", "--candidate-root", str(candidate), "config"])
    assert subprocess_calls == []


@pytest.mark.parametrize("field", preflight_support.FIELDS)
def test_committed_manifest_refuses_missing_deployment_binding(tmp_path, monkeypatch, field):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    support._provision(module, roots)
    path = support._manifest_path(roots[2])
    manifest = json.loads(path.read_bytes())
    manifest["deploymentEnvironment"].pop(field)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="observer deployment configuration"):
        module.load_committed_observer_release_manifest(
            manifest_path=path,
            candidate_root=roots[0],
            observer_signer_image=support.SIGNER_IMAGE,
        )


def test_host_preflight_clears_all_ambient_deployment_inputs(tmp_path, monkeypatch):
    captured = []
    environment, _services, inspect = preflight_support._case(
        tmp_path, monkeypatch, captured_environments=captured
    )
    for name in preflight_support.FIELDS:
        monkeypatch.setenv(name, "unadmitted-host-override")

    assert inspect() == ()
    assert len(captured) == 2
    for actual in captured:
        assert {name: actual[name] for name in preflight_support.FIELDS} == {
            name: environment[name] for name in preflight_support.FIELDS
        }


def test_committed_manifest_versions_and_binds_all_deployment_inputs(tmp_path, monkeypatch):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    result = support._provision(module, roots)
    manifest = json.loads(support._manifest_path(roots[2]).read_bytes())

    assert manifest["schemaVersion"] == 2
    assert set(manifest["deploymentEnvironment"]) == set(preflight_support.FIELDS)
    assert {name: result["environment"][name] for name in preflight_support.FIELDS} == manifest[
        "deploymentEnvironment"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("YFEISTAI_ENVIRONMENT_ID", "another-environment"),
        ("YFEISTAI_SHARED_INGRESS_OBSERVER_ID", "another-observer"),
        ("YFEISTAI_SHARED_INGRESS_OBSERVER_URL", "https://another-observer.example.test"),
        (
            "YFEISTAI_SHARED_INGRESS_CONTROL_URL",
            "https://another-control.example.test/v1/control-canaries",
        ),
        ("YFEISTAI_SHARED_INGRESS_UPSTREAM_URL", "https://another-upstream.example.test"),
        (
            "YFEISTAI_SHARED_INGRESS_CANDIDATE_HEALTH_URL",
            "https://another-candidate.example.test/api/v1/system/teaching-health/observer-canary",
        ),
        ("YFEISTAI_SHARED_INGRESS_AUTH_EXPIRES_AT", "2026-09-06T12:20:00Z"),
    ),
)
def test_preflight_rejects_valid_but_unadmitted_deployment_input(
    tmp_path, monkeypatch, field, value
):
    environment, _services, inspect = preflight_support._case(tmp_path, monkeypatch)
    environment[field] = value
    assert inspect() == (preflight_support.ERROR,)


def test_committed_legacy_schema_is_refused(tmp_path, monkeypatch):
    module = support._provisioner()
    roots = support._roots(tmp_path)
    support._install_metadata_seam(module, monkeypatch, roots)
    support._provision(module, roots)
    manifest_path = support._manifest_path(roots[2])
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schemaVersion"] = 1
    manifest.pop("deploymentEnvironment", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="observer release manifest"):
        module.load_committed_observer_release_manifest(
            manifest_path=manifest_path,
            candidate_root=roots[0],
            observer_signer_image=support.SIGNER_IMAGE,
        )
