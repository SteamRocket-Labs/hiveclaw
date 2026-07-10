from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


def _draft():
    from app.models.hr_creation import HrCreationDraft

    return HrCreationDraft(
        id=uuid4(),
        tenant_id=uuid4(),
        hr_agent_id=uuid4(),
        session_id=uuid4(),
        requested_by_user_id=uuid4(),
        status="awaiting_confirmation",
        blueprint_version=2,
        blueprint_hash="sha256:canonical",
        blueprint_json={"name": "Researcher", "role_description": "Research markets."},
        preview_json={"status": "preview"},
    )


def test_confirmation_binds_authenticated_user_to_exact_version_and_hash():
    from app.services.hr_creation_service import HrCreationConflict, confirm_hr_creation_draft_record

    draft = _draft()
    confirmer = draft.requested_by_user_id
    now = datetime.now(UTC)

    confirm_hr_creation_draft_record(
        draft,
        confirming_user_id=confirmer,
        blueprint_version=2,
        blueprint_hash="sha256:canonical",
        now=now,
    )

    assert draft.status == "confirmed"
    assert draft.confirmed_by_user_id == confirmer
    assert draft.confirmed_at == now

    stale = _draft()
    with pytest.raises(HrCreationConflict, match="version"):
        confirm_hr_creation_draft_record(
            stale,
            confirming_user_id=stale.requested_by_user_id,
            blueprint_version=1,
            blueprint_hash="sha256:canonical",
            now=now,
        )


def test_confirmation_rejects_blueprints_with_unresolved_creation_gates():
    from app.services.hr_creation_service import HrCreationConflict, confirm_hr_creation_draft_record

    draft = _draft()
    draft.preview_json = {"status": "preview", "missing_gates": ["identity", "boundaries"]}

    with pytest.raises(HrCreationConflict, match="unresolved") as exc:
        confirm_hr_creation_draft_record(
            draft,
            confirming_user_id=draft.requested_by_user_id,
            blueprint_version=draft.blueprint_version,
            blueprint_hash=draft.blueprint_hash,
        )

    assert exc.value.code == "missing_gates"
    assert draft.status == "awaiting_confirmation"


def test_claim_is_idempotent_and_lease_bounded():
    from app.services.hr_creation_service import HrCreationConflict, claim_hr_creation_draft_record

    draft = _draft()
    now = datetime.now(UTC)
    draft.status = "confirmed"

    outcome = claim_hr_creation_draft_record(draft, now=now, lease_seconds=120)

    assert outcome == "claimed"
    assert draft.status == "creating"
    assert draft.creation_idempotency_key == f"hr-draft:{draft.id}"
    assert draft.claim_expires_at == now + timedelta(seconds=120)

    with pytest.raises(HrCreationConflict, match="in progress"):
        claim_hr_creation_draft_record(
            draft,
            now=now + timedelta(seconds=30),
            lease_seconds=120,
        )

    assert claim_hr_creation_draft_record(
        draft,
        now=now + timedelta(seconds=121),
        lease_seconds=120,
    ) == "claimed"


def test_completed_draft_returns_existing_asset_without_reexecution():
    from app.services.hr_creation_service import claim_hr_creation_draft_record

    draft = _draft()
    draft.status = "completed"
    draft.created_agent_id = uuid4()
    draft.creation_idempotency_key = f"hr-draft:{draft.id}"

    assert claim_hr_creation_draft_record(
        draft,
        now=datetime.now(UTC),
    ) == "completed"


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_draft_is_the_single_created_agent_binding_source(owner_sessionmaker):
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.hr_creation import HrCreationDraft
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id = uuid4()
    user_id = uuid4()
    hr_agent_id = uuid4()
    session_id = uuid4()
    draft_id = uuid4()
    employee_id = uuid4()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="HR draft binding", slug=f"hr-draft-{tenant_id.hex[:8]}"))

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        user = User(
            id=user_id,
            username=f"hr-draft-{user_id.hex[:10]}",
            email=f"{user_id.hex[:10]}@hr-draft.test",
            password_hash="x",
            display_name="HR Draft Owner",
            tenant_id=tenant_id,
        )
        hr_agent = Agent(
            id=hr_agent_id,
            tenant_id=tenant_id,
            name="__system_hr__",
            role_description="System HR",
            creator_id=user_id,
            sponsor_user_id=user_id,
            owner_user_id=user_id,
            agent_class="internal_system",
        )
        db.add_all([user, hr_agent])
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=hr_agent_id, tenant_id=tenant_id, user_id=user_id))
        draft = HrCreationDraft(
            id=draft_id,
            tenant_id=tenant_id,
            hr_agent_id=hr_agent_id,
            session_id=session_id,
            requested_by_user_id=user_id,
            status="creating",
            blueprint_version=1,
            blueprint_hash="sha256:canonical",
            blueprint_json={"name": "Researcher"},
            preview_json={"status": "preview", "missing_gates": []},
        )
        db.add(draft)
        await db.flush()

        employee = Agent(
            id=employee_id,
            tenant_id=tenant_id,
            name="Researcher",
            role_description="Research markets",
            creator_id=user_id,
            sponsor_user_id=user_id,
            owner_user_id=user_id,
        )
        db.add(employee)
        # The employee row is the referenced result. Flush it before updating
        # the already-persisted canonical draft, while staying in one transaction.
        await db.flush()
        draft.created_agent_id = employee_id
        draft.status = "provisioning"
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        saved_draft = (
            await db.execute(select(HrCreationDraft).where(HrCreationDraft.id == draft_id))
        ).scalar_one()
        saved_employee = (await db.execute(select(Agent).where(Agent.id == employee_id))).scalar_one()
        assert saved_draft.created_agent_id == employee_id
        assert saved_employee.id == employee_id
