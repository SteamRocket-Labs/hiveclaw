from __future__ import annotations

from types import SimpleNamespace

import pytest


class _FakeAwaitableConnection:
    def __init__(self, record: dict):
        self._record = record
        self.closed = False

    def __await__(self):
        async def _inner():
            return self

        return _inner().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_make_no_proxy_connect_scopes_override_and_restores(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    calls: list[dict] = []

    def original_connect(*args, **kwargs):
        calls.append(kwargs.copy())
        return _FakeAwaitableConnection(kwargs)

    fake_websockets = SimpleNamespace(connect=original_connect)
    monkeypatch.setattr(feishu_ws, "_websockets", fake_websockets, raising=False)
    monkeypatch.setattr(feishu_ws, "_PROXY_PATCH_AVAILABLE", True, raising=False)

    scoped_ctx = feishu_ws._make_no_proxy_connect(original_connect)

    assert fake_websockets.connect is original_connect

    async with scoped_ctx():
        assert fake_websockets.connect is not original_connect
        async with fake_websockets.connect("wss://example.com/socket"):
            pass

    assert fake_websockets.connect is original_connect
    assert calls == [{"proxy": None}]
