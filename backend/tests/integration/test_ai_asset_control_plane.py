"""Real PostgreSQL proof for native apply, immutable revisions, and rollback."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.ai_asset import AIAssetRecord
from app.models.audit import AuditLog
from app.models.config_revision import ConfigRevision
from app.models.skill import Skill, SkillFile
from app.models.tenant import Tenant
from app.models.user import User
from app.services.ai_asset_adapters import project_skill
from app.services.ai_assets import register_agent_asset, register_projection, rollback_asset


async def _seed_principals(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Asset Tenant", slug=f"asset-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"asset-{suffix}",
                email=f"asset-{suffix}@example.test",
                password_hash="x",
                display_name="Asset Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await db.commit()
    return tenant_id, user_id


async def test_agent_rollback_applies_native_row_and_links_new_revision(owner_sessionmaker) -> None:
    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        agent = Agent(
            id=agent_id,
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name="Original Agent",
            role_description="Original role",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        record = await register_agent_asset(
            db,
            agent,
            change_source="create",
            actor_user_id=user_id,
        )
        await db.commit()
        asset_id = record.id

    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.name = "Changed Agent"
        agent.role_description = "Changed role"
        await register_agent_asset(db, agent, change_source="update", actor_user_id=user_id)
        await db.commit()

    async with owner_sessionmaker() as db:
        record, revision = await rollback_asset(
            db,
            tenant_id=tenant_id,
            asset_id=asset_id,
            target_version=1,
            actor_user_id=user_id,
        )
        await db.commit()
        rollback_revision_id = revision.id

    async with owner_sessionmaker() as db:
        _, repeated_revision = await rollback_asset(
            db,
            tenant_id=tenant_id,
            asset_id=asset_id,
            target_version=1,
            actor_user_id=user_id,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        restored = await db.get(Agent, agent_id)
        revisions = (
            (
                await db.execute(
                    select(ConfigRevision)
                    .where(ConfigRevision.entity_type == "ai_asset", ConfigRevision.entity_id == asset_id)
                    .order_by(ConfigRevision.version)
                )
            )
            .scalars()
            .all()
        )
        current = await db.get(AIAssetRecord, asset_id)
        audits = (
            (
                await db.execute(
                    select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "ai_asset.rollback")
                )
            )
            .scalars()
            .all()
        )

    assert restored.name == "Original Agent"
    assert restored.role_description == "Original role"
    assert [item.version for item in revisions] == [1, 2, 3, 4]
    assert revisions[-2].rollback_of_revision_id == revisions[0].id
    assert revisions[-2].id == rollback_revision_id
    assert revisions[-1].rollback_of_revision_id == revisions[0].id
    assert repeated_revision.version == 4
    assert current.active_revision_id == repeated_revision.id
    assert len(audits) == 2
    assert all(audit.details["target_version"] == 1 for audit in audits)


async def test_deleted_registry_skill_can_be_recreated_from_active_revision(owner_sessionmaker) -> None:
    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    skill_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        skill = Skill(
            id=skill_id,
            tenant_id=tenant_id,
            name="Deploy Review",
            description="Review deployments",
            category="engineering",
            icon="D",
            folder_name="deploy-review",
            is_builtin=False,
            is_default=False,
        )
        skill.files = [SkillFile(path="SKILL.md", content="original instructions")]
        db.add(skill)
        await db.flush()
        record = await register_projection(
            db,
            project_skill(skill, owner_user_id=user_id),
            change_source="create",
            actor_user_id=user_id,
        )
        await db.commit()
        asset_id = record.id

    async with owner_sessionmaker() as db:
        skill = (
            await db.execute(select(Skill).where(Skill.id == skill_id).options(selectinload(Skill.files)))
        ).scalar_one()
        await register_projection(
            db,
            project_skill(skill, owner_user_id=user_id, lifecycle_status="deleted"),
            change_source="delete",
            actor_user_id=user_id,
        )
        await db.delete(skill)
        await db.commit()

    async with owner_sessionmaker() as db:
        _, revision = await rollback_asset(
            db,
            tenant_id=tenant_id,
            asset_id=asset_id,
            target_version=1,
            actor_user_id=user_id,
        )
        await db.commit()
        rollback_revision_id = revision.id

    async with owner_sessionmaker() as db:
        restored = (
            await db.execute(select(Skill).where(Skill.id == skill_id).options(selectinload(Skill.files)))
        ).scalar_one()
        record = await db.get(AIAssetRecord, asset_id)
        revisions = (
            (
                await db.execute(
                    select(ConfigRevision)
                    .where(ConfigRevision.entity_type == "ai_asset", ConfigRevision.entity_id == asset_id)
                    .order_by(ConfigRevision.version)
                )
            )
            .scalars()
            .all()
        )

    assert restored.name == "Deploy Review"
    assert [(item.path, item.content) for item in restored.files] == [("SKILL.md", "original instructions")]
    assert record.lifecycle_status == "active"
    assert record.active_revision_id == rollback_revision_id
    assert [item.version for item in revisions] == [1, 2, 3]
    assert revisions[-1].rollback_of_revision_id == revisions[0].id


async def test_file_native_backfill_is_dry_run_first_and_quarantines_unknown_owner(
    owner_sessionmaker, tmp_path
) -> None:
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import definition_store_for_agent, definition_store_for_tenant
    from app.services.ai_assets import backfill_file_native_assets

    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    agent_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=user_id,
                owner_user_id=user_id,
                name="Asset Agent",
                status="idle",
            )
        )
        await db.commit()

    skill_dir = tmp_path / str(agent_id) / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Review\ndescription: Review changes.\n---\n\n# Review\n",
        encoding="utf-8",
    )
    definition_store_for_agent(agent_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="agent-reviewer", description="Review", system_prompt="Review changes.")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=tmp_path).save(
        SubagentSpec(name="legacy-reviewer", description="Review", system_prompt="Review changes.")
    )

    async with owner_sessionmaker() as db:
        dry_run = await backfill_file_native_assets(
            db,
            tenant_id=tenant_id,
            data_root=tmp_path,
            apply=False,
        )
        assert dry_run["counts"]["missing"] == 3
        assert (
            await db.execute(select(AIAssetRecord).where(AIAssetRecord.tenant_id == tenant_id))
        ).scalars().all() == []

        applied = await backfill_file_native_assets(
            db,
            tenant_id=tenant_id,
            data_root=tmp_path,
            apply=True,
            actor_user_id=user_id,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        repeated = await backfill_file_native_assets(
            db,
            tenant_id=tenant_id,
            data_root=tmp_path,
            apply=False,
        )
        rows = list(
            (
                await db.execute(
                    select(AIAssetRecord).where(AIAssetRecord.tenant_id == tenant_id).order_by(AIAssetRecord.native_key)
                )
            ).scalars()
        )

    assert applied["counts"]["registered"] == 3
    assert repeated["counts"]["current"] == 3
    assert len(rows) == 3
    legacy = next(row for row in rows if row.native_key.endswith(":legacy-reviewer"))
    assert legacy.lifecycle_status == "quarantined"
    assert legacy.trust_state == "review_required"
    assert legacy.admission_state == "review_required"


async def test_config_revision_snapshot_fields_are_database_immutable(owner_sessionmaker) -> None:
    tenant_id, user_id = await _seed_principals(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent = Agent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            creator_id=user_id,
            owner_user_id=user_id,
            name="Immutable Agent",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        record = await register_agent_asset(db, agent, change_source="create", actor_user_id=user_id)
        await db.commit()

    async with owner_sessionmaker() as db:
        trigger_names = set(
            (
                await db.execute(
                    text(
                        "SELECT tgname FROM pg_trigger "
                        "WHERE tgrelid = 'config_revisions'::regclass AND NOT tgisinternal"
                    )
                )
            ).scalars()
        )
        revision = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        functions = set(
            (
                await db.execute(
                    text("SELECT proname FROM pg_proc WHERE proname = 'enforce_config_revision_immutability'")
                )
            ).scalars()
        )
        assert "trg_config_revision_immutability" in trigger_names, (revision, functions)
        stored = (
            await db.execute(
                select(ConfigRevision).where(
                    ConfigRevision.entity_type == "ai_asset",
                    ConfigRevision.entity_id == record.id,
                )
            )
        ).scalar_one()
        assert stored.content.get("asset_type") == "agent"
        with pytest.raises(DBAPIError, match="config revision snapshots are immutable"):
            await db.execute(
                update(ConfigRevision)
                .where(ConfigRevision.entity_type == "ai_asset", ConfigRevision.entity_id == record.id)
                .values(content={"tampered": True})
            )
            await db.commit()
        await db.rollback()
