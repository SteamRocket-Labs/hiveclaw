from __future__ import annotations

import asyncio
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

    def original_connect(*args, proxy=True, **kwargs):
        calls.append({"proxy": proxy, **kwargs})
        return _FakeAwaitableConnection({"proxy": proxy, **kwargs})

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


@pytest.mark.asyncio
async def test_make_no_proxy_connect_skips_proxy_when_connect_does_not_support_it(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    calls: list[str] = []

    def original_connect(uri):
        calls.append(uri)
        return _FakeAwaitableConnection({})

    fake_websockets = SimpleNamespace(connect=original_connect)
    monkeypatch.setattr(feishu_ws, "_websockets", fake_websockets, raising=False)
    monkeypatch.setattr(feishu_ws, "_PROXY_PATCH_AVAILABLE", True, raising=False)

    scoped_ctx = feishu_ws._make_no_proxy_connect(original_connect)

    async with scoped_ctx():
        async with fake_websockets.connect("wss://example.com/socket"):
            pass

    assert fake_websockets.connect is original_connect
    assert calls == ["wss://example.com/socket"]


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


@pytest.mark.asyncio
async def test_start_client_keeps_client_registered_after_transient_connect_failure(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    agent_id = uuid4()
    scheduled: list[object] = []
    sleep_calls: list[int] = []

    class _FakeClient:
        def __init__(self):
            self.connect_calls = 0
            self.disconnect_calls = 0

        async def _connect(self):
            self.connect_calls += 1
            raise TimeoutError

        async def _disconnect(self):
            self.disconnect_calls += 1

        async def _ping_loop(self):
            return None

    fake_client = _FakeClient()

    def fake_create_task(coro, name=None):
        scheduled.append(coro)
        return SimpleNamespace(done=lambda: False, cancel=lambda: None, name=name)

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        raise asyncio.CancelledError

    manager = feishu_ws.FeishuWSManager()
    monkeypatch.setattr(manager, "_create_event_handler", lambda _agent_id: object())
    monkeypatch.setattr(feishu_ws, "_PROXY_PATCH_AVAILABLE", False, raising=False)
    monkeypatch.setattr(feishu_ws, "lark", SimpleNamespace(LogLevel=SimpleNamespace(INFO="info")))
    monkeypatch.setattr(feishu_ws, "ws", SimpleNamespace(Client=lambda *_args, **_kwargs: fake_client))
    monkeypatch.setattr(feishu_ws.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(feishu_ws.asyncio, "sleep", fake_sleep)

    await manager.start_client(agent_id, "app_id", "app_secret")

    await scheduled[0]

    assert manager._clients[agent_id] is fake_client
    assert fake_client.connect_calls == 1
    assert fake_client.disconnect_calls >= 1
    assert sleep_calls == [5]


@pytest.mark.asyncio
async def test_start_client_does_not_retry_invalid_feishu_credentials(monkeypatch):
    import app.services.feishu_ws as feishu_ws

    agent_id = uuid4()
    scheduled: list[object] = []
    sleep_calls: list[int] = []

    class _FakeClientException(Exception):
        code = 1000040345

        def __str__(self):
            return "1000040345: app_id or app_secret is invalid"

    class _FakeClient:
        def __init__(self):
            self.disconnect_calls = 0

        async def _connect(self):
            raise _FakeClientException

        async def _disconnect(self):
            self.disconnect_calls += 1

        async def _ping_loop(self):
            return None

    fake_client = _FakeClient()

    def fake_create_task(coro, name=None):
        scheduled.append(coro)
        return SimpleNamespace(done=lambda: False, cancel=lambda: None, name=name)

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        raise asyncio.CancelledError

    manager = feishu_ws.FeishuWSManager()
    monkeypatch.setattr(manager, "_create_event_handler", lambda _agent_id: object())
    monkeypatch.setattr(feishu_ws, "_PROXY_PATCH_AVAILABLE", False, raising=False)
    monkeypatch.setattr(feishu_ws, "lark", SimpleNamespace(LogLevel=SimpleNamespace(INFO="info")))
    monkeypatch.setattr(feishu_ws, "ws", SimpleNamespace(Client=lambda *_args, **_kwargs: fake_client))
    monkeypatch.setattr(feishu_ws.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(feishu_ws.asyncio, "sleep", fake_sleep)

    await manager.start_client(agent_id, "app_id", "app_secret")
    await scheduled[0]

    assert agent_id not in manager._clients
    assert fake_client.disconnect_calls == 1
    assert sleep_calls == []
