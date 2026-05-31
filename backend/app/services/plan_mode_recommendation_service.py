"""Plan Mode recommendation ledger helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.models.plan_recommendation import AgentPlanRecommendation


class PlanRecommendationError(Exception):
    """Raised when a Plan Mode opt-out cannot be bound to a recommendation."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _same_uuid(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    return str(left) == str(right)


def _coerce_uuid(value: UUID | str | None) -> UUID:
    if value is None:
        raise PlanRecommendationError(
            "plan_recommendation_required",
            "A declined Plan Mode opt-out must reference a declined plan recommendation.",
        )
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanRecommendationError(
            "invalid_plan_recommendation_id",
            "Plan recommendation id is not a valid UUID.",
        ) from exc


async def create_plan_recommendation(
    db: Any,
    *,
    agent_id: UUID,
    recommended_to_user_id: UUID | None,
    session_id: str | None,
    source: str,
    tenant_id: UUID | None = None,
    runtime_task_id: UUID | None = None,
    original_request: str,
    title: str | None = None,
    intent_type: str = "autonomous_wake",
    action_kind: str = "create_enabled_trigger",
    tool_name: str = "set_trigger",
    metadata_json: dict[str, Any] | None = None,
) -> AgentPlanRecommendation | None:
    """Persist a recommendation shown to a real user.

    Returns ``None`` when the caller cannot identify a real user or session; in
    that case later opt-out validation must fail closed.
    """
    if recommended_to_user_id is None or not session_id:
        return None
    recommendation = AgentPlanRecommendation(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=str(session_id),
        runtime_task_id=runtime_task_id,
        recommended_to_user_id=recommended_to_user_id,
        source=source,
        intent_type=intent_type,
        action_kind=action_kind,
        tool_name=tool_name,
        title=(title or original_request or "")[:500],
        original_request=original_request,
        status="recommended",
        metadata_json=metadata_json or {},
    )
    db.add(recommendation)
    if hasattr(db, "flush"):
        await db.flush()
    return recommendation


async def find_latest_recommendation_for_decline(
    db: Any,
    *,
    agent_id: UUID,
    user_id: UUID | None,
    session_id: str | None,
) -> AgentPlanRecommendation | None:
    """Find the latest recommendation this user can decline in this session."""
    if user_id is None or not session_id:
        return None
    stmt = (
        select(AgentPlanRecommendation)
        .where(
            AgentPlanRecommendation.agent_id == agent_id,
            AgentPlanRecommendation.recommended_to_user_id == user_id,
            AgentPlanRecommendation.session_id == str(session_id),
            AgentPlanRecommendation.status == "recommended",
        )
        .order_by(AgentPlanRecommendation.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def decline_recommendation(
    recommendation: AgentPlanRecommendation,
    *,
    user_id: UUID,
) -> AgentPlanRecommendation:
    """Mark a recommendation declined by the authenticated user."""
    recommendation.status = "declined"
    recommendation.declined_by_user_id = user_id
    recommendation.declined_at = datetime.now(timezone.utc)
    return recommendation


async def decline_latest_recommendation_for_user(
    db: Any,
    *,
    agent_id: UUID,
    user_id: UUID | None,
    session_id: str | None,
) -> AgentPlanRecommendation | None:
    recommendation = await find_latest_recommendation_for_decline(
        db,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
    )
    if recommendation is None or user_id is None:
        return None
    return decline_recommendation(recommendation, user_id=user_id)


async def require_declined_plan_recommendation(
    db: Any,
    *,
    recommendation_id: UUID | str | None,
    agent_id: UUID,
    user_id: UUID | None,
    action_kind: str | None = None,
) -> AgentPlanRecommendation:
    """Validate a REST opt-out against a declined recommendation row."""
    recommendation_uuid = _coerce_uuid(recommendation_id)
    if user_id is None:
        raise PlanRecommendationError("user_required", "A declined Plan Mode opt-out requires an authenticated user.")

    stmt = select(AgentPlanRecommendation).where(AgentPlanRecommendation.id == recommendation_uuid)
    result = await db.execute(stmt)
    recommendation = result.scalar_one_or_none()
    if recommendation is None:
        raise PlanRecommendationError("plan_recommendation_not_found", "Plan recommendation was not found.")
    if not _same_uuid(getattr(recommendation, "agent_id", None), agent_id):
        raise PlanRecommendationError(
            "plan_recommendation_agent_mismatch",
            "Plan recommendation belongs to a different agent.",
        )
    if not _same_uuid(getattr(recommendation, "recommended_to_user_id", None), user_id):
        raise PlanRecommendationError(
            "plan_recommendation_user_mismatch",
            "Plan recommendation belongs to a different user.",
        )
    if str(getattr(recommendation, "status", "") or "") != "declined":
        raise PlanRecommendationError(
            "plan_recommendation_not_declined",
            "Plan recommendation has not been declined by the user.",
        )
    if not getattr(recommendation, "session_id", None):
        raise PlanRecommendationError(
            "plan_recommendation_session_missing",
            "Plan recommendation is not bound to a session.",
        )
    if action_kind and str(getattr(recommendation, "action_kind", "") or "") != action_kind:
        raise PlanRecommendationError(
            "plan_recommendation_action_mismatch",
            "Plan recommendation belongs to a different action kind.",
        )
    return recommendation


__all__ = [
    "PlanRecommendationError",
    "create_plan_recommendation",
    "decline_latest_recommendation_for_user",
    "decline_recommendation",
    "find_latest_recommendation_for_decline",
    "require_declined_plan_recommendation",
]
