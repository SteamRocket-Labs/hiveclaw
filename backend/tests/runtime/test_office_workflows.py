"""§9 P13 red tests: office workflows — the first real caller, on real PG.

The built-in examples go through the SAME compiler/admission as any agent
submission; the contract-review flow produces workspace artifacts and can be
promoted; the distribution flow's external send NEVER runs without gate
approval.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import get_settings
from app.database import tenant_scoped_session
from app.runtime.workflow_admission import AdmissionLimits, WorkflowAdmissionError, admit_workflow
from app.runtime.workflow_compiler import compile_workflow
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.office_workflow_examples import (
    CONTRACT_REVIEW_EXAMPLE,
    DOCUMENT_DISTRIBUTION_EXAMPLE,
    OFFICE_LEAF_CATALOG,
    OFFICE_WORKFLOW_EXAMPLES,
    WEEKLY_REPORT_EXAMPLE,
)
from app.services.workflow_runtime_service import CheckpointGateDecider, WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-office", slug=f"wo-{tid.hex[:10]}"))
    return tid


# ── the examples are data and pass the standard gates ─────────────


def test_every_office_example_compiles_against_the_office_catalog():
    for name, definition in OFFICE_WORKFLOW_EXAMPLES.items():
        compiled = compile_workflow(definition, known_leaves=set(OFFICE_LEAF_CATALOG))
        assert compiled.definition.name == name


def test_office_examples_pass_admission_with_realistic_args():
    limits = AdmissionLimits.from_settings(get_settings())
    cases = {
        "office-contract-review": {"doc_path": "contracts/msa.docx"},
        "office-weekly-report": {"sources": ["sales.xlsx", "support.csv"], "week": "2026-W23"},
        "office-document-distribution": {"doc_path": "out/brief.docx", "recipients": ["a@x.com"]},
    }
    for name, args in cases.items():
        compiled = compile_workflow(OFFICE_WORKFLOW_EXAMPLES[name])
        result = admit_workflow(compiled, args=args, limits=limits)
        assert result.admitted is True


def test_office_leaves_are_capability_bound():
    """Leaf capability binding is real: an empty catalog refuses the example."""
    with pytest.raises(Exception, match="leaves"):
        compile_workflow(CONTRACT_REVIEW_EXAMPLE, known_leaves={"some-other-leaf"})

    compiled = compile_workflow(WEEKLY_REPORT_EXAMPLE)
    with pytest.raises(WorkflowAdmissionError, match="leaf"):
        admit_workflow(
            compiled,
            args={"sources": ["a"], "week": "W1"},
            limits=AdmissionLimits.from_settings(get_settings()),
            allowed_leaves={"not-office"},
        )


# ── contract review: artifacts + promotable ───────────────────────


async def test_contract_review_produces_artifacts_and_promote_proposal(
    tenant_id, owner_sessionmaker, workflow_principals
):
    service = WorkflowRuntimeService(session_factory=owner_sessionmaker)
    from app.models.chat_session import ChatSession

    session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as db:
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=workflow_principals.agent_id,
                user_id=workflow_principals.user_id,
                title="Office contract review",
                source_channel="web",
            )
        )

    async def office_leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(
            ok=True,
            output={"text": f"done: {request.step_id}", "sources": []},
            result_ref=f"workspace/artifacts/{request.step_id}.md",
            tokens_used=10,
        )

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=CONTRACT_REVIEW_EXAMPLE,
        args={"doc_path": "contracts/msa.docx"},
        leaf_executor=office_leaf,
        agent_id=workflow_principals.agent_id,
        user_id=workflow_principals.user_id,
        parent_session_id=session_id,
        root_session_id=session_id,
    )

    assert handle.outcome.status == "completed"
    loaded = await service.load_run(handle.run_id, tenant_id=tenant_id)
    assert loaded is not None
    statuses = {step.step_id: step.status for step in loaded.steps}
    assert statuses == {"parse": "done", "clauses": "done", "risk-table": "done"}
    artifact_refs = [step.result_ref for step in loaded.steps if step.result_ref]
    assert any("risk-table" in (ref or "") for ref in artifact_refs), "the risk table artifact must be journaled"

    # …and only the initiating session owner can submit its immutable evidence.
    from app.services.workflow_promotion_service import WorkflowPromotionService

    promotions = WorkflowPromotionService(session_factory=owner_sessionmaker)
    proposal = await promotions.submit(
        tenant_id=tenant_id,
        agent_id=workflow_principals.agent_id,
        run_id=handle.run_id,
        requester_user_id=workflow_principals.user_id,
    )
    assert proposal.status == "pending"
    assert proposal.run_id == handle.run_id


# ── distribution: the external send is gate-locked ────────────────


async def test_external_send_requires_gate_approval(tenant_id, owner_sessionmaker):
    from app.agents.coordination import CoordinationRuntime

    service = WorkflowRuntimeService(
        session_factory=owner_sessionmaker,
        gate_decider=CheckpointGateDecider(CoordinationRuntime()),
    )
    sent: list[str] = []

    async def office_leaf(request: LeafRequest) -> LeafOutcome:
        if request.step_id == "send":
            sent.append(request.task)
        return LeafOutcome(ok=True, output={"text": "ok"}, tokens_used=5)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=DOCUMENT_DISTRIBUTION_EXAMPLE,
        args={"doc_path": "out/brief.docx", "recipients": ["a@x.com"]},
        leaf_executor=office_leaf,
    )

    assert handle.outcome.status == "suspended", "the run must stop at the approval gate"
    assert sent == [], "the external send must NEVER run before approval"

    assert service.gate_decider.approve(str(handle.run_id), "approve-send") is True
    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=office_leaf)

    assert outcome.status == "completed"
    assert len(sent) == 1, "after approval the send runs exactly once"
