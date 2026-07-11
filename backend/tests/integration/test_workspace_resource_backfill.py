from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest


async def test_workspace_resource_backfill_is_dry_run_first_and_quarantines_unknown_owner(
    owner_sessionmaker,
    tmp_path,
) -> None:
    from app.services.workspace_resource_authority import backfill_legacy_workspace_resources

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Resource Tenant", slug=f"resource-{suffix}"))
        db.add(
            User(
                id=user_id,
                username=f"resource-{suffix}",
                email=f"resource-{suffix}@example.test",
                password_hash="x",
                display_name="Resource Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                creator_id=user_id,
                owner_user_id=user_id,
                name="Resource Agent",
                status="idle",
            )
        )
        await db.commit()

    legacy = tmp_path / str(agent_id) / "workspace" / "legacy" / "report.xlsx"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy-xlsx")

    async with owner_sessionmaker() as db:
        dry_run = await backfill_legacy_workspace_resources(
            db,
            tenant_id=tenant_id,
            data_root=tmp_path,
            apply=False,
        )
        assert dry_run["mode"] == "dry_run"
        assert dry_run["counts"]["missing"] == 1
        assert (
            await db.execute(select(WorkspaceResourceManifest).where(WorkspaceResourceManifest.agent_id == agent_id))
        ).scalars().all() == []

        applied = await backfill_legacy_workspace_resources(
            db,
            tenant_id=tenant_id,
            data_root=tmp_path,
            apply=True,
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        repeated = await backfill_legacy_workspace_resources(
            db,
            tenant_id=tenant_id,
            data_root=tmp_path,
            apply=False,
        )
        row = (
            await db.execute(
                select(WorkspaceResourceManifest).where(
                    WorkspaceResourceManifest.agent_id == agent_id,
                    WorkspaceResourceManifest.path == "workspace/legacy/report.xlsx",
                )
            )
        ).scalar_one()

    assert applied["counts"]["quarantined"] == 1
    assert repeated["counts"]["current"] == 1
    assert row.owner_user_id is None
    assert row.root_session_id is None
    assert row.authority_state == "quarantined"
    assert row.source == "legacy_filesystem_backfill"
    assert row.content_hash == hashlib.sha256(b"legacy-xlsx").hexdigest()
