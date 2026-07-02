"""A2 — MB-scale responses must not be serialized by the stdlib json encoder."""

from __future__ import annotations


def test_app_uses_orjson_default_response_class() -> None:
    from fastapi.responses import ORJSONResponse

    from app.main import app

    response_class = app.router.default_response_class
    resolved = getattr(response_class, "value", response_class)
    assert resolved is ORJSONResponse
