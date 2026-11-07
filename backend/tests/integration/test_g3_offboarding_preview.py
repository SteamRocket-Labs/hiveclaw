import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.notification import Notification
from app.services.user_offboarding_service import build_user_offboarding_preview
from tests.integration.test_offboarding_member_handover import offboard, seed_handover


@pytest.mark.asyncio
async def test_preview_names_match_the_receivers_actual_handover(owner_sessionmaker):
    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        preview = await build_user_offboarding_preview(db, target_user=state.leaver, actor=state.admin)
        assert preview.handover["knowledge"] == [{"id": str(state.doc.id), "title": state.doc.title}]
        assert [item["id"] for item in preview.handover["work"]] == [str(state.task.id)]
        assert preview.handover["triggers"] == [{"id": str(state.trigger.id), "name": state.trigger.name}]
        await offboard(state)
        notice = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id,
            Notification.type == "offboarding_handover"))
        actual = json.loads(notice.body)
        assert {item["id"] for item in preview.owned_agents} == {item["id"] for item in actual["agents"]}
        assert preview.handover["knowledge"] == actual["knowledge"]
        assert {item["id"] for item in preview.handover["work"]} == {item["task_id"] for item in actual["work"]}
        assert {item["id"] for item in preview.handover["triggers"]} == {item["id"] for item in actual["triggers"]}
        assert state.peer_task.created_by == state.peer.id
        await db.rollback()


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
@pytest.mark.asyncio
async def test_company_admin_reads_stored_scope_of_deactivated_bot_but_member_does_not(owner_sessionmaker, role):
    from app.api.agents import get_agent_permissions
    from app.models.agent import AgentPermission

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.admin.role = role
        state.bot.deactivated_at = datetime.now(timezone.utc)
        state.bot.deactivation_reason = "retirement_pending"
        db.add(AgentPermission(tenant_id=state.tenant.id, agent_id=state.bot.id,
            scope_type="user", scope_id=state.successor.id, access_level="use"))
        await db.flush()
        scope = await get_agent_permissions(state.bot.id, current_user=state.admin, db=db)
        assert scope["scope_type"] == "user"
        assert scope["scope_ids"] == [str(state.successor.id)]
        assert scope["scope_names"] == [{"id": str(state.successor.id), "name": state.successor.display_name}]
        assert scope["can_manage_permissions"] is False
        member = await get_agent_permissions(state.bot.id, current_user=state.leaver, db=db)
        assert member["scope_type"] == "effective"
        assert member["scope_ids"] == member["scope_names"] == []
        from types import SimpleNamespace
        from app.database import pin_rls_tenant_context
        foreign_admin = SimpleNamespace(id=state.admin.id, role=role, tenant_id=uuid4())
        await pin_rls_tenant_context(db, foreign_admin.tenant_id)
        with pytest.raises(HTTPException) as denied:
            await get_agent_permissions(state.bot.id, current_user=foreign_admin, db=db)
        assert denied.value.status_code == 404
        await db.rollback()


@pytest.mark.parametrize("inactive", ["owner", "company", "deleted"])
@pytest.mark.asyncio
async def test_include_deactivated_preserves_the_other_lifecycle_filters(owner_sessionmaker, inactive):
    from app.api.agents import list_agents

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.bot.deactivated_at = datetime.now(timezone.utc)
        if inactive == "owner":
            state.leaver.is_active = False
        elif inactive == "company":
            state.tenant.is_active = False
        else:
            state.bot.deleted_at = datetime.now(timezone.utc)
        await db.flush()
        inventory = await list_agents(tenant_id=state.tenant.id, current_user=state.admin, db=db, include_deactivated=True)
        assert inventory == []
        await db.rollback()


@pytest.mark.parametrize("authority_state", [" Quarantined ", "QUARANTINED", "quarantined"])
@pytest.mark.asyncio
async def test_preview_skips_schedules_already_quarantined_after_normalisation(owner_sessionmaker, authority_state):
    from app.services.trigger_resource_authority import quarantine_transferred_trigger_authority

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.trigger.config = {**state.trigger.config, "authority_state": authority_state}
        await db.flush()
        preview = await build_user_offboarding_preview(db, target_user=state.leaver, actor=state.admin,
            successor_user_id=state.successor.id)
        assert preview.handover["triggers"] == []
        actual = await quarantine_transferred_trigger_authority(db, agent_id=state.bot.id,
            previous_owner_id=state.leaver.id, reason="owner_changed")
        assert actual == []
        assert state.trigger.is_enabled is True
        await db.rollback()


@pytest.mark.parametrize("job_status", ["queued", "claimed", "running"])
@pytest.mark.parametrize("owner", ["leaver", "successor"])
@pytest.mark.asyncio
async def test_preview_exposes_the_same_knowledge_processing_blocker_as_submit(owner_sessionmaker, job_status, owner):
    from app.api.users import preview_user_offboarding

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.job.scope_id = getattr(state, owner).id
        state.job.status = state.job.stage = job_status
        await db.flush()
        preview = await preview_user_offboarding(state.leaver.id, tenant_id=str(state.tenant.id),
            current_user=state.admin, db=db, successor_user_id=state.successor.id)
        with pytest.raises(HTTPException) as blocked:
            await offboard(state)
        assert blocked.value.status_code == 409
        assert preview["blockers"] == [blocked.value.detail["message"]]
        assert state.leaver.is_active is True
        assert state.bot.owner_user_id == state.leaver.id
        await db.rollback()


@pytest.mark.asyncio
async def test_audit_keeps_the_bot_name_when_its_owner_is_inactive(owner_sessionmaker):
    from app.api.enterprise import list_audit_logs
    from app.models.audit import AuditLog

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.leaver.is_active = False
        state.bot.deactivated_at = datetime.now(timezone.utc)
        db.add_all([
            AuditLog(tenant_id=state.tenant.id, agent_id=state.bot.id, user_id=state.admin.id,
                action="agent:retirement_requested", details={"schema": "private"}),
            AuditLog(tenant_id=state.tenant.id, user_id=state.admin.id, action="user:offboarded",
                details={"target_user_id": str(state.leaver.id), "already_inactive": True}),
        ])
        await db.flush()
        logs = await list_audit_logs(tenant_id=str(state.tenant.id), current_user=state.admin, db=db)
        assert len(logs) == 2
        bot_log = next(log for log in logs if log.agent_id == state.bot.id)
        assert getattr(bot_log, "agent_name", None) == state.bot.name
        assert "schema" not in bot_log.details
        assert next(log for log in logs if log.agent_id is None).details["already_inactive"] is True
        await db.rollback()


@pytest.mark.asyncio
async def test_admin_inventory_includes_deactivated_bots_without_exposing_them_to_members(owner_sessionmaker):
    from app.api.agents import list_agents

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        state.bot.deactivated_at = datetime.now(timezone.utc)
        await db.flush()
        active = await list_agents(tenant_id=state.tenant.id, current_user=state.admin, db=db)
        inventory = await list_agents(tenant_id=state.tenant.id, current_user=state.admin, db=db, include_deactivated=True)
        member = await list_agents(tenant_id=state.tenant.id, current_user=state.leaver, db=db, include_deactivated=True)
        assert active == []
        assert len(inventory) == 1
        assert inventory[0].owner_user_id == state.leaver.id
        assert inventory[0].deactivated_at is not None
        assert member == []
        await db.rollback()


@pytest.mark.asyncio
async def test_preview_uses_the_selected_receivers_existing_copy_when_knowledge_merges(owner_sessionmaker):
    from app.models.knowledge import KnowledgeDocument

    async with owner_sessionmaker() as db:
        state = await seed_handover(db)
        existing = KnowledgeDocument(tenant_id=state.tenant.id, scope_type="person", scope_id=state.successor.id,
            owner_user_id=state.successor.id, title="Receiver copy", source_kind="paste",
            source_sha256=state.doc.source_sha256, canonical_md_path="receiver-copy.md", status="ready")
        db.add(existing)
        await db.flush()
        preview = await build_user_offboarding_preview(db, target_user=state.leaver, actor=state.admin,
            successor_user_id=state.successor.id)
        expected = [{"id": str(existing.id), "title": "Receiver copy", "merged": True}]
        assert preview.handover["knowledge"] == expected
        await offboard(state)
        notice = await db.scalar(select(Notification).where(Notification.user_id == state.successor.id,
            Notification.type == "offboarding_handover"))
        assert json.loads(notice.body)["knowledge"] == expected
        await db.rollback()
