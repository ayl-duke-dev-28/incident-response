"""Structured JSON logging with request/incident correlation.

Uses a ContextVar so any log call inside a request handler automatically carries
the incident_id and trace_id — no need to thread it through every function.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

_incident_id: ContextVar[str | None] = ContextVar("incident_id", default=None)
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_incident_id(value: str | None) -> None:
    _incident_id.set(value)


def set_trace_id(value: str | None) -> None:
    _trace_id.set(value)


def get_incident_id() -> str | None:
    return _incident_id.get()


def get_trace_id() -> str | None:
    return _trace_id.get()


class JsonFormatter(logging.Formatter):
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        inc = _incident_id.get()
        if inc:
            payload["incident_id"] = inc
        trace = _trace_id.get()
        if trace:
            payload["trace_id"] = trace
        # Attach any custom fields passed via extra=
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
