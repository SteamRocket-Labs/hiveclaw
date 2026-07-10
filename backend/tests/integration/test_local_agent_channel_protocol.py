"""Real PostgreSQL proof for signed Local Agent snapshots, cursor replay, and receipts."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.invocation_span import InvocationSpan
from app.models.local_agent_channel import (
    LocalAgentCapabilitySnapshot,
    LocalAgentChannelEvent,
    LocalAgentChannelMessage,
    LocalAgentChannelSession,
)
from app.models.local_bridge import LocalAgentBridgeConnection
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_agent_channel_service as channel_service
from app.services.local_agent_protocol import verify_capability_snapshot
from app.services.local_bridge_service import BridgeAuthContext


async def _seed(owner_sessionmaker):
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Local Protocol Tenant", slug=f"local-protocol-{suffix}"))
        db.add(
            User(
                id=owner_id,
                username=f"local-owner-{suffix}",
                email=f"local-owner-{suffix}@example.test",
                password_hash="x",
                display_name="Local Protocol Owner",
                tenant_id=tenant_id,
                role="member",
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=owner_id,
                owner_user_id=owner_id,
                sponsor_user_id=owner_id,
                name="Owner Mac Agent",
                role_description="Runs governed local work",
                agent_type="local_agent",
                status="running",
            )
        )
        await db.flush()
        db.add(
            LocalAgentBridgeConnection(
                id=connection_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=owner_id,
                device_name="Owner Mac",
                client_kind="codex",
                device_fingerprint=f"device-{suffix}",
                token_hash=f"token-{suffix}",
                scopes=[
                    "local_agent:connect",
                    "local_agent:receive",
                    "local_agent:send",
                    "local_agent:report",
                ],
                status="active",
            )
        )
    context = BridgeAuthContext(
        connection_id=connection_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=owner_id,
        scopes=(
            "local_agent:connect",
            "local_agent:receive",
            "local_agent:send",
            "local_agent:report",
        ),
        client_kind="codex",
        device_name="Owner Mac",
    )
    return tenant_id, owner_id, agent_id, context


async def test_local_agent_protocol_is_signed_monotonic_idempotent_and_receipted(
    owner_sessionmaker,
) -> None:
    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        ready = await channel_service.mark_channel_ready(
            db,
            context=context,
            runtime_kind="codex",
            capabilities={
                "execute": True,
                "event_stream": True,
                "result_report": True,
                "file_download": True,
            },
        )
        session = LocalAgentChannelSession(
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            source_agent_id=agent_id,
            source="a2a",
            status="active",
        )
        db.add(session)
        await db.flush()
        session_id = session.id

    assert ready["effective_capabilities"] == [
        "event_stream",
        "execute",
        "file_download",
        "result_report",
    ]
    assert len(ready["snapshot_hash"]) == 64

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        snapshot = (
            await db.execute(
                select(LocalAgentCapabilitySnapshot).where(
                    LocalAgentCapabilitySnapshot.snapshot_hash == ready["snapshot_hash"]
                )
            )
        ).scalar_one()
        assert verify_capability_snapshot(
            channel_service.capability_snapshot_payload(snapshot),
            signing_secret=channel_service.local_capability_signing_secret(),
        )

        first = await channel_service.enqueue_channel_message(
            db,
            session_id=session_id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            sender_agent_id=agent_id,
            content="Inspect the repository and return evidence.",
            attachments=[],
            metadata={"source": "a2a"},
            idempotency_key="a2a:task-1",
        )
        replay = await channel_service.enqueue_channel_message(
            db,
            session_id=session_id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            sender_agent_id=agent_id,
            content="Inspect the repository and return evidence.",
            attachments=[],
            metadata={"source": "a2a"},
            idempotency_key="a2a:task-1",
        )
        assert replay["id"] == first["id"]
        assert replay["receipt"] == first["receipt"]
        message_id = uuid.UUID(first["id"])

        first_poll = await channel_service.poll_pending_channel_messages(db, context=context)
        same_snapshot_poll = await channel_service.poll_pending_channel_messages(db, context=context)
        assert same_snapshot_poll == []
        reconnected = await channel_service.mark_channel_ready(
            db,
            context=context,
            runtime_kind="codex",
            capabilities={
                "execute": True,
                "event_stream": True,
                "result_report": True,
                "file_download": True,
            },
        )
        assert reconnected["snapshot_hash"] != ready["snapshot_hash"]
        replay_poll = await channel_service.poll_pending_channel_messages(db, context=context)
        assert [row["id"] for row in first_poll] == [str(message_id)]
        assert [row["id"] for row in replay_poll] == [str(message_id)]

        progress = await channel_service.record_channel_event(
            db,
            context=context,
            session_id=session_id,
            message_id=message_id,
            event_type="delta",
            payload={"text": "working"},
        )
        after_first = await channel_service.list_channel_events(
            db,
            session_id=session_id,
            owner_user_id=owner_id,
            after_sequence=1,
        )
        assert progress["sequence"] == 2
        assert [event["sequence"] for event in after_first] == [2]

        completed = await channel_service.record_channel_result(
            db,
            context=context,
            session_id=session_id,
            message_id=message_id,
            result_status="completed",
            output="Repository evidence is ready.",
            artifacts=[{"path": "workspace/results/evidence.md"}],
            metadata={"runtime": "codex"},
        )
        completed_replay = await channel_service.record_channel_result(
            db,
            context=context,
            session_id=session_id,
            message_id=message_id,
            result_status="completed",
            output="This duplicate payload must not create a second side effect.",
            artifacts=[],
            metadata={"runtime": "codex"},
        )
        assert completed_replay["receipt"] == completed["receipt"]
        assert completed["receipt"]["request_hash"] == first["receipt"]["request_hash"]
        assert completed["receipt"]["capability_snapshot_hash"] == ready["snapshot_hash"]
        assert completed["receipt"]["result_refs"] == ["workspace/results/evidence.md"]

        message_count = await db.scalar(
            select(func.count(LocalAgentChannelMessage.id)).where(
                LocalAgentChannelMessage.tenant_id == tenant_id,
                LocalAgentChannelMessage.idempotency_key == "a2a:task-1",
            )
        )
        result_event_count = await db.scalar(
            select(func.count(LocalAgentChannelEvent.id)).where(
                LocalAgentChannelEvent.message_id == message_id,
                LocalAgentChannelEvent.event_type == "result",
            )
        )
        span = (
            await db.execute(
                select(InvocationSpan).where(
                    InvocationSpan.tenant_id == tenant_id,
                    InvocationSpan.trace_id == completed["receipt"]["trace_id"],
                    InvocationSpan.span_id == completed["receipt"]["span_id"],
                )
            )
        ).scalar_one()
        assert message_count == 1
        assert result_event_count == 1
        assert span.status == "ok"
        assert span.input_hash == completed["receipt"]["request_hash"]
        assert span.idempotency_key == "a2a:task-1"
        assert span.side_effect_refs == ["workspace/results/evidence.md"]


async def test_local_agent_explicit_agent_deny_removes_execute_from_new_snapshot(
    owner_sessionmaker,
) -> None:
    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            CapabilityPolicy(
                tenant_id=tenant_id,
                agent_id=agent_id,
                capability="local_agent.execute",
                allowed=False,
                requires_approval=False,
            )
        )
        ready = await channel_service.mark_channel_ready(
            db,
            context=context,
            runtime_kind="codex",
            capabilities={"execute": True, "event_stream": True, "result_report": True},
        )
        session = LocalAgentChannelSession(
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            source_agent_id=agent_id,
            source="a2a",
            status="active",
        )
        db.add(session)
        await db.flush()
        assert "execute" not in ready["effective_capabilities"]
        with pytest.raises(HTTPException) as exc_info:
            await channel_service.enqueue_channel_message(
                db,
                session_id=session.id,
                owner_user_id=owner_id,
                sender_user_id=owner_id,
                sender_agent_id=agent_id,
                content="This must be denied.",
                idempotency_key="a2a:denied-task",
            )
        assert exc_info.value.status_code == 403
