"""CLI-only discovery and runtime availability for interactive classrooms."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from typing import Any

import pytest

from deeptutor.app import facade as app_facade
from deeptutor.app.facade import DeepTutorApp, TurnRequest
from deeptutor.multi_user.context import reset_current_tenant, set_current_tenant
from deeptutor.services import config as config_service
from deeptutor.teaching.tenant_context import TenantContext

_SERVER_MODULES = {"asyncpg", "boto3", "fastapi", "sqlalchemy"}
_UNAVAILABLE_ERROR = (
    "Capability `interactive_classroom` is unavailable. "
    "Install server dependencies, enable the tenant platform, and select an active tenant."
)


class _Registry:
    def get_manifests(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "interactive_classroom",
                "cli_aliases": ["classroom"],
            }
        ]


class _Runtime:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []

    async def start_turn(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.start_calls.append(payload)
        return {"id": "session-1"}, {"id": "turn-1"}


class _Store:
    async def update_session_preferences(self, *_args: object) -> None:
        raise AssertionError("unavailable capability must not mutate the session")


def _app() -> DeepTutorApp:
    app = DeepTutorApp.__new__(DeepTutorApp)
    app.capabilities = _Registry()
    app.runtime = _Runtime()
    app.store = _Store()
    return app


def _tenant() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-a",
        schema_name="tenant_tenant_a",
        user_id="learner-a",
        permissions=frozenset(),
    )


def _set_server_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing: str | None = None,
) -> None:
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: object, **kwargs: object) -> object | None:
        if name == missing:
            return None
        if name in _SERVER_MODULES:
            return SimpleNamespace(name=name)
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(app_facade.importlib.util, "find_spec", find_spec)


def test_agents_public_base_agent_import_remains_compatible() -> None:
    from deeptutor.agents import BaseAgent
    from deeptutor.agents.base_agent import BaseAgent as ConcreteBaseAgent

    assert BaseAgent is ConcreteBaseAgent


@pytest.mark.parametrize(
    ("platform_enabled", "tenant_installed", "missing_dependency", "expected"),
    [
        (False, True, None, False),
        (True, False, None, False),
        (True, True, "fastapi", False),
        (True, True, None, True),
    ],
)
def test_interactive_classroom_availability_requires_server_platform_and_tenant(
    monkeypatch: pytest.MonkeyPatch,
    platform_enabled: bool,
    tenant_installed: bool,
    missing_dependency: str | None,
    expected: bool,
) -> None:
    _set_server_dependencies(monkeypatch, missing=missing_dependency)
    monkeypatch.setattr(
        config_service,
        "load_platform_settings",
        lambda: SimpleNamespace(enabled=platform_enabled),
    )
    token = set_current_tenant(_tenant()) if tenant_installed else None
    try:
        availability = _app().get_capability_availability("classroom")
    finally:
        if token is not None:
            reset_current_tenant(token)

    assert availability.available is expected


@pytest.mark.asyncio
async def test_start_turn_rejects_unavailable_classroom_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_server_dependencies(monkeypatch)
    monkeypatch.setattr(
        config_service,
        "load_platform_settings",
        lambda: SimpleNamespace(enabled=False),
    )
    app = _app()

    with pytest.raises(RuntimeError, match=f"^{re.escape(_UNAVAILABLE_ERROR)}$"):
        await app.start_turn(
            TurnRequest(
                content="Explain Fourier transform",
                capability="classroom",
                config={"mode": "micro", "course_id": "course-a"},
            )
        )

    assert app.runtime.start_calls == []


@pytest.mark.asyncio
async def test_start_turn_fails_closed_on_invalid_platform_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_server_dependencies(monkeypatch)

    def invalid_settings() -> object:
        raise ValueError("database_url contains secret diagnostics")

    monkeypatch.setattr(config_service, "load_platform_settings", invalid_settings)
    app = _app()

    with pytest.raises(RuntimeError, match=f"^{re.escape(_UNAVAILABLE_ERROR)}$"):
        await app.start_turn(
            TurnRequest(
                content="Explain Fourier transform",
                capability="classroom",
                config={"mode": "micro", "course_id": "course-a"},
            )
        )

    assert app.runtime.start_calls == []


def test_cli_only_package_discovers_classroom_contract_without_server_dependencies() -> None:
    source_root = Path(__file__).resolve().parents[2]
    cli_project = tomllib.loads(
        (source_root / "packaging" / "deeptutor-cli" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    dependencies = {
        dependency.split("[", 1)[0].split("<", 1)[0].split(">", 1)[0].split("=", 1)[0]
        for dependency in cli_project["project"]["dependencies"]
    }
    assert dependencies.isdisjoint(_SERVER_MODULES)

    script = r'''
import importlib.abc
import json
import sys

blocked = {"asyncpg", "boto3", "fastapi", "sqlalchemy"}

class BlockServerDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise ModuleNotFoundError(f"blocked CLI-only dependency: {fullname}")
        return None

sys.meta_path.insert(0, BlockServerDependencies())

from deeptutor.agents.interactive_classroom.capability import InteractiveClassroomCapability
from deeptutor.app.facade import DeepTutorApp
from deeptutor.runtime.registry.capability_registry import CapabilityRegistry

registry = CapabilityRegistry()
registry.load_builtins()
capability = registry.get("interactive_classroom")
app = DeepTutorApp.__new__(DeepTutorApp)
app.capabilities = registry
print(json.dumps({
    "loaded": isinstance(capability, InteractiveClassroomCapability),
    "alias": app.resolve_capability("classroom"),
    "required": sorted(capability.manifest.request_schema["required"]),
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=source_root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "loaded": True,
        "alias": "interactive_classroom",
        "required": ["course_id", "mode"],
    }
