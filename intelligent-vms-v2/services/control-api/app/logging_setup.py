"""Structured logging with correlation IDs and secret redaction.

Every log line carries a correlation id so one operator action can be traced
across the control API, recorder and analytics worker. A redaction processor
strips anything that looks like a credential, because camera RTSP URLs embed
passwords and would otherwise end up in support bundles.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
actor_id: ContextVar[str] = ContextVar("actor_id", default="-")

_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z]+://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")
_SECRET_KEYS = {
    "password", "passwd", "secret", "token", "access_token", "refresh_token",
    "play_token", "authorization", "api_key", "apikey", "client_secret",
    "private_key", "master_key", "credential", "secret_blob", "totp_secret",
}
REDACTED = "***redacted***"


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_correlation_id(value: str | None = None) -> str:
    cid = value or new_correlation_id()
    correlation_id.set(cid)
    return cid


def redact_text(text: str) -> str:
    return _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", text)


def _redact_value(key: str, value: Any) -> Any:
    if key.lower() in _SECRET_KEYS:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(key, v) for v in value)
    return value


def _redaction_processor(_logger, _name, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: _redact_value(k, v) for k, v in event_dict.items()}


def _context_processor(_logger, _name, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict.setdefault("correlation_id", correlation_id.get())
    actor = actor_id.get()
    if actor and actor != "-":
        event_dict.setdefault("actor", actor)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True,
                      log_file: Path | None = None,
                      max_bytes: int = 64 * 1024 * 1024,
                      backup_count: int = 10,
                      service: str = "control-api") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"))

    logging.basicConfig(format="%(message)s",
                        level=getattr(logging, level.upper(), logging.INFO),
                        handlers=handlers, force=True)

    renderer = (structlog.processors.JSONRenderer() if json_output
                else structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _context_processor,
            _redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service)

    for noisy in ("uvicorn.access", "httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "vms"):
    return structlog.get_logger(name)
