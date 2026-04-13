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
