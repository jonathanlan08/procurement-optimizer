"""Structured JSON logging to stdout — stdlib only.

One JSON object per line: ``ts`` (ISO-8601 UTC), ``level``, ``logger``, ``message``,
plus any extra fields callers attach via ``extra={...}``. Keys that look like secrets
(password/token/secret/key/authorization/cookie, case-insensitive) have their values
redacted before they ever reach stdout.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final, TextIO

_SECRETISH: Final[tuple[str, ...]] = (
    "password",
    "token",
    "secret",
    "key",
    "authorization",
    "cookie",
)

_REDACTED: Final[str] = "[redacted]"

# Attributes every LogRecord carries regardless of `extra=`; anything else found on
# record.__dict__ was attached by the caller and belongs in the JSON payload.
_STANDARD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _is_secretish(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRETISH)


class JsonFormatter(logging.Formatter):
    """Renders a LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS:
                continue
            payload[key] = _REDACTED if _is_secretish(key) else value

        if record.exc_info:
            payload["stack"] = self.formatException(record.exc_info)
        elif record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str)


class _JsonStreamHandler(logging.StreamHandler[TextIO]):
    """Marker subclass so configure_logging can recognize its own handler."""


def configure_logging(level: str = "INFO") -> None:
    """Idempotent root-logger setup: one StreamHandler emitting JSON to stdout.

    Safe to call more than once (e.g. from multiple entry points) — repeat calls
    only update the level on the existing handler rather than stacking up
    duplicate handlers.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers:
        if isinstance(handler, _JsonStreamHandler):
            handler.setLevel(level)
            return

    handler = _JsonStreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
