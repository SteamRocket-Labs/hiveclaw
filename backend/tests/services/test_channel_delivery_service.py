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

    def test_microsoft_teams_matrix(self) -> None:
        caps = ChannelDeliveryService.resolve_capabilities(
            "microsoft_teams",
            SimpleNamespace(channel_type="microsoft_teams", is_configured=True, is_connected=True, extra_config={}),
        )
        assert caps["official_api"] is True
        assert caps["capabilities"]["live_text"] is True
        assert caps["capabilities"]["deferred_text"] is True
        assert caps["capabilities"]["on_message_current_sender"] is True
        assert caps["capabilities"]["on_message_by_name"] is False


class TestIdentityFromDeliveryTarget:
    def test_wecom_identity_uses_user_id(self) -> None:
        identity = ChannelDeliveryService.identity_from_delivery_target({"channel": "wecom", "user_id": "zhangsan"})
        assert identity == "wecom:zhangsan"

    def test_web_identity_uses_username(self) -> None:
        identity = ChannelDeliveryService.identity_from_delivery_target({"channel": "web", "username": "alice"})
        assert identity == "web:alice"

    def test_microsoft_teams_identity_uses_conversation_and_sender(self) -> None:
        identity = ChannelDeliveryService.identity_from_delivery_target(
            {"channel": "microsoft_teams", "conversation_id": "conv-1", "sender_id": "user-1"}
        )
        assert identity == "microsoft_teams:conv-1:user-1"


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
    async def test_send_text_slack_uses_channel_id(self, monkeypatch) -> None:
        import app.api.slack as slack_mod

        called: dict[str, object] = {}

        async def fake_send(bot_token: str, channel: str, text: str):
            called["bot_token"] = bot_token
            called["channel"] = channel
            called["text"] = text

        monkeypatch.setattr(slack_mod, "_send_slack_messages", fake_send)
        config = SimpleNamespace(
            channel_type="slack",
            app_secret="xoxb-token",
            app_id="slack",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )

        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "slack", "channel_id": "C123", "sender_id": "U123"},
            text="done",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert called == {"bot_token": "xoxb-token", "channel": "C123", "text": "done"}

    @pytest.mark.asyncio
    async def test_send_text_dingtalk_uses_session_webhook(self, monkeypatch) -> None:
        import httpx

        posted: dict[str, object] = {}

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, json):
                posted["url"] = url
                posted["json"] = json

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        config = SimpleNamespace(
            channel_type="dingtalk",
            app_secret="secret",
            app_id="app",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )

        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "dingtalk", "session_webhook": "https://oapi.dingtalk.com/robot/send"},
            text="done",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert posted["url"] == "https://oapi.dingtalk.com/robot/send"
        assert posted["json"]["msgtype"] == "markdown"
        assert posted["json"]["markdown"]["text"] == "done"

    @pytest.mark.asyncio
    async def test_send_text_discord_uses_interaction_followup(self, monkeypatch) -> None:
        import app.api.discord_bot as discord_mod

        called: dict[str, object] = {}

        async def fake_send(application_id: str, bot_token: str, interaction_token: str, text: str):
            called["application_id"] = application_id
            called["bot_token"] = bot_token
            called["interaction_token"] = interaction_token
            called["text"] = text

        monkeypatch.setattr(discord_mod, "_send_discord_followup", fake_send)
        config = SimpleNamespace(
            channel_type="discord",
            app_secret="bot-token",
            app_id="app-123",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )

        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "discord", "interaction_token": "interaction-token"},
            text="done",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert called == {
            "application_id": "app-123",
            "bot_token": "bot-token",
            "interaction_token": "interaction-token",
            "text": "done",
        }

    @pytest.mark.asyncio
    async def test_send_text_microsoft_teams_uses_saved_conversation(self, monkeypatch) -> None:
        import app.api.teams as teams_mod

        called: dict[str, object] = {}

        async def fake_send(config, conversation_id: str, activity: dict):
            called["config"] = config
            called["conversation_id"] = conversation_id
            called["activity"] = activity

        monkeypatch.setattr(teams_mod, "_send_teams_message", fake_send)
        config = SimpleNamespace(
            channel_type="microsoft_teams",
            app_secret="bot-secret",
            app_id="bot-app",
            is_configured=True,
            is_connected=True,
            extra_config={"service_url": "https://smba.trafficmanager.net/amer/"},
        )

        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={
                "channel": "microsoft_teams",
                "conversation_id": "conv-1",
                "reply_to_id": "activity-1",
                "recipient_id": "user-1",
                "recipient_name": "Ada",
                "bot_id": "bot-1",
            },
            text="done",
            delivery_mode="deferred",
        )

        assert result.ok is True
        assert called["config"] is config
        assert called["conversation_id"] == "conv-1"
        assert called["activity"] == {
            "type": "message",
            "from": {"id": "bot-1"},
            "conversation": {"id": "conv-1"},
            "recipient": {"id": "user-1", "name": "Ada"},
            "replyToId": "activity-1",
            "text": "done",
        }

    @pytest.mark.asyncio
    async def test_send_text_failure_log_includes_channel_and_error(self, monkeypatch) -> None:
        import app.api.telegram as telegram_mod
        import app.services.channel_delivery_service as delivery_mod

        async def fake_send(*_args, **_kwargs):
            raise RuntimeError("telegram down")

        logged: list[tuple[str, tuple[object, ...]]] = []

        def fake_warning(message: str, *args: object) -> None:
            logged.append((message, args))

        monkeypatch.setattr(telegram_mod, "_send_telegram_message", fake_send)
        monkeypatch.setattr(delivery_mod.logger, "warning", fake_warning)

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
            reply_target={"channel": "telegram", "chat_id": 99887766},
            text="hello from deferred",
            delivery_mode="deferred",
        )

        assert result.ok is False
        assert logged == [("[ChannelDelivery] Text delivery failed via telegram: telegram down", ())]

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
        tenant_id = uuid4()
        target_user = SimpleNamespace(id=uuid4(), username="alice", display_name="Alice", tenant_id=tenant_id)
        existing_session = ChatSession(
            id=uuid4(),
            agent_id=agent_id,
            user_id=target_user.id,
            title="Existing",
            source_channel="web",
        )
        db = _SequenceDB(
            [
                target_user,
                existing_session,
                None,  # apply_web_session_contract conflict lookup
                SimpleNamespace(id=agent_id, name="Web Agent"),
            ]
        )
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
        # RLS 阶段2b: the persisted web ChatMessage must carry the recipient's
        # tenant_id — a NULL would be globally visible under the USING-only policy.
        from app.models.audit import ChatMessage

        persisted = [obj for obj in db.added if isinstance(obj, ChatMessage)]
        assert len(persisted) == 1
        assert persisted[0].tenant_id == tenant_id


class TestSendFile:
    @pytest.mark.asyncio
    async def test_send_file_failure_log_includes_channel_and_error(self, monkeypatch, tmp_path) -> None:
        import app.api.telegram as telegram_mod
        import app.services.channel_delivery_service as delivery_mod

        async def fake_send_file(*_args, **_kwargs):
            raise RuntimeError("telegram upload down")

        logged: list[tuple[str, tuple[object, ...]]] = []

        def fake_warning(message: str, *args: object) -> None:
            logged.append((message, args))

        monkeypatch.setattr(telegram_mod, "_send_telegram_file", fake_send_file)
        monkeypatch.setattr(delivery_mod.logger, "warning", fake_warning)

        file_path = tmp_path / "report.txt"
        file_path.write_text("report", encoding="utf-8")
        config = SimpleNamespace(
            channel_type="telegram",
            app_secret="bot-token",
            app_id="telegram",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )
        result = await ChannelDeliveryService.send_file(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "telegram", "chat_id": 99887766},
            file_path=file_path,
            message="see attached",
            delivery_mode="deferred",
        )

        assert result.ok is False
        assert logged == [("[ChannelDelivery] File delivery failed via telegram: telegram upload down", ())]

    @pytest.mark.asyncio
    async def test_send_file_wechat_falls_back_to_signed_download_link(self, monkeypatch, tmp_path) -> None:
        import app.services.wechat_ilink_client as ilink_mod

        agent_id = uuid4()
        file_path = tmp_path / "Serenity_投资观点追踪.md"
        file_path.write_text("report", encoding="utf-8")
        sent_texts: list[str] = []

        class FakeILinkClient:
            def __init__(self, _base_url):
                pass

            async def send_message(self, *, bot_token, to_user_id, context_token, text):
                sent_texts.append(text)

            async def upload_media(self, **_kwargs):
                raise RuntimeError("cdn upload rejected")

        monkeypatch.setattr(ilink_mod, "ILinkClient", FakeILinkClient)
        monkeypatch.setattr(
            "app.services.wechat_personal_service.get_channel_credentials",
            lambda _config: {"base_url": "https://ilink.example", "bot_token": "bot-token"},
        )
        monkeypatch.setattr(
            "app.services.file_download_tokens.build_channel_file_download_url",
            lambda *, agent_id, path, expires_delta=None: (
                f"https://backend.example.com/api/agents/{agent_id}/files/download?path={path}&token=signed"
            ),
        )

        config = SimpleNamespace(
            channel_type="wechat_personal",
            app_secret="secret",
            app_id="wechat",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )

        result = await ChannelDeliveryService.send_file(
            db=_FakeDB(config),
            agent_id=agent_id,
            reply_target={"channel": "wechat_personal", "to_user_id": "wxid_abc", "context_token": "ctx"},
            file_path=file_path,
            message="请查收",
            delivery_mode="live",
        )

        assert result.ok is True
        assert result.status == "success"
        assert result.detail["fallback_used"] is True
        assert "微信文件直传失败" in sent_texts[-1]
        assert "token=signed" in sent_texts[-1]

    @pytest.mark.asyncio
    async def test_send_file_wechat_uploads_and_sends_media(self, monkeypatch, tmp_path) -> None:
        import app.services.wechat_ilink_client as ilink_mod

        agent_id = uuid4()
        file_path = tmp_path / "report.pdf"
        file_path.write_bytes(b"pdf bytes")
        calls: list[tuple[str, dict]] = []

        class FakeILinkClient:
            def __init__(self, base_url):
                calls.append(("client", {"base_url": base_url}))

            async def send_message(self, **kwargs):
                calls.append(("text", kwargs))

            async def upload_media(self, **kwargs):
                calls.append(("upload", kwargs))
                return ilink_mod.UploadResult(
                    download_param="download-param",
                    aes_key_hex="00" * 16,
                    plaintext_size=len(kwargs["file_data"]),
                    ciphertext_size=16,
                )

            async def send_media_message(self, **kwargs):
                calls.append(("media", kwargs))

        monkeypatch.setattr(ilink_mod, "ILinkClient", FakeILinkClient)
        monkeypatch.setattr(
            "app.services.wechat_personal_service.get_channel_credentials",
            lambda _config: {"base_url": "https://ilink.example", "bot_token": "bot-token"},
        )

        config = SimpleNamespace(
            channel_type="wechat_personal",
            app_secret="secret",
            app_id="wechat",
            is_configured=True,
            is_connected=True,
            extra_config={},
        )

        result = await ChannelDeliveryService.send_file(
            db=_FakeDB(config),
            agent_id=agent_id,
            reply_target={"channel": "wechat_personal", "to_user_id": "wxid_abc", "context_token": "ctx"},
            file_path=file_path,
            message="请查收",
            delivery_mode="live",
        )

        assert result.ok is True
        assert result.message == "WeChat personal file delivered."
        assert ("client", {"base_url": "https://ilink.example"}) in calls
        assert calls[1][0] == "text"
        assert calls[1][1]["text"] == "请查收"
        assert calls[2][0] == "upload"
        assert calls[2][1]["bot_token"] == "bot-token"
        assert calls[2][1]["to_user_id"] == "wxid_abc"
        assert calls[2][1]["file_data"] == b"pdf bytes"
        assert calls[2][1]["media_type"] == ilink_mod.MEDIA_TYPE_FILE
        assert calls[3][0] == "media"
        assert calls[3][1]["context_token"] == "ctx"
        assert calls[3][1]["file_name"] == "report.pdf"
