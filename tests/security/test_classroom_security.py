from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy.exc import IntegrityError

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.artifact_validation import ArtifactValidationError, _parse_artifact
from deeptutor.teaching.artifacts import (
    ArtifactManifestEntry,
    ArtifactManifestError,
    classroom_artifact_key,
)
from deeptutor.teaching.contracts import GenerationRequest
from deeptutor.teaching.database import tenant_connection
from deeptutor.teaching.learning_events import LearningEventBatch
from deeptutor.teaching.object_store import LocalClassroomArtifactStore, ObjectStoreAccessDenied
from deeptutor.teaching.openmaic.auth import (
    ServiceRequest,
    sign_service_request,
    verify_service_request,
)
from deeptutor.teaching.openmaic.data_planes import DataPlaneSelection, ProviderProfileRecord
from deeptutor.teaching.openmaic.provider_secrets import ProviderSecretResolver
from deeptutor.teaching.schema_names import tenant_schema_name
from deeptutor.teaching.services.learning_sessions import LearningSessionService
from deeptutor.teaching.tenant_context import TenantContext
from deeptutor.teaching.tickets import ClassroomTicketService, TicketExpired, TicketReplay


class _FakeEngine:
    def __init__(self) -> None:
        self.options: list[dict[str, object]] = []

    def execution_options(self, **options):
        self.options.append(options)
        return self

    @asynccontextmanager
    async def connect(self):
        yield object()


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _BindingRepository:
    def __init__(self, profile: ProviderProfileRecord) -> None:
        self.profile = profile

    async def resolve_bound_profile(self, _selection: DataPlaneSelection):
        return self.profile


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _ReplaySession:
    def __init__(self, consumed_jtis: set[str]) -> None:
        self._consumed_jtis = consumed_jtis
        self._pending_jti: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()

    async def scalar(self, _statement):
        return SimpleNamespace(id="session-a", classroom_version_id="version-a")

    def add(self, model) -> None:
        self._pending_jti = model.jti

    async def flush(self) -> None:
        assert self._pending_jti is not None
        if self._pending_jti in self._consumed_jtis:
            raise IntegrityError("duplicate ticket", {}, Exception("duplicate"))
        self._consumed_jtis.add(self._pending_jti)


def _ticket_service(tmp_path: Path, clock: _Clock) -> ClassroomTicketService:
    secret = tmp_path / "classroom-ticket-secret"
    secret.write_text("security-ticket-secret-" + "x" * 48, encoding="utf-8")
    return ClassroomTicketService.from_settings(
        PlatformSettings(classroom_ticket_secret_file=secret),
        clock=clock,
    )


def test_tenant_database_and_object_prefixes_fail_closed(tmp_path: Path) -> None:
    engine = _FakeEngine()

    async def exercise() -> None:
        async with tenant_connection(engine, "tenant-a"):
            pass
        async with tenant_connection(engine, "tenant-b"):
            pass
        store = LocalClassroomArtifactStore(tmp_path / "objects", "tenant-a")
        foreign_key = classroom_artifact_key("tenant-b", "asset-b", 1, "classroom.json")
        with pytest.raises(ObjectStoreAccessDenied):
            await store.exists(foreign_key)

    asyncio.run(exercise())

    assert engine.options == [
        {"schema_translate_map": {"tenant": tenant_schema_name("tenant-a")}},
        {"schema_translate_map": {"tenant": tenant_schema_name("tenant-b")}},
    ]
    assert engine.options[0] != engine.options[1]


def test_event_ticket_expires_and_replay_is_rejected_across_service_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(datetime(2026, 8, 20, 12, 0, tzinfo=UTC))
    tickets = _ticket_service(tmp_path, clock)
    token = tickets.issue(
        tenant_id="tenant-a",
        user_id="student-a",
        session_id="session-a",
        classroom_version_id="version-a",
        allowed_action="learning_event.append",
        ttl_seconds=300,
    )
    clock.value += timedelta(seconds=301)
    with pytest.raises(TicketExpired):
        tickets.verify(
            token,
            expected_tenant_id="tenant-a",
            expected_user_id="student-a",
            expected_session_id="session-a",
            expected_version_id="version-a",
            expected_action="learning_event.append",
        )

    clock.value -= timedelta(seconds=301)
    consumed_jtis: set[str] = set()
    first = LearningSessionService(engine=object(), ticket_service=tickets)
    second = LearningSessionService(engine=object(), ticket_service=tickets)
    for service in (first, second):
        monkeypatch.setattr(
            service,
            "_session_factory",
            lambda _context: lambda: _ReplaySession(consumed_jtis),
        )
    context = TenantContext(
        tenant_id="tenant-a",
        schema_name=tenant_schema_name("tenant-a"),
        user_id="student-a",
        permissions=frozenset(),
    )
    protected_calls = 0

    async def protected_action(_session, claims):
        nonlocal protected_calls
        protected_calls += 1
        return claims.jti

    async def exercise_replay() -> None:
        await first.consume_event_ticket(
            context,
            session_id="session-a",
            token=token,
            protected_action=protected_action,
        )
        with pytest.raises(TicketReplay):
            await second.consume_event_ticket(
                context,
                session_id="session-a",
                token=token,
                protected_action=protected_action,
            )

    asyncio.run(exercise_replay())
    assert protected_calls == 1


def test_service_signature_tampering_is_rejected() -> None:
    request = ServiceRequest(
        method="POST",
        path="/api/yfeistai/v1/outlines",
        tenant_id="tenant-a",
        job_id="job-a",
        timestamp=1_776_000_000,
        idempotency_key="idem-a",
        body=b"{}",
    )
    secret = SecretStr("service-secret")
    signature = sign_service_request(request, secret)

    assert verify_service_request(
        request,
        signature,
        secret,
        now_seconds=request.timestamp,
    )
    for tampered in (
        replace(request, tenant_id="tenant-b"),
        replace(request, job_id="job-b"),
        replace(request, body=b'{"forged":true}'),
    ):
        assert not verify_service_request(
            tampered,
            signature,
            secret,
            now_seconds=request.timestamp,
        )


@pytest.mark.parametrize(
    "authority_field",
    ["tenant_id", "user_id", "session_id", "classroom_version_id"],
)
def test_learning_events_reject_client_authority(authority_field: str) -> None:
    event = {
        "schema_version": "1.0",
        "event_id": "event-a",
        "event_type": "classroom.started",
        "occurred_at": "2026-08-20T12:00:00Z",
        authority_field: "forged",
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LearningEventBatch.model_validate({"events": [event]})


def test_interactive_iframe_messages_are_source_origin_nonce_and_frame_bound() -> None:
    root = Path(__file__).parents[2]
    component = (root / "web/components/classroom/InteractiveScene.tsx").read_text(encoding="utf-8")
    bridge = (root / "web/lib/openmaic-adapter/playback/interactive-bridge.ts").read_text(
        encoding="utf-8"
    )

    assert "event.source !== frameRef.current?.contentWindow" in component
    assert "event.origin !== 'null'" in component
    assert "readInteractiveMessage(event.data, sessionNonce, ownedFrameInstanceId)" in component
    assert 'sandbox="allow-scripts"' in component
    assert "data.sessionNonce!==nonce" in bridge
    assert "data.eventId!==pending.eventId" in bridge
    assert "frameId" in bridge


def test_artifacts_reject_unsupported_mime_and_oversized_payloads() -> None:
    with pytest.raises(ArtifactManifestError):
        ArtifactManifestEntry(
            relative_name="payload.exe",
            content_type="application/x-msdownload",
            sha256="a" * 64,
            size=1,
        ).validate()

    with pytest.raises(ArtifactValidationError, match="artifact_invalid"):
        _parse_artifact(
            {
                "relative_path": "classroom.json",
                "sha256": "a" * 64,
                "size_bytes": 512 * 1024 * 1024 + 1,
                "mime_type": "application/json",
                "temporary_download_path": "/api/yfeistai/v1/artifacts/job-a/classroom.json",
                "expires_at": "2026-08-20T13:00:00Z",
            },
            job_id="job-a",
            now=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        )


def test_provider_secret_is_redacted_from_api_values_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_value = "PROVIDER_SECRET_SENTINEL"
    secret_ref = "shared/providers/provider-a"
    secret_file = tmp_path.joinpath(*secret_ref.split("/"))
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text(secret_value, encoding="utf-8")
    selection = DataPlaneSelection(
        tenant_id="tenant-a",
        route_ref="shared-primary",
        provider_profile_ref="provider-a",
        mode="shared",
        worker_pool_ref="shared-generation",
        queue_ref="openmaic.shared",
    )
    profile = ProviderProfileRecord(
        profile_id="provider-a",
        scope="shared",
        tenant_id=None,
        owner_key="shared",
        provider_type="openai-compatible",
        model_name="model-a",
        api_base_url=None,
        secret_ref=secret_ref,
        status="active",
    )
    resolver = ProviderSecretResolver(
        tmp_path,
        runtime_mode="shared",
        binding_repository=_BindingRepository(profile),
    )

    with caplog.at_level("DEBUG"):
        secret = asyncio.run(resolver.resolve(selection=selection))

    assert secret.get_secret_value() == secret_value
    assert secret_value not in repr(secret)
    assert secret_value not in repr(resolver)
    assert secret_value not in caplog.text
    assert all(
        forbidden not in field_name.lower()
        for field_name in GenerationRequest.model_fields
        for forbidden in ("secret", "api_key", "access_key")
    )


def test_only_gateway_has_public_ports_and_openmaic_is_not_proxied() -> None:
    root = Path(__file__).parents[2]
    platform = (root / "docker-compose.platform.yml").read_text(encoding="utf-8")
    data_plane = (root / "docker-compose.data-plane.yml").read_text(encoding="utf-8")
    gateway = (root / "deploy/nginx/yfeistai-classroom.conf").read_text(encoding="utf-8")

    platform_openmaic = platform.split("\n  openmaic:\n", 1)[1].split("\n  openmaic-render:\n", 1)[
        0
    ]
    data_plane_openmaic = data_plane.split("\n  openmaic:\n", 1)[1].split(
        "\n  openmaic-render:\n", 1
    )[0]
    assert "ports:" not in platform_openmaic
    assert "expose:" not in platform_openmaic
    assert "ports:" not in data_plane_openmaic
    assert "expose:" not in data_plane_openmaic
    assert "openmaic" not in gateway.lower()
    assert "proxy_pass $deeptutor_api" in gateway
    assert "proxy_pass $deeptutor_web" in gateway


def test_security_suite_uses_production_entry_points() -> None:
    assert "secret" not in inspect.signature(ClassroomTicketService).parameters
    assert (
        "protected_action"
        in inspect.signature(LearningSessionService.consume_event_ticket).parameters
    )
