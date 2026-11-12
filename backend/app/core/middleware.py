"""FastAPI middleware for request tracing and logging."""

import uuid
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.core.logging_config import set_trace_id
from app.core.request_timing import RequestTiming, install_cursor_timing, request_timing
from loguru import logger


class TraceIdMiddleware(BaseHTTPMiddleware):
    """Middleware to inject trace ID into request context and log requests."""

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        install_cursor_timing()

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate or extract trace ID from header
        trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())[:12]
        set_trace_id(trace_id)

        # Add trace ID to request state for access in endpoints
        request.state.trace_id = trace_id

        start_time = time.perf_counter()
        timing = RequestTiming()
        timing_token = request_timing.set(timing)

        # Log request
        client_host = request.client.host if request.client else "-"
        logger.info(f"--> {request.method} {request.url.path} [client: {client_host}]")

        try:
            response = await call_next(request)
            duration = time.perf_counter() - start_time
            timing.active = False

            # Add trace ID to response headers
            response.headers["X-Trace-Id"] = trace_id
            # In-app duration for public-probe attribution: total minus this
            # header value is edge/network transit.
            response.headers["Server-Timing"] = timing.header(duration)

            slow_threshold = get_settings().SLOW_REQUEST_WARN_SECONDS
            if duration >= slow_threshold:
                logger.warning(
                    f"slow_request method={request.method} path={request.url.path} "
                    f"status={response.status_code} duration_ms={duration * 1000:.0f} "
                    f"threshold_s={slow_threshold} "
                    f"db_ms={timing.db_seconds * 1000:.1f} statements={timing.statements} "
                    f"pool_ms={timing.pool_seconds * 1000:.1f}"
                )

            # Log response
            logger.info(f"<-- {request.method} {request.url.path} {response.status_code} {duration:.3f}s")

            return response

        except Exception as exc:
            duration = time.perf_counter() - start_time
            logger.error(f"<-- {request.method} {request.url.path} ERROR {duration:.3f}s - {exc}")
            raise
        finally:
            timing.active = False
            request_timing.reset(timing_token)
