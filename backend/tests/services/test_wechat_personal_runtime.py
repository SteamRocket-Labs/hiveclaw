from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SequenceSession:
    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


class _AllRowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SingleExecuteSession:
    def __init__(self, result):
        self.result = result
        self.flushes = 0

    async def execute(self, _stmt):
        return self.result

    async def flush(self):
        self.flushes += 1


@pytest.mark.asyncio
async def test_disconnect_duplicate_wechat_account_bindings_keeps_only_target_agent_connected():
    from app.services.wechat_personal_service import disconnect_duplicate_account_bindings

    tenant_id = uuid4()
    target_agent_id = uuid4()
    stale_agent_id = uuid4()
    other_tenant_agent_id = uuid4()
    unrelated_agent_id = uuid4()

    stale = SimpleNamespace(
        agent_id=stale_agent_id,
        tenant_id=tenant_id,
        is_connected=True,
        extra_config={"ilink_bot_id": "bot-1", "ilink_user_id": "wx-owner"},
    )
    legacy_null_tenant = SimpleNamespace(
        agent_id=uuid4(),
        tenant_id=None,
        is_connected=True,
        extra_config={"ilink_bot_id": "bot-1", "ilink_user_id": "wx-owner"},
    )
    current = SimpleNamespace(
        agent_id=target_agent_id,
        tenant_id=tenant_id,
        is_connected=True,
        extra_config={"ilink_bot_id": "bot-1", "ilink_user_id": "wx-owner"},
    )
    other_tenant = SimpleNamespace(
        agent_id=other_tenant_agent_id,
        tenant_id=uuid4(),
        is_connected=True,
        extra_config={"ilink_bot_id": "bot-1", "ilink_user_id": "wx-owner"},
    )
    unrelated = SimpleNamespace(
        agent_id=unrelated_agent_id,
        tenant_id=tenant_id,
        is_connected=True,
        extra_config={"ilink_bot_id": "bot-2", "ilink_user_id": "wx-other"},
    )
    db = _SingleExecuteSession(_AllRowsResult([stale, legacy_null_tenant, current, other_tenant, unrelated]))

    stale_agent_ids = await disconnect_duplicate_account_bindings(
        db=db,
        agent_id=target_agent_id,
        account_id="bot-1",
        user_id="wx-owner",
        tenant_id=tenant_id,
    )

    assert stale_agent_ids == [stale_agent_id, legacy_null_tenant.agent_id]
    assert stale.is_connected is False
    assert stale.extra_config == {}
    assert legacy_null_tenant.is_connected is False
    assert legacy_null_tenant.extra_config == {}
    assert current.is_connected is True
    assert other_tenant.is_connected is True
    assert unrelated.is_connected is True
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_connect_wechat_replaces_stale_account_streams(monkeypatch):
    import app.api.wechat_personal as wechat_api
    import app.services.wechat_personal_stream as stream_mod

    agent_id = uuid4()
    stale_agent_id = uuid4()
    tenant_id = uuid4()
    calls: list[tuple] = []
    captured: dict[str, object] = {}

    class _CommitDB:
        async def commit(self):
            calls.append(("commit",))

    class _StreamManager:
        async def stop_client(self, stopped_agent_id):
            calls.append(("stop", stopped_agent_id))

        async def start_client(self, **kwargs):
            calls.append(("start", kwargs["agent_id"], kwargs["bot_token"], kwargs["base_url"]))

    async def fake_require_manage(_db, current_user, checked_agent_id):
        assert checked_agent_id == agent_id
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id, owner_user_id=current_user.id)

    async def fake_retrieve_confirmed_credentials(session_key):
        assert session_key == "session-1"
        return {
            "account_id": "bot-1",
            "bot_token": "token-1",
            "base_url": "https://ilink.example",
            "user_id": "wx-owner",
        }

    async def fake_disconnect_duplicate_account_bindings(**kwargs):
        captured["duplicate_kwargs"] = kwargs
        return [stale_agent_id]

    async def fake_connect_channel(**kwargs):
        captured["connect_kwargs"] = kwargs
        return SimpleNamespace(extra_config={"ilink_bot_token": "encrypted"})

    monkeypatch.setattr(wechat_api, "require_agent_manage_access", fake_require_manage)
    monkeypatch.setattr(wechat_api, "retrieve_confirmed_credentials", fake_retrieve_confirmed_credentials)
    monkeypatch.setattr(
        wechat_api,
        "disconnect_duplicate_account_bindings",
        fake_disconnect_duplicate_account_bindings,
        raising=False,
    )
    monkeypatch.setattr(wechat_api, "connect_channel", fake_connect_channel)
    monkeypatch.setattr(
        wechat_api,
        "get_channel_credentials",
        lambda _config: {"bot_token": "token-1", "base_url": "https://ilink.example"},
    )
    monkeypatch.setattr(stream_mod, "wechat_personal_stream_manager", _StreamManager())

    result = await wechat_api.connect(
        agent_id=agent_id,
        body=wechat_api.ConnectRequest(session_key="session-1"),
        current_user=SimpleNamespace(id=uuid4()),
        db=_CommitDB(),
    )

    assert result.connected is True
    assert captured["duplicate_kwargs"]["agent_id"] == agent_id
    assert captured["duplicate_kwargs"]["account_id"] == "bot-1"
    assert captured["duplicate_kwargs"]["user_id"] == "wx-owner"
    assert captured["duplicate_kwargs"]["tenant_id"] == tenant_id
    assert captured["connect_kwargs"]["tenant_id"] == tenant_id
    assert calls == [
        ("commit",),
        ("stop", stale_agent_id),
        ("start", agent_id, "token-1", "https://ilink.example"),
    ]


@pytest.mark.asyncio
async def test_process_wechat_message_sets_sender_scoped_identity_and_session_contract(monkeypatch):
    import app.services.wechat_personal_stream as wechat_stream
    from app.core.execution_context import clear_execution_identity
    from app.services.channel_delivery_service import channel_delivery_target

    agent_id = uuid4()
    tenant_id = uuid4()
    platform_user_id = uuid4()
    creator_id = uuid4()
    session_id = uuid4()
    captured: dict[str, object] = {}

    db = _SequenceSession(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id, creator_id=creator_id)),
            _ScalarResult(
                SimpleNamespace(id=platform_user_id, username="wechat_wxid_abc", display_name="WeChat wxid_abc")
            ),
            _RowsResult([]),
        ]
    )

    async def fake_find_or_create_channel_session(*, delivery_target=None, external_conv_id=None, **_kwargs):
        captured["external_conv_id"] = external_conv_id
        captured["session_delivery_target"] = dict(delivery_target or {})
        return SimpleNamespace(id=session_id, last_message_at=None, delivery_target_json=delivery_target)

    async def fake_call_agent_llm(_db, _agent_id, _user_text, **kwargs):
        from app.core.execution_context import get_execution_identity

        captured["llm_kwargs"] = kwargs
        captured["runtime_delivery_target"] = channel_delivery_target.get()
        captured["execution_identity"] = get_execution_identity()
        return "wechat reply"

    async def fake_compute_history_limit_for_agent(_agent_id):
        return 10

    async def fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr("app.database.async_session", lambda: db)
    monkeypatch.setattr("app.services.wechat_personal_stream.tenant_scoped_session", lambda *a, **k: db)
    monkeypatch.setattr("app.services.wechat_personal_stream.resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(
        "app.services.memory_service.compute_history_limit_for_agent", fake_compute_history_limit_for_agent
    )
    monkeypatch.setattr(
        "app.services.channel_session.find_or_create_channel_session", fake_find_or_create_channel_session
    )
    monkeypatch.setattr("app.services.channel_agent_runtime.call_agent_llm", fake_call_agent_llm)

    clear_execution_identity()
    token = channel_delivery_target.set(
        {
            "channel": "wechat_personal",
            "to_user_id": "wxid_abc",
            "context_token": "ctx-1",
            "user_label": "WeChat wxid_abc",
        }
    )
    try:
        reply = await wechat_stream._process_wechat_message(
            agent_id=agent_id,
            sender_id="wxid_abc",
            user_text="你好",
            delivery_target={
                "channel": "wechat_personal",
                "to_user_id": "wxid_abc",
                "context_token": "ctx-1",
                "user_label": "WeChat wxid_abc",
            },
        )
    finally:
        channel_delivery_target.reset(token)

    assert reply == "wechat reply"
    assert captured["external_conv_id"] == "wechat_p2p_wxid_abc"
    assert captured["session_delivery_target"] == {
        "channel": "wechat_personal",
        "to_user_id": "wxid_abc",
        "context_token": "ctx-1",
        "user_label": "WeChat wxid_abc",
    }
    assert captured["runtime_delivery_target"] == {
        "channel": "wechat_personal",
        "to_user_id": "wxid_abc",
        "context_token": "ctx-1",
        "user_label": "WeChat wxid_abc",
        "session_id": str(session_id),
    }
    assert captured["llm_kwargs"]["session_id"] == str(session_id)
    assert captured["llm_kwargs"]["session_source"] == "wechat_personal"
    assert captured["llm_kwargs"]["session_channel"] == "wechat_personal"
    assert captured["llm_kwargs"]["allow_bare_plan_confirmation"] is True
    assert captured["execution_identity"].identity_type == "delegated_user"
    assert captured["execution_identity"].identity_id == platform_user_id
    assert captured["execution_identity"].label == "WeChat wxid_abc via wechat_personal"
    assert captured["execution_identity"].identity_id != creator_id


@pytest.mark.asyncio
async def test_handle_wechat_file_message_persists_upload_and_passes_workspace_path(
    monkeypatch,
    tmp_path,
):
    import app.services.wechat_personal_stream as wechat_stream
    from app.services.wechat_ilink_client import InboundMessage, MediaRef

    agent_id = uuid4()
    captured: dict[str, object] = {}
    sent_replies: list[str] = []

    class _FakeILinkClient:
        def __init__(self, base_url):
            captured.setdefault("base_urls", []).append(base_url)

        async def download_media(self, media_ref):
            assert media_ref.encrypt_query_param == "encrypted-file"
            return b"quarterly report bytes"

        async def get_config(self, *_args, **_kwargs):
            return SimpleNamespace(typing_ticket="")

        async def send_message(self, *, text, **_kwargs):
            sent_replies.append(text)

    async def fake_store_context_token(*_args, **_kwargs):
        captured["stored_context_token"] = True

    async def fake_get_typing_ticket(*_args, **_kwargs):
        return None

    async def fake_get_context_token(*_args, **_kwargs):
        return None

    async def fake_process_wechat_message(*, user_text, delivery_target, **_kwargs):
        captured["user_text"] = user_text
        captured["delivery_target"] = dict(delivery_target)
        return "已收到文件"

    monkeypatch.setattr(
        wechat_stream,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )
    monkeypatch.setattr(wechat_stream, "ILinkClient", _FakeILinkClient)
    monkeypatch.setattr(wechat_stream, "store_context_token", fake_store_context_token)
    monkeypatch.setattr("app.services.wechat_personal_service.get_typing_ticket", fake_get_typing_ticket)
    monkeypatch.setattr(wechat_stream, "get_context_token", fake_get_context_token)
    monkeypatch.setattr(wechat_stream, "_enqueue_wechat_personal_message", fake_process_wechat_message)

    msg = InboundMessage(
        seq=1,
        message_id=2,
        from_user_id="wxid_sender",
        to_user_id="wxid_bot",
        session_id="session-1",
        create_time_ms=1,
        context_token="ctx-1",
        message_type=4,
        file_name="quarterly-report.pdf",
        file_media=MediaRef(encrypt_query_param="encrypted-file", aes_key="YWVzLWtleQ=="),
    )

    await wechat_stream.WeChatPersonalStreamManager()._handle_message(
        agent_id=agent_id,
        bot_token="bot-token",
        base_url="https://ilink.example",
        msg=msg,
    )

    saved_path = tmp_path / str(agent_id) / "workspace" / "uploads" / "quarterly-report.pdf"
    assert saved_path.read_bytes() == b"quarterly report bytes"
    assert "workspace/uploads/quarterly-report.pdf" in captured["user_text"]
    assert captured["delivery_target"]["channel"] == "wechat_personal"
    assert captured["delivery_target"]["to_user_id"] == "wxid_sender"
    assert captured["stored_context_token"] is True
    assert sent_replies == ["已收到文件"]


@pytest.mark.asyncio
async def test_handle_wechat_image_message_adds_vision_marker(monkeypatch, tmp_path):
    import base64

    import app.services.wechat_personal_stream as wechat_stream
    from app.services.wechat_ilink_client import InboundMessage, MediaRef

    agent_id = uuid4()
    png_bytes = b"\x89PNG\r\n\x1a\nimage bytes"
    captured: dict[str, object] = {}

    class _FakeILinkClient:
        def __init__(self, _base_url):
            pass

        async def download_media(self, media_ref):
            assert media_ref.encrypt_query_param == "encrypted-image"
            return png_bytes

        async def get_config(self, *_args, **_kwargs):
            return SimpleNamespace(typing_ticket="")

        async def send_message(self, **_kwargs):
            pass

    async def fake_noop(*_args, **_kwargs):
        return None

    async def fake_process_wechat_message(*, user_text, **_kwargs):
        captured["user_text"] = user_text
        return "图片已处理"

    monkeypatch.setattr(
        wechat_stream,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )
    monkeypatch.setattr(wechat_stream, "ILinkClient", _FakeILinkClient)
    monkeypatch.setattr(wechat_stream, "store_context_token", fake_noop)
    monkeypatch.setattr("app.services.wechat_personal_service.get_typing_ticket", fake_noop)
    monkeypatch.setattr(wechat_stream, "get_context_token", fake_noop)
    monkeypatch.setattr(wechat_stream, "_enqueue_wechat_personal_message", fake_process_wechat_message)

    msg = InboundMessage(
        seq=1,
        message_id=2,
        from_user_id="wxid_sender",
        to_user_id="wxid_bot",
        session_id="session-1",
        create_time_ms=1,
        context_token="ctx-1",
        message_type=2,
        image_media=MediaRef(encrypt_query_param="encrypted-image", aes_key="YWVzLWtleQ=="),
    )

    await wechat_stream.WeChatPersonalStreamManager()._handle_message(
        agent_id=agent_id,
        bot_token="bot-token",
        base_url="https://ilink.example",
        msg=msg,
    )

    upload_dir = tmp_path / str(agent_id) / "workspace" / "uploads"
    saved_images = list(upload_dir.glob("wechat_image_*.jpg"))
    assert len(saved_images) == 1
    assert saved_images[0].read_bytes() == png_bytes
    expected_marker = f"[image_data:data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}]"
    assert expected_marker in captured["user_text"]
