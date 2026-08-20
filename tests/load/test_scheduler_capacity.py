from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def _load_harness():
    path = Path(__file__).parents[2] / "scripts" / "load_classroom.py"
    spec = importlib.util.spec_from_file_location("task6_load_classroom", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_first_release_profile() -> None:
    module = _load_harness()

    profile = module.load_profile("first-release")

    assert profile.tenants == 50
    assert profile.registered_users == 100_000
    assert profile.daily_active_users == 10_000
    assert profile.concurrent_classrooms == 200
    assert profile.shared_generation_slots == 20
    assert profile.default_tenant_slots == 2


def test_noisy_tenant_is_fair_and_capacity_is_bounded() -> None:
    module = _load_harness()
    profile = module.load_profile("first-release")

    report = module.run_profile(
        profile,
        provider_delay_ms=1,
        provider_error_rate=0,
        seed=7,
    )

    assert report.capacity_model == "simulated"
    assert report.scheduler.total_jobs == profile.concurrent_classrooms
    assert report.scheduler.max_global_active == profile.shared_generation_slots
    assert report.scheduler.max_tenant_active <= profile.default_tenant_slots
    assert report.scheduler.max_concurrent_classrooms == profile.concurrent_classrooms
    assert report.scheduler.foreign_tenants_before_noisy_third == profile.tenants - 1
    assert report.summary["generation_provider"].count == profile.concurrent_classrooms
    assert report.summary["generation_provider"].error_rate == 0


def test_mock_provider_error_rate_is_controllable() -> None:
    module = _load_harness()
    profile = module.load_profile("first-release")

    report = module.run_profile(
        profile,
        provider_delay_ms=0,
        provider_error_rate=1,
        seed=11,
    )

    assert report.summary["generation_provider"].count == profile.concurrent_classrooms
    assert report.summary["generation_provider"].error_rate == 1
