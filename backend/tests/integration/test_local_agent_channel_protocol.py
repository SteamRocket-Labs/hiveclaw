"""Real PostgreSQL proof for signed Local Agent snapshots, cursor replay, and receipts."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.execution_context import ExecutionPrincipal
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.invocation_span import InvocationSpan
from app.models.local_agent_channel import (
    LocalAgentCapabilitySnapshot,
    LocalAgentChannelEvent,
    LocalAgentChannelMessage,
    LocalAgentChannelSession,
)
from app.models.local_bridge import LocalAgentBridgeConnection
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_agent_channel_service as channel_service
from app.services.local_agent_protocol import verify_capability_snapshot
from app.services.local_bridge_service import BridgeAuthContext


async def _grant_local_capabilities(
    db,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    execute_requires_approval: bool = False,
) -> None:
    for capability, requires_approval in (
        ("execute", execute_requires_approval),
        ("event_stream", False),
        ("result_report", False),
        ("file_download", True),
        ("file_upload", True),
    ):
        db.add(
            CapabilityPolicy(
                tenant_id=tenant_id,
                agent_id=agent_id,
                capability=f"local_agent.{capability}",
                allowed=True,
                requires_approval=requires_approval,
                conditions={"test": True},
            )
        )
    await db.flush()


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
                expires_at=channel_service.utcnow() + timedelta(days=30),
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
    source_agent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    principal = ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=source_agent_id,
        requester_user_id=owner_id,
        root_session_id=str(parent_session_id),
    )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await _grant_local_capabilities(db, tenant_id=tenant_id, agent_id=agent_id)
        db.add(
            Agent(
                id=source_agent_id,
                tenant_id=tenant_id,
                creator_id=owner_id,
                owner_user_id=owner_id,
                sponsor_user_id=owner_id,
                name="Cloud Source Agent",
                role_description="Delegates governed local work",
                agent_type="worker",
                status="running",
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=parent_session_id,
                tenant_id=tenant_id,
                agent_id=source_agent_id,
                user_id=owner_id,
                title="Source Agent Session",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        await db.flush()
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
            sender_agent_id=source_agent_id,
            content="Inspect the repository and return evidence.",
            attachments=[],
            metadata={
                "source": "a2a",
                "execution_target": "local_agent",
                "sender_agent_id": str(source_agent_id),
                "sender_agent_name": "Cloud Source Agent",
                "target_agent_id": str(agent_id),
                "target_agent_name": "Owner Mac Agent",
                "target_owner_user_id": str(owner_id),
                "parent_session_id": str(parent_session_id),
                "execution_principal": principal.to_evidence(),
            },
            idempotency_key="a2a:task-1",
        )
        replay = await channel_service.enqueue_channel_message(
            db,
            session_id=session_id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            sender_agent_id=source_agent_id,
            content="Inspect the repository and return evidence.",
            attachments=[],
            metadata={
                "source": "a2a",
                "execution_target": "local_agent",
                "sender_agent_id": str(source_agent_id),
                "sender_agent_name": "Cloud Source Agent",
                "target_agent_id": str(agent_id),
                "target_agent_name": "Owner Mac Agent",
                "target_owner_user_id": str(owner_id),
                "parent_session_id": str(parent_session_id),
                "execution_principal": principal.to_evidence(),
            },
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
        assert replay_poll == []

        persisted_message = await db.get(LocalAgentChannelMessage, message_id)
        assert persisted_message is not None
        assert persisted_message.delivery_attempt_count == 1
        assert persisted_message.delivery_lease_expires_at is not None
        persisted_message.delivery_lease_expires_at = channel_service.utcnow() - timedelta(seconds=1)
        await db.commit()
        reconciled_poll = await channel_service.poll_pending_channel_messages(db, context=context)
        assert [row["id"] for row in reconciled_poll] == [str(message_id)]
        assert reconciled_poll[0]["delivery_attempt_count"] == 2

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
        assert progress["sequence"] == 3
        assert [event["sequence"] for event in after_first] == [2, 3]
        assert after_first[0]["type"] == "delivery_requeued"

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
        completion_outbox = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.tenant_id == tenant_id,
                    RuntimeNotificationOutbox.source_kind == "a2a_delegation",
                    RuntimeNotificationOutbox.source_run_id == str(message_id),
                )
            )
        ).scalar_one()
        assert completion_outbox.parent_session_id == parent_session_id
        assert completion_outbox.parent_agent_id == source_agent_id
        assert completion_outbox.parent_user_id == owner_id
        assert completion_outbox.task_type == "a2a_local_delegation"
        assert completion_outbox.delivery_mode == "parent_continuation"
        assert completed["source_delivery"]["notification_id"] == str(completion_outbox.id)


async def test_local_agent_missing_policy_is_denied_by_default(
    owner_sessionmaker,
) -> None:
    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
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

        assert ready["effective_capabilities"] == []
        with pytest.raises(HTTPException) as exc_info:
            await channel_service.enqueue_channel_message(
                db,
                session_id=session.id,
                owner_user_id=owner_id,
                sender_user_id=owner_id,
                sender_agent_id=agent_id,
                content="No policy must not mean allow.",
                idempotency_key="a2a:missing-policy",
            )
        assert exc_info.value.status_code == 403


async def test_local_agent_requires_approval_releases_exact_message_after_owner_decision(
    owner_sessionmaker,
) -> None:
    from app.services.approval_service import ApprovalService

    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await _grant_local_capabilities(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            execute_requires_approval=True,
        )
        ready = await channel_service.mark_channel_ready(
            db,
            context=context,
            runtime_kind="codex",
            capabilities={"execute": True, "event_stream": True, "result_report": True},
        )
        assert "execute" in ready["effective_capabilities"]
        session = LocalAgentChannelSession(
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            source_agent_id=agent_id,
            source="a2a",
            status="active",
        )
        db.add(session)
        await db.flush()

        waiting = await channel_service.enqueue_channel_message(
            db,
            session_id=session.id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            sender_agent_id=agent_id,
            content="Inspect only this repository.",
            idempotency_key="a2a:approval-required",
        )
        assert waiting["status"] == "waiting_approval"
        assert waiting["approval_id"]
        assert await channel_service.poll_pending_channel_messages(db, context=context) == []

        approval = await db.get(ApprovalRequest, uuid.UUID(waiting["approval_id"]))
        owner = await db.get(User, owner_id)
        assert approval is not None
        assert approval.action_type == "local_agent.execute"
        assert approval.details["local_agent_message_id"] == waiting["id"]
        assert "Inspect only this repository" not in str(approval.details)
        assert owner is not None

        resolved = await ApprovalService().resolve_approval(db, approval.id, owner, "approve")
        assert resolved.status == "approved"
        released = await db.get(LocalAgentChannelMessage, uuid.UUID(waiting["id"]))
        assert released is not None
        assert released.status == "pending"
        delivered = await channel_service.poll_pending_channel_messages(db, context=context)
        assert [row["id"] for row in delivered] == [waiting["id"]]


async def test_local_agent_rejected_approval_never_dispatches(
    owner_sessionmaker,
) -> None:
    from app.services.approval_service import ApprovalService

    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await _grant_local_capabilities(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            execute_requires_approval=True,
        )
        await channel_service.mark_channel_ready(
            db,
            context=context,
            runtime_kind="codex",
            capabilities={"execute": True, "event_stream": True, "result_report": True},
        )
        session = LocalAgentChannelSession(
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            source_agent_id=agent_id,
            source="web",
            status="active",
        )
        db.add(session)
        await db.flush()
        waiting = await channel_service.enqueue_channel_message(
            db,
            session_id=session.id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            content="Do not run after rejection.",
            idempotency_key="web:approval-rejected",
        )
        approval = await db.get(ApprovalRequest, uuid.UUID(waiting["approval_id"]))
        owner = await db.get(User, owner_id)
        assert approval is not None and owner is not None

        await ApprovalService().resolve_approval(db, approval.id, owner, "reject")
        rejected = await db.get(LocalAgentChannelMessage, uuid.UUID(waiting["id"]))
        assert rejected is not None
        assert rejected.status == "rejected"
        span = (
            await db.execute(
                select(InvocationSpan).where(
                    InvocationSpan.tenant_id == tenant_id,
                    InvocationSpan.trace_id == waiting["receipt"]["trace_id"],
                    InvocationSpan.span_id == waiting["receipt"]["span_id"],
                )
            )
        ).scalar_one()
        assert span.status == "error"
        assert span.error == "Owner rejected this Local Agent action."
        assert await channel_service.poll_pending_channel_messages(db, context=context) == []


async def test_local_agent_approval_rechecks_live_policy_before_release(
    owner_sessionmaker,
) -> None:
    from app.services.approval_service import ApprovalService

    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await _grant_local_capabilities(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            execute_requires_approval=True,
        )
        await channel_service.mark_channel_ready(
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
        waiting = await channel_service.enqueue_channel_message(
            db,
            session_id=session.id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            sender_agent_id=agent_id,
            content="Policy may change while this waits.",
            idempotency_key="a2a:policy-revoked-after-request",
        )
        policy = (
            await db.execute(
                select(CapabilityPolicy).where(
                    CapabilityPolicy.tenant_id == tenant_id,
                    CapabilityPolicy.agent_id == agent_id,
                    CapabilityPolicy.capability == "local_agent.execute",
                )
            )
        ).scalar_one()
        policy.allowed = False
        await db.commit()
        approval = await db.get(ApprovalRequest, uuid.UUID(waiting["approval_id"]))
        owner = await db.get(User, owner_id)
        assert approval is not None and owner is not None

        resolved = await ApprovalService().resolve_approval(db, approval.id, owner, "approve")
        message = await db.get(LocalAgentChannelMessage, uuid.UUID(waiting["id"]))
        assert resolved.status == "approved"
        assert resolved.execution_status == "failed"
        assert message is not None
        assert message.status == "rejected"
        assert message.result == "Local Agent policy changed before approval release."
        assert await channel_service.poll_pending_channel_messages(db, context=context) == []


async def test_local_agent_delivery_reconciler_stops_automatic_replay_at_attempt_limit(
    owner_sessionmaker,
) -> None:
    tenant_id, owner_id, agent_id, context = await _seed(owner_sessionmaker)
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await _grant_local_capabilities(db, tenant_id=tenant_id, agent_id=agent_id)
        await channel_service.mark_channel_ready(
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
        queued = await channel_service.enqueue_channel_message(
            db,
            session_id=session.id,
            owner_user_id=owner_id,
            sender_user_id=owner_id,
            sender_agent_id=agent_id,
            content="Reconcile this delivery.",
            idempotency_key="a2a:reconcile-limit",
        )
        assert len(await channel_service.poll_pending_channel_messages(db, context=context)) == 1
        message = await db.get(LocalAgentChannelMessage, uuid.UUID(queued["id"]))
        assert message is not None
        message.delivery_attempt_count = channel_service.MAX_DELIVERY_ATTEMPTS
        message.delivery_lease_expires_at = channel_service.utcnow() - timedelta(seconds=1)
        await db.commit()

        assert await channel_service.poll_pending_channel_messages(db, context=context) == []
        await db.refresh(message)
        assert message.status == "needs_reconciliation"


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
