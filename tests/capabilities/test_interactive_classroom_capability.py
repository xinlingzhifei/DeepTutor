"""Interactive classroom capability orchestration and discovery."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from deeptutor.core.context import UnifiedContext
from deeptutor.core.stream import StreamEventType
from deeptutor.core.stream_bus import StreamBus
from deeptutor.multi_user.context import (
    reset_current_tenant,
    reset_current_user,
    set_current_tenant,
    set_current_user,
    user_from_token_payload,
)
from deeptutor.services.auth import TokenPayload
from deeptutor.services.session.sqlite_store import SQLiteSessionStore
from deeptutor.services.session.turn_runtime import TurnRuntimeManager
from deeptutor.teaching.policies.student_generation import StudentGenerationEstimate
from deeptutor.teaching.services.student_classrooms import (
    StudentClassroomDenied,
    StudentClassroomView,
)
from deeptutor.teaching.tenant_context import TenantContext


class _FakeStudentClassroomService:
    def __init__(
        self,
        record: StudentClassroomView | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.record = record
        self.error = error
        self.calls: list[tuple[str, TenantContext, object]] = []
        self.estimate_value = StudentGenerationEstimate(
            scene_range=(1, 3),
            duration_minutes_range=(5, 15),
            quota_units=2,
            requires_outline_confirmation=False,
            requires_approval=False,
        )

    async def estimate(self, context: TenantContext, request: object):
        self.calls.append(("estimate", context, request))
        return self.estimate_value

    async def create(self, context: TenantContext, request: object):
        self.calls.append(("create", context, request))
        if self.error is not None:
            raise self.error
        assert self.record is not None
        return self.record


def _tenant() -> TenantContext:
    return TenantContext(
        tenant_id="tenant-trusted",
        schema_name="tenant_trusted",
        user_id="learner-trusted",
        permissions=frozenset(),
    )


def _context(
    *,
    mode: str = "micro",
    language: str = "en",
    content_mode: str = "source_grounded",
) -> UnifiedContext:
    return UnifiedContext(
        session_id="session-1",
        user_message="Explain Fourier transform",
        active_capability="interactive_classroom",
        knowledge_bases=["kb-authorized"],
        config_overrides={
            "mode": mode,
            "course_id": "course-a",
            "question": "A focused Fourier lesson",
            "content_mode": content_mode,
        },
        language=language,
        metadata={"class_id": "class-from-client"},
    )


async def _trusted_class_id(context: TenantContext, course_id: str) -> str:
    assert context.tenant_id == "tenant-trusted"
    assert context.user_id == "learner-trusted"
    assert course_id == "course-a"
    return "class-trusted"


def _record(
    *,
    mode: str = "micro",
    status: str = "queued",
    approval_id: str | None = None,
    job_id: str | None = "job-1",
    outline: dict[str, object] | None = None,
) -> StudentClassroomView:
    return StudentClassroomView(
        asset_id="asset-1",
        request_id="request-1",
        approval_id=approval_id,
        generation_job_id=job_id,
        status=status,
        course_id="course-a",
        class_id="class-trusted",
        mode=mode,
        owner_id="learner-trusted",
        revision=1,
        outline=outline,
    )


@pytest.mark.asyncio
async def test_micro_capability_uses_trusted_tenant_and_emits_unified_result() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    trusted = _tenant()
    service = _FakeStudentClassroomService(_record())
    capability = InteractiveClassroomCapability(
        service=service,
        class_resolver=_trusted_class_id,
    )
    stream = StreamBus()
    token = set_current_tenant(trusted)
    try:
        await capability.run(_context(), stream)
    finally:
        reset_current_tenant(token)

    assert [name for name, _, _ in service.calls] == ["estimate", "create"]
    assert all(context is trusted for _, context, _ in service.calls)
    request = service.calls[0][2]
    assert request.course_id == "course-a"
    assert request.class_id == "class-trusted"
    assert request.source_type == "knowledge_base"
    assert request.source_ref == "kb-authorized"
    assert request.objective == "A focused Fourier lesson"

    starts = [
        event.stage
        for event in stream._history
        if event.type == StreamEventType.STAGE_START
    ]
    assert starts == ["policy_check", "briefing", "queued"]
    result = next(
        event.metadata
        for event in stream._history
        if event.type == StreamEventType.RESULT
    )
    assert result["estimate"] == {
        "scene_range": (1, 3),
        "duration_minutes_range": (5, 15),
        "quota_units": 2,
        "requires_outline_confirmation": False,
        "requires_approval": False,
    }
    assert result["approval_id"] is None
    assert result["job_id"] == "job-1"
    assert result["outline"] is None
    assert result["classroom"]["asset_id"] == "asset-1"
    assert result["metadata"]["cost_summary"] == {
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "total_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }


@pytest.mark.asyncio
async def test_open_creation_does_not_select_context_knowledge_base() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    service = _FakeStudentClassroomService(_record())
    token = set_current_tenant(_tenant())
    try:
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(_context(content_mode="open_creation"), StreamBus())
    finally:
        reset_current_tenant(token)

    request = service.calls[0][2]
    assert request.content_mode == "open_creation"
    assert request.source_type is None
    assert request.source_ref is None


@pytest.mark.asyncio
async def test_full_capability_emits_outline_confirmation_result() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    service = _FakeStudentClassroomService(
        _record(
            mode="full",
            status="awaiting_outline_confirmation",
            job_id="job-outline",
            outline={"title": "Fourier path"},
        )
    )
    stream = StreamBus()
    token = set_current_tenant(_tenant())
    try:
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(
            _context(mode="full", language="zh"),
            stream,
        )
    finally:
        reset_current_tenant(token)

    starts = [
        event.stage
        for event in stream._history
        if event.type == StreamEventType.STAGE_START
    ]
    assert starts == ["policy_check", "briefing", "outline", "queued"]
    result = next(
        event.metadata
        for event in stream._history
        if event.type == StreamEventType.RESULT
    )
    assert result["job_id"] == "job-outline"
    assert result["outline"] == {"title": "Fourier path"}


@pytest.mark.asyncio
async def test_approval_required_returns_approval_without_starting_a_job() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    service = _FakeStudentClassroomService(
        _record(
            status="awaiting_approval",
            approval_id="approval-1",
            job_id=None,
        )
    )
    stream = StreamBus()
    token = set_current_tenant(_tenant())
    try:
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(_context(), stream)
    finally:
        reset_current_tenant(token)

    result = next(
        event.metadata
        for event in stream._history
        if event.type == StreamEventType.RESULT
    )
    assert result["approval_id"] == "approval-1"
    assert result["job_id"] is None


@pytest.mark.asyncio
async def test_generation_denial_is_streamed_and_propagated() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    service = _FakeStudentClassroomService(error=StudentClassroomDenied("policy denied"))
    stream = StreamBus()
    token = set_current_tenant(_tenant())
    try:
        with pytest.raises(StudentClassroomDenied, match="policy denied"):
            await InteractiveClassroomCapability(
                service=service,
                class_resolver=_trusted_class_id,
            ).run(_context(), stream)
    finally:
        reset_current_tenant(token)

    messages = [
        event.content
        for event in stream._history
        if event.type == StreamEventType.PROGRESS
    ]
    assert "Generation is not allowed for this request." in messages
    assert not any(event.type == StreamEventType.RESULT for event in stream._history)


@pytest.mark.asyncio
async def test_turn_runtime_executes_zero_arg_builtin_with_trusted_composition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from deeptutor.agents.interactive_classroom import capability as capability_module
    from deeptutor.runtime.registry import capability_registry as registry_module

    store = SQLiteSessionStore(tmp_path / "chat_history.db")
    runtime = TurnRuntimeManager(store)
    service = _FakeStudentClassroomService(_record())
    captured: dict[str, object] = {}

    class FakeContextBuilder:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def build(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                conversation_history=[],
                conversation_summary="",
                context_text="",
                token_count=0,
                budget=0,
            )

    async def resolve_class(context: TenantContext, course_id: str) -> str:
        captured["binding_context"] = context
        captured["course_id"] = course_id
        return "class-trusted"

    def build_service(context: TenantContext) -> _FakeStudentClassroomService:
        captured["service_context"] = context
        return service

    async def noop_async(*_args: object, **_kwargs: object) -> None:
        return None

    model_catalog = {
        "version": 1,
        "services": {
            "llm": {
                "active_profile_id": "p-test",
                "active_model_id": "m-test",
                "profiles": [
                    {
                        "id": "p-test",
                        "name": "Test",
                        "binding": "openai",
                        "base_url": "https://api.openai.com/v1",
                        "api_key": "sk-test",
                        "models": [
                            {
                                "id": "m-test",
                                "name": "Test",
                                "model": "gpt-4o-mini",
                            }
                        ],
                    }
                ],
            }
        },
    }
    monkeypatch.setattr(
        "deeptutor.multi_user.model_access.apply_allowed_llm_selection",
        lambda selection: selection,
    )
    monkeypatch.setattr(
        "deeptutor.services.config.get_model_catalog_service",
        lambda: SimpleNamespace(load=lambda: model_catalog),
    )
    monkeypatch.setattr(
        "deeptutor.services.model_selection.runtime.activate_llm_selection",
        lambda _selection: (SimpleNamespace(model="", provider_name=""), None),
    )
    monkeypatch.setattr(
        "deeptutor.services.model_selection.runtime.reset_llm_selection",
        lambda _token: None,
    )
    monkeypatch.setattr(
        "deeptutor.services.session.context_builder.ContextBuilder",
        FakeContextBuilder,
    )
    monkeypatch.setattr(
        "deeptutor.services.memory.get_memory_store",
        lambda: SimpleNamespace(read_l3_concat=lambda: "", emit=noop_async),
    )
    monkeypatch.setattr(
        "deeptutor.services.skill.get_skill_service",
        lambda: SimpleNamespace(
            summary_entries=lambda: [],
            load_always_for_context=lambda: "",
            load_for_context=lambda _skills: "",
        ),
    )
    monkeypatch.setattr(runtime, "_maybe_generate_session_title", noop_async)
    monkeypatch.setattr(
        capability_module,
        "resolve_student_class_id",
        resolve_class,
        raising=False,
    )
    monkeypatch.setattr(
        capability_module,
        "build_student_classroom_service",
        build_service,
        raising=False,
    )
    monkeypatch.setattr(registry_module, "_default_registry", None)

    trusted = _tenant()
    user_token = set_current_user(
        user_from_token_payload(
            TokenPayload(
                username="learner-trusted",
                role="user",
                user_id="learner-trusted",
            )
        )
    )
    tenant_token = set_current_tenant(trusted)
    try:
        _, turn = await runtime.start_turn(
            {
                "type": "start_turn",
                "content": "Explain Fourier transform",
                "session_id": None,
                "capability": "interactive_classroom",
                "tools": [],
                "knowledge_bases": ["kb-authorized"],
                "attachments": [],
                "language": "en",
                "llm_selection": {
                    "profile_id": "p-test",
                    "model_id": "m-test",
                },
                "config": {
                    "mode": "micro",
                    "course_id": "course-a",
                    "question": "A focused Fourier lesson",
                    "content_mode": "source_grounded",
                },
            }
        )
        events = [
            event
            async for event in runtime.subscribe_turn(turn["id"], after_seq=0)
        ]
    finally:
        reset_current_tenant(tenant_token)
        reset_current_user(user_token)
        registry_module._default_registry = None

    assert captured == {
        "binding_context": trusted,
        "course_id": "course-a",
        "service_context": trusted,
    }
    assert [name for name, _, _ in service.calls] == ["estimate", "create"]
    result = next(event for event in events if event["type"] == "result")
    assert result["metadata"]["job_id"] == "job-1"
    assert not any(event["type"] == "error" for event in events)


@pytest.mark.asyncio
async def test_capability_does_not_accept_tenant_identity_from_unified_context() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    context = _context()
    context.metadata["tenant_context"] = {
        "tenant_id": "tenant-from-client",
        "user_id": "learner-from-client",
    }
    service = _FakeStudentClassroomService(_record())

    with pytest.raises(RuntimeError, match="tenant context is not installed"):
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(context, StreamBus())
    assert service.calls == []


def test_manifest_and_builtin_discovery_match_public_contract() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )
    from deeptutor.app.facade import DeepTutorApp
    from deeptutor.runtime.registry.capability_registry import CapabilityRegistry

    manifest = InteractiveClassroomCapability.manifest
    assert manifest.name == "interactive_classroom"
    assert manifest.description == "Generate a private interactive classroom for the current course."
    assert manifest.stages == ["policy_check", "briefing", "outline", "queued"]
    assert manifest.tools_used == ["rag"]
    assert manifest.cli_aliases == ["classroom"]
    assert set(manifest.request_schema["required"]) == {"mode", "course_id"}

    registry = CapabilityRegistry()
    registry.load_builtins()
    assert isinstance(registry.get("interactive_classroom"), InteractiveClassroomCapability)

    app = DeepTutorApp.__new__(DeepTutorApp)
    app.capabilities = registry
    assert app.resolve_capability("classroom") == "interactive_classroom"


def test_interactive_classroom_status_files_have_exact_key_parity() -> None:
    prompt_root = (
        Path(__file__).resolve().parents[2]
        / "deeptutor"
        / "agents"
        / "interactive_classroom"
        / "prompts"
    )
    expected = {
        "policy_checking",
        "building_grounded_brief",
        "outline_queued",
        "micro_queued",
        "awaiting_approval",
        "awaiting_outline_confirmation",
        "generation_denied",
    }

    def status_keys(language: str) -> set[str]:
        data: Any = yaml.safe_load(
            (prompt_root / language / "interactive_classroom.yaml").read_text(
                encoding="utf-8"
            )
        )
        return set(data["status"])

    assert status_keys("en") == expected
    assert status_keys("zh") == expected
