from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.external_capability import ExternalExtensionCatalogEntry
from app.services.capability_factor_intake import (
    capture_capability_factor,
    create_promotion_proposal,
    decide_promotion_proposal,
)


class _ScalarResult:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


class _IntakeSession:
    def __init__(self, execute_values=None):
        self.execute_values = list(execute_values or [])
        self.added = []
        self.commit_calls = 0
        self.rollback_calls = 0

    async def execute(self, _stmt):
        value = self.execute_values.pop(0) if self.execute_values else None
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "rows":
            return _ScalarResult(rows=value[1])
        return _ScalarResult(value=value)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_capture_external_usage_factor_is_not_self_evolution_eligible_or_runtime_active():
    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    db = _IntakeSession()

    result = await capture_capability_factor(
        db,
        tenant_id=tenant_id,
        originating_agent_id=agent_id,
        originating_user_id=user_id,
        data={
            "factor_kind": "external_usage_factor",
            "display_name": "Review Pack worked well",
            "summary": "Owner recommends promoting this external plugin after repeated success.",
            "source_refs": [{"snapshot_id": "snap-1"}],
            "trace_refs": [{"session_id": "session-1"}],
            "upstream_source_ref": "github:acme/review-pack",
            "reuse_score": {"success_count": 4, "failure_count": 0},
            "suggested_scope": "workspace",
        },
    )

    factor = db.added[0]
    review = db.added[1]
    assert result["factor"]["id"] == str(factor.id)
    assert factor.factor_kind == "external_usage_factor"
    assert factor.originating_agent_id == agent_id
    assert factor.authoring_contract_json["self_evolution_eligible"] is False
    assert factor.authoring_contract_json["external_origin"] is True
    assert factor.status == "captured"
    assert review.factor_id == factor.id
    assert review.decision == "pending"
    assert not any(isinstance(row, ExternalExtensionCatalogEntry) for row in db.added)
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_promotion_proposal_records_decision_without_publishing_catalog():
    tenant_id = uuid4()
    user_id = uuid4()
    factor_id = uuid4()
    review_id = uuid4()
    proposal_id = uuid4()
    factor = SimpleNamespace(
        id=factor_id,
        tenant_id=tenant_id,
        originating_agent_id=uuid4(),
        originating_user_id=user_id,
        factor_kind="skill_candidate",
        source_refs_json=[],
        trace_refs_json=[],
        artifact_ref="evolution/skill_candidates/candidate-1",
        artifact_sha256="sha",
        upstream_source_ref=None,
        upstream_content_sha256=None,
        license_report_json={},
        display_name="Better research brief",
        summary="A locally grown skill candidate.",
        authoring_contract_json={"self_evolution_eligible": True},
        declared_components_json={"skills": ["research-brief"]},
        declared_permissions_json={},
        sensitivity_report_json={},
        reuse_score_json={"reusability": 0.8},
        suggested_scope="workspace",
        status="needs_review",
        created_at=None,
        updated_at=None,
    )
    review = SimpleNamespace(id=review_id, tenant_id=tenant_id, factor_id=factor_id, decision="propose")
    proposal = SimpleNamespace(
        id=proposal_id,
        tenant_id=tenant_id,
        factor_id=factor_id,
        review_id=review_id,
        proposed_snapshot_kind="skill",
        proposed_catalog_scope="workspace",
        proposed_activation_policy="requestable",
        proposed_selector_json={},
        approver_id=None,
        decision="pending",
        decision_reason=None,
        resulting_snapshot_id=None,
        created_at=None,
        updated_at=None,
    )
    db = _IntakeSession([factor, review, proposal, factor])

    proposal_result = await create_promotion_proposal(
        db,
        tenant_id=tenant_id,
        factor_id=factor_id,
        requested_by_user_id=user_id,
        data={
            "proposed_snapshot_kind": "skill",
            "proposed_catalog_scope": "workspace",
            "proposed_activation_policy": "requestable",
            "proposed_selector": {"component": "research-brief"},
        },
    )
    decision_result = await decide_promotion_proposal(
        db,
        tenant_id=tenant_id,
        proposal_id=proposal_id,
        approver_id=user_id,
        decision="approved",
        reason="Verified by owner review",
        resulting_snapshot_id=uuid4(),
    )

    assert proposal_result["proposal"]["decision"] == "pending"
    assert factor.status == "promoted"
    assert decision_result["proposal"]["decision"] == "approved"
    assert proposal.approver_id == user_id
    assert not any(isinstance(row, ExternalExtensionCatalogEntry) for row in db.added)
