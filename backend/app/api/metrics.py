"""Prometheus metrics endpoint."""

from fastapi import APIRouter, Response

from app.memory.metrics import render_prometheus

router = APIRouter(tags=["metrics"])


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    return Response(
        content=render_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
