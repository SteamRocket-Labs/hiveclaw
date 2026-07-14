"""Completed ephemeral run evidence stays neutral until agent/user review."""

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


async def _run_contract_review(
    service: WorkflowRuntimeService,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    definition_source: str = "ephemeral",
    run_metadata: dict | None = None,
):
    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={"text": request.step_id}, tokens_used=1)

    return await service.start_run(
        tenant_id=tenant_id,
        definition_data=CONTRACT_REVIEW_EXAMPLE,
        args={"doc_path": "contracts/msa.docx"},
        leaf_executor=leaf,
        agent_id=agent_id,
        definition_source=definition_source,
        run_metadata=run_metadata,
    )


async def test_completed_runs_surface_neutral_review_evidence_without_count_gate(tenant_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(3):
        handle = await _run_contract_review(service, tenant_id)
        assert handle.outcome.status == "completed"

    suggestions = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)

    matching = [s for s in suggestions if s.name == "office-contract-review"]
    assert len(matching) == 1, "the repeated flow must surface exactly one suggestion"
    assert matching[0].run_count >= 3
    assert len(matching[0].sample_run_ids) >= 3
    assert "promotion_eligible" not in matching[0].quality_evidence
    assert matching[0].quality_evidence["model_promotion_review"] == "not_requested"


async def test_dynamic_workflow_repeats_produce_suggestion_with_quality_evidence(tenant_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(3):
        handle = await _run_contract_review(
            service,
            tenant_id,
            definition_source="dynamic_workflow",
            run_metadata={
                "dynamic_workflow": {
                    "proposal_id": "proposal-1",
                    "candidate_id": "fanout-critic",
                    "success_criteria": ["Every contract slice cites evidence."],
                    "failure_policy": {"repair_rounds": 1},
                }
            },
        )
        assert handle.outcome.status == "completed"

    suggestions = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)

    matching = [s for s in suggestions if s.name == "office-contract-review"]
    assert len(matching) == 1
    assert matching[0].run_count >= 3
    assert "promotion_eligible" not in matching[0].quality_evidence
    assert matching[0].quality_evidence["model_promotion_review"] == "not_requested"
    assert matching[0].quality_evidence["leaf_failed"] == 0
    assert matching[0].quality_evidence["success_criteria_count"] == 1


async def test_threshold_argument_cannot_hide_completed_evidence(tenant_id, owner_sessionmaker):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(2):
        await _run_contract_review(service, tenant_id)

    suggestions = await collect_promote_suggestions(
        tenant_id=tenant_id,
        session_factory=owner_sessionmaker,
        threshold=999,
    )
    matching = [s for s in suggestions if s.name == "office-contract-review"]
    assert len(matching) == 1
    assert matching[0].run_count == 2


async def test_already_registered_name_is_not_suggested(tenant_id, owner_sessionmaker, workflow_principals):
    from app.services.workflow_definitions import WorkflowDefinitionService

    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    for _ in range(3):
        await _run_contract_review(service, tenant_id)

    definitions = WorkflowDefinitionService(session_factory=owner_sessionmaker)
    record = await definitions.create_draft(
        tenant_id=tenant_id, definition_data=CONTRACT_REVIEW_EXAMPLE, visibility_scope="tenant"
    )
    await definitions.activate(record.id, tenant_id=tenant_id, actor_user_id=workflow_principals.user_id)

    suggestions = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)
    assert all(s.name != "office-contract-review" for s in suggestions), (
        "a flow that already became a template needs no further suggestion"
    )


async def test_agent_filter_scopes_suggestions(tenant_id, owner_sessionmaker):
    """agent_id narrows the evidence to that agent's runs (agent-page surface)."""
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    for _ in range(3):
        await _run_contract_review(service, tenant_id, agent_id=agent_a)
    for _ in range(2):
        await _run_contract_review(service, tenant_id, agent_id=agent_b)

    for_a = await collect_promote_suggestions(tenant_id=tenant_id, agent_id=agent_a, session_factory=owner_sessionmaker)
    matching = [s for s in for_a if s.name == "office-contract-review"]
    assert len(matching) == 1
    assert matching[0].run_count == 3, "B's runs must not inflate A's evidence"

    for_b = await collect_promote_suggestions(tenant_id=tenant_id, agent_id=agent_b, session_factory=owner_sessionmaker)
    assert any(s.name == "office-contract-review" and s.run_count == 2 for s in for_b)

    tenant_wide = await collect_promote_suggestions(tenant_id=tenant_id, session_factory=owner_sessionmaker)
    assert any(s.run_count == 5 for s in tenant_wide if s.name == "office-contract-review")
