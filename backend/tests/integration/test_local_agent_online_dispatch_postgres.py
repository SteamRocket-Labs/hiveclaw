"""Online requests reach their original bridge without duplicate delivery."""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.database import tenant_scoped_session
from app.models.local_agent_channel import LocalAgentChannelMessage
from app.services import local_agent_channel_service as channel
from tests.integration import test_m3e_local_approval_return_postgres as local_return
from tests.integration.test_m3f_live_local_policy_postgres import _allow_without_approval

delegation = local_return.delegation
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_online_dispatch_reaches_only_the_original_connection_once(
    delegation, app_user_sessionmaker, monkeypatch
):
    from app.api import local_agent_channel as api

    class Socket:
        def __init__(self):
            self.frames = []

        async def send_json(self, payload):
            self.frames.append(payload)

    seed = delegation
    original_socket, other_socket = Socket(), Socket()
    manager = api.LocalAgentChannelConnectionManager()
    monkeypatch.setattr(api, "channel_ws_manager", manager)

    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        await _allow_without_approval(db, seed)
        message = await db.get(LocalAgentChannelMessage, seed["message_id"], populate_existing=True)
        assert message.status == "pending" and message.delivery_attempt_count == 0
        await manager.connect(context=seed["selected"], websocket=original_socket)
        await manager.connect(context=replace(seed["selected"], connection_id=uuid4()), websocket=other_socket)
        result = await channel.dispatch_channel_message(
            db, message_id=seed["message_id"], owner_user_id=seed["user_id"]
        )
        assert result["status"] == "sent"
        assert len(original_socket.frames) == 1
        assert original_socket.frames[0]["message"]["id"] == str(seed["message_id"])
        assert other_socket.frames == []
        message = await db.get(LocalAgentChannelMessage, seed["message_id"], populate_existing=True)
        assert message.status == "delivered" and message.delivery_attempt_count == 1
        assert message.request_hash == seed["request_hash"]
        assert message.metadata_json["last_live_delivery"]["connection_id"] == str(seed["selected"].connection_id)

        repeated = await channel.dispatch_channel_message(
            db, message_id=seed["message_id"], owner_user_id=seed["user_id"]
        )
        assert repeated["status"] == "not_pending"
        assert len(original_socket.frames) == 1 and other_socket.frames == []
