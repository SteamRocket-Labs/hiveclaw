from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


class _CreateDraftDB:
    def __init__(self) -> None:
        self.added = []

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


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

    # Network retries of the exact same authenticated decision are idempotent.
    confirm_hr_creation_draft_record(
        draft,
        confirming_user_id=confirmer,
        blueprint_version=2,
        blueprint_hash="sha256:canonical",
        now=now + timedelta(seconds=1),
    )
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


def test_confirmation_rejects_expired_blueprint_even_before_background_sweep():
    from app.services.hr_creation_service import HrCreationConflict, confirm_hr_creation_draft_record

    draft = _draft()
    now = datetime.now(UTC)
    draft.expires_at = now - timedelta(seconds=1)

    with pytest.raises(HrCreationConflict, match="expired") as exc:
        confirm_hr_creation_draft_record(
            draft,
            confirming_user_id=draft.requested_by_user_id,
            blueprint_version=draft.blueprint_version,
            blueprint_hash=draft.blueprint_hash,
            now=now,
        )

    assert exc.value.code == "expired"


@pytest.mark.asyncio
async def test_new_hr_preview_initializes_steps_for_async_safe_serialization() -> None:
    from sqlalchemy import inspect
    from sqlalchemy.orm.attributes import NO_VALUE

    from app.services.hr_creation_service import upsert_hr_creation_draft

    db = _CreateDraftDB()
    draft = await upsert_hr_creation_draft(
        db,
        tenant_id=uuid4(),
        hr_agent_id=uuid4(),
        session_id=uuid4(),
        requested_by_user_id=uuid4(),
        preview_payload={
            "blueprint_hash": "sha256:preview",
            "blueprint": {"name": "Researcher"},
        },
    )

    assert inspect(draft).attrs.provisioning_steps.loaded_value is not NO_VALUE
    assert draft.provisioning_steps == []
    assert draft.status == "awaiting_confirmation"
    assert draft.expires_at is not None
    assert timedelta(days=6, hours=23) < draft.expires_at - datetime.now(UTC) <= timedelta(days=7)


def test_claim_is_idempotent_and_lease_bounded():
    from app.services.hr_creation_service import HrCreationConflict, claim_hr_creation_draft_record

    draft = _draft()
    now = datetime.now(UTC)
    draft.status = "confirmed"

    claim = claim_hr_creation_draft_record(draft, now=now, lease_seconds=120)

    assert claim.state == "claimed"
    assert claim.token == draft.claim_token
    assert claim.version == 1
    assert draft.status == "creating"
    assert draft.creation_idempotency_key == f"hr-draft:{draft.id}"
    assert draft.claim_expires_at == now + timedelta(seconds=120)

    with pytest.raises(HrCreationConflict, match="in progress"):
        claim_hr_creation_draft_record(
            draft,
            now=now + timedelta(seconds=30),
            lease_seconds=120,
        )

    reclaimed = claim_hr_creation_draft_record(
        draft,
        now=now + timedelta(seconds=121),
        lease_seconds=120,
    )
    assert reclaimed.state == "claimed"
    assert reclaimed.version == 2
    assert reclaimed.token != claim.token


def test_completed_draft_returns_existing_asset_without_reexecution():
    from app.services.hr_creation_service import claim_hr_creation_draft_record

    draft = _draft()
    draft.status = "completed"
    draft.created_agent_id = uuid4()
    draft.creation_idempotency_key = f"hr-draft:{draft.id}"

    claim = claim_hr_creation_draft_record(draft, now=datetime.now(UTC))
    assert claim.state == "completed"
    assert claim.token is None


def test_claim_renewal_is_fenced_against_stale_workers():
    from app.services.hr_creation_service import (
        HrCreationConflict,
        claim_hr_creation_draft_record,
        renew_hr_creation_claim_record,
    )

    draft = _draft()
    draft.status = "confirmed"
    now = datetime.now(UTC)
    first = claim_hr_creation_draft_record(draft, now=now, lease_seconds=60)

    renewed_until = renew_hr_creation_claim_record(
        draft,
        claim=first,
        now=now + timedelta(seconds=30),
        lease_seconds=90,
    )
    assert renewed_until == now + timedelta(seconds=120)
    assert draft.claim_heartbeat_at == now + timedelta(seconds=30)

    second = claim_hr_creation_draft_record(
        draft,
        now=now + timedelta(seconds=121),
        lease_seconds=60,
    )
    with pytest.raises(HrCreationConflict, match="stale") as exc:
        renew_hr_creation_claim_record(
            draft,
            claim=first,
            now=now + timedelta(seconds=122),
            lease_seconds=60,
        )
    assert exc.value.code == "stale_claim"
    assert second.version == first.version + 1


def test_blueprint_validation_happens_without_mutating_the_claim():
    from app.services.hr_creation_service import HrCreationConflict, validate_hr_creation_blueprint

    draft = _draft()
    draft.status = "confirmed"
    draft.blueprint_json = {"name": " ", "role_description": "Research markets."}

    with pytest.raises(HrCreationConflict, match="name") as exc:
        validate_hr_creation_blueprint(draft.blueprint_json)

    assert exc.value.code == "invalid_blueprint"
    assert draft.status == "confirmed"
    assert draft.claim_expires_at is None


def test_ready_is_derived_only_from_required_step_receipts():
    from app.models.hr_creation import HrProvisioningStep
    from app.services.hr_creation_service import derive_hr_provisioning_readiness

    draft = _draft()
    required = HrProvisioningStep(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        step_key="workspace",
        step_kind="workspace",
        required=True,
        status="failed",
        input_hash="sha256:workspace",
    )
    optional = HrProvisioningStep(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        step_key="optional:telemetry",
        step_kind="optional",
        required=False,
        status="failed",
        input_hash="sha256:optional",
        error_message="telemetry unavailable",
    )

    blocked = derive_hr_provisioning_readiness([required, optional])
    assert blocked.ready is False
    assert blocked.creation_state == "provisioning_failed"
    assert blocked.blocking_step_keys == ("workspace",)

    required.status = "completed"
    ready = derive_hr_provisioning_readiness([required, optional])
    assert ready.ready is True
    assert ready.creation_state == "ready_with_warnings"
    assert ready.warning_step_keys == ("optional:telemetry",)


def test_completed_transition_refuses_missing_required_steps_and_fences_claim():
    from app.models.hr_creation import HrProvisioningStep
    from app.services.hr_creation_service import (
        HrCreationConflict,
        claim_hr_creation_draft_record,
        mark_hr_creation_completed_record,
    )

    draft = _draft()
    draft.status = "confirmed"
    claim = claim_hr_creation_draft_record(draft)
    agent_id = uuid4()
    steps = [
        HrProvisioningStep(
            tenant_id=draft.tenant_id,
            draft_id=draft.id,
            step_key="workspace",
            step_kind="workspace",
            required=True,
            status="pending",
            input_hash="sha256:workspace",
        ),
        HrProvisioningStep(
            tenant_id=draft.tenant_id,
            draft_id=draft.id,
            step_key="finalize",
            step_kind="finalize",
            required=True,
            status="pending",
            input_hash="sha256:finalize",
        ),
    ]

    with pytest.raises(HrCreationConflict, match="required provisioning") as exc:
        mark_hr_creation_completed_record(
            draft,
            claim=claim,
            steps=steps,
            agent_id=agent_id,
            provisioning={},
        )
    assert exc.value.code == "required_steps_incomplete"
    assert draft.status == "creating"

    steps[0].status = "completed"
    mark_hr_creation_completed_record(
        draft,
        claim=claim,
        steps=steps,
        agent_id=agent_id,
        provisioning={"workspace": "completed"},
    )
    assert draft.status == "completed"
    assert steps[1].status == "completed"
    assert draft.claim_token is None


def test_public_step_payload_keeps_operator_receipts_and_raw_errors_private():
    import json

    from app.models.hr_creation import HrProvisioningStep
    from app.services.hr_creation_service import hr_provisioning_step_payload

    draft = _draft()
    step = HrProvisioningStep(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        step_key="capability:mcp:github",
        step_kind="mcp_server",
        required=True,
        status="failed",
        input_hash="sha256:mcp",
        receipt_json={"provider_payload": {"api_key": "sk-secret", "request_id": "internal-1"}},
        error_code="exception",
        error_message="request failed with api_key=sk-secret at internal.service.local",
    )

    payload = hr_provisioning_step_payload(step)
    serialized = json.dumps(payload)

    assert "receipt" not in payload
    assert payload["evidence_available"] is True
    assert payload["error_message"] == "This required capability is not ready. Resolve it, then resume provisioning."
    assert "sk-secret" not in serialized
    assert "internal.service.local" not in serialized


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
        saved_draft = (await db.execute(select(HrCreationDraft).where(HrCreationDraft.id == draft_id))).scalar_one()
        saved_employee = (await db.execute(select(Agent).where(Agent.id == employee_id))).scalar_one()
        assert saved_draft.created_agent_id == employee_id
        assert saved_employee.id == employee_id
