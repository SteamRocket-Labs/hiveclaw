"""Stopped colleague notices use the existing ordered live delivery path."""
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.services.runtime_notification_outbox import (
    CompletionNotification, RuntimeNotificationOutboxService, enqueue_completion_notification,
)
from app.services.session_event_contract import serialize_session_event
from app.services.session_event_outbox import SessionEventOutboxPublisher
from app.services.session_v2_persistence import SessionEventDraft, append_session_events
from tests.services.test_runtime_notification_outbox import _seed_parent_session


@pytest.mark.usefixtures("migrated_pg_url")
async def test_x9b_stopped_colleague_end_notice_reaches_the_session_socket(owner_sessionmaker, monkeypatch):
    from app.services import web_chat_stream_bus as bus
    from app.services.web_chat_broker import web_chat_broker

    # The outbox drain below is not tenant-scoped; earlier tests in the same
    # database can leave claimable notifications. Settle them first so this
    # test observes only its own notification.
    async def settle(_item):
        return {}

    preclear = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    for _ in range(50):
        if not (await preclear.drain_once(worker_id="x9b-preclear", deliver=settle))["claimed"]:
            break
    tenant, user, agent, session = await _seed_parent_session(owner_sessionmaker)
    root, child = uuid.uuid4(), uuid.uuid4()
    colleague, child_session = uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=colleague, tenant_id=tenant, name="Colleague", creator_id=user, sponsor_user_id=user))
        await db.flush()
        db.add(ChatSession(id=child_session, tenant_id=tenant, agent_id=colleague, user_id=user,
            parent_session_id=session, source_channel="web", visibility_scope="direct_user"))
        db.add(RuntimeTask(id=root, tenant_id=tenant, task_type="web_chat_turn", status="killed",
            parent_agent_id=agent, parent_session_id=str(session), root_user_id=user, root_session_id=str(session),
            prompt="Stop this delegated work"))
        db.add(RuntimeTask(id=child, tenant_id=tenant, task_type="delegation", status="killed",
            parent_agent_id=agent, parent_session_id=str(session), child_agent_id=colleague,
            child_session_id=str(child_session), root_runtime_task_id=root, root_user_id=user,
            root_session_id=str(session), prompt="Delegated work"))
        await db.flush()
        await append_session_events(db, tenant_id=tenant, agent_id=agent, session_id=session, drafts=[
            SessionEventDraft(item_id=root, item_kind="run", lifecycle="cancelled", actor={"type": "runtime"},
                scope={"level": "run", "session_id": str(session), "thread_id": str(session),
                    "turn_id": str(root), "run_id": str(root)}, payload={"reason": "user_cancel"})])
        await enqueue_completion_notification(db, CompletionNotification(tenant_id=tenant,
            source_kind="a2a_delegation", source_run_id=str(child), task_type="delegation", terminal_status="killed",
            parent_session_id=session, parent_agent_id=agent, parent_user_id=user, root_runtime_task_id=root,
            child_agent_name="Colleague", child_session_id=str(child_session),
            summary="Stopped; the colleague did not finish this work.", delivery_mode="parent_continuation"))
        await db.commit()

    async def forbidden_resume(*_args, **_kwargs):
        pytest.fail("A stopped root must not start another model turn")

    monkeypatch.setattr("app.services.agent_session_continuation.start_web_chat_run", forbidden_resume)
    service = RuntimeNotificationOutboxService(session_factory=owner_sessionmaker)
    assert (await service.drain_once(worker_id="x9b-stop-notice"))["delivered"] == 1
    published, received = [], []

    class RedisTransport:
        async def publish(self, channel, data):
            published.append((channel, data))

        async def subscribe(self, *_channels):
            pass

        async def listen(self):
            for channel, data in published:
                yield {"type": "message", "channel": channel, "data": data}

    transport = RedisTransport()

    async def redis():
        return SimpleNamespace(publish=transport.publish, pubsub=lambda: transport)

    async def send(agent_id, session_id, payload):
        assert (agent_id, session_id) == (str(agent), str(session))
        received.append(payload)

    monkeypatch.setattr(bus, "get_redis", redis)
    monkeypatch.setattr(web_chat_broker, "send_session_message", send)
    publisher = SessionEventOutboxPublisher(session_factory=owner_sessionmaker)
    outcome = await publisher.drain_once(worker_id="x9b-live-publish", tenant_id=tenant)
    await bus._listen_web_chat_stream_once()
    assert outcome["published"] == len(received) > 0
    assert all(channel == bus.SESSION_EVENT_LIVE_CHANNEL for channel, _ in published)
    async with owner_sessionmaker() as db:
        saved = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == session).order_by(ChatTranscriptEvent.sequence)))
        assert received == [serialize_session_event(row, audience="user") for row in saved]
        assert (await db.get(RuntimeTask, root)).status == "killed"
    notice = next(frame for frame in received if frame.get("legacy_event_type") == "agent_task_notification")
    assert notice["payload"]["metadata"]["continuation_denial"] == "runtime_root_cancelled"
    assert notice["payload"]["metadata"]["result_manifest"]["items"][0]["source_run_id"] == str(child)
    print("X9B_PUSH " + json.dumps({"outcome": outcome, "types": [row.event_type for row in saved],
        "sequences": [frame["sequence"] for frame in received], "same_as_reload": True}))
