"""Short-lived, server-bound classroom access tickets."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import secrets
import stat
from typing import Literal, TypeAlias

from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, ValidationError

from deeptutor.services.config import PlatformSettings

CLASSROOM_TICKET_ISSUER = "yfeistai"
CLASSROOM_TICKET_AUDIENCE = "yfeistai-classroom"
CLASSROOM_TICKET_ALGORITHM = "HS256"

ClassroomTicketAction: TypeAlias = Literal[
    "classroom.enter",
    "learning_event.append",
    "classroom.document.read",
    "classroom.media.read",
    "classroom.export.read",
]

_READ_ACTIONS = frozenset(
    {
        "classroom.document.read",
        "classroom.media.read",
        "classroom.export.read",
    }
)


class ClassroomTicketClaims(BaseModel):
    """The complete signed classroom authorization scope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    iss: Literal["yfeistai"]
    aud: Literal["yfeistai-classroom"]
    tenant_id: str
    user_id: str
    session_id: str
    classroom_version_id: str
    resource_id: str | None = None
    allowed_action: ClassroomTicketAction
    exp: int
    iat: int
    jti: str


class ClassroomTicketError(RuntimeError):
    """Base error for a rejected classroom ticket operation."""


class TicketConfigurationError(ClassroomTicketError):
    """The dedicated classroom ticket signing secret is unavailable."""


class TicketInvalid(ClassroomTicketError):
    """The ticket is malformed or has an invalid signature or claim shape."""


class TicketExpired(TicketInvalid):
    """The ticket validity window has ended."""


class TicketScopeError(TicketInvalid):
    """The ticket does not match the trusted server-side request scope."""


class TicketReplay(TicketInvalid):
    """A single-use classroom ticket was already committed."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_timestamp(clock: Callable[[], datetime]) -> int:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise TicketConfigurationError("classroom ticket clock must be timezone-aware")
    return int(value.timestamp())


def _read_ticket_secret(settings: PlatformSettings) -> str:
    path = settings.classroom_ticket_secret_file
    if path is None or not path.is_absolute():
        raise TicketConfigurationError("absolute classroom ticket secret file is required")
    try:
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise TicketConfigurationError("classroom ticket secret file must not be a symlink")
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise TicketConfigurationError("classroom ticket secret file must be a regular file")
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except TicketConfigurationError:
        raise
    except (OSError, UnicodeError):
        raise TicketConfigurationError("classroom ticket secret file could not be read") from None
    if "\x00" in value or len(value.encode("utf-8")) < 32:
        raise TicketConfigurationError("classroom ticket secret is invalid")
    return value


def _require_non_blank(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be blank")


def _validate_action_resource(
    action: str,
    resource_id: str | None,
) -> None:
    if action in _READ_ACTIONS:
        if resource_id is None or not resource_id.strip():
            raise ValueError("read tickets require a concrete resource_id")
        return
    if action in {"classroom.enter", "learning_event.append"}:
        if resource_id is not None:
            raise ValueError("non-read tickets must not contain resource_id")
        return
    raise ValueError("classroom ticket action is not supported")


class ClassroomTicketService:
    """Issue and verify narrowly scoped HS256 classroom JWTs."""

    def __init__(
        self,
        *,
        settings: PlatformSettings,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._secret = _read_ticket_secret(settings)
        self._clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: PlatformSettings,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "ClassroomTicketService":
        return cls(settings=settings, clock=clock)

    def issue(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        classroom_version_id: str,
        allowed_action: ClassroomTicketAction,
        ttl_seconds: int,
        resource_id: str | None = None,
    ) -> str:
        for name, value in (
            ("tenant_id", tenant_id),
            ("user_id", user_id),
            ("session_id", session_id),
            ("classroom_version_id", classroom_version_id),
        ):
            _require_non_blank(name, value)
        _validate_action_resource(allowed_action, resource_id)
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise ValueError("ticket ttl_seconds must be an integer")
        maximum_ttl = 60 if allowed_action in _READ_ACTIONS else 300
        if ttl_seconds < 1 or ttl_seconds > maximum_ttl:
            raise ValueError("ticket ttl_seconds is outside the allowed range")

        issued_at = _aware_timestamp(self._clock)
        claims = ClassroomTicketClaims(
            iss=CLASSROOM_TICKET_ISSUER,
            aud=CLASSROOM_TICKET_AUDIENCE,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            classroom_version_id=classroom_version_id,
            resource_id=resource_id,
            allowed_action=allowed_action,
            iat=issued_at,
            exp=issued_at + ttl_seconds,
            jti=secrets.token_urlsafe(24),
        )
        return jwt.encode(
            claims.model_dump(),
            self._secret,
            algorithm=CLASSROOM_TICKET_ALGORITHM,
        )

    def verify(
        self,
        token: str,
        *,
        expected_tenant_id: str,
        expected_user_id: str,
        expected_session_id: str,
        expected_version_id: str,
        expected_action: ClassroomTicketAction,
        expected_resource_id: str | None = None,
    ) -> ClassroomTicketClaims:
        claims = self._verified_claims(token)
        expected = (
            (claims.tenant_id, expected_tenant_id),
            (claims.user_id, expected_user_id),
            (claims.session_id, expected_session_id),
            (claims.classroom_version_id, expected_version_id),
            (claims.allowed_action, expected_action),
            (claims.resource_id, expected_resource_id),
        )
        if any(actual != required for actual, required in expected):
            raise TicketScopeError("classroom ticket scope does not match request")
        return claims

    def verify_read(
        self,
        token: str,
        *,
        expected_tenant_id: str,
        expected_user_id: str,
        expected_version_id: str,
        expected_action: ClassroomTicketAction,
        expected_resource_id: str,
    ) -> ClassroomTicketClaims:
        """Verify a read ticket before resolving its signed session id in the database."""

        if expected_action not in _READ_ACTIONS:
            raise TicketScopeError("classroom ticket scope does not match request")
        claims = self._verified_claims(token)
        expected = (
            (claims.tenant_id, expected_tenant_id),
            (claims.user_id, expected_user_id),
            (claims.classroom_version_id, expected_version_id),
            (claims.allowed_action, expected_action),
            (claims.resource_id, expected_resource_id),
        )
        if any(actual != required for actual, required in expected):
            raise TicketScopeError("classroom ticket scope does not match request")
        return claims

    def _verified_claims(self, token: str) -> ClassroomTicketClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[CLASSROOM_TICKET_ALGORITHM],
                audience=CLASSROOM_TICKET_AUDIENCE,
                issuer=CLASSROOM_TICKET_ISSUER,
                options={"verify_exp": False},
            )
            claims = ClassroomTicketClaims.model_validate(payload)
            _validate_action_resource(claims.allowed_action, claims.resource_id)
        except (JWTError, ValidationError, TypeError, ValueError):
            raise TicketInvalid("classroom ticket is invalid") from None

        now = _aware_timestamp(self._clock)
        if claims.exp <= now:
            raise TicketExpired("classroom ticket has expired")
        maximum_ttl = 60 if claims.allowed_action in _READ_ACTIONS else 300
        if (
            claims.iat > now
            or claims.exp <= claims.iat
            or claims.exp - claims.iat > maximum_ttl
            or len(claims.jti) < 22
        ):
            raise TicketInvalid("classroom ticket is invalid")
        return claims


__all__ = [
    "CLASSROOM_TICKET_ALGORITHM",
    "CLASSROOM_TICKET_AUDIENCE",
    "CLASSROOM_TICKET_ISSUER",
    "ClassroomTicketAction",
    "ClassroomTicketClaims",
    "ClassroomTicketError",
    "ClassroomTicketService",
    "TicketConfigurationError",
    "TicketExpired",
    "TicketInvalid",
    "TicketReplay",
    "TicketScopeError",
]
