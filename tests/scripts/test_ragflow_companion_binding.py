"""Local-only binding of independently validated platform and engine evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from tests.scripts import test_platform_compose as platform_support
from tests.scripts.test_ragflow_engine_release import PINNED_RECIPE, _release

ROOT = Path(__file__).resolve().parents[2]
engine = _release()


def _binding():
    path = ROOT / "scripts/ragflow_companion_binding.py"
    assert path.is_file(), "independent RAGFlow companion binding implementation is missing"
    spec = importlib.util.spec_from_file_location("ragflow_companion_binding", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


@pytest.fixture
def inputs(tmp_path):
    candidate = platform_support._write_candidate_with_promotion_receipt(tmp_path)
    recipe = tmp_path / "Dockerfile_ci"
    recipe.write_bytes(PINNED_RECIPE)
    prepared = tmp_path / "prepared"
    base = "infiniflow/ragflow-base:v2.1@sha256:" + "b" * 64
    engine.prepare_build_inputs(
        repository_root=ROOT, upstream_recipe=recipe, base_image=base, output_dir=prepared
    )
    manifest = b'{"schemaVersion":2}'
    tag = platform_support.RELEASE_TAG
    expected = {
        "repository_root": ROOT,
        "upstream_recipe": recipe,
        "base_image": base,
        "prepared_dir": prepared,
        "source_repository": platform_support.SOURCE_REPOSITORY,
        "source_head": platform_support.SOURCE_HEAD,
        "release_tag": tag,
        "run_id": "987654321",
        "run_attempt": 1,
        "image": f"{engine.IMAGE_REPOSITORY}:{tag}@{_digest(manifest)}",
        "build_digest": _digest(manifest),
        "remote_manifest": manifest,
    }
    return {
        "candidate_root": candidate,
        "expected_source_repository": platform_support.SOURCE_REPOSITORY,
        "expected_source_head": platform_support.SOURCE_HEAD,
        "expected_release_tag": tag,
        "expected_platform_run_id": platform_support.GITHUB_RUN_ID,
        "expected_platform_run_attempt": 1,
        "deployment_batch_id": "shared-host-20260907-01",
        "engine_receipt": engine.canonical_bytes(engine.create_receipt(**expected)),
        "engine_expected_inputs": expected,
    }


def test_binding_bytes_serializes_validated_snapshot_without_writes(inputs, tmp_path):
    module = _binding()
    assert callable(getattr(module, "binding_bytes", None)), "binding_bytes is not implemented"
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    body = module.binding_bytes(**inputs)
    assert isinstance(body, bytes)
    assert body == engine.canonical_bytes(module.create_binding(**inputs))
    assert module.validate_binding(body, **inputs) == json.loads(body)
    assert not hasattr(module, "write_binding"), (
        "companion helper must not publish filesystem paths"
    )
    assert {
        str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before


def test_binding_hashes_validated_evidence_without_modifying_platform(inputs):
    module = _binding()
    root = inputs["candidate_root"]
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    document = module.create_binding(**inputs)
    assert document == {
        "schemaVersion": 1,
        "kind": "ragflow-platform-companion-binding",
        "validationScope": "local-contract-only",
        "deploymentBatchId": inputs["deployment_batch_id"],
        "sourceRepository": inputs["expected_source_repository"],
        "sourceHead": inputs["expected_source_head"],
        "releaseTag": inputs["expected_release_tag"],
        "platform": {
            "imageLockSha256": _digest((root / "deploy/image-lock.json").read_bytes()),
            "promotionReceiptSha256": _digest(
                (root / "artifacts/promotion_receipt.json").read_bytes()
            ),
            "runId": inputs["expected_platform_run_id"],
            "runAttempt": 1,
        },
        "engine": {
            "receiptSha256": _digest(inputs["engine_receipt"]),
            "image": inputs["engine_expected_inputs"]["image"],
            "runId": inputs["engine_expected_inputs"]["run_id"],
            "runAttempt": 1,
        },
    }
    body = engine.canonical_bytes(document)
    assert module.validate_binding(body, **inputs) == document
    assert {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(
    "batch", ["", "../batch", "x/y", "x\\y", "a b", "a\n", "-batch", "a" * 129, None]
)
def test_rejects_unsafe_batch_id(inputs, batch):
    inputs["deployment_batch_id"] = batch
    with pytest.raises(ValueError):
        _binding().create_binding(**inputs)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("expected_source_repository", "other/repository"),
        ("expected_source_head", "b" * 40),
        ("expected_source_head", "0" * 40),
        ("expected_release_tag", "yfeistai-first-release-20260906-aaaaaaaa"),
        ("expected_platform_run_id", "12"),
        ("expected_platform_run_id", "0"),
        ("expected_platform_run_attempt", 2),
        ("expected_platform_run_attempt", True),
    ],
)
def test_rejects_wrong_trusted_platform_identity(inputs, key, value):
    inputs[key] = value
    with pytest.raises(ValueError):
        _binding().create_binding(**inputs)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source_repository", "other/repository"),
        ("source_head", "b" * 40),
        ("release_tag", "yfeistai-first-release-20260906-aaaaaaaa"),
        ("run_id", "12"),
        ("run_attempt", 2),
        ("run_attempt", True),
        ("base_image", "infiniflow/ragflow-base:v2.1@sha256:" + "0" * 64),
        ("build_digest", "sha256:" + "0" * 64),
        ("build_digest", "not-a-digest"),
        ("remote_manifest", b"changed"),
    ],
)
def test_engine_receipt_never_supplies_its_own_expected_identity(inputs, key, value):
    inputs["engine_expected_inputs"][key] = value
    with pytest.raises(ValueError):
        _binding().create_binding(**inputs)


@pytest.mark.parametrize(
    "member",
    [
        "deploy/image-lock.json",
        "artifacts/promotion_intent.json",
        "artifacts/promotion_receipt.json",
    ],
)
@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_rejects_missing_or_tampered_platform_evidence(inputs, member, damage):
    path = inputs["candidate_root"] / member
    if damage == "missing":
        path.rename(path.with_suffix(".held"))
    else:
        path.write_bytes(b"{}\n")
    with pytest.raises(ValueError):
        _binding().create_binding(**inputs)


@pytest.mark.parametrize("body", [b"", b"{}\n", b"null\n", b"[]\n"])
def test_rejects_missing_or_tampered_engine_receipt(inputs, body):
    inputs["engine_receipt"] = body
    with pytest.raises(ValueError):
        _binding().create_binding(**inputs)


@pytest.mark.parametrize(
    "damage", ["unknown-field", "zero-digest", "noncanonical", "wrong-type", "batch-replay"]
)
def test_binding_validation_rejects_changes_and_cross_batch_replay(inputs, damage):
    module = _binding()
    document = module.create_binding(**inputs)
    if damage == "unknown-field":
        document["extra"] = True
    elif damage == "zero-digest":
        document["engine"]["receiptSha256"] = "sha256:" + "0" * 64
    elif damage == "wrong-type":
        document["schemaVersion"] = True
    elif damage == "batch-replay":
        inputs["deployment_batch_id"] = "shared-host-20260907-02"
    body = engine.canonical_bytes(document)
    if damage == "noncanonical":
        body = json.dumps(document, indent=2).encode()
    with pytest.raises(ValueError):
        module.validate_binding(body, **inputs)


@pytest.mark.parametrize(
    "member",
    [
        "deploy/image-lock.json",
        "artifacts/promotion_intent.json",
        "artifacts/promotion_receipt.json",
    ],
)
def test_source_change_during_engine_validation_rejects(inputs, monkeypatch, member):
    module = _binding()
    original = module.engine.validate_receipt

    def change_source(body, **expected):
        result = original(body, **expected)
        path = inputs["candidate_root"] / member
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(module.engine, "validate_receipt", change_source)
    with pytest.raises(ValueError):
        module.create_binding(**inputs)


@pytest.mark.parametrize(
    "member",
    [
        "deploy/image-lock.json",
        "docker-compose.yml",
        "docker-compose.platform.yml",
        "docker-compose.data-plane.yml",
        "deploy/docker-compose.openmaic-shared-ingress-observer.yml",
        "artifacts/promotion_intent.json",
        "artifacts/promotion_receipt.json",
    ],
)
def test_source_and_receipt_leases_remain_open_through_serialization(inputs, monkeypatch, member):
    module = _binding()
    original = module.canonical_bytes

    def change_after_validation(document):
        body = original(document)
        if document.get("kind") == "ragflow-platform-companion-binding":
            path = inputs["candidate_root"] / member
            path.write_bytes(path.read_bytes() + b" ")
        return body

    monkeypatch.setattr(module, "canonical_bytes", change_after_validation)
    with pytest.raises(ValueError):
        module.binding_bytes(**inputs)
