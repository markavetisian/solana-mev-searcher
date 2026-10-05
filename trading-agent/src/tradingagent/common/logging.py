"""Structured JSON logging.

Every record carries: timestamp, component, event, severity plus whatever context is bound (token, slot,
request_id, execution_id, latency_ms, error). Context propagates through asyncio tasks via contextvars.
Secrets never reach the log: values of keys that look secret are redacted and known secret strings registered
via `register_secret` are scrubbed from every message.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("ta_log_ctx", default=None)
_SECRET_KEY_HINTS = ("secret", "private", "password", "api_key", "apikey", "token", "keypair", "seed")
_ALLOWED_TOKEN_KEYS = {"token", "token_mint", "token_symbol", "tokens", "token_amount", "token_name"}
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Scrub this exact string from all future log output (RPC URLs with embedded keys, bot tokens...)."""
    if value and len(value) >= 8:
        _registered_secrets.add(value)


def _scrub(text: str) -> str:
    for s in _registered_secrets:
        if s in text:
            text = text.replace(s, "***REDACTED***")
    return text


def _redact(key: str, value: Any) -> Any:
    k = key.lower()
    if k not in _ALLOWED_TOKEN_KEYS and any(h in k for h in _SECRET_KEY_HINTS):
        return "***REDACTED***"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "severity": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": getattr(record, "event", record.getMessage()),
        }
        ctx = _context.get() or {}
        for k, v in ctx.items():
            payload.setdefault(k, _redact(k, v))
        extra = getattr(record, "fields", None)
        if extra:
            for k, v in extra.items():
                payload[k] = _redact(k, v)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return _scrub(json.dumps(payload, default=str, separators=(",", ":")))


class ComponentLogger:
    """Thin wrapper: logger.info("event_name", token=..., slot=...)."""

    def __init__(self, component: str) -> None:
        self.component = component
        self._log = logging.getLogger(f"ta.{component}")

    def _emit(self, level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(
                level,
                event,
                exc_info=exc_info,
                extra={"component": self.component, "event": event, "fields": fields},
            )

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, **fields)

    def error(self, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._emit(logging.ERROR, event, exc_info=exc_info, **fields)

    def critical(self, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._emit(logging.CRITICAL, event, exc_info=exc_info, **fields)


def get_logger(component: str) -> ComponentLogger:
    return ComponentLogger(component)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    current = dict(_context.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


def configure_logging(level: str = "INFO", stream: Any = None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "anthropic", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
