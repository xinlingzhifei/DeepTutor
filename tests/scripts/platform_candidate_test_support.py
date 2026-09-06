from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POCKETBASE_BASE_COMPOSE_DRIFTS = ("tag-only", "different-digest")


def _renderer():
    path = ROOT / "scripts" / "render_platform_compose.py"
    spec = importlib.util.spec_from_file_location(
        "render_platform_compose_for_candidate_consumer_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def write_schema3_candidate_root(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    renderer = _renderer()
    candidate_root = tmp_path / "candidate"
    deploy = candidate_root / "deploy"
    deploy.mkdir(parents=True)
    compose_paths = (
        candidate_root / "docker-compose.yml",
        candidate_root / "docker-compose.platform.yml",
        candidate_root / "docker-compose.data-plane.yml",
        deploy / "docker-compose.openmaic-shared-ingress-observer.yml",
    )
    for source, destination in zip(
        (
            ROOT / "docker-compose.yml",
            ROOT / "docker-compose.platform.yml",
            ROOT / "docker-compose.data-plane.yml",
            ROOT / "deploy" / "docker-compose.openmaic-shared-ingress-observer.yml",
        ),
        compose_paths,
        strict=True,
    ):
        destination.write_bytes(source.read_bytes())

    digest_index = 0

    def resolve_digest(_reference: str) -> str:
        nonlocal digest_index
        digest_index += 1
        return "sha256:" + f"{digest_index:064x}"

    source_head = "a" * 40
    lock = renderer.write_image_lock(
        deploy / "image-lock.json",
        digest_resolver=resolve_digest,
        compose_paths=compose_paths,
        source_repository="xinlingzhifei/DeepTutor",
        source_head=source_head,
        release_tag=f"yfeistai-first-release-20260904-{source_head[:8]}",
        openmaic_head="0cf2a330411681190e89f48e20f305345ff99f87",
    )
    assert lock["schemaVersion"] == 3
    pocketbase = lock["images"]["pocketbase"]
    assert pocketbase["digest"] != "sha256:" + "0" * 64
    assert pocketbase["reference"].endswith(f"@{pocketbase['digest']}")
    return candidate_root, lock


def drift_pocketbase_base_compose(
    candidate_root: Path,
    lock: dict[str, Any],
    drift: str,
) -> None:
    pocketbase = lock["images"]["pocketbase"]
    reference = pocketbase["reference"]
    if drift == "tag-only":
        replacement = f"{pocketbase['repository']}:{pocketbase['tag']}"
    elif drift == "different-digest":
        replacement = f"{pocketbase['repository']}:{pocketbase['tag']}@sha256:" + "f" * 64
    else:
        raise AssertionError(f"unsupported PocketBase drift: {drift}")
    compose_path = candidate_root / "docker-compose.yml"
    original = compose_path.read_text(encoding="utf-8")
    assert original.count(reference) == 1
    compose_path.write_text(
        original.replace(reference, replacement, 1),
        encoding="utf-8",
    )
    assert (
        json.loads((candidate_root / "deploy" / "image-lock.json").read_text(encoding="utf-8"))[
            "images"
        ]["pocketbase"]
        == pocketbase
    )
