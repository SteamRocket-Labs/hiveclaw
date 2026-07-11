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
