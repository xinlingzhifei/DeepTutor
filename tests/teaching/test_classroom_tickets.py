from __future__ import annotations

from datetime import UTC, datetime, timedelta
import inspect
from pathlib import Path

from jose import jwt
import pytest

from deeptutor.services.config import PlatformSettings
from deeptutor.teaching.tickets import (
    CLASSROOM_TICKET_ALGORITHM,
    ClassroomTicketService,
    TicketConfigurationError,
    TicketExpired,
    TicketInvalid,
    TicketScopeError,
)

_SECRET = "ticket-secret-" + "a" * 48


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _ticket_service(
    tmp_path: Path,
    *,
    clock: _Clock | None = None,
) -> ClassroomTicketService:
    secret_file = tmp_path / "classroom-ticket-secret"
    secret_file.write_text(_SECRET, encoding="utf-8")
    return ClassroomTicketService.from_settings(
        PlatformSettings(classroom_ticket_secret_file=secret_file),
        clock=clock or _Clock(datetime(2026, 8, 10, 12, 0, tzinfo=UTC)),
    )


def _event_token(service: ClassroomTicketService) -> str:
    return service.issue(
        tenant_id="tenant-a",
        user_id="student-a",
        session_id="session-a",
        classroom_version_id="version-a",
        allowed_action="learning_event.append",
        ttl_seconds=300,
    )


def _verify_event(service: ClassroomTicketService, token: str):
    return service.verify(
        token,
        expected_tenant_id="tenant-a",
        expected_user_id="student-a",
        expected_session_id="session-a",
        expected_version_id="version-a",
        expected_action="learning_event.append",
    )


def test_ticket_service_has_no_raw_secret_constructor_bypass() -> None:
    parameters = inspect.signature(ClassroomTicketService).parameters

    assert "secret" not in parameters
    assert "settings" in parameters


def test_event_ticket_is_strictly_bound_to_server_scope(tmp_path: Path) -> None:
    service = _ticket_service(tmp_path)
    token = _event_token(service)

    claims = _verify_event(service, token)

    assert claims.iss == "yfeistai"
    assert claims.aud == "yfeistai-classroom"
    assert claims.user_id == "student-a"
    assert claims.resource_id is None
    assert claims.exp - claims.iat == 300
    assert len(claims.jti) >= 22

    mismatches = (
        {"expected_tenant_id": "tenant-b"},
        {"expected_user_id": "student-b"},
        {"expected_session_id": "session-b"},
        {"expected_version_id": "version-b"},
        {"expected_action": "classroom.enter"},
    )
    expected = {
        "expected_tenant_id": "tenant-a",
        "expected_user_id": "student-a",
        "expected_session_id": "session-a",
        "expected_version_id": "version-a",
        "expected_action": "learning_event.append",
    }
    for mismatch in mismatches:
        with pytest.raises(TicketScopeError):
            service.verify(token, **(expected | mismatch))


@pytest.mark.parametrize(
    "action",
    [
        "classroom.document.read",
        "classroom.media.read",
        "classroom.export.read",
    ],
)
def test_read_ticket_is_resource_bound_and_repeatable(
    tmp_path: Path,
    action: str,
) -> None:
    service = _ticket_service(tmp_path)
    token = service.issue(
        tenant_id="tenant-a",
        user_id="student-a",
        session_id="session-a",
        classroom_version_id="version-a",
        allowed_action=action,
        resource_id="resource-a",
        ttl_seconds=60,
    )
    expected = {
        "expected_tenant_id": "tenant-a",
        "expected_user_id": "student-a",
        "expected_session_id": "session-a",
        "expected_version_id": "version-a",
        "expected_action": action,
        "expected_resource_id": "resource-a",
    }

    assert service.verify(token, **expected).resource_id == "resource-a"
    assert service.verify(token, **expected).resource_id == "resource-a"
    with pytest.raises(TicketScopeError):
        service.verify(token, **(expected | {"expected_resource_id": "resource-b"}))


@pytest.mark.parametrize(
    ("action", "resource_id", "ttl_seconds"),
    [
        ("learning_event.append", "event-1", 60),
        ("classroom.enter", "classroom-1", 60),
        ("classroom.document.read", None, 60),
        ("classroom.media.read", None, 60),
        ("classroom.export.read", None, 60),
        ("learning_event.append", None, 0),
        ("learning_event.append", None, 301),
        ("classroom.media.read", "media-1", 61),
    ],
)
def test_ticket_issue_rejects_invalid_resource_or_ttl_shape(
    tmp_path: Path,
    action: str,
    resource_id: str | None,
    ttl_seconds: int,
) -> None:
    service = _ticket_service(tmp_path)

    with pytest.raises(ValueError):
        service.issue(
            tenant_id="tenant-a",
            user_id="student-a",
            session_id="session-a",
            classroom_version_id="version-a",
            allowed_action=action,
            resource_id=resource_id,
            ttl_seconds=ttl_seconds,
        )


def test_ticket_expiry_uses_injected_aware_clock(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 8, 10, 12, 0, tzinfo=UTC))
    service = _ticket_service(tmp_path, clock=clock)
    token = _event_token(service)
    clock.value += timedelta(seconds=301)

    with pytest.raises(TicketExpired):
        _verify_event(service, token)


def test_each_event_ticket_has_a_new_128_bit_or_stronger_jti(tmp_path: Path) -> None:
    service = _ticket_service(tmp_path)

    first = _verify_event(service, _event_token(service)).jti
    second = _verify_event(service, _event_token(service)).jti

    assert first != second
    assert len(first) >= 22
    assert len(second) >= 22


@pytest.mark.parametrize(
    ("kind", "contents"),
    [
        ("missing", None),
        ("empty", b""),
        ("nul", b"a" * 32 + b"\x00"),
        ("short", b"a" * 31),
        ("encoding", b"\xff" + b"a" * 40),
    ],
)
def test_ticket_secret_file_fails_closed(
    tmp_path: Path,
    kind: str,
    contents: bytes | None,
) -> None:
    secret_file = tmp_path / f"secret-{kind}"
    if contents is not None:
        secret_file.write_bytes(contents)

    with pytest.raises(TicketConfigurationError):
        ClassroomTicketService.from_settings(
            PlatformSettings(classroom_ticket_secret_file=secret_file)
        )


def test_ticket_secret_file_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "actual-secret"
    target.write_text("a" * 48, encoding="utf-8")
    link = tmp_path / "ticket-secret-link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this Windows host")

    with pytest.raises(TicketConfigurationError):
        ClassroomTicketService.from_settings(PlatformSettings(classroom_ticket_secret_file=link))


def test_ticket_rejects_non_hs256_or_malformed_tokens(tmp_path: Path) -> None:
    service = _ticket_service(tmp_path)

    with pytest.raises(TicketInvalid):
        _verify_event(service, "not-a-jwt")

    now = int(datetime(2026, 8, 10, 12, 0, tzinfo=UTC).timestamp())
    payload = {
        "iss": "yfeistai",
        "aud": "yfeistai-classroom",
        "tenant_id": "tenant-a",
        "user_id": "student-a",
        "session_id": "session-a",
        "classroom_version_id": "version-a",
        "resource_id": None,
        "allowed_action": "learning_event.append",
        "iat": now,
        "exp": now + 60,
        "jti": "a" * 32,
    }
    non_hs256 = jwt.encode(payload, _SECRET, algorithm="HS384")
    with pytest.raises(TicketInvalid):
        _verify_event(service, non_hs256)


@pytest.mark.parametrize(
    ("action", "resource_id", "maximum_ttl"),
    [
        ("learning_event.append", None, 300),
        ("classroom.document.read", "document-a", 60),
    ],
)
def test_verify_rejects_signed_ticket_beyond_action_ttl(
    tmp_path: Path,
    action: str,
    resource_id: str | None,
    maximum_ttl: int,
) -> None:
    service = _ticket_service(tmp_path)
    now = int(datetime(2026, 8, 10, 12, 0, tzinfo=UTC).timestamp())
    token = jwt.encode(
        {
            "iss": "yfeistai",
            "aud": "yfeistai-classroom",
            "tenant_id": "tenant-a",
            "user_id": "student-a",
            "session_id": "session-a",
            "classroom_version_id": "version-a",
            "resource_id": resource_id,
            "allowed_action": action,
            "iat": now,
            "exp": now + maximum_ttl + 1,
            "jti": "b" * 32,
        },
        _SECRET,
        algorithm=CLASSROOM_TICKET_ALGORITHM,
    )

    with pytest.raises(TicketInvalid):
        service.verify(
            token,
            expected_tenant_id="tenant-a",
            expected_user_id="student-a",
            expected_session_id="session-a",
            expected_version_id="version-a",
            expected_action=action,
            expected_resource_id=resource_id,
        )


def test_verify_rejects_extra_or_missing_required_claims(tmp_path: Path) -> None:
    service = _ticket_service(tmp_path)
    now = int(datetime(2026, 8, 10, 12, 0, tzinfo=UTC).timestamp())
    payload = {
        "iss": "yfeistai",
        "aud": "yfeistai-classroom",
        "tenant_id": "tenant-a",
        "user_id": "student-a",
        "session_id": "session-a",
        "classroom_version_id": "version-a",
        "resource_id": None,
        "allowed_action": "learning_event.append",
        "iat": now,
        "exp": now + 60,
        "jti": "c" * 32,
    }
    invalid_payloads = (payload | {"unexpected": "claim"}, payload | {"jti": None})

    for invalid_payload in invalid_payloads:
        token = jwt.encode(
            invalid_payload,
            _SECRET,
            algorithm=CLASSROOM_TICKET_ALGORITHM,
        )
        with pytest.raises(TicketInvalid):
            _verify_event(service, token)
