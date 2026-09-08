"""Real PostgreSQL regression: the user-global Local Agent default stays recoverable.

Reproduces the production shape: the original default session was bound to its
source agent by the first dispatched request (source_agent_id set,
chat_session_id NULL, one approved message), then reload created a newer empty
NULL default that shadowed it. Also verifies agent-scoped mirrored sessions,
other owners/tenants, archived, and non-web rows are never selected, and that
recovery creates no duplicate session, message, or replay.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.local_agent_channel import (
    LocalAgentChannelMessage,
    LocalAgentChannelSession,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services import local_agent_channel_service as channel_service


@pytest.mark.asyncio
async def test_default_session_recovery_prefers_bound_original_over_all_row_shapes(
    owner_sessionmaker,
):
    suffix = uuid.uuid4().hex[:10]
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Default Recovery Tenant", slug=f"default-recovery-{suffix}"))
        db.add(Tenant(id=other_tenant_id, name="Other Tenant", slug=f"other-{suffix}"))
        for user_id, tenant, name in (
            (owner_id, tenant_id, "owner"),
            (other_owner_id, tenant_id, "other-owner"),
        ):
            db.add(
                User(
                    id=user_id,
                    username=f"{name}-{suffix}",
                    email=f"{name}-{suffix}@example.test",
                    password_hash="x",
                    display_name=name,
                    tenant_id=tenant,
                    role="member",
                )
            )
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=owner_id,
                owner_user_id=owner_id,
                sponsor_user_id=owner_id,
                name=f"recovery-agent-{suffix}",
                agent_type="local_agent",
                status="running",
            )
        )
        await db.flush()
        mirrored_chat = ChatSession(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=owner_id,
            agent_id=agent_id,
            title="agent scoped",
        )
        db.add(mirrored_chat)
        await db.flush()

        # The original: user-global default bound by its first dispatched request.
        original = LocalAgentChannelSession(
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            source_agent_id=agent_id,
            chat_session_id=None,
            source="web",
            status="active",
        )
        db.add(original)
        await db.flush()
        db.add(
            LocalAgentChannelMessage(
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                source_agent_id=agent_id,
                session_id=original.id,
                sender_user_id=owner_id,
                direction="hive_to_local",
                content="synthetic pwd request",
                idempotency_key=f"recovery-{suffix}",
                request_hash="hash",
                replay_key=f"local:recovery-{suffix}",
                receipt_trace_id=f"local-agent:recovery-{suffix}",
                receipt_span_id=f"remote-action:recovery-{suffix}",
                status="delivered",
            )
        )
        # The shadow: newer empty NULL replacement this same bug created.
        db.add(
            LocalAgentChannelSession(
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                source_agent_id=None,
                chat_session_id=None,
                source="web",
                status="active",
            )
        )
        # Agent-scoped mirrored session: bound agent AND chat mirror — excluded.
        db.add(
            LocalAgentChannelSession(
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                source_agent_id=agent_id,
                chat_session_id=mirrored_chat.id,
                source="web",
                status="active",
            )
        )
        # Archived bound-shaped row: excluded by status.
        db.add(
            LocalAgentChannelSession(
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                source_agent_id=agent_id,
                chat_session_id=None,
                source="web",
                status="archived",
            )
        )
        # Non-web bound row: excluded by source.
        db.add(
            LocalAgentChannelSession(
                tenant_id=tenant_id,
                owner_user_id=owner_id,
                source_agent_id=agent_id,
                chat_session_id=None,
                source="a2a",
                status="active",
            )
        )
        # Another owner's bound default: excluded by owner.
        db.add(
            LocalAgentChannelSession(
                tenant_id=tenant_id,
                owner_user_id=other_owner_id,
                source_agent_id=agent_id,
                chat_session_id=None,
                source="web",
                status="active",
            )
        )
        await db.commit()
        sessions_before = (
            await db.execute(
                select(func.count())
                .select_from(LocalAgentChannelSession)
                .where(LocalAgentChannelSession.tenant_id == tenant_id)
            )
        ).scalar_one()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        payload = await channel_service.get_or_create_default_channel_session(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
        )

        assert payload["id"] == original.id
        assert payload["agent_id"] == agent_id
        assert payload["status"] == "active"

        # Recovery is idempotent and creates no duplicate rows or replay.
        again = await channel_service.get_or_create_default_channel_session(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_id,
        )
        assert again["id"] == original.id
        sessions_after = (
            await db.execute(
                select(func.count())
                .select_from(LocalAgentChannelSession)
                .where(LocalAgentChannelSession.tenant_id == tenant_id)
            )
        ).scalar_one()
        assert sessions_after == sessions_before
        messages = (
            (
                await db.execute(
                    select(LocalAgentChannelMessage).where(LocalAgentChannelMessage.session_id == original.id)
                )
            )
            .scalars()
            .all()
        )
        assert [message.content for message in messages] == ["synthetic pwd request"]

    # Cross-tenant isolation: the same owner id in another tenant is a
    # different principal and must not recover this tenant's default.
    async with tenant_scoped_session(other_tenant_id, session_factory=owner_sessionmaker) as db:
        fresh = await channel_service.get_or_create_default_channel_session(
            db,
            tenant_id=other_tenant_id,
            owner_user_id=owner_id,
        )
        assert fresh["id"] != original.id
        assert fresh["agent_id"] is None
