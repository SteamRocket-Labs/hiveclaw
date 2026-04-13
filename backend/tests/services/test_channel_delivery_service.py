from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.channel_delivery_service import ChannelDeliveryService


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, config):
        self._config = config

    async def execute(self, _stmt):
        return _ScalarResult(self._config)


class _SequenceDB:
    def __init__(self, results):
        self._results = list(results)
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return _ScalarResult(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


class TestResolveCapabilities:
    def test_feishu_matrix(self) -> None:
        caps = ChannelDeliveryService.resolve_capabilities(
            "feishu",
            SimpleNamespace(channel_type="feishu", is_configured=True, is_connected=True, extra_config={}),
        )
        assert caps["official_api"] is True
        assert caps["connected"] is True
        assert caps["capabilities"]["live_text"] is True
        assert caps["capabilities"]["deferred_text"] is True
        assert caps["capabilities"]["on_message_by_name"] is True

    def test_telegram_matrix(self) -> None:
        caps = ChannelDeliveryService.resolve_capabilities(
            "telegram",
            SimpleNamespace(channel_type="telegram", is_configured=True, is_connected=True, extra_config={}),
        )
        assert caps["official_api"] is True
        assert caps["capabilities"]["inbound_file"] is True
        assert caps["capabilities"]["outbound_file"] is True
        assert caps["capabilities"]["deferred_file"] is True
        assert caps["capabilities"]["on_message_by_name"] is False

    def test_wechat_personal_matrix(self) -> None:
        caps = ChannelDeliveryService.resolve_capabilities(
            "wechat_personal",
            SimpleNamespace(channel_type="wechat_personal", is_configured=True, is_connected=True, extra_config={}),
        )
        assert caps["official_api"] is False
        assert caps["third_party_transport"] == "ilink"
        assert caps["capabilities"]["live_text"] is True
        assert caps["capabilities"]["deferred_text"] == "conditional"
        assert any("token" in item.lower() for item in caps["limitations"])

    def test_wecom_matrix(self) -> None:
        caps = ChannelDeliveryService.resolve_capabilities(
            "wecom",
            SimpleNamespace(channel_type="wecom", is_configured=True, is_connected=True, extra_config={}),
        )
        assert caps["official_api"] is True
        assert caps["connected"] is True
        assert caps["capabilities"]["live_text"] is True
        assert caps["capabilities"]["deferred_text"] is True
        assert caps["capabilities"]["deferred_file"] is False
        assert caps["capabilities"]["on_message_current_sender"] is True
        assert caps["capabilities"]["on_message_by_name"] is False

    def test_web_matrix(self) -> None:
        caps = ChannelDeliveryService.resolve_capabilities("web", None)
        assert caps["capabilities"]["live_text"] is True
        assert caps["capabilities"]["deferred_text"] is True
        assert caps["capabilities"]["on_message_current_sender"] is True
        assert caps["capabilities"]["on_message_by_name"] is False


class TestIdentityFromDeliveryTarget:
    def test_wecom_identity_uses_user_id(self) -> None:
        identity = ChannelDeliveryService.identity_from_delivery_target(
            {"channel": "wecom", "user_id": "zhangsan"}
        )
        assert identity == "wecom:zhangsan"

    def test_web_identity_uses_username(self) -> None:
        identity = ChannelDeliveryService.identity_from_delivery_target(
            {"channel": "web", "username": "alice"}
        )
        assert identity == "web:alice"


class TestSendText:
    @pytest.mark.asyncio
    async def test_send_text_telegram_uses_persisted_chat_id(self, monkeypatch) -> None:
        import app.api.telegram as telegram_mod

        called: dict[str, object] = {}

        async def fake_send(bot_token: str, chat_id: int, text: str):
            called["bot_token"] = bot_token
            called["chat_id"] = chat_id
            called["text"] = text
            return {"ok": True}

        monkeypatch.setattr(telegram_mod, "_send_telegram_message", fake_send)

        config = SimpleNamespace(
            channel_type="telegram",
            app_secret="bot-token",
            app_id="telegram",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )
        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "telegram", "chat_id": 99887766, "sender_id": 123},
            text="hello from deferred",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert result.status == "success"
        assert called == {
            "bot_token": "bot-token",
            "chat_id": 99887766,
            "text": "hello from deferred",
        }

    @pytest.mark.asyncio
    async def test_send_text_wechat_without_context_token_is_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.services.wechat_personal_service.get_channel_credentials",
            lambda _config: {"base_url": "https://ilink.example", "client_id": "cid", "client_secret": "secret"},
        )

        config = SimpleNamespace(
            channel_type="wechat_personal",
            app_id="cid",
            app_secret="secret",
            is_configured=True,
            is_connected=True,
            extra_config={"base_url": "https://ilink.example"},
        )
        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "wechat_personal", "to_user_id": "wxid_abc"},
            text="hello",
            delivery_mode="deferred",
        )

        assert result.ok is False
        assert result.status == "unavailable"
        assert "token" in result.message.lower()

    @pytest.mark.asyncio
    async def test_send_text_wecom_uses_current_user_id(self, monkeypatch) -> None:
        import app.api.wecom as wecom_mod

        called: dict[str, object] = {}

        async def fake_send(*, corp_id: str, corp_secret: str, agent_id: str, to_user: str, text: str):
            called["corp_id"] = corp_id
            called["corp_secret"] = corp_secret
            called["agent_id"] = agent_id
            called["to_user"] = to_user
            called["text"] = text
            return {"errcode": 0}

        monkeypatch.setattr(wecom_mod, "_send_wecom_text_message", fake_send, raising=False)

        config = SimpleNamespace(
            channel_type="wecom",
            app_id="corp-id",
            app_secret="corp-secret",
            is_configured=True,
            is_connected=True,
            extra_config={"wecom_agent_id": "1000002"},
        )
        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "wecom", "user_id": "lisi", "user_label": "李四"},
            text="hello from wecom",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert result.status == "success"
        assert called == {
            "corp_id": "corp-id",
            "corp_secret": "corp-secret",
            "agent_id": "1000002",
            "to_user": "lisi",
            "text": "hello from wecom",
        }

    @pytest.mark.asyncio
    async def test_send_text_web_persists_message_and_pushes_websocket(self, monkeypatch) -> None:
        from app.models.chat_session import ChatSession

        agent_id = uuid4()
        target_user = SimpleNamespace(id=uuid4(), username="alice", display_name="Alice")
        existing_session = ChatSession(
            id=uuid4(),
            agent_id=agent_id,
            user_id=target_user.id,
            title="Existing",
            source_channel="web",
        )
        db = _SequenceDB([
            target_user,
            existing_session,
            None,  # apply_web_session_contract conflict lookup
            SimpleNamespace(id=agent_id, name="Web Agent"),
        ])
        pushed: list[dict] = []

        class _FakeWS:
            async def send_json(self, payload):
                pushed.append(payload)

        monkeypatch.setattr(
            "app.api.websocket.manager",
            SimpleNamespace(active_connections={str(agent_id): [(_FakeWS(), str(existing_session.id))]}),
        )

        result = await ChannelDeliveryService.send_text(
            db=db,
            agent_id=agent_id,
            reply_target={"channel": "web", "username": "alice", "user_label": "Alice"},
            text="hello from trigger",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert result.status == "success"
        assert result.channel == "web"
        assert existing_session.delivery_target_json == {
            "channel": "web",
            "username": "alice",
            "user_label": "Alice",
            "session_id": str(existing_session.id),
        }
        assert pushed == [
            {
                "type": "trigger_notification",
                "content": "hello from trigger",
                "triggers": ["web_message"],
                "agent_name": "Web Agent",
            }
        ]
        assert db.commits == 1
