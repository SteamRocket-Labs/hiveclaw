from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest


class _ToolResult:
    def __init__(self, tool):
        self._tool = tool

    def scalar_one_or_none(self):
        return self._tool


class _FakeSession:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def execute(self, _stmt):
        self.calls.append(_stmt)
        return _ToolResult(SimpleNamespace(config={"modelscope_api_token": "modelscope-token"}))


@pytest.mark.asyncio
async def test_modelscope_token_lookup_is_cached(monkeypatch):
    import app.services.resource_discovery as resource_discovery

    if hasattr(resource_discovery, "clear_provider_config_cache"):
        resource_discovery.clear_provider_config_cache()

    calls = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(db, **_kwargs):
        yield db

    monkeypatch.setattr(resource_discovery, "async_session", lambda: _FakeSession(calls))
    monkeypatch.setattr(resource_discovery, "enter_rls_bypass", fake_enter_rls_bypass)

    assert await resource_discovery._get_modelscope_api_token() == "modelscope-token"
    assert await resource_discovery._get_modelscope_api_token() == "modelscope-token"
    assert len(calls) == 1
