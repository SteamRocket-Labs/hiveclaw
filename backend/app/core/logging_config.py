"""Centralized logging configuration using loguru."""

import sys
import logging
from contextvars import ContextVar

from loguru import logger as _logger

# Context variable for trace ID
from uuid import uuid4

trace_id_var: ContextVar[str] = ContextVar("trace_id", default=None)
NOISY_SUCCESS_LOGGER_PREFIXES = ("httpx", "httpcore", "uvicorn.access")


def get_trace_id() -> str:
    """Get current trace ID from context."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set trace ID in context."""
    trace_id_var.set(trace_id)


def configure_logging():
    """Configure loguru with custom format including trace ID."""
    # Remove default handler
    _logger.remove()

    # Add stdout handler with custom format and filter to ensure trace_id exists
    _logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[trace_id]:-<12}</cyan> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        enqueue=True,
        backtrace=True,
        diagnose=True,
        filter=lambda record: (record["extra"].setdefault("trace_id", get_trace_id() or str(uuid4())) is not None)
    )

    return _logger


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

            _logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

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
