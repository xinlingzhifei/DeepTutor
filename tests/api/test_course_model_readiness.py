from types import SimpleNamespace

import pytest

from deeptutor.api.routers import classroom_jobs, settings, teaching_catalog
from deeptutor.multi_user import context as user_context
from deeptutor.teaching.openmaic import data_planes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "dedicated", "missing", "disabled"])
async def test_course_readiness_uses_current_tenant_route(monkeypatch, mode):
    calls = []

    class Selector:
        def __init__(self, **kwargs):
            assert kwargs["audit_decisions"] is False

        async def resolve(self, tenant_id):
            calls.append(tenant_id)
            if mode == "missing":
                raise data_planes.DataPlaneUnavailable()
            if mode == "disabled":
                return None
            return SimpleNamespace(mode=mode)

    async def shared_status():
        calls.append("shared-status")
        return {"state": "not_configured", "ready": False, "can_configure": True}

    monkeypatch.setattr(data_planes, "DataPlaneSelector", Selector)
    monkeypatch.setattr(teaching_catalog, "load_platform_settings", lambda: object())
    monkeypatch.setattr(classroom_jobs, "get_data_plane_repository", lambda: object())
    monkeypatch.setattr(settings, "get_openmaic_status", shared_status)
    monkeypatch.setattr(user_context, "get_current_user", lambda: SimpleNamespace(is_admin=True))

    response = await teaching_catalog.course_model_readiness(SimpleNamespace(tenant_id="tenant-a"))
    assert calls[0] == "tenant-a"
    assert response["ready"] is (mode == "dedicated")
    assert ("shared-status" in calls) is (mode == "shared")
    assert (
        response["state"]
        == {
            "shared": "not_configured",
            "dedicated": "synced",
            "missing": "error",
            "disabled": "not_enabled",
        }[mode]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [True, False])
async def test_openmaic_status_only_exposes_model_details_to_admin(monkeypatch, is_admin):
    from deeptutor.teaching.openmaic import catalog_sync

    async def status(_service):
        return {"state": "synced", "ready": True, "model": "private-model", "provider": "openai"}

    monkeypatch.setattr(catalog_sync, "shared_model_status", status)
    monkeypatch.setattr(
        settings, "get_current_user", lambda: SimpleNamespace(is_admin=is_admin)
    )
    monkeypatch.setattr(settings, "get_model_catalog_service", lambda: object())
    result = await settings.get_openmaic_status()
    assert result["ready"] is True
    assert result["can_configure"] is is_admin
    assert ("model" in result) is is_admin
    assert ("provider" in result) is is_admin


@pytest.mark.asyncio
async def test_resync_requires_admin_and_uses_saved_catalog(monkeypatch):
    from fastapi import HTTPException

    from deeptutor.teaching.openmaic import catalog_sync

    saved = {"services": {"llm": {"profiles": []}}}
    service = SimpleNamespace(load=lambda: saved)
    calls = []

    async def sync(actual):
        calls.append(actual.load())

    async def status(_service):
        return {"state": "not_configured", "ready": False}

    monkeypatch.setattr(catalog_sync, "synchronize_shared_catalog", sync)
    monkeypatch.setattr(catalog_sync, "shared_model_status", status)
    monkeypatch.setattr(settings, "get_model_catalog_service", lambda: service)
    monkeypatch.setattr(
        settings, "get_current_user", lambda: SimpleNamespace(is_admin=False)
    )
    with pytest.raises(HTTPException) as denied:
        await settings.resync_openmaic()
    assert denied.value.status_code == 403
    assert calls == []
    monkeypatch.setattr(settings, "get_current_user", lambda: SimpleNamespace(is_admin=True))
    assert (await settings.resync_openmaic())["ready"] is False
    assert calls == [service.load()]
