"""Tests for the unversioned Prometheus metrics endpoint."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_prometheus_metrics_endpoint_returns_text_plain():
    from app.api.metrics import router
    from app.memory.metrics import record_extract_task_failure, reset_all

    reset_all()
    record_extract_task_failure("web", "RuntimeError")

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "hive_memory_extract_task_failure_total" in response.text


def test_prometheus_metrics_router_is_registered_without_api_prefix():
    source = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text()

    assert "from app.api.metrics import router as metrics_router" in source
    assert "app.include_router(metrics_router)" in source
    assert "_api_routers.append(metrics_router)" not in source
