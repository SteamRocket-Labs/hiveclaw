from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_factor import CapabilityFactor, CapabilityFactorReview, CapabilityPromotionProposal


FACTOR_KINDS = {
    "skill_candidate",
    "subagent_candidate",
    "workflow_pattern",
    "tool_usage_pattern",
    "mcp_usage_pattern",
    "external_usage_factor",
    "external_fork_candidate",
}
PROMOTION_DECISIONS = {"approved", "rejected", "needs_changes"}


async def capture_capability_factor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    originating_agent_id: uuid.UUID | None,
    originating_user_id: uuid.UUID | None,
    data: dict[str, Any],
) -> dict[str, Any]:
    factor_kind = _required_choice(data.get("factor_kind"), allowed=FACTOR_KINDS, field_name="factor_kind")
    contract = _authoring_contract_for_factor(factor_kind, data.get("authoring_contract"))
    factor = CapabilityFactor(
        tenant_id=tenant_id,
        originating_agent_id=originating_agent_id,
        originating_user_id=originating_user_id,
        factor_kind=factor_kind,
        source_refs_json=_list_or_empty(data.get("source_refs")),
        trace_refs_json=_list_or_empty(data.get("trace_refs")),
        artifact_ref=_optional_string(data.get("artifact_ref")),
        artifact_sha256=_optional_string(data.get("artifact_sha256")),
        upstream_source_ref=_optional_string(data.get("upstream_source_ref")),
        upstream_content_sha256=_optional_string(data.get("upstream_content_sha256")),
        license_report_json=_dict_or_empty(data.get("license_report")),
        display_name=_required_string(data.get("display_name"), field_name="display_name"),
        summary=_optional_string(data.get("summary")),
        authoring_contract_json=contract,
        declared_components_json=_dict_or_empty(data.get("declared_components")),
        declared_permissions_json=_dict_or_empty(data.get("declared_permissions")),
        sensitivity_report_json=_dict_or_empty(data.get("sensitivity_report")),
        reuse_score_json=_dict_or_empty(data.get("reuse_score")),
        suggested_scope=_optional_string(data.get("suggested_scope")),
        status=_optional_string(data.get("status")) or "captured",
    )
    try:
        db.add(factor)
        await db.flush()
        review = CapabilityFactorReview(
            tenant_id=tenant_id,
            factor_id=factor.id,
            automated_report_json={
                "intake_status": "captured",
                "self_evolution_eligible": contract.get("self_evolution_eligible") is True,
            },
            admission_report_json={"admission": "factor_intake_only"},
            findings_json=[],
            dedupe_report_json={},
            eval_report_json=_dict_or_empty(data.get("eval_report")),
            decision="pending",
        )
        db.add(review)
        await db.flush()
        await db.commit()
        return {"factor": _factor_to_dict(factor), "review": _review_to_dict(review)}
    except Exception:
        await db.rollback()
        raise


async def list_capability_factors(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    status: str | None = None,
    factor_kind: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(CapabilityFactor).where(CapabilityFactor.tenant_id == tenant_id)
    if agent_id is not None:
        stmt = stmt.where(CapabilityFactor.originating_agent_id == agent_id)
    if status is not None:
        stmt = stmt.where(CapabilityFactor.status == status)
    if factor_kind is not None:
        stmt = stmt.where(CapabilityFactor.factor_kind == factor_kind)
    result = await db.execute(stmt.order_by(CapabilityFactor.created_at.desc()))
    return [_factor_to_dict(row) for row in result.scalars().all()]


async def create_promotion_proposal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factor_id: uuid.UUID,
    requested_by_user_id: uuid.UUID | None,
    data: dict[str, Any],
) -> dict[str, Any]:
    factor = await _get_factor(db, tenant_id=tenant_id, factor_id=factor_id)
    review = await _get_latest_review(db, tenant_id=tenant_id, factor_id=factor_id)
    if review is None:
        review = CapabilityFactorReview(
            tenant_id=tenant_id,
            factor_id=factor_id,
            automated_report_json={"intake_status": "proposal_requested"},
            admission_report_json={},
            findings_json=[],
            dedupe_report_json={},
            eval_report_json={},
            decision="pending",
        )
        db.add(review)
        await db.flush()
    proposal = CapabilityPromotionProposal(
        tenant_id=tenant_id,
        factor_id=factor_id,
        review_id=review.id,
        proposed_snapshot_kind=_required_string(
            data.get("proposed_snapshot_kind"), field_name="proposed_snapshot_kind"
        ),
        proposed_catalog_scope=_optional_string(data.get("proposed_catalog_scope")) or "workspace",
        proposed_activation_policy=_optional_string(data.get("proposed_activation_policy")) or "requestable",
        proposed_selector_json=_dict_or_empty(data.get("proposed_selector")),
        decision="pending",
    )
    factor.status = "proposed"
    try:
        db.add(proposal)
        await db.flush()
        await db.commit()
        return {"proposal": _proposal_to_dict(proposal), "factor": _factor_to_dict(factor)}
    except Exception:
        await db.rollback()
        raise


async def list_promotion_proposals(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factor_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(CapabilityPromotionProposal).where(CapabilityPromotionProposal.tenant_id == tenant_id)
    if factor_id is not None:
        stmt = stmt.where(CapabilityPromotionProposal.factor_id == factor_id)
    if decision is not None:
        stmt = stmt.where(CapabilityPromotionProposal.decision == decision)
    result = await db.execute(stmt.order_by(CapabilityPromotionProposal.created_at.desc()))
    return [_proposal_to_dict(row) for row in result.scalars().all()]


async def decide_promotion_proposal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    approver_id: uuid.UUID | None,
    decision: str,
    reason: str | None,
    resulting_snapshot_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    if decision not in PROMOTION_DECISIONS:
        raise ValueError("unsupported promotion decision")
    proposal = await _get_proposal(db, tenant_id=tenant_id, proposal_id=proposal_id)
    factor = await _get_factor(db, tenant_id=tenant_id, factor_id=proposal.factor_id)
    proposal.approver_id = approver_id
    proposal.decision = decision
    proposal.decision_reason = reason
    proposal.resulting_snapshot_id = resulting_snapshot_id
    if decision == "approved":
        factor.status = "promoted"
    elif decision == "rejected":
        factor.status = "rejected"
    else:
        factor.status = "needs_review"
    try:
        await db.flush()
        await db.commit()
        return {"proposal": _proposal_to_dict(proposal), "factor": _factor_to_dict(factor)}
    except Exception:
        await db.rollback()
        raise


async def archive_capability_factor(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factor_id: uuid.UUID,
) -> dict[str, Any]:
    factor = await _get_factor(db, tenant_id=tenant_id, factor_id=factor_id)
    factor.status = "archived"
    try:
        await db.flush()
        await db.commit()
        return {"factor": _factor_to_dict(factor)}
    except Exception:
        await db.rollback()
        raise


async def _get_factor(db: AsyncSession, *, tenant_id: uuid.UUID, factor_id: uuid.UUID) -> CapabilityFactor:
    result = await db.execute(
        select(CapabilityFactor).where(CapabilityFactor.id == factor_id, CapabilityFactor.tenant_id == tenant_id)
    )
    factor = result.scalar_one_or_none()
    if factor is None:
        raise ValueError("capability factor not found")
    return factor


async def _get_latest_review(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    factor_id: uuid.UUID,
) -> CapabilityFactorReview | None:
    result = await db.execute(
        select(CapabilityFactorReview)
        .where(CapabilityFactorReview.tenant_id == tenant_id, CapabilityFactorReview.factor_id == factor_id)
        .order_by(CapabilityFactorReview.created_at.desc())
    )
    return result.scalar_one_or_none()


async def _get_proposal(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
) -> CapabilityPromotionProposal:
    result = await db.execute(
        select(CapabilityPromotionProposal).where(
            CapabilityPromotionProposal.id == proposal_id,
            CapabilityPromotionProposal.tenant_id == tenant_id,
        )
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        raise ValueError("capability promotion proposal not found")
    return proposal


def _authoring_contract_for_factor(factor_kind: str, value: Any) -> dict[str, Any]:
    contract = _dict_or_empty(value)
    if factor_kind in {"external_usage_factor", "external_fork_candidate"}:
        contract["external_origin"] = True
        contract["self_evolution_eligible"] = False
    else:
        contract.setdefault("external_origin", False)
        contract.setdefault("self_evolution_eligible", True)
    return contract


def _factor_to_dict(row: CapabilityFactor) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "originating_agent_id": _uuid_to_str(getattr(row, "originating_agent_id", None)),
        "originating_user_id": _uuid_to_str(getattr(row, "originating_user_id", None)),
        "factor_kind": row.factor_kind,
        "source_refs": getattr(row, "source_refs_json", None) or [],
        "trace_refs": getattr(row, "trace_refs_json", None) or [],
        "artifact_ref": getattr(row, "artifact_ref", None),
        "artifact_sha256": getattr(row, "artifact_sha256", None),
        "upstream_source_ref": getattr(row, "upstream_source_ref", None),
        "upstream_content_sha256": getattr(row, "upstream_content_sha256", None),
        "license_report": getattr(row, "license_report_json", None) or {},
        "display_name": row.display_name,
        "summary": getattr(row, "summary", None),
        "authoring_contract": getattr(row, "authoring_contract_json", None) or {},
        "declared_components": getattr(row, "declared_components_json", None) or {},
        "declared_permissions": getattr(row, "declared_permissions_json", None) or {},
        "sensitivity_report": getattr(row, "sensitivity_report_json", None) or {},
        "reuse_score": getattr(row, "reuse_score_json", None) or {},
        "suggested_scope": getattr(row, "suggested_scope", None),
        "status": row.status,
        "created_at": _isoformat_or_none(getattr(row, "created_at", None)),
        "updated_at": _isoformat_or_none(getattr(row, "updated_at", None)),
    }


def _review_to_dict(row: CapabilityFactorReview) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "factor_id": str(row.factor_id),
        "automated_report": getattr(row, "automated_report_json", None) or {},
        "admission_report": getattr(row, "admission_report_json", None) or {},
        "findings": getattr(row, "findings_json", None) or [],
        "dedupe_report": getattr(row, "dedupe_report_json", None) or {},
        "eval_report": getattr(row, "eval_report_json", None) or {},
        "decision": row.decision,
        "created_at": _isoformat_or_none(getattr(row, "created_at", None)),
        "updated_at": _isoformat_or_none(getattr(row, "updated_at", None)),
    }


def _proposal_to_dict(row: CapabilityPromotionProposal) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "factor_id": str(row.factor_id),
        "review_id": _uuid_to_str(getattr(row, "review_id", None)),
        "proposed_snapshot_kind": row.proposed_snapshot_kind,
        "proposed_catalog_scope": row.proposed_catalog_scope,
        "proposed_activation_policy": row.proposed_activation_policy,
        "proposed_selector": getattr(row, "proposed_selector_json", None) or {},
        "approver_id": _uuid_to_str(getattr(row, "approver_id", None)),
        "decision": row.decision,
        "decision_reason": getattr(row, "decision_reason", None),
        "resulting_snapshot_id": _uuid_to_str(getattr(row, "resulting_snapshot_id", None)),
        "created_at": _isoformat_or_none(getattr(row, "created_at", None)),
        "updated_at": _isoformat_or_none(getattr(row, "updated_at", None)),
    }


def _required_choice(value: Any, *, allowed: set[str], field_name: str) -> str:
    text = _required_string(value, field_name=field_name)
    if text not in allowed:
        raise ValueError(f"{field_name} is not supported")
    return text


def _required_string(value: Any, *, field_name: str) -> str:
    text = _optional_string(value)
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _uuid_to_str(value: Any) -> str | None:
    return str(value) if value else None


def _isoformat_or_none(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
