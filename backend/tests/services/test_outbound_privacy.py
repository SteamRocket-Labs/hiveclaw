"""Outbound privacy redact tests for Phase 9."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.channel_delivery_service import ChannelDeliveryService
from app.services.outbound_privacy import OutboundRedactDecision, redact_outbound
from app.services.principal_context import (
    Principal,
    PrincipalRole,
    PrincipalStack,
)
from app.services.privacy_layer import SensitivityLevel


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


class TestRedactOutbound:
    def test_secret_shaped_documentation_is_not_rejected(self) -> None:
        decision = redact_outbound(
            "deploy with api_key=sk-AAAAAAAAAAAAAAAAAAAAAAAA",
            channel="feishu",
        )
        assert isinstance(decision, OutboundRedactDecision)
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC
        assert decision.text == "deploy with api_key=sk-AAAAAAAAAAAAAAAAAAAAAAAA"

    def test_exact_active_secret_is_redacted_and_rejected(self) -> None:
        from app.services.exact_secret_boundary import ExactSecretBoundary

        active_secret = "sk-live-channel-secret-0123456789"
        decision = redact_outbound(
            f"prefix::{active_secret}::suffix",
            channel="feishu",
            secret_boundary=ExactSecretBoundary.from_pairs(
                (("channel-config://agent-1/telegram/bot_token", active_secret),)
            ),
        )

        assert decision.rejected is True
        assert decision.sensitivity == SensitivityLevel.PL4_CREDENTIAL
        assert decision.text == "prefix::[REDACTED_SECRET]::suffix"
        assert decision.secret_evidence_refs == ("channel-config://agent-1/telegram/bot_token",)

    def test_semantic_keyword_does_not_mechanically_classify_or_strip_output(self) -> None:
        decision = redact_outbound(
            "Vendor proposal references Q3 salary band 280k",
            channel="feishu",
        )
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC
        assert decision.text == "Vendor proposal references Q3 salary band 280k"

    def test_explicitly_typed_pl3_owner_private_web_channel_allows_passthrough(self) -> None:
        owner = Principal(role=PrincipalRole.OWNER, id="alice", label="Alice")
        current = Principal(role=PrincipalRole.CURRENT_USER, id="alice", label="Alice")
        stack = PrincipalStack(direct_owner=owner, current_user=current)
        decision = redact_outbound(
            "Internal note: planned salary review next week",
            channel="web",
            principal_stack=stack,
            declared_sensitivity=SensitivityLevel.PL3_SENSITIVE,
        )
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL3_SENSITIVE
        assert "salary" in decision.text.lower()

    def test_explicitly_typed_pl3_external_channel_is_stripped(self) -> None:
        decision = redact_outbound(
            "A confidential business note without magic keywords",
            channel="feishu",
            declared_sensitivity=SensitivityLevel.PL3_SENSITIVE,
        )
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL3_SENSITIVE
        assert decision.text == "[REDACTED_PL3]"

    def test_pl2_pii_replaced_with_typed_placeholder(self) -> None:
        decision = redact_outbound(
            "Please copy alice@example.com on the reply",
            channel="slack",
        )
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL2_PII
        assert "alice@example.com" not in decision.text
        assert "<Email_" in decision.text

    def test_pl1_passthrough(self) -> None:
        decision = redact_outbound("morning standup at 10am", channel="feishu")
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC
        assert decision.text == "morning standup at 10am"


class TestChannelDeliveryRedaction:
    @pytest.mark.asyncio
    async def test_send_text_blocks_exact_active_channel_credential(self, monkeypatch) -> None:
        called: dict[str, object] = {}

        async def fake_send(bot_token, chat_id, text):
            called["text"] = text
            return {"ok": True}

        import app.api.telegram as telegram_mod

        monkeypatch.setattr(telegram_mod, "_send_telegram_message", fake_send)

        active_secret = "telegram-bot-token-live-0123456789"
        config = SimpleNamespace(
            channel_type="telegram",
            app_secret=active_secret,
            app_id="telegram",
            is_configured=True,
            is_connected=True,
            extra_config={},
            encrypt_key=None,
            verification_token=None,
        )
        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            agent_id=uuid4(),
            reply_target={"channel": "telegram", "chat_id": 1, "sender_id": 2},
            text=f"prefix::{active_secret}::suffix",
        )

        assert result.ok is False
        assert result.status == "denied"
        assert "PL4" in result.message or "credential" in result.message.lower()
        assert called == {}

    @pytest.mark.asyncio
    async def test_send_text_blocks_exact_tenant_tool_credential_not_owned_by_channel(
        self,
        monkeypatch,
    ) -> None:
        from app.services.exact_secret_boundary import ExactSecretBoundary

        active_secret = "tool-secret-live-0123456789"
        tenant_id = uuid4()
        agent_id = uuid4()
        config = SimpleNamespace(
            channel_type="telegram",
            app_secret="telegram-bot-token-live-0123456789",
            app_id="telegram",
            is_configured=True,
            is_connected=True,
            extra_config={},
            encrypt_key=None,
            verification_token=None,
        )

        async def fake_load(_db, *, tenant_id: object, agent_id: object):
            assert tenant_id == tenant_id_value
            assert agent_id == agent_id_value
            return ExactSecretBoundary.from_pairs((("tool-config://tenant-1/search/api_key", active_secret),))

        tenant_id_value = tenant_id
        agent_id_value = agent_id
        monkeypatch.setattr(
            "app.services.credential_boundary_loader.load_exact_secret_boundary",
            fake_load,
        )

        result = await ChannelDeliveryService.send_text(
            db=_FakeDB(config),
            tenant_id=tenant_id,
            agent_id=agent_id,
            reply_target={"channel": "telegram", "chat_id": 1, "sender_id": 2},
            text=f"prefix::{active_secret}::suffix",
        )

        assert result.ok is False
        assert result.status == "denied"
        assert result.detail["secret_evidence_refs"] == [
            "tool-config://tenant-1/search/api_key",
        ]

    @pytest.mark.asyncio
    async def test_send_text_preserves_ordinary_semantic_content_for_external_channel(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        async def fake_send(bot_token, chat_id, text):
            captured["text"] = text
            return {"ok": True}

        import app.api.telegram as telegram_mod

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
            reply_target={"channel": "telegram", "chat_id": 1, "sender_id": 2},
            text="Vendor asking about salary range for the role",
        )

        assert result.ok is True
        text_sent = str(captured.get("text", ""))
        assert text_sent == "Vendor asking about salary range for the role"
