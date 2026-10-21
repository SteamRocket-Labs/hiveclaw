"""Creation entry uses the same live identity boundary as authentication."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.database import pin_rls_tenant_context
from tests.integration.test_m1g_originated_creation_postgres import origin, direct_origin, _direct_call  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("home_active", [True, False])
async def test_direct_creation_checks_the_platform_requesters_current_home_company(
    direct_origin, owner_sessionmaker, tmp_path, home_active,  # noqa: F811
):
    user, context, _model, _input = direct_origin
    home = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Requester home company", slug=f"creation-home-{home.hex}", is_active=home_active))
        await db.flush()
        principal = await db.get(User, user.id)
        principal.role, principal.tenant_id = "platform_admin", home
        await db.commit()
    result = await _direct_call(context)
    async with owner_sessionmaker() as db:
        drafts = (await db.scalars(select(HrCreationDraft).where(
            HrCreationDraft.tenant_id == uuid.UUID(context.tenant_id), HrCreationDraft.requested_by_user_id == user.id))).all()
        jobs = [await db.get(RuntimeTask, draft.provisioning_task_id) for draft in drafts]
        (tmp_path / "creation-identity-observation.json").write_text(json.dumps({"result": result, "home_active": home_active,
            "draft_count": len(drafts), "jobs": [{"id": str(job.id), "task_type": job.task_type, "status": job.status} for job in jobs]}, indent=2))
    if home_active:
        assert result.get("draft_status") == "confirmed" and result.get("provisioning_task_id"), result
        assert len(jobs) == 1 and jobs[0].task_type == "hr_provisioning" and jobs[0].status == "pending"
    else:
        assert result.get("status") == "error" and result.get("error") == "source_execute_denied", result
    async with owner_sessionmaker() as db:
        drafts = (await db.scalars(select(HrCreationDraft).where(
            HrCreationDraft.tenant_id == uuid.UUID(context.tenant_id), HrCreationDraft.requested_by_user_id == user.id))).all()
        assert len(drafts) == int(home_active)
        assert (await db.get(User, user.id)).tenant_id == home


async def test_creation_rechecks_a_previously_loaded_identity_without_removing_history(
    origin, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from fastapi import HTTPException
    from app.services.hr_creation_source import load_creation_requester, load_creation_source_agent

    user, context = origin
    home = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Cached requester home", slug=f"cached-creation-{home.hex}"))
        await db.flush()
        principal = await db.get(User, user.id)
        principal.role, principal.tenant_id = "platform_admin", home
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, uuid.UUID(context.tenant_id))
        cached = await load_creation_requester(db, user.id)
        assert cached is not None
        assert (await load_creation_source_agent(db, cached, context.agent_id, require_execute=True)).id == context.agent_id
        async with owner_sessionmaker() as writer:
            (await writer.get(Tenant, home)).is_active = False
            await writer.commit()
        with pytest.raises(HTTPException) as blocked:
            await load_creation_source_agent(db, cached, context.agent_id, require_execute=True)
        assert blocked.value.status_code == 403
        historical = await load_creation_requester(db, user.id)
        assert historical is not None and historical.id == user.id and historical.tenant_id == home
