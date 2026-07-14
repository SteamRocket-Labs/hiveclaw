"""Prometheus metrics endpoint."""

from fastapi import APIRouter, Response

from app.memory.metrics import render_prometheus as render_core_prometheus
from app.services.office_preview_metrics import render_office_preview_prometheus

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    return Response(
        content=render_core_prometheus().rstrip() + "\n" + render_office_preview_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
