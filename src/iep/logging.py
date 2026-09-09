"""Structured JSON logging with a request-scoped correlation id.

A dependency-free formatter is used deliberately: the requirement is one JSON
object per line carrying the correlation id, the dossier and the job, which is
about forty lines of stdlib. Adding a logging framework would not change the
output but would change what has to be explained in an incident.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("iep_log_context")

# Values that must never reach a log line, regardless of how a caller nests
# them inside `extra`. Document text is excluded at the call sites, not here.
_REDACTED_KEYS = frozenset(
    {"api_key", "authorization", "password", "token", "secret", "x-api-key", "llm_api_key"}
)
_REDACTED = "[redacted]"

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
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


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k.lower() in _REDACTED_KEYS else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_CONTEXT.get({}))
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _redact(value)
        if record.exc_info:
            # The formatted traceback stays in the operator-facing log. The API
            # error handler never forwards it to a client.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log record emitted inside the block."""
    current = dict(_CONTEXT.get({}))
    current.update({k: v for k, v in fields.items() if v is not None})
    token = _CONTEXT.set(current)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get({}))
