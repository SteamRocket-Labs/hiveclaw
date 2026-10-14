"""Real PostgreSQL checks for tenant audit scope and knowledge identity columns."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.api.enterprise import enterprise_audit_log_scope
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import User
from app.services.company_knowledge_identity import company_knowledge_grant_audiences


async def test_audit_scope_and_identity_columns_round_trip_on_postgres(owner_sessionmaker) -> None:
    suffix = uuid.uuid4().hex[:10]
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ada_id = uuid.uuid4()
    decoy_id = uuid.uuid4()
    open_id = f"ou_{suffix}"
    shared_email = f"pat-{suffix}@example.test"

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="M0A Tenant", slug=f"m0a-{suffix}"))
        db.add(Tenant(id=other_tenant_id, name="M0A Other", slug=f"m0b-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"owner-{suffix}",
                email=f"owner-{suffix}@example.test",
                password_hash="x",
                display_name="M0A Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        db.add(
            User(
                id=ada_id,
                username=f"ada-{suffix}",
                email=f"ada-{suffix}@example.test",
                password_hash="x",
                display_name="Ada",
                tenant_id=tenant_id,
                role="member",
                feishu_open_id=open_id,
                is_active=True,
            )
        )
        db.add(
            User(
                id=decoy_id,
                username=f"decoy-{suffix}",
                email=shared_email,
                password_hash="x",
                display_name="Email Lookalike",
                tenant_id=tenant_id,
                role="member",
                is_active=True,
            )
        )
        await db.flush()
        agent = Agent(
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name=f"M0A Agent {suffix}",
            role_description="Audit scope fixture.",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        marker = {"marker": suffix}
        db.add(
            AuditLog(
                tenant_id=tenant_id,
                agent_id=None,
                user_id=user_id,
                action="user:offboarded",
                details=marker,
            )
        )
        db.add(
            AuditLog(
                tenant_id=None,
                agent_id=agent.id,
                user_id=user_id,
                action="agent:legacy",
                details=marker,
            )
        )
        db.add(
            AuditLog(
                tenant_id=other_tenant_id,
                agent_id=agent.id,
                user_id=user_id,
                action="other:company",
                details=marker,
            )
        )
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                name="Ada Directory",
                feishu_open_id=open_id,
                email=f"ada-dir-{suffix}@example.test",
            )
        )
        db.add(
            OrgMember(
                tenant_id=tenant_id,
                name="Unregistered Pat",
                email=shared_email,
            )
        )
        await db.flush()

        visible = (
            (
                await db.execute(
                    select(AuditLog).where(
                        enterprise_audit_log_scope(tenant_id),
                        AuditLog.details["marker"].astext == suffix,
                    )
                )
            )
            .scalars()
            .all()
        )
        by_agent = (
            (
                await db.execute(
                    select(AuditLog).where(
                        enterprise_audit_log_scope(tenant_id, agent_id=agent.id),
                        AuditLog.details["marker"].astext == suffix,
                    )
                )
            )
            .scalars()
            .all()
        )
        users = list((await db.execute(select(User).where(User.tenant_id == tenant_id))).scalars().all())
        members = list((await db.execute(select(OrgMember).where(OrgMember.tenant_id == tenant_id))).scalars().all())

    visible_actions = {row.action for row in visible}
    assert visible_actions == {"user:offboarded", "agent:legacy"}
    assert {row.action for row in by_agent} == {"agent:legacy"}
    audiences = company_knowledge_grant_audiences(users, members)
    grantable_ids = {item["user_id"] for item in audiences["grantable_users"]}
    assert str(ada_id) in grantable_ids
    assert str(decoy_id) in grantable_ids
    unlinked = {item["label"]: item["reason"] for item in audiences["visible_unlinked"]}
    assert unlinked == {"Unregistered Pat": "unregistered"}
    assert all(item["org_member_id"] not in grantable_ids for item in audiences["visible_unlinked"])
