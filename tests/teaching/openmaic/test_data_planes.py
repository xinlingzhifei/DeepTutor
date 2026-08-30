from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from deeptutor.teaching.openmaic.data_planes import (
    ROUTE_BINDING_CONFIG_REVISION,
    DataPlaneDecision,
    DataPlaneResolution,
    DataPlaneRouteRecord,
    DataPlaneSelection,
    DataPlaneSelector,
    DataPlaneUnavailable,
    ProviderProfileRecord,
    provider_config_digest,
    route_config_digest,
)


def _shared_resolution(
    tenant_id: str,
    *,
    tenant_mode: str = "shared",
) -> DataPlaneResolution:
    return DataPlaneResolution(
        tenant_id=tenant_id,
        tenant_mode=tenant_mode,
        route=DataPlaneRouteRecord(
            route_id="shared-primary",
            tenant_id=None,
            owner_key="shared",
            mode="shared",
            base_url="http://openmaic-shared:3000",
            worker_pool="shared-generation",
            queue_name="openmaic.shared",
            provider_profile_id="platform-default",
            status="active",
            health_status="healthy",
        ),
        provider_profile=ProviderProfileRecord(
            profile_id="platform-default",
            scope="shared",
            tenant_id=None,
            owner_key="shared",
            provider_type="openai-compatible",
            model_name="platform-model",
            api_base_url=None,
            secret_ref="shared/providers/platform-default",
            status="active",
        ),
    )


def _dedicated_resolution(tenant_id: str) -> DataPlaneResolution:
    return DataPlaneResolution(
        tenant_id=tenant_id,
        tenant_mode="dedicated",
        route=DataPlaneRouteRecord(
            route_id=f"dedicated-{tenant_id}",
            tenant_id=tenant_id,
            owner_key=tenant_id,
            mode="dedicated",
            base_url=f"http://openmaic-{tenant_id}:3000",
            worker_pool=f"generation-{tenant_id}",
            queue_name=f"openmaic.{tenant_id}",
            provider_profile_id=f"provider-{tenant_id}",
            status="active",
            health_status="healthy",
        ),
        provider_profile=ProviderProfileRecord(
            profile_id=f"provider-{tenant_id}",
            scope="dedicated",
            tenant_id=tenant_id,
            owner_key=tenant_id,
            provider_type="openai-compatible",
            model_name="private-model",
            api_base_url=None,
            secret_ref=f"tenants/{tenant_id}/providers/provider-{tenant_id}",
            status="active",
        ),
    )


class RecordingRepository:
    def __init__(self, resolution: DataPlaneResolution | None) -> None:
        self.resolution = resolution
        self.calls: list[str] = []
        self.decisions: list[DataPlaneDecision] = []

    async def resolve(self, tenant_id: str):
        self.calls.append(tenant_id)
        return self.resolution

    async def record_decision(self, decision: DataPlaneDecision) -> None:
        self.decisions.append(decision)


def test_standard_tenant_resolves_healthy_shared_data_plane() -> None:
    resolution = _shared_resolution("tenant-standard")
    assert resolution.route is not None
    assert resolution.provider_profile is not None
    repository = RecordingRepository(resolution)
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=True),
        repository=repository,
    )

    assert asyncio.run(selector.resolve("tenant-standard")) == DataPlaneSelection(
        tenant_id="tenant-standard",
        route_ref="shared-primary",
        provider_profile_ref="platform-default",
        mode="shared",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
        config_revision=ROUTE_BINDING_CONFIG_REVISION,
        route_config_digest=route_config_digest(resolution.route),
        provider_config_digest=provider_config_digest(resolution.provider_profile),
    )
    assert repository.decisions == [
        DataPlaneDecision(
            tenant_id="tenant-standard",
            route_ref="shared-primary",
            provider_profile_ref="platform-default",
            mode="shared",
            decision="selected",
            reason_code="healthy",
        )
    ]
    assert "openmaic-shared" not in repr(repository.decisions)
    assert "shared/providers" not in repr(repository.decisions)


def test_dedicated_tenant_never_falls_back_to_healthy_shared_plane() -> None:
    repository = RecordingRepository(
        _shared_resolution(
            "tenant-private",
            tenant_mode="dedicated",
        )
    )
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=True),
        repository=repository,
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(selector.resolve("tenant-private"))

    assert repository.decisions == [
        DataPlaneDecision(
            tenant_id="tenant-private",
            route_ref="shared-primary",
            provider_profile_ref="platform-default",
            mode="dedicated",
            decision="unavailable",
            reason_code="route_mode_mismatch",
        )
    ]


@pytest.mark.parametrize(
    ("status", "health_status", "reason_code"),
    [
        ("disabled", "healthy", "route_inactive"),
        ("active", "unknown", "route_unhealthy"),
        ("active", "unhealthy", "route_unhealthy"),
    ],
)
def test_route_must_be_active_and_healthy(
    status: str,
    health_status: str,
    reason_code: str,
) -> None:
    resolution = _dedicated_resolution("tenant-private")
    resolution = replace(
        resolution,
        route=replace(
            resolution.route,
            status=status,
            health_status=health_status,
        ),
    )
    repository = RecordingRepository(resolution)
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=True),
        repository=repository,
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(selector.resolve("tenant-private"))

    assert repository.decisions == [
        DataPlaneDecision(
            tenant_id="tenant-private",
            route_ref="dedicated-tenant-private",
            provider_profile_ref="provider-tenant-private",
            mode="dedicated",
            decision="unavailable",
            reason_code=reason_code,
        )
    ]


@pytest.mark.parametrize(
    ("tamper", "reason_code"),
    [
        ("resolution_tenant", "tenant_mismatch"),
        ("route_tenant", "route_owner_mismatch"),
        ("route_owner", "route_owner_mismatch"),
        ("profile_id", "provider_binding_mismatch"),
        ("profile_scope", "provider_binding_mismatch"),
        ("profile_tenant", "provider_binding_mismatch"),
        ("profile_owner", "provider_binding_mismatch"),
        ("profile_inactive", "provider_inactive"),
    ],
)
def test_selector_rejects_cross_owner_route_and_provider_bindings(
    tamper: str,
    reason_code: str,
) -> None:
    resolution = _dedicated_resolution("tenant-private")
    if tamper == "resolution_tenant":
        resolution = replace(resolution, tenant_id="tenant-foreign")
    elif tamper == "route_tenant":
        resolution = replace(
            resolution,
            route=replace(resolution.route, tenant_id="tenant-foreign"),
        )
    elif tamper == "route_owner":
        resolution = replace(
            resolution,
            route=replace(resolution.route, owner_key="tenant-foreign"),
        )
    elif tamper == "profile_id":
        resolution = replace(
            resolution,
            provider_profile=replace(
                resolution.provider_profile,
                profile_id="provider-foreign",
            ),
        )
    elif tamper == "profile_scope":
        resolution = replace(
            resolution,
            provider_profile=replace(
                resolution.provider_profile,
                scope="shared",
            ),
        )
    elif tamper == "profile_tenant":
        resolution = replace(
            resolution,
            provider_profile=replace(
                resolution.provider_profile,
                tenant_id="tenant-foreign",
            ),
        )
    elif tamper == "profile_owner":
        resolution = replace(
            resolution,
            provider_profile=replace(
                resolution.provider_profile,
                owner_key="tenant-foreign",
            ),
        )
    elif tamper == "profile_inactive":
        resolution = replace(
            resolution,
            provider_profile=replace(
                resolution.provider_profile,
                status="disabled",
            ),
        )
    repository = RecordingRepository(resolution)
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=True),
        repository=repository,
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(selector.resolve("tenant-private"))

    assert repository.decisions == [
        DataPlaneDecision(
            tenant_id="tenant-private",
            route_ref="dedicated-tenant-private",
            provider_profile_ref=resolution.provider_profile.profile_id,
            mode="dedicated",
            decision="unavailable",
            reason_code=reason_code,
        )
    ]


def test_enabled_tenant_without_openmaic_route_fails_closed() -> None:
    repository = RecordingRepository(
        DataPlaneResolution(
            tenant_id="tenant-standard",
            tenant_mode="shared",
            route=None,
            provider_profile=None,
        )
    )
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=True),
        repository=repository,
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(selector.resolve("tenant-standard"))

    assert repository.decisions == [
        DataPlaneDecision(
            tenant_id="tenant-standard",
            route_ref=None,
            provider_profile_ref=None,
            mode="shared",
            decision="unavailable",
            reason_code="no_route",
        )
    ]


def test_missing_resolution_records_safe_fail_closed_decision() -> None:
    repository = RecordingRepository(None)
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=True),
        repository=repository,
    )

    with pytest.raises(DataPlaneUnavailable):
        asyncio.run(selector.resolve("tenant-missing"))

    assert repository.decisions == [
        DataPlaneDecision(
            tenant_id="tenant-missing",
            route_ref=None,
            provider_profile_ref=None,
            mode=None,
            decision="unavailable",
            reason_code="no_route",
        )
    ]


def test_disabled_teaching_preserves_legacy_mode_without_repository_access() -> None:
    repository = RecordingRepository(None)
    selector = DataPlaneSelector(
        settings=SimpleNamespace(enabled=False),
        repository=repository,
    )

    assert asyncio.run(selector.resolve("legacy-tenant")) is None
    assert repository.calls == []
    assert repository.decisions == []
