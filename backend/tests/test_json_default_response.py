"""FastAPI must use its native direct-byte JSON serialization path."""

from __future__ import annotations


def test_app_uses_native_json_default_response_class() -> None:
    from fastapi.responses import JSONResponse

    from app.main import app

    response_class = app.router.default_response_class
    resolved = getattr(response_class, "value", response_class)
    assert resolved is JSONResponse
