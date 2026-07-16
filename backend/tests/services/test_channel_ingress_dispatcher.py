from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_dispatcher_has_an_explicit_handler_for_every_external_ingress_surface():
    from app.services.channel_ingress_dispatcher import SUPPORTED_CHANNEL_INGRESS_HANDLERS

    assert set(SUPPORTED_CHANNEL_INGRESS_HANDLERS) == {
        "slack.event_callback",
        "feishu.event",
        "feishu.card_action",
        "telegram.update",
        "discord.interaction",
        "teams.activity",
        "wecom.webhook",
        "dingtalk.stream_message",
        "wecom.stream_message",
        "wechat_personal.stream_message",
    }


@pytest.mark.asyncio
async def test_dispatcher_rejects_unknown_handler_without_importing_provider_code():
    from app.services.channel_ingress_dispatcher import dispatch_channel_ingress_event

    item = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        handler_key="unknown.provider",
        payload={},
    )
    with pytest.raises(ValueError, match="unsupported channel ingress handler"):
        await dispatch_channel_ingress_event(item)


def test_replay_request_carries_only_verified_body_and_internal_marker():
    from app.services.channel_ingress_dispatcher import ChannelIngressReplayRequest

    event_id = uuid4()
    request = ChannelIngressReplayRequest(event_id=event_id, body={"event_id": "evt-1"})

    assert request.state.channel_ingress_event_id == event_id
    assert request.headers == {}


@pytest.mark.asyncio
async def test_materialized_message_recovery_reuses_original_canonical_input_identity(monkeypatch):
    from app.services import session_live_input
    from app.services.channel_ingress_dispatcher import _resume_materialized_user_message

    tenant_id, agent_id, user_id, session_id, event_id, message_id = (uuid4() for _ in range(6))
    message = SimpleNamespace(
        id=message_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=str(session_id),
        content="recover these exact bytes",
    )
    session = SimpleNamespace(
        id=session_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source_channel="feishu",
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id)

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        async def execute(self, statement):
            sql = str(statement)
            if "FROM chat_messages" in sql:
                return _Result(message)
            if "FROM chat_sessions" in sql:
                return _Result(session)
            if "FROM agents" in sql:
                return _Result(agent)
            if "FROM users" in sql:
                return _Result(user)
            raise AssertionError(sql)

    captured = {}

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return {
            "input_id": str(kwargs["input_id"]),
            "target_run_id": str(uuid4()),
            "run": None,
        }

    monkeypatch.setattr(session_live_input, "submit_live_human_input", fake_submit)
    item = SimpleNamespace(id=event_id, tenant_id=tenant_id, agent_id=agent_id, provider="feishu")

    result = await _resume_materialized_user_message(_DB(), item)

    assert result is not None and result["input_id"] == str(event_id)
    assert captured["input_id"] == event_id
    assert captured["idempotency_key"] == f"channel:feishu:ingress:{event_id}"
    assert captured["runtime_metadata"] == {
        "source": "feishu",
        "channel": "feishu",
        "channel_ingress_event_id": str(event_id),
        "budget_interactive": False,
    }
