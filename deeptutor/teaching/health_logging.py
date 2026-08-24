"""Concurrency-safe redaction for private dependency health transports."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from threading import Lock

_PRIVATE_HEALTH_LOG_MESSAGE = "private health transport log redacted"
_health_transport_active: ContextVar[bool] = ContextVar(
    "teaching_health_transport_active",
    default=False,
)
_factory_lock = Lock()
_handler_lock = Lock()
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class _HealthRedactionHandlerFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "_teaching_health_redacted", False):
            return True
        for field in tuple(record.__dict__):
            if field not in _STANDARD_LOG_RECORD_FIELDS and field != ("_teaching_health_redacted"):
                record.__dict__.pop(field, None)
        record.msg = _PRIVATE_HEALTH_LOG_MESSAGE
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _install_record_factory() -> None:
    with _factory_lock:
        current = logging.getLogRecordFactory()
        if getattr(current, "_teaching_health_redactor", False):
            return

        def health_record_factory(*args, **kwargs):
            record = current(*args, **kwargs)
            if _health_transport_active.get():
                record._teaching_health_redacted = True
                record.msg = _PRIVATE_HEALTH_LOG_MESSAGE
                record.args = ()
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
            return record

        health_record_factory._teaching_health_redactor = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(health_record_factory)


def _install_handler_filters() -> None:
    with _handler_lock:
        logging._acquireLock()  # type: ignore[attr-defined]
        try:
            handlers = list(logging.getLogger().handlers)
            logger_snapshot = tuple(logging.Logger.manager.loggerDict.values())
            for candidate in logger_snapshot:
                if isinstance(candidate, logging.Logger):
                    handlers.extend(candidate.handlers)
            if logging.lastResort is not None:
                handlers.append(logging.lastResort)
            for handler in {id(handler): handler for handler in handlers}.values():
                if not any(
                    isinstance(filter_, _HealthRedactionHandlerFilter)
                    for filter_ in handler.filters
                ):
                    handler.addFilter(_HealthRedactionHandlerFilter())
        finally:
            logging._releaseLock()  # type: ignore[attr-defined]


@contextmanager
def redact_health_transport_logs() -> Iterator[None]:
    """Redact logs in this context, including work delegated with ``to_thread``."""

    _install_record_factory()
    _install_handler_filters()
    token = _health_transport_active.set(True)
    try:
        yield
    finally:
        _health_transport_active.reset(token)
