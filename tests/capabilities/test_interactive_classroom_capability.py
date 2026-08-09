"""Interactive classroom capability orchestration and discovery."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from deeptutor.core.context import UnifiedContext
from deeptutor.core.stream import StreamEventType
from deeptutor.core.stream_bus import StreamBus
from deeptutor.multi_user.context import (
    get_current_tenant_or_none,
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
    question: str = "A focused Fourier lesson",
    user_message: str = "Explain Fourier transform",
) -> UnifiedContext:
    return UnifiedContext(
        session_id="session-1",
        user_message=user_message,
        active_capability="interactive_classroom",
        knowledge_bases=["kb-authorized"],
        config_overrides={
            "mode": mode,
            "course_id": "course-a",
            "question": question,
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
async def test_fallback_question_rejects_more_than_4000_effective_characters() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    service = _FakeStudentClassroomService(_record())
    token = set_current_tenant(_tenant())
    try:
        with pytest.raises(
            RuntimeError,
            match="Interactive classroom request is invalid.",
        ) as captured:
            await InteractiveClassroomCapability(
                service=service,
                class_resolver=_trusted_class_id,
            ).run(
                _context(question="", user_message="x" * 4001),
                StreamBus(),
            )
    finally:
        reset_current_tenant(token)

    assert getattr(captured.value, "code", None) == "interactive_classroom_invalid_request"
    assert isinstance(captured.value.__cause__, ValueError)
    assert service.calls == []


@pytest.mark.asyncio
async def test_fallback_question_accepts_4000_and_caps_title_at_255() -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )

    service = _FakeStudentClassroomService(_record())
    token = set_current_tenant(_tenant())
    try:
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(
            _context(question="", user_message=f"  {'x' * 4000}  "),
            StreamBus(),
        )
    finally:
        reset_current_tenant(token)

    request = service.calls[0][2]
    assert request.objective == "x" * 4000
    assert request.title == "x" * 255


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
        with pytest.raises(
            RuntimeError,
            match="Classroom generation is not allowed for this request.",
        ) as captured:
            await InteractiveClassroomCapability(
                service=service,
                class_resolver=_trusted_class_id,
            ).run(_context(), stream)
    finally:
        reset_current_tenant(token)

    assert getattr(captured.value, "code", None) == "interactive_classroom_denied"
    assert getattr(captured.value, "status", None) == "denied"
    assert isinstance(captured.value.__cause__, StudentClassroomDenied)
    messages = [
        event.content
        for event in stream._history
        if event.type == StreamEventType.PROGRESS
    ]
    assert "Generation is not allowed for this request." in messages
    assert not any(event.type == StreamEventType.RESULT for event in stream._history)


_SECRET_RESOLVER_ERROR = (
    "SELECT learner_id FROM tenant_secret WHERE provider_key='sk-live-secret'; "
    "store=<TenantStore password='db-secret'>"
)
_SECRET_DENIED_ERROR = "policy denied from tenant_rule_<secret-policy-row>"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["success", "resolver", "denied"])
async def test_turn_runtime_executes_zero_arg_builtin_with_trusted_composition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure_mode: str,
) -> None:
    from deeptutor.agents.interactive_classroom import capability as capability_module
    from deeptutor.runtime.registry import capability_registry as registry_module

    store = SQLiteSessionStore(tmp_path / "chat_history.db")
    runtime = TurnRuntimeManager(store)
    service = _FakeStudentClassroomService(
        _record(),
        error=(
            StudentClassroomDenied(_SECRET_DENIED_ERROR)
            if failure_mode == "denied"
            else None
        ),
    )
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
        if failure_mode == "resolver":
            raise RuntimeError(_SECRET_RESOLVER_ERROR)
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
        with caplog.at_level(logging.ERROR, logger="deeptutor.runtime.orchestrator"):
            session, turn = await runtime.start_turn(
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

    if failure_mode != "success":
        error = next(event for event in events if event["type"] == "error")
        expected_error = (
            {
                "content": "Classroom generation is not allowed for this request.",
                "code": "interactive_classroom_denied",
                "status": "denied",
            }
            if failure_mode == "denied"
            else {
                "content": "Interactive classroom generation is temporarily unavailable.",
                "code": "interactive_classroom_unavailable",
                "status": "failed",
            }
        )
        assert error["content"] == expected_error["content"]
        assert error["metadata"] == {
            "code": expected_error["code"],
            "status": expected_error["status"],
            "turn_terminal": True,
        }
        persisted = {
            "events": await store.get_turn_events(turn["id"]),
            "session": await store.get_session_with_messages(session["id"]),
            "turn": await store.get_turn(turn["id"]),
        }
        serialized = json.dumps(persisted, ensure_ascii=False, default=str)
        assert "sk-live-secret" not in serialized
        assert "db-secret" not in serialized
        assert "tenant_secret" not in serialized
        assert "secret-policy-row" not in serialized
        assert (
            _SECRET_DENIED_ERROR if failure_mode == "denied" else _SECRET_RESOLVER_ERROR
        ) in caplog.text
        assert persisted["turn"]["status"] == "failed"
        assert persisted["turn"]["error"] == expected_error["content"]
        done = next(event for event in events if event["type"] == "done")
        assert done["metadata"]["status"] == expected_error["status"]
        assert not any(event["type"] == "result" for event in events)
        return

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
async def test_capability_requires_installed_tenant_even_in_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )
    from deeptutor.teaching import tenant_context as tenant_context_module

    service = _FakeStudentClassroomService(_record())
    monkeypatch.setattr(
        tenant_context_module,
        "load_platform_settings",
        lambda: SimpleNamespace(enabled=False),
    )
    assert get_current_tenant_or_none() is None

    with pytest.raises(
        RuntimeError,
        match="Interactive classroom generation is temporarily unavailable.",
    ) as captured:
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(_context(), StreamBus())

    assert getattr(captured.value, "code", None) == "interactive_classroom_unavailable"
    assert isinstance(captured.value.__cause__, RuntimeError)
    assert service.calls == []


@pytest.mark.asyncio
async def test_capability_does_not_accept_tenant_identity_from_unified_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from deeptutor.agents.interactive_classroom.capability import (
        InteractiveClassroomCapability,
    )
    from deeptutor.teaching import tenant_context as tenant_context_module

    context = _context()
    context.metadata["tenant_context"] = {
        "tenant_id": "tenant-from-client",
        "user_id": "learner-from-client",
    }
    service = _FakeStudentClassroomService(_record())
    monkeypatch.setattr(
        tenant_context_module,
        "load_platform_settings",
        lambda: SimpleNamespace(enabled=True),
    )

    with pytest.raises(
        RuntimeError,
        match="Interactive classroom generation is temporarily unavailable.",
    ) as captured:
        await InteractiveClassroomCapability(
            service=service,
            class_resolver=_trusted_class_id,
        ).run(context, StreamBus())
    assert getattr(captured.value, "code", None) == "interactive_classroom_unavailable"
    assert isinstance(captured.value.__cause__, RuntimeError)
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
