from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

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


def test_card_action_callback_is_scheduled_from_dispatcher(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    scheduled: dict[str, object] = {}
    handlers: dict[str, object] = {}

    class _FakeBuilder:
        def register_p2_customized_event(self, name, handler):
            handlers[name] = handler
            return self

        def build(self):
            return handlers

    class _FakeDispatcherHandler:
        @staticmethod
        def builder(*_args, **_kwargs):
            return _FakeBuilder()

    monkeypatch.setattr(feishu_ws, "lark", SimpleNamespace(EventDispatcherHandler=_FakeDispatcherHandler))

    def fake_run_coroutine_threadsafe(coro, loop):
        scheduled["coro"] = coro
        scheduled["loop"] = loop
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(feishu_ws.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)
    monkeypatch.setattr(feishu_ws.asyncio, "get_running_loop", lambda: "loop-token")

    manager = feishu_ws.FeishuWSManager()
    dispatcher = manager._create_event_handler(uuid4())

    assert "card.action.trigger" in dispatcher
    dispatcher["card.action.trigger"]({"event": {"operator": {"open_id": "ou_test"}}})

    assert scheduled["loop"] == "loop-token"
    assert scheduled["coro"] is not None


def test_message_read_event_is_acknowledged_without_scheduling(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    handlers: dict[str, object] = {}

    class _FakeBuilder:
        def register_p2_customized_event(self, name, handler):
            handlers[name] = handler
            return self

        def build(self):
            return handlers

    class _FakeDispatcherHandler:
        @staticmethod
        def builder(*_args, **_kwargs):
            return _FakeBuilder()

    monkeypatch.setattr(feishu_ws, "lark", SimpleNamespace(EventDispatcherHandler=_FakeDispatcherHandler))

    scheduled: list[object] = []

    def fake_create_task(coro):
        scheduled.append(coro)

    monkeypatch.setattr(feishu_ws.asyncio, "create_task", fake_create_task)

    manager = feishu_ws.FeishuWSManager()
    dispatcher = manager._create_event_handler(uuid4())

    assert "im.message.message_read_v1" in dispatcher
    dispatcher["im.message.message_read_v1"]({"event": {}})

    assert scheduled == []


def test_receive_event_with_raw_body_is_scheduled(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    handlers: dict[str, object] = {}

    class _FakeBuilder:
        def register_p2_customized_event(self, name, handler):
            handlers[name] = handler
            return self

        def build(self):
            return handlers

    class _FakeDispatcherHandler:
        @staticmethod
        def builder(*_args, **_kwargs):
            return _FakeBuilder()

    scheduled: list[object] = []

    class _FakeLoop:
        def create_task(self, coro):
            scheduled.append(coro)
            coro.close()

    monkeypatch.setattr(feishu_ws, "lark", SimpleNamespace(EventDispatcherHandler=_FakeDispatcherHandler))
    monkeypatch.setattr(feishu_ws.asyncio, "get_running_loop", lambda: _FakeLoop())

    manager = feishu_ws.FeishuWSManager()
    dispatcher = manager._create_event_handler(uuid4())
    event = SimpleNamespace(
        raw_body=json.dumps(
            {
                "header": {"event_type": "im.message.receive_v1"},
                "event": {"message": {"message_type": "text"}},
            }
        ).encode("utf-8")
    )

    dispatcher["im.message.receive_v1"](event)

    assert scheduled
