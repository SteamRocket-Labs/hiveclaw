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
    def test_pl4_credential_is_rejected(self) -> None:
        decision = redact_outbound(
            "deploy with api_key=sk-AAAAAAAAAAAAAAAAAAAAAAAA",
            channel="feishu",
        )
        assert isinstance(decision, OutboundRedactDecision)
        assert decision.rejected is True
        assert decision.sensitivity == SensitivityLevel.PL4_CREDENTIAL
        assert "sk-AAAAAAAA" not in decision.text

    def test_pl3_external_channel_is_stripped(self) -> None:
        decision = redact_outbound(
            "Vendor proposal references Q3 salary band 280k",
            channel="feishu",
        )
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL3_SENSITIVE
        assert "salary" not in decision.text.lower()
        assert "[REDACTED_PL3]" in decision.text

    def test_pl3_owner_private_web_channel_allows_passthrough(self) -> None:
        owner = Principal(role=PrincipalRole.OWNER, id="alice", label="Alice")
        current = Principal(role=PrincipalRole.CURRENT_USER, id="alice", label="Alice")
        stack = PrincipalStack(direct_owner=owner, current_user=current)
        decision = redact_outbound(
            "Internal note: planned salary review next week",
            channel="web",
            principal_stack=stack,
        )
        assert decision.rejected is False
        assert decision.sensitivity == SensitivityLevel.PL3_SENSITIVE
        assert "salary" in decision.text.lower()

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
    async def test_send_text_blocks_pl4_credential(self, monkeypatch) -> None:
        called: dict[str, object] = {}

        async def fake_send(bot_token, chat_id, text):
            called["text"] = text
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
            text="rotate api_key=sk-AAAAAAAAAAAAAAAAAAAAAAAA tomorrow",
        )

        assert result.ok is False
        assert result.status == "denied"
        assert "PL4" in result.message or "credential" in result.message.lower()
        assert called == {}

    @pytest.mark.asyncio
    async def test_send_text_redacts_pl3_for_external_channel(self, monkeypatch) -> None:
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
        assert "salary" not in text_sent.lower()
        assert "[REDACTED_PL3]" in text_sent
