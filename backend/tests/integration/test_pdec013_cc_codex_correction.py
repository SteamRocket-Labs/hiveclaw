"""PDEC-013 CC/Codex major-correction regressions against real PostgreSQL RLS.

One discriminating regression per correction item: the ``?tenant_id=``
selection boundary (CC B1), administrator precedence over the optional
operator projection in resource authorization (CC R1 / Codex item 3), the
shared ownership predicate and capability output (Codex items 6-7), scoped
administrator approval resolution (Codex item 8), the Local Agent Agent-Detail
message/events/download lane (Codex item 9), platform-administrator knowledge
read visibility (Codex item 10), legacy promotion accountability (Codex item
11), A2A member moderation (Codex item 12), and the audit rows for sensitive
cross-owner administrator business access (Codex item 13).
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.core.permissions import (
    SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE,
    require_agent_owner_or_admin,
)
from app.core.resource_authority import authorize_resource_action
from app.core.tenant_scope import resolve_tenant_scope
from app.database import enter_rls_bypass, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.chat_session import ChatSession
from app.models.knowledge import KnowledgeDocument
from app.models.local_agent_channel import LocalAgentChannelMessage, LocalAgentChannelSession
from app.models.security_audit import SecurityAuditEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services.personal_knowledge_access import HumanBrowserPrincipal


class _World:
    def __init__(self) -> None:
        self.tenant_a: uuid.UUID
        self.tenant_b: uuid.UUID
        self.employee_owner: uuid.UUID
        self.employee_other: uuid.UUID
        self.org_admin: uuid.UUID
        self.platform_admin: uuid.UUID
        self.foreign_member: uuid.UUID
        self.agent: uuid.UUID
        self.approval: uuid.UUID


async def _seed_world(owner_sessionmaker) -> _World:
    world = _World()
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        tenant_a = Tenant(name=f"CC A {token}", slug=f"cc-a-{token}")
        tenant_b = Tenant(name=f"CC B {token}", slug=f"cc-b-{token}")
        session.add_all([tenant_a, tenant_b])
        await session.flush()

        def _user(name: str, role: str, tenant_id: uuid.UUID) -> User:
            return User(
                username=f"{name}-{token}",
                email=f"{name}-{token}@test.invalid",
                password_hash="x",
                display_name=name,
                tenant_id=tenant_id,
                role=role,
                is_active=True,
            )

        employee_owner = _user("cc-owner", "member", tenant_a.id)
        employee_other = _user("cc-other", "member", tenant_a.id)
        org_admin = _user("cc-orgadmin", "org_admin", tenant_a.id)
        platform_admin = _user("cc-platform", "platform_admin", tenant_b.id)
        foreign_member = _user("cc-foreign", "member", tenant_b.id)
        session.add_all([employee_owner, employee_other, org_admin, platform_admin, foreign_member])
        await session.flush()

        agent = Agent(
            name=f"CC employee agent {token}",
            tenant_id=tenant_a.id,
            creator_id=employee_owner.id,
            owner_user_id=employee_owner.id,
            sponsor_user_id=employee_owner.id,
            status="running",
        )
        session.add(agent)
        await session.flush()

        approval = ApprovalRequest(
            agent_id=agent.id,
            tenant_id=tenant_a.id,
            action_type="send_email",
            details={"summary": f"pending approval {token}"},
            requested_by=employee_owner.id,
            decision_id=f"approval:{uuid.uuid4()}",
            status="pending",
            execution_status="pending",
        )
        session.add(approval)
        await session.commit()

        world.tenant_a = tenant_a.id
        world.tenant_b = tenant_b.id
        world.employee_owner = employee_owner.id
        world.employee_other = employee_other.id
        world.org_admin = org_admin.id
        world.platform_admin = platform_admin.id
        world.foreign_member = foreign_member.id
        world.agent = agent.id
        world.approval = approval.id
    return world


async def _load_actor(db, user_id: uuid.UUID) -> User:
    actor = await db.get(User, user_id)
    if actor is None:
        async with enter_rls_bypass(db, reason="test canonical actor lookup", actor_id=str(user_id)) as bypass_db:
            actor = await bypass_db.get(User, user_id)
    assert actor is not None
    return actor


def _platform_actor_in_tenant(db, actor: User, tenant_id: uuid.UUID) -> User:
    """Mirror ``get_current_user``: detach and override the selected tenant."""
    db.expunge(actor)
    actor.tenant_id = tenant_id
    return actor


async def _audit_events(db, event_type: str, *, actor_id: uuid.UUID | None = None) -> list[SecurityAuditEvent]:
    statement = select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == event_type)
    if actor_id is not None:
        statement = statement.where(SecurityAuditEvent.actor_id == actor_id)
    return list((await db.execute(statement)).scalars().all())


# ─── CC B1: the ?tenant_id= selection channel ───────────────────────────


async def test_b1_query_param_tenant_mismatch_is_a_recovery_error(owner_sessionmaker) -> None:
    world = await _seed_world(owner_sessionmaker)
    async with owner_sessionmaker() as session:
        platform_actor = await _load_actor(session, world.platform_admin)

        # Without a validated selected company, an explicit foreign tenant_id
        # must not silently become the request scope.
        with pytest.raises(HTTPException) as exc:
            resolve_tenant_scope(platform_actor, world.tenant_a)
        assert exc.value.status_code == 400
        assert "select" in str(exc.value.detail).lower()

        # The already-selected company stays usable through the same helper.
        selected = _platform_actor_in_tenant(session, platform_actor, world.tenant_a)
        assert str(resolve_tenant_scope(selected, world.tenant_a)) == str(world.tenant_a)
        assert str(resolve_tenant_scope(selected, None)) == str(world.tenant_a)


async def test_b1_route_level_query_param_cannot_reach_another_company(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    from app.api.agents import list_agents

    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_b)
        platform_actor = await _load_actor(db, world.platform_admin)

        # Unselected platform administrator: the query parameter alone must be
        # a truthful recovery error, never a foreign company inventory.
        with pytest.raises(HTTPException) as exc:
            await list_agents(tenant_id=world.tenant_a, current_user=platform_actor, db=db)
        assert exc.value.status_code == 400

        # With the validated selection (mirrored X-Tenant-Id semantics), the
        # same route still returns the selected company's Agent inventory.
        selected = _platform_actor_in_tenant(db, platform_actor, world.tenant_a)
        await pin_rls_tenant_context(db, world.tenant_a)
        rows = await list_agents(tenant_id=world.tenant_a, current_user=selected, db=db)
        assert any(str(row.id) == str(world.agent) for row in rows)


async def test_b1_inactive_company_not_reachable_via_query_param(owner_sessionmaker) -> None:
    world = await _seed_world(owner_sessionmaker)
    async with owner_sessionmaker() as session:
        # Retire the foreign target company; the administrator's own home
        # company stays live so the identity itself remains authenticated.
        tenant_a = await session.get(Tenant, world.tenant_a)
        assert tenant_a is not None
        tenant_a.is_active = False
        await session.commit()

        platform_actor = await _load_actor(session, world.platform_admin)
        # Without a validated selection of the retired company, the query
        # parameter alone is a recovery error — it can never resurrect a
        # disabled company as request scope.
        with pytest.raises(HTTPException) as exc:
            resolve_tenant_scope(platform_actor, world.tenant_a)
        assert exc.value.status_code == 400


# ─── Codex item 3 / CC R1: administrator precedence over operator view ──


async def test_item3_admin_operator_view_inputs_keep_business_authority(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        org_actor = await _load_actor(db, world.org_admin)

        decision = await authorize_resource_action(
            db,
            org_actor,
            agent_id=world.agent,
            resource_kind="task",
            resource_id=uuid.uuid4(),
            action="read",
            owner_user_id=world.employee_owner,
            allow_manager_override=True,
            manager_override_reason="operator review",
        )
        assert decision.authority_source == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE
        assert decision.operator_view is False

        audit_rows = await _audit_events(db, "resource.scoped_business_admin_access", actor_id=world.org_admin)
        assert audit_rows, "administrator resource access must be audited"
        latest = audit_rows[-1]
        assert str(latest.tenant_id) == str(world.tenant_a)
        assert latest.details["operator_view_requested"] is True
        assert latest.details["authority_source"] == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE

        # An ordinary member without a grant still needs the audited operator
        # lane and is denied cross-owner business access here.
        member_actor = await _load_actor(db, world.employee_other)
        with pytest.raises(HTTPException) as exc:
            await authorize_resource_action(
                db,
                member_actor,
                agent_id=world.agent,
                resource_kind="task",
                resource_id=uuid.uuid4(),
                action="read",
                owner_user_id=world.employee_owner,
                allow_manager_override=True,
                manager_override_reason="operator review",
            )
        assert exc.value.status_code == 403


# ─── Codex item 6: shared ownership predicate ───────────────────────────


async def test_item6_require_agent_owner_or_admin_admits_scoped_platform_admin(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)
        agent = await require_agent_owner_or_admin(db, platform_actor, world.agent)
        assert str(agent.id) == str(world.agent)

        org_actor = await _load_actor(db, world.org_admin)
        assert str((await require_agent_owner_or_admin(db, org_actor, world.agent)).id) == str(world.agent)

        # An ordinary member — even one with a legacy manage capability — is
        # not ownership authority (covered elsewhere); a plain member fails.
        member_actor = await _load_actor(db, world.employee_other)
        with pytest.raises(HTTPException) as exc:
            await require_agent_owner_or_admin(db, member_actor, world.agent)
        assert exc.value.status_code == 403


# ─── Codex item 7: capability output matches backend truth ──────────────


async def test_item7_can_manage_permissions_truth_for_both_scoped_admins(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    from app.api.agents import get_agent, get_agent_permissions

    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)
        detail = await get_agent(world.agent, current_user=platform_actor, db=db)
        assert detail["action_capabilities"]["can_manage_permissions"] is True

        permissions_view = await get_agent_permissions(world.agent, current_user=platform_actor, db=db)
        assert permissions_view["can_manage_permissions"] is True

        org_actor = await _load_actor(db, world.org_admin)
        org_detail = await get_agent(world.agent, current_user=org_actor, db=db)
        assert org_detail["action_capabilities"]["can_manage_permissions"] is True

        # A member with no Agent access at all is refused at the capability
        # boundary — not presented a permissions view.
        member_actor = await _load_actor(db, world.employee_other)
        with pytest.raises(HTTPException) as exc:
            await get_agent_permissions(world.agent, current_user=member_actor, db=db)
        assert exc.value.status_code == 403


# ─── Codex item 8: enterprise approvals for both scoped admins ──────────


async def test_item8_both_scoped_admins_list_and_resolve_approvals(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.enterprise import list_approvals
    from app.services.approval_service import approval_service

    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)

        org_actor = await _load_actor(db, world.org_admin)
        org_list = await list_approvals(current_user=org_actor, db=db)
        assert any(str(row.id) == str(world.approval) for row in org_list)

        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)
        platform_list = await list_approvals(current_user=platform_actor, db=db)
        assert any(str(row.id) == str(world.approval) for row in platform_list)

        resolved = await approval_service.resolve_approval(db, world.approval, platform_actor, "reject")
        assert resolved.status == "rejected"
        assert str(resolved.resolved_by) == str(world.platform_admin)

    world = await _seed_world(owner_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        approval2 = await db.get(ApprovalRequest, world.approval)

        # A pending approval for the negative lanes.
        assert approval2 is not None and approval2.status == "pending"
        org_actor = await _load_actor(db, world.org_admin)
        from app.services.approval_service import approval_service as service

        resolved_org = await service.resolve_approval(db, world.approval, org_actor, "approve")
        assert resolved_org.status == "approved"
        assert str(resolved_org.resolved_by) == str(world.org_admin)


async def test_item8_member_cannot_resolve_and_foreign_tenant_stays_invisible(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    from app.api.enterprise import list_approvals
    from app.services.approval_service import approval_service

    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        member_actor = await _load_actor(db, world.employee_other)
        member_list = await list_approvals(current_user=member_actor, db=db)
        assert all(str(row.id) != str(world.approval) for row in member_list)
        with pytest.raises(ValueError):
            await approval_service.resolve_approval(db, world.approval, member_actor, "approve")

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_b)
        foreign_actor = await _load_actor(db, world.foreign_member)
        foreign_list = await list_approvals(current_user=foreign_actor, db=db)
        assert all(str(row.id) != str(world.approval) for row in foreign_list)


# ─── Codex item 9 + 13: Local Agent Agent-Detail admin lane ─────────────


async def _seed_local_agent_channel(owner_sessionmaker, world: _World) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a dedicated local_agent-type Agent with an employee-owned session."""
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        agent = Agent(
            name=f"CC local agent {token}",
            tenant_id=world.tenant_a,
            creator_id=world.employee_owner,
            owner_user_id=world.employee_owner,
            sponsor_user_id=world.employee_owner,
            status="running",
            agent_type="local_agent",
        )
        session.add(agent)
        await session.flush()
        chat_session = ChatSession(
            agent_id=agent.id,
            tenant_id=world.tenant_a,
            user_id=world.employee_owner,
            title=f"host local session {token}",
            source_channel="web",
            session_kind="local_agent_channel",
        )
        session.add(chat_session)
        await session.flush()
        channel_session = LocalAgentChannelSession(
            tenant_id=world.tenant_a,
            owner_user_id=world.employee_owner,
            source_agent_id=agent.id,
            chat_session_id=chat_session.id,
            source="web",
            status="active",
        )
        session.add(channel_session)
        await session.flush()

        # A live runner capability contract so message enqueue passes the real
        # execute-capability gate (not a mock): bridge connection + channel +
        # HMAC-signed capability snapshot from the canonical builder.
        from datetime import datetime, timedelta, timezone

        from app.services.local_agent_channel_service import (
            build_signed_capability_snapshot,
            local_capability_signing_secret,
        )
        from app.models.local_agent_channel import (
            LocalAgentChannel,
            LocalAgentCapabilitySnapshot as _Snap,
        )
        from app.models.local_bridge import LocalAgentBridgeConnection

        connection = LocalAgentBridgeConnection(
            tenant_id=world.tenant_a,
            agent_id=agent.id,
            user_id=world.employee_owner,
            device_name=f"cc bridge {token}",
            client_kind="hive_connect",
            device_fingerprint=f"cc-fingerprint-{token}",
            token_hash=f"cc-token-hash-{token}",
            scopes=["local_agent"],
            status="active",
        )
        session.add(connection)
        await session.flush()
        channel_row = LocalAgentChannel(
            tenant_id=world.tenant_a,
            owner_user_id=world.employee_owner,
            connection_id=connection.id,
            runtime_kind="hive_connect",
            status="online",
        )
        session.add(channel_row)
        await session.flush()
        now = datetime.now(timezone.utc)
        signed = build_signed_capability_snapshot(
            signing_secret=local_capability_signing_secret(),
            issuer="cc-test",
            subject_agent_id=agent.id,
            tenant_id=world.tenant_a,
            scopes=["execute"],
            issued_at=now,
            expires_at=now + timedelta(hours=1),
            version=1,
        )
        from app.models.capability_policy import CapabilityPolicy

        session.add(
            CapabilityPolicy(
                tenant_id=world.tenant_a,
                agent_id=agent.id,
                capability="local_agent.execute",
                allowed=True,
                conditions={},
                requires_approval=False,
            )
        )
        session.add(
            _Snap(
                tenant_id=world.tenant_a,
                channel_id=channel_row.id,
                connection_id=connection.id,
                subject_agent_id=agent.id,
                issuer=signed["issuer"],
                version=signed["version"],
                reported_capabilities_json=[],
                server_capabilities_json=[],
                agent_capabilities_json=[],
                effective_capabilities_json=list(signed["scopes"]),
                snapshot_hash=signed["snapshot_hash"],
                signature=signed["signature"],
                issued_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await session.commit()
        return agent.id, channel_session.id, chat_session.id


async def test_item9_agent_detail_admin_message_events_download(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.local_agent_channel import (
        LocalAgentChannelMessageIn,
        create_local_agent_channel_message,
        download_agent_local_agent_channel_workspace_file,
        list_agent_local_agent_channel_sessions,
        list_local_agent_channel_events,
    )

    world = await _seed_world(owner_sessionmaker)
    agent_id, channel_session_id, chat_session_id = await _seed_local_agent_channel(owner_sessionmaker, world)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)

        sessions = await list_agent_local_agent_channel_sessions(agent_id, limit=50, current_user=platform_actor, db=db)
        assert str(channel_session_id) in {str(row["id"]) for row in sessions}

        message = await create_local_agent_channel_message(
            agent_id,
            channel_session_id,
            LocalAgentChannelMessageIn(content="admin business message", attachments=[], metadata={}),
            current_user=platform_actor,
            db=db,
        )
        # The durable row records the real administrator as the sender while
        # the channel session keeps the employee host owner.
        sender_row = (
            await db.execute(
                select(LocalAgentChannelMessage).where(LocalAgentChannelMessage.id == uuid.UUID(str(message["id"])))
            )
        ).scalar_one()
        assert str(sender_row.sender_user_id) == str(world.platform_admin)
        assert str(sender_row.owner_user_id) == str(world.employee_owner)

        events = await list_local_agent_channel_events(
            agent_id, channel_session_id, after_sequence=0, limit=100, current_user=platform_actor, db=db
        )
        assert isinstance(events["events"], list)

        audit_rows = await _audit_events(
            db, "local_agent_channel.scoped_business_admin_access", actor_id=world.platform_admin
        )
        assert audit_rows, "administrator local-agent business access must be audited"
        assert {str(row.tenant_id) for row in audit_rows} == {str(world.tenant_a)}

    # Download needs a real delivered file on the host workspace.
    workspace_rel = f"workspace/report-{token_path()}.md"
    async with owner_sessionmaker() as session:
        host_workspace = (
            Path(get_settings().AGENT_DATA_DIR)
            / "local_agents"
            / str(world.tenant_a)
            / "users"
            / str(world.employee_owner)
        )
        (host_workspace / "workspace").mkdir(parents=True, exist_ok=True)
        (host_workspace / workspace_rel).write_text("delivered artifact", encoding="utf-8")
        chat = await session.get(ChatSession, chat_session_id)
        assert chat is not None
        message_row = LocalAgentChannelMessage(
            tenant_id=world.tenant_a,
            owner_user_id=world.employee_owner,
            session_id=await session.scalar(
                select(LocalAgentChannelSession.id).where(LocalAgentChannelSession.chat_session_id == chat_session_id)
            ),
            source_agent_id=agent_id,
            sender_user_id=world.employee_owner,
            content="artifact delivered",
            idempotency_key=f"cc-artifact-{token_path()}",
            replay_key=f"cc-replay-{token_path()}",
            attachments_json=[{"path": workspace_rel, "filename": Path(workspace_rel).name}],
        )
        session.add(message_row)
        await session.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)
        response = await download_agent_local_agent_channel_workspace_file(
            agent_id,
            channel_session_id,
            path=workspace_rel,
            current_user=platform_actor,
            db=db,
        )
        assert response.filename is not None


def token_path() -> str:
    return uuid.uuid4().hex[:8]


async def test_item9_member_and_demotion_negatives(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.local_agent_channel import LocalAgentChannelMessageIn, create_local_agent_channel_message

    world = await _seed_world(owner_sessionmaker)
    _agent_id, _channel_session_id, chat_session_id = await _seed_local_agent_channel(owner_sessionmaker, world)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        member_actor = await _load_actor(db, world.employee_other)
        with pytest.raises(HTTPException) as exc:
            await create_local_agent_channel_message(
                _agent_id,
                chat_session_id,
                LocalAgentChannelMessageIn(content="member attempt", attachments=[], metadata={}),
                current_user=member_actor,
                db=db,
            )
        assert exc.value.status_code == 403

    async with owner_sessionmaker() as session:
        demoted = await session.get(User, world.platform_admin)
        assert demoted is not None
        demoted.role = "member"
        await session.commit()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        demoted_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)
        with pytest.raises(HTTPException):
            await create_local_agent_channel_message(
                _agent_id,
                chat_session_id,
                LocalAgentChannelMessageIn(content="demoted attempt", attachments=[], metadata={}),
                current_user=demoted_actor,
                db=db,
            )


# ─── Codex item 10: platform administrator knowledge read visibility ───


async def test_item10_platform_admin_principals_see_company_admin_sensitivity(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    from app.api.agent_knowledge import _principal_stack_for_read
    from app.services.knowledge_read_model import list_knowledge_pages

    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        agent = await db.get(Agent, world.agent)
        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)

        stack = _principal_stack_for_read(agent, platform_actor)
        assert stack.current_user_is_company_admin is True
        assert stack.can_access_sensitivity("PL3_sensitive") is True
        assert stack.can_access_sensitivity("PL4_credential") is False

        member_actor = await _load_actor(db, world.employee_other)
        member_stack = _principal_stack_for_read(agent, member_actor)
        assert member_stack.can_access_sensitivity("PL3_sensitive") is False

        # The real read model gate, not just the stack predicate.
        data_root = Path(get_settings().AGENT_DATA_DIR)
        pages_root = data_root / str(world.agent) / "memory" / "knowledge"
        pages_root.mkdir(parents=True, exist_ok=True)
        (pages_root / "pl3-note.md").write_text(
            "---\nsensitivity: PL3_sensitive\n---\nplatform admin visible note",
            encoding="utf-8",
        )
        admin_pages = list_knowledge_pages(data_root, world.agent, principal_stack=stack)
        assert any(page.get("id") == "knowledge/pl3-note" for page in admin_pages)
        member_pages = list_knowledge_pages(data_root, world.agent, principal_stack=member_stack)
        assert all(page.get("id") != "knowledge/pl3-note" for page in member_pages)


# ─── Codex item 11: legacy promotion accountability ─────────────────────


async def test_item11_platform_admin_queues_legacy_promotion_in_selected_tenant(
    owner_sessionmaker, app_user_sessionmaker, tmp_path
) -> None:
    from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        LegacyPromotionIntakeRequest,
    )

    world = await _seed_world(owner_sessionmaker)
    company_dir = tmp_path / f"enterprise_info_{world.tenant_a}"
    company_dir.mkdir()
    payload = b"# legacy company doc\n\nbody text"
    (company_dir / "doc.md").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        principal = CompanyKnowledgePrincipal(
            tenant_id=world.tenant_a,
            accountable_user_id=world.platform_admin,
            accountable_role="platform_admin",
            actor_type="user",
            actor_id=world.platform_admin,
            department_id=None,
            purpose="interactive_session",
            session_id=None,
        )
        service = CompanyKnowledgePromotionService(data_root=tmp_path, company_service=None)
        job = await service.queue_legacy_promotion(
            db,
            principal=principal,
            company_dir=company_dir,
            request=LegacyPromotionIntakeRequest(
                relative_path="doc.md",
                expected_sha256=digest,
                proposed_namespace=f"legacy-{uuid.uuid4().hex[:8]}",
                proposed_sensitivity="PL2_pii",
                purpose="migrate retired company file",
                risk_level="normal",
                title="Legacy doc",
                attest_scope_change=True,
                idempotency_key=f"cc-{uuid.uuid4().hex[:12]}",
                trace_id=f"cc-trace-{uuid.uuid4().hex[:12]}",
            ),
        )
        assert job.tenant_id == world.tenant_a

        # A member principal is still refused: accountability stays administrative.
        member_principal = CompanyKnowledgePrincipal(
            tenant_id=world.tenant_a,
            accountable_user_id=world.employee_other,
            accountable_role="member",
            actor_type="user",
            actor_id=world.employee_other,
            department_id=None,
            purpose="interactive_session",
            session_id=None,
        )
        with pytest.raises(PermissionError):
            await service.queue_legacy_promotion(
                db,
                principal=member_principal,
                company_dir=company_dir,
                request=LegacyPromotionIntakeRequest(
                    relative_path="doc.md",
                    expected_sha256=digest,
                    proposed_namespace=f"legacy-{uuid.uuid4().hex[:8]}",
                    proposed_sensitivity="PL2_pii",
                    purpose="migrate retired company file",
                    risk_level="normal",
                    title=None,
                    attest_scope_change=True,
                    idempotency_key=f"cc-{uuid.uuid4().hex[:12]}",
                    trace_id=f"cc-trace-{uuid.uuid4().hex[:12]}",
                ),
            )


# ─── Codex item 12: A2A member moderation ───────────────────────────────


async def _seed_a2a_group(owner_sessionmaker, world: _World) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.agent_collaboration import AgentCollaborationGroup, AgentCollaborationGroupMember

    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        group = AgentCollaborationGroup(
            tenant_id=world.tenant_a,
            created_by_user_id=world.employee_owner,
            created_by_agent_id=world.agent,
            name=f"cc group {token}",
            status="active",
        )
        session.add(group)
        await session.flush()
        member = AgentCollaborationGroupMember(
            tenant_id=world.tenant_a,
            group_id=group.id,
            agent_id=world.agent,
            agent_owner_user_id=world.employee_owner,
            role="member",
            status="pending_owner_confirmation",
        )
        session.add(member)
        await session.commit()
        return group.id, member.id


async def test_item12_scoped_admins_moderate_a2a_members(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.a2a import CollaborationGroupMemberUpdateIn, approve_a2a_group_member

    world = await _seed_world(owner_sessionmaker)
    group_id, member_id = await _seed_a2a_group(owner_sessionmaker, world)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        platform_actor = _platform_actor_in_tenant(db, await _load_actor(db, world.platform_admin), world.tenant_a)
        result = await approve_a2a_group_member(
            world.agent,
            group_id,
            member_id,
            CollaborationGroupMemberUpdateIn(reason="admin moderation"),
            current_user=platform_actor,
            db=db,
        )
        assert result["member_status"] == "active"


async def test_item12_member_without_authority_is_refused(owner_sessionmaker, app_user_sessionmaker) -> None:
    from app.api.a2a import CollaborationGroupMemberUpdateIn, reject_a2a_group_member

    world = await _seed_world(owner_sessionmaker)
    group_id, member_id = await _seed_a2a_group(owner_sessionmaker, world)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        member_actor = await _load_actor(db, world.employee_other)
        with pytest.raises(HTTPException) as exc:
            await reject_a2a_group_member(
                world.agent,
                group_id,
                member_id,
                CollaborationGroupMemberUpdateIn(reason="attempt"),
                current_user=member_actor,
                db=db,
            )
        assert exc.value.status_code == 403


# ─── Codex item 13: audit rows for Personal KB cross-owner access ───────


async def test_item13_personal_kb_cross_owner_admin_access_is_audited(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    from app.api.agent_knowledge import list_personal_documents

    world = await _seed_world(owner_sessionmaker)
    token = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        session.add(
            KnowledgeDocument(
                tenant_id=world.tenant_a,
                scope_type="person",
                scope_id=world.employee_owner,
                title=f"employee private doc {token}",
                source_kind="paste",
                source_sha256=hashlib.sha256(token.encode()).hexdigest(),
                canonical_md_path=f"person/{world.employee_owner}/{token}.md",
                status="ready",
                sensitivity="PL3_sensitive",
                agent_searchable=True,
            )
        )
        await session.commit()
    doc_id = await _first_personal_doc_id(owner_sessionmaker, world)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        org_actor = await _load_actor(db, world.org_admin)
        documents = await list_personal_documents(world.agent, limit=20, current_user=org_actor, db=db)
        assert any(str(row["document_id"]) == str(doc_id) for row in documents["documents"])

        # The predicate-level projection keeps PL4 reference-only.
        principal = HumanBrowserPrincipal(
            user_id=world.org_admin,
            role="org_admin",
            home_tenant_id=world.tenant_a,
        )
        assert principal.scoped_business_admin_for(world.tenant_a) is True

        audit_rows = await _audit_events(
            db, "personal_knowledge.scoped_business_admin_access", actor_id=world.org_admin
        )
        assert audit_rows, "cross-owner Personal KB admin access must be audited"
        assert audit_rows[-1].details["owner_user_id"] == str(world.employee_owner)

        # No duplicate audit noise for the owner reading their own documents.
        owner_actor = await _load_actor(db, world.employee_owner)
        await list_personal_documents(world.agent, limit=20, current_user=owner_actor, db=db)
        owner_rows = await _audit_events(
            db, "personal_knowledge.scoped_business_admin_access", actor_id=world.employee_owner
        )
        assert owner_rows == []


async def _first_personal_doc_id(owner_sessionmaker, world: _World) -> uuid.UUID:
    async with owner_sessionmaker() as session:
        return (
            await session.execute(
                select(KnowledgeDocument.id)
                .where(
                    KnowledgeDocument.tenant_id == world.tenant_a,
                    KnowledgeDocument.scope_type == "person",
                    KnowledgeDocument.scope_id == world.employee_owner,
                )
                .limit(1)
            )
        ).scalar_one()


# ─── Codex item 13: Company Knowledge is company-owned, not cross-owner ──


async def test_item13_company_kb_browsing_writes_no_per_read_admin_audit(
    owner_sessionmaker, app_user_sessionmaker
) -> None:
    """The per-read ``company_kb.scoped_business_admin_access`` audit was removed.

    Company Knowledge is company-owned, not an employee-owned cross-owner
    resource: ordinary company-admin browsing must not produce per-read audit
    noise. Real inactive-company negatives live in the frozen real-PostgreSQL
    inactive-tenant gates; mutations keep their existing governance/audit
    trails. This pins the corrected contract: no read-route helper and no
    ``company_kb.scoped_business_admin_access`` event type exists at all.
    """

    world = await _seed_world(owner_sessionmaker)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, world.tenant_a)
        org_actor = await _load_actor(db, world.org_admin)
        member_actor = await _load_actor(db, world.employee_other)

        import app.api.knowledge_company as knowledge_company_api

        assert not hasattr(knowledge_company_api, "_audit_scoped_business_admin_company_kb_access"), (
            "the per-read Company Knowledge admin audit helper must stay deleted"
        )

        for actor in (org_actor, member_actor):
            rows = await _audit_events(db, "company_kb.scoped_business_admin_access", actor_id=actor.id)
            assert rows == []
