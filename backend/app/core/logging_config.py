"""Centralized logging configuration using loguru."""

import logging
import os
import re
import sys
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from loguru import logger as _logger

# Context variable for trace ID
trace_id_var: ContextVar[str] = ContextVar("trace_id", default=None)
_PROCESS_TRACE_ID = "process-" + uuid4().hex[:12]
NOISY_SUCCESS_LOGGER_PREFIXES = ("httpx", "httpcore", "uvicorn.access", "websockets.server", "websockets.legacy.server")
_WEBSOCKET_QUERY_RE = re.compile(r'("WebSocket [^"?]+)\?[^"]+(")')
_SENSITIVE_QUERY_PARAM_RE = re.compile(r"([?&](?:access_key|ticket|token|session_id|app_secret)=)[^&\s\"']+")


def get_trace_id() -> str:
    """Get current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set trace ID in context."""
    trace_id_var.set(trace_id)


def clear_trace_id() -> None:
    """Clear the current request/task trace ID."""
    trace_id_var.set(None)


def _fallback_trace_id() -> str:
    return get_trace_id() or _PROCESS_TRACE_ID


def enrich_log_record(record: dict[str, Any]) -> bool:
    """Attach a stable trace_id to every loguru record."""
    record["extra"].setdefault("trace_id", _fallback_trace_id())
    return True


def configure_logging(*, sink: Any = sys.stdout, enqueue: bool = True):
    """Configure loguru with custom format including trace ID."""
    # Remove default handler
    _logger.remove()

    log_format = os.getenv("HIVE_LOG_FORMAT", "json").strip().lower()
    common = {
        "level": "INFO",
        "enqueue": enqueue,
        "backtrace": True,
        "diagnose": False,
        "filter": enrich_log_record,
    }
    if log_format in {"text", "pretty", "plain"}:
        _logger.add(
            sink,
            format=(
                "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[trace_id]:-<20} | "
                "{name}:{function}:{line} - {message}"
            ),
            **common,
        )
    else:
        _logger.add(
            sink,
            serialize=True,
            **common,
        )

    return _logger


def sanitize_standard_log_message(message: str) -> str:
    """Strip sensitive query strings from standard-library log messages."""
    sanitized = _WEBSOCKET_QUERY_RE.sub(r"\1\2", str(message))
    return _SENSITIVE_QUERY_PARAM_RE.sub(r"\1<redacted>", sanitized)


def intercept_standard_logging():
    """Redirect standard library logging to loguru."""
    class InterceptHandler(logging.Handler):
        def emit(self, record):
            # Get corresponding loguru level
            try:
                level = _logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            # Find the caller's frame
            frame, depth = logging.currentframe(), 2
            while frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            _logger.opt(depth=depth, exception=record.exc_info).log(level, sanitize_standard_log_message(record.getMessage()))

    # Replace root standard logger handler. Child loggers should still
    # propagate so pytest caplog and other logging observers can attach at root.
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in logging.root.manager.loggerDict:
        child_logger = logging.getLogger(name)
        child_logger.handlers = []
        child_logger.propagate = True

    # Suppress per-request success logs from chat providers and channel polling.
    # We still keep warnings/errors from these clients.
    for name in tuple(logging.root.manager.loggerDict) + NOISY_SUCCESS_LOGGER_PREFIXES:
        if not any(name == prefix or name.startswith(f"{prefix}.") for prefix in NOISY_SUCCESS_LOGGER_PREFIXES):
            continue
        logging.getLogger(name).setLevel(logging.WARNING)


# Configure on import
logger = configure_logging()
