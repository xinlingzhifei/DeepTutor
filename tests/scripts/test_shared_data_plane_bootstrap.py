from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_bootstrap():
    module_path = ROOT / "scripts" / "bootstrap_shared_data_plane.py"
    assert module_path.is_file(), "shared data-plane bootstrap has not been implemented"
    spec = importlib.util.spec_from_file_location(
        "bootstrap_shared_data_plane_under_test",
        module_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        sys.modules.pop(spec.name, None)


def test_shared_bootstrap_verifies_contract_before_persisting() -> None:
    module = _load_bootstrap()
    registration = module.SharedDataPlaneRegistration()
    calls: list[str] = []

    async def verify_health(candidate):
        assert candidate == registration
        calls.append("verify")
        return module.VerifiedDataPlaneHealth(
            service="openmaic",
            upstream_commit="0cf2a330411681190e89f48e20f305345ff99f87",
            app_version="0.3.1",
            contract_versions=("1.0",),
            capabilities=("outline", "content", "micro", "export", "cancel", "artifact-manifest"),
            export_formats=("classroom_zip", "pptx", "offline_html", "mp4"),
        )

    async def persist(candidate):
        assert candidate == registration
        calls.append("persist")

    asyncio.run(
        module.register_shared_data_plane(
            registration,
            verify_health=verify_health,
            persist=persist,
        )
    )

    assert calls == ["verify", "persist"]


def test_shared_bootstrap_rejects_incompatible_contract_without_persisting() -> None:
    module = _load_bootstrap()
    persisted = False

    async def verify_health(_registration):
        return module.VerifiedDataPlaneHealth(
            service="openmaic",
            upstream_commit="wrong",
            app_version="0.3.1",
            contract_versions=("1.0",),
            capabilities=("outline", "content", "micro", "export", "cancel", "artifact-manifest"),
            export_formats=("classroom_zip", "pptx", "offline_html", "mp4"),
        )

    async def persist(_registration):
        nonlocal persisted
        persisted = True

    with pytest.raises(ValueError, match="contract"):
        asyncio.run(
            module.register_shared_data_plane(
                module.SharedDataPlaneRegistration(),
                verify_health=verify_health,
                persist=persist,
            )
        )

    assert persisted is False
