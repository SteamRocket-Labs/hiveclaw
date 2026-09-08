"""HR-provisioned triggers must carry the trusted claim authority.

WHY THIS FILE EXISTS
--------------------
``run_hr_provisioning`` created its ``first_task_boot`` (and blueprint
scheduled) triggers with only the plan-exemption stamp. The trigger daemon
attributes the fired RuntimeTask ledger row from the trigger config authority
keys (``batch_trigger_authority``); without ``created_by``/``root_session_id``
both stay NULL, and the owner-facing runtime-task listing and autonomy
overview — which filter by ``root_user_id == requester`` first — correctly
hide the completed task from the very owner who confirmed the HR blueprint.
The fix stamps the trusted identity returned by
``_claim_canonical_hr_blueprint`` (authenticated confirmer + preview session)
onto every HR-created trigger; blueprint strings can never mint authority
because ``stamp_trigger_authority`` overwrites candidate fields.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _legacy_hr_boot_config() -> dict:
    """The pre-fix trigger config: exemption stamp only, no authority keys."""

    from app.tools.handlers.hr import _stamp_hr_blueprint_trigger_exemption

    return _stamp_hr_blueprint_trigger_exemption({"at": "2026-09-09T00:00:00+00:00", "trigger_class": "scheduled_job"})


def _fixed_hr_boot_config(*, owner_user_id, root_session_id, candidate: dict | None = None) -> dict:
    """Build the boot-trigger config exactly the way the fixed runner does."""

    from app.services.hr_provisioning_runner import _stamp_hr_trigger_authority
    from app.tools.handlers.hr import _stamp_hr_blueprint_trigger_exemption

    return _stamp_hr_trigger_authority(
        _stamp_hr_blueprint_trigger_exemption(
            candidate or {"at": "2026-09-09T00:00:00+00:00", "trigger_class": "scheduled_job"}
        ),
        owner_user_id=owner_user_id,
        root_session_id=root_session_id,
    )


def test_legacy_hr_trigger_config_reproduces_missing_runtime_task_attribution() -> None:
    from app.services.trigger_daemon import batch_trigger_authority

    trigger = SimpleNamespace(id=uuid4(), config=_legacy_hr_boot_config(), name="first_task_boot", type="once")
    root_user_id, root_session_id = batch_trigger_authority([trigger])
    assert root_user_id is None
    assert root_session_id is None


def test_stamped_hr_trigger_config_attributes_owner_and_session_to_runtime_task() -> None:
    from app.services.trigger_daemon import batch_trigger_authority
    from app.services.trigger_resource_authority import (
        trigger_authority_state,
        trigger_owner_user_id,
        trigger_root_session_id,
    )

    owner_id = uuid4()
    session_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        config=_fixed_hr_boot_config(owner_user_id=owner_id, root_session_id=session_id),
        name="first_task_boot",
        type="once",
    )
    assert trigger_owner_user_id(trigger) == owner_id
    assert trigger_root_session_id(trigger) == session_id
    assert trigger_authority_state(trigger) == "owned"
    root_user_id, root_session_id = batch_trigger_authority([trigger])
    assert root_user_id == owner_id
    assert root_session_id == session_id


def test_blueprint_strings_cannot_mint_trigger_authority() -> None:
    """A model-authored ``created_by`` in the blueprint is overwritten, kept, or quarantined — never trusted."""

    from app.services.trigger_resource_authority import trigger_owner_user_id

    claim_owner = uuid4()
    claim_session = uuid4()
    forged = _fixed_hr_boot_config(
        owner_user_id=claim_owner,
        root_session_id=claim_session,
        candidate={
            "at": "2026-09-09T00:00:00+00:00",
            "trigger_class": "scheduled_job",
            "created_by": str(uuid4()),
            "root_session_id": str(uuid4()),
            "authority_state": "owned",
        },
    )
    trigger = SimpleNamespace(id=uuid4(), config=forged, name="first_task_boot", type="once")
    assert trigger_owner_user_id(trigger) == claim_owner
    assert forged["root_session_id"] == str(claim_session)


def test_run_hr_provisioning_wires_trusted_claim_identity_into_every_trigger_creation_site() -> None:
    from app.services.hr_provisioning_runner import run_hr_provisioning

    source = inspect.getsource(run_hr_provisioning)
    assert source.count("_stamp_hr_trigger_authority(") >= 4  # 2 creations + 2 replay repairs
    for site in ('name="first_task_boot"', "name=_trigger_name"):
        assert site in source
    # The trusted identity comes from the canonical claim tuple, not from args.
    assert "owner_user_id=user_id" in source
    assert "root_session_id=session_id" in source


@pytest.mark.asyncio
async def test_owner_sees_stamped_hr_trigger_while_another_member_is_denied(owner_sessionmaker) -> None:
    """Authorized owner receipt with another-user denial preserved (DB-backed)."""

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent, AgentPermission
    from app.models.tenant import Tenant
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.services.trigger_resource_authority import filter_authorized_triggers

    tenant_id = uuid4()
    owner_id = uuid4()
    other_member_id = uuid4()
    employee_agent_id = uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="HR authority regression", slug=f"hr-auth-{tenant_id.hex[:8]}"))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        owner = User(
            id=owner_id,
            username=f"hr-owner-{owner_id.hex[:8]}",
            email=f"{owner_id.hex[:8]}@hr-auth.test",
            password_hash="x",
            display_name="HR Owner",
            role="member",
            tenant_id=tenant_id,
        )
        other = User(
            id=other_member_id,
            username=f"hr-other-{other_member_id.hex[:8]}",
            email=f"{other_member_id.hex[:8]}@hr-auth.test",
            password_hash="x",
            display_name="Other Member",
            role="member",
            tenant_id=tenant_id,
        )
        employee = Agent(
            id=employee_agent_id,
            tenant_id=tenant_id,
            name="Provisioned Employee",
            role_description="Research.",
            creator_id=owner_id,
            sponsor_user_id=owner_id,
            owner_user_id=owner_id,
            agent_class="internal_tenant",
            status="running",
        )
        db.add_all([owner, other, employee])
        await db.flush()
        # HR provisioning grants company scope "use"; the other member reaches
        # the agent but must still fail the per-resource owner authority check.
        db.add(
            AgentPermission(
                agent_id=employee_agent_id,
                tenant_id=tenant_id,
                scope_type="company",
                access_level="use",
            )
        )
        db.add(
            AgentTrigger(
                agent_id=employee_agent_id,
                tenant_id=tenant_id,
                name="first_task_boot",
                type="once",
                config=_fixed_hr_boot_config(owner_user_id=owner_id, root_session_id=uuid4()),
                reason="Start the first task.",
            )
        )

    async with owner_sessionmaker() as db:
        owner = await db.get(User, owner_id)
        other = await db.get(User, other_member_id)
        triggers = (
            (await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id == employee_agent_id))).scalars().all()
        )
        assert len(triggers) == 1

        owner_visible = await filter_authorized_triggers(db, owner, agent_id=employee_agent_id, triggers=list(triggers))
        assert len(owner_visible) == 1
        assert owner_visible[0][1].authority_source == "resource_owner"

        other_visible = await filter_authorized_triggers(db, other, agent_id=employee_agent_id, triggers=list(triggers))
        assert other_visible == []
