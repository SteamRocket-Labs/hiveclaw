"""Tests for the unversioned Prometheus metrics endpoint."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_prometheus_metrics_endpoint_returns_text_plain():
    from app.api.metrics import router
    from app.memory.metrics import record_extract_task_failure, reset_all
    from app.services.office_preview_metrics import (
        record_office_preview,
        reset_office_preview_metrics,
    )

    reset_all()
    reset_office_preview_metrics()
    record_extract_task_failure("web", "RuntimeError")
    record_office_preview(
        source_kind="artifact_snapshot",
        preview_mode="html",
        status="success",
        office_format="docx",
        duration_seconds=0.25,
        output_bytes=1024,
        cache_hit=True,
        error_code=None,
    )

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "hive_memory_extract_task_failure_total" in response.text
    assert (
        'office_preview_requests_total{source_kind="artifact_snapshot",preview_mode="html",status="success"} 1'
        in response.text
    )
    assert 'office_preview_cache_hits_total{source_kind="artifact_snapshot"} 1' in response.text
    assert 'office_preview_render_seconds_count{format="docx",preview_mode="html"} 1' in response.text
    assert 'office_preview_output_bytes_sum{format="docx",preview_mode="html"} 1024' in response.text


def test_prometheus_metrics_router_is_registered_without_api_prefix():
    source = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()

    assert "from app.api.metrics import router as metrics_router" in source
    assert "app.include_router(metrics_router)" in source
    assert "_api_routers.append(metrics_router)" not in source
