"""C2 — app-side timing: Server-Timing response header + slow-request warning.

The Server-Timing header lets a public-network probe split total latency into
in-app duration vs edge/network transit — the L1 attribution instrument from
docs/performance-slimming-plan-2026-07-02.md.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from app.core import request_timing as rt
from app.core.middleware import TraceIdMiddleware
from tests.test_run_timing_phase_sites import finish_statement, serialized_log_sink  # noqa: F401


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
    # The header now includes db and pool metrics, so validate the entire value.
    match = re.fullmatch(r'app;dur=(\d+\.\d), db;dur=(\d+\.\d);desc="(\d+)", pool;dur=(\d+\.\d)', header)
    assert match, header
    assert float(match.group(1)) >= 0.0


def test_slow_request_logs_structured_warning(monkeypatch) -> None:
    from loguru import logger

    from app.config import get_settings
    from app.core.request_timing import RequestTiming

    monkeypatch.setattr(get_settings(), "SLOW_REQUEST_WARN_SECONDS", 0.01)
    monkeypatch.setattr("app.core.middleware.RequestTiming",
        lambda: RequestTiming(db_seconds=0.0123, statements=3, pool_seconds=0.0057))

    records: list[str] = []
    sink_id = logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        client = TestClient(_build_app())
        response = client.get("/slow")
    finally:
        logger.remove(sink_id)

    slow_warnings = [r for r in records if "slow_request" in r and "/slow" in r and "duration_ms=" in r]
    assert slow_warnings, f"expected slow_request warning, got: {records}"
    assert len(slow_warnings) == 1
    assert "db_ms=12.3 statements=3 pool_ms=5.7" in slow_warnings[0]
    assert 'db;dur=12.3;desc="3"' in response.headers["server-timing"]
    assert response.headers["server-timing"].endswith("pool;dur=5.7")


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


@pytest.mark.parametrize("template", [
    "/api/agents/{agent_id}/sessions/runs",
    "/api/v1/agents/{agent_id}/sessions/{session_id}/runs",
])
def test_send_request_logs_statement_sites_after_the_response(template, monkeypatch, serialized_log_sink):  # noqa: F811
    signature = "app.db:load:12 < app.service:read:34 < app.api.chat_sessions:send:56"
    frame_limits = []

    def application_frame(limit):
        frame_limits.append(limit)
        return signature

    monkeypatch.setattr(rt, "_application_frame", application_frame)
    app = _build_app()

    @rt.timed_phase("hook_dispatch")
    async def admission():
        finish_statement()
        finish_statement()

    async def send():
        finish_statement()
        await admission()
        return {"accepted": True}

    app.post(template)(send)
    app.post("/unrelated/{agent_id}/runs")(send)
    final_body_seen = False

    async def observed(scope, receive, send_message):
        async def capture(message):
            nonlocal final_body_seen
            if message["type"] == "http.response.body" and not message.get("more_body"):
                logger.complete()
                assert "request_phase_sites " not in serialized_log_sink.getvalue()
                final_body_seen = True
            await send_message(message)

        await app(scope, receive, capture)

    url = template.format(agent_id="private-agent", session_id="private-session")
    response = TestClient(observed).post(url, headers={"X-Trace-Id": "private-client-trace"})
    assert response.status_code == 200
    assert final_body_seen
    header = re.fullmatch(
        r'app;dur=\d+\.\d, db;dur=\d+\.\d;desc="(\d+)", pool;dur=\d+\.\d',
        response.headers["server-timing"],
    )
    assert header and header[1] == "3"
    logger.complete()
    phase_lines = []
    for line in serialized_log_sink.getvalue().splitlines():
        message = json.loads(line)["record"]["message"]
        if message.startswith("request_phase_sites "):
            assert len(line.encode("utf-8")) + 1 < 15_000
            assert "private-client-trace" not in line
            phase_lines.append(json.loads(message.removeprefix("request_phase_sites ")))
    assert {line["phase"]: line["sites"][signature]["n"] for line in phase_lines} == {
        "request": 1, "hook_dispatch": 2,
    }
    assert sum(line["other_n"] + sum(site["n"] for site in line["sites"].values())
               for line in phase_lines) == int(header[1])
    for line in phase_lines:
        assert set(line) == {"path_template", "phase", "sites", "other_n", "other_db_ms"}
        assert line["path_template"] == template
        assert "private" not in json.dumps(line)
    assert frame_limits == [3, 3, 3]

    serialized_log_sink.truncate(0)
    serialized_log_sink.seek(0)
    unrelated = TestClient(app).post("/unrelated/private-agent/runs")
    assert 'desc="3"' in unrelated.headers["server-timing"]
    assert frame_limits == [3, 3, 3]
    logger.complete()
    assert "request_phase_sites " not in serialized_log_sink.getvalue()

    def failed_emission(_self):
        raise RuntimeError("private diagnostic failure")

    monkeypatch.setattr(rt._PhaseSites, "summaries", failed_emission)
    response = TestClient(app).post(url)
    assert response.status_code == 200
    assert 'desc="3"' in response.headers["server-timing"]
