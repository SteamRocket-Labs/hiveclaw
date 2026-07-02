"""C2 — app-side timing: Server-Timing response header + slow-request warning.

The Server-Timing header lets a public-network probe split total latency into
in-app duration vs edge/network transit — the L1 attribution instrument from
docs/performance-slimming-plan-2026-07-02.md.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware import TraceIdMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(0.05)
        return {"ok": True}

    return app


def test_server_timing_header_present() -> None:
    client = TestClient(_build_app())

    response = client.get("/fast")

    assert response.status_code == 200
    header = response.headers.get("server-timing", "")
    assert header.startswith("app;dur=")
    assert float(header.split("=", 1)[1]) >= 0.0


def test_slow_request_logs_structured_warning(monkeypatch) -> None:
    from loguru import logger

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "SLOW_REQUEST_WARN_SECONDS", 0.01)

    records: list[str] = []
    sink_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        client = TestClient(_build_app())
        client.get("/slow")
    finally:
        logger.remove(sink_id)

    slow_warnings = [r for r in records if "slow_request" in r and "/slow" in r and "duration_ms=" in r]
    assert slow_warnings, f"expected slow_request warning, got: {records}"


def test_fast_request_does_not_warn() -> None:
    from loguru import logger

    records: list[str] = []
    sink_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        client = TestClient(_build_app())
        client.get("/fast")
    finally:
        logger.remove(sink_id)

    assert not [r for r in records if "slow_request" in r]
