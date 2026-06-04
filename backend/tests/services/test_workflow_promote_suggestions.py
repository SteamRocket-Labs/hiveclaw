"""§9 P13 red tests: repeated ephemeral runs → promote suggestion evidence."""

from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.office_workflow_examples import CONTRACT_REVIEW_EXAMPLE
from app.services.workflow_promote_suggestions import collect_promote_suggestions
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-suggest", slug=f"sg-{tid.hex[:10]}"))
    return tid


async def _run_contract_review(service: WorkflowRuntimeService, tenant_id: uuid.UUID):
    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={"text": request.step_id}, tokens_used=1)

    return await service.start_run(
        tenant_id=tenant_id,
        definition_data=CONTRACT_REVIEW_EXAMPLE,
        args={"doc_path": "contracts/msa.docx"},
        leaf_executor=leaf,
    )


async def test_three_repeats_produce_a_suggestion(tenant_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(3):
        handle = await _run_contract_review(service, tenant_id)
        assert handle.outcome.status == "completed"

    suggestions = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)

    matching = [s for s in suggestions if s.name == "office-contract-review"]
    assert len(matching) == 1, "the repeated flow must surface exactly one suggestion"
    assert matching[0].run_count >= 3
    assert len(matching[0].sample_run_ids) >= 3


async def test_below_threshold_stays_silent(tenant_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(2):
        await _run_contract_review(service, tenant_id)

    suggestions = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)
    assert all(s.name != "office-contract-review" for s in suggestions)


async def test_already_registered_name_is_not_suggested(tenant_id, owner_sessionmaker):
    from app.services.workflow_definitions import WorkflowDefinitionService

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(3):
        await _run_contract_review(service, tenant_id)

    definitions = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    record = await definitions.create_draft(
        tenant_id=tenant_id, definition_data=CONTRACT_REVIEW_EXAMPLE, visibility_scope="tenant"
    )
    await definitions.activate(record.id, tenant_id=tenant_id, actor_user_id=uuid.uuid4())

    suggestions = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)
    assert all(s.name != "office-contract-review" for s in suggestions), (
        "a flow that already became a template needs no further suggestion"
    )
