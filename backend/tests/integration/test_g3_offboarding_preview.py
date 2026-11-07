import json
from datetime import datetime, timezone

import pytest
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
