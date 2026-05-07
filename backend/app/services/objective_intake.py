"""Objective intake, gating, and upsert helpers.

This module is the single intake path for autonomous goals. It does not
execute work; it turns signals into auditable objective ledger rows.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.objective import AgentObjective
from app.services.focus_state import normalize_focus_task_id

ACTIVE_AUTONOMY_CLASSES = {"explicit_user_request", "confirmed_hr_blueprint", "internal_self_improvement"}
APPROVAL_AUTONOMY_CLASSES = {"external_side_effect"}
NON_OBJECTIVE_FEEDBACK_MARKERS = (
    "以后回答",
    "回答要",
    "更严谨",
    "别只说",
    "不要只说",
    "be more",
    "respond more",
)
VAGUE_OBJECTIVE_MARKERS = ("关注一下", "留意一下", "有机会", "之后看看", "keep an eye", "watch this")
EXPLICIT_OBJECTIVE_MARKERS = (
    "帮我",
    "请",
    "每天",
    "每周",
    "每月",
    "提醒",
    "定时",
    "发日报",
    "发周报",
    "follow up",
    "remind",
    "send",
    "report",
)
EXTERNAL_SIDE_EFFECT_MARKERS = (
    "发到",
    "发送",
    "通知",
    "邮件",
    "飞书",
    "feishu",
    "email",
    "send",
)
TERMINAL_OBJECTIVE_STATUSES = {"completed", "rejected"}
AUTONOMOUS_OBJECTIVE_STATUSES = {"active", "open", "running", "blocked"}


@dataclass(slots=True)
class ObjectiveGateDecision:
    status: str
    requires_approval: bool = False
    reason: str = ""


@dataclass(slots=True)
class ObjectiveCandidate:
    agent_id: uuid.UUID
    tenant_id: uuid.UUID | None
    description: str
    source: str
    autonomy_class: str
    risk_level: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    objective_key: str | None = None
    success_criteria: str | None = None
    priority: int = 0
    wake_policy: dict[str, Any] | None = None
    suggested_status: str | None = None

    def __post_init__(self) -> None:
        self.description = str(self.description or "").strip()
        self.objective_key = normalize_focus_task_id(self.objective_key or self.description[:80])
        if self.suggested_status is None:
            self.suggested_status = gate_objective_candidate(self).status
        if self.wake_policy is None:
            self.wake_policy = {}


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _daily_wake_policy(text: str) -> dict[str, Any] | None:
    lowered = text.lower()
    if "每周一" in text or "weekly" in lowered:
        return {"type": "cron", "config": {"expr": "0 9 * * 1"}}
    if "每天" in text or "每日" in text or "daily" in lowered:
        return {"type": "cron", "config": {"expr": "0 9 * * *"}}
    match = re.search(r"每\s*(\d+)\s*小时", text)
    if match:
        return {"type": "interval", "config": {"minutes": max(int(match.group(1)), 1) * 60}}
    if "明天" in text or "tomorrow" in lowered:
        return {"type": "once", "config": {"delay_seconds": 24 * 60 * 60}}
    return None


def gate_objective_candidate(candidate: ObjectiveCandidate) -> ObjectiveGateDecision:
    autonomy_class = str(candidate.autonomy_class or "").strip()
    risk_level = str(candidate.risk_level or "").strip()
    if autonomy_class in APPROVAL_AUTONOMY_CLASSES or risk_level == "high":
        return ObjectiveGateDecision(
            status="proposed",
            requires_approval=True,
            reason="External or high-risk objective requires approval before activation.",
        )
    if autonomy_class in ACTIVE_AUTONOMY_CLASSES:
        return ObjectiveGateDecision(status="active", reason="Objective source is authorized for autonomous activation.")
    return ObjectiveGateDecision(status="proposed", reason="Objective is inferred and needs confirmation.")


def extract_candidates_from_messages(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    messages: list[dict[str, Any]] | None,
    source: str = "conversation",
) -> list[ObjectiveCandidate]:
    """Extract deterministic objective candidates from conversation messages.

    This is intentionally conservative. Ambiguous text becomes `proposed`;
    user feedback stays in memory and is not turned into a business objective.
    """
    candidates: list[ObjectiveCandidate] = []
    for message in messages or []:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        if not content or _contains_any(content, NON_OBJECTIVE_FEEDBACK_MARKERS):
            continue

        wake_policy = _daily_wake_policy(content)
        is_vague = _contains_any(content, VAGUE_OBJECTIVE_MARKERS)
        is_explicit = wake_policy is not None or _contains_any(content, EXPLICIT_OBJECTIVE_MARKERS)
        if not is_explicit and not is_vague:
            continue

        autonomy_class = "implicit_inference" if is_vague else "explicit_user_request"
        risk_level = "medium" if is_vague else ("medium" if _contains_any(content, EXTERNAL_SIDE_EFFECT_MARKERS) else "low")
        confidence = 0.65 if is_vague else 0.92
        candidates.append(
            ObjectiveCandidate(
                agent_id=agent_id,
                tenant_id=tenant_id,
                description=content[:500],
                source=source,
                autonomy_class=autonomy_class,
                risk_level=risk_level,
                confidence=confidence,
                evidence={"message": content},
                wake_policy=wake_policy or {},
            )
        )
    return candidates


def _agent_tenant_id(agent: Any) -> uuid.UUID | None:
    tenant_id = getattr(agent, "tenant_id", None)
    if tenant_id is None:
        return None
    return tenant_id if isinstance(tenant_id, uuid.UUID) else uuid.UUID(str(tenant_id))


def _merge_metadata(existing: dict | None, candidate: ObjectiveCandidate, decision: ObjectiveGateDecision) -> dict[str, Any]:
    metadata = dict(existing or {})
    metadata.update(
        {
            "intake_source": candidate.source,
            "autonomy_class": candidate.autonomy_class,
            "risk_level": candidate.risk_level,
            "confidence": candidate.confidence,
            "requires_approval": decision.requires_approval,
            "gate_reason": decision.reason,
            "evidence": candidate.evidence,
        }
    )
    if candidate.wake_policy:
        metadata["wake_policy"] = candidate.wake_policy
    return metadata


def _merge_objective_status(existing_status: str | None, decision_status: str) -> str:
    current = str(existing_status or "").strip().lower()
    proposed = str(decision_status or "proposed").strip().lower()
    if current in TERMINAL_OBJECTIVE_STATUSES:
        return current
    if current in AUTONOMOUS_OBJECTIVE_STATUSES and proposed == "proposed":
        return current
    return proposed


async def _load_objective_by_key(db: AsyncSession, agent_id: Any, objective_key: str) -> AgentObjective | None:
    result = await db.execute(
        select(AgentObjective).where(
            AgentObjective.agent_id == agent_id,
            AgentObjective.objective_key == objective_key,
        )
    )
    objectives = result.scalars().all()
    return objectives[0] if objectives else None


def _apply_candidate_update(
    objective: AgentObjective,
    *,
    candidate: ObjectiveCandidate,
    decision: ObjectiveGateDecision,
    tenant_id: uuid.UUID | None,
) -> None:
    objective.description = candidate.description or objective.description
    objective.tenant_id = getattr(objective, "tenant_id", None) or tenant_id
    objective.source = candidate.source or objective.source
    if candidate.success_criteria:
        objective.success_criteria = candidate.success_criteria
    objective.status = _merge_objective_status(getattr(objective, "status", None), decision.status)
    objective.priority = max(int(getattr(objective, "priority", 0) or 0), int(candidate.priority or 0))
    objective.metadata_json = _merge_metadata(getattr(objective, "metadata_json", None), candidate, decision)


@asynccontextmanager
async def _nested_savepoint(db: AsyncSession):
    begin_nested = getattr(db, "begin_nested", None)
    if begin_nested is None:
        yield
        return
    async with begin_nested():
        yield


async def upsert_objective_candidate(
    db: AsyncSession,
    agent: Any,
    candidate: ObjectiveCandidate,
    *,
    commit: bool = True,
) -> AgentObjective:
    """Insert or update an objective candidate in the durable ledger."""
    decision = gate_objective_candidate(candidate)
    agent_id = getattr(agent, "id", candidate.agent_id)
    tenant_id = _agent_tenant_id(agent) or candidate.tenant_id
    objective_key = normalize_focus_task_id(candidate.objective_key or candidate.description)

    objective = await _load_objective_by_key(db, agent_id, objective_key)
    if objective is None:
        objective = AgentObjective(
            agent_id=agent_id,
            tenant_id=tenant_id,
            objective_key=objective_key,
            description=candidate.description,
            status=decision.status,
            priority=candidate.priority,
            source=candidate.source,
            success_criteria=candidate.success_criteria,
            metadata_json=_merge_metadata(None, candidate, decision),
        )
        try:
            async with _nested_savepoint(db):
                db.add(objective)
                await db.flush()
        except IntegrityError:
            logger.warning(
                "[ObjectiveIntake] unique conflict for agent={} objective_key={}; refetching existing row",
                agent_id,
                objective_key,
            )
            objective = await _load_objective_by_key(db, agent_id, objective_key)
            if objective is None:
                raise
            _apply_candidate_update(objective, candidate=candidate, decision=decision, tenant_id=tenant_id)
    else:
        _apply_candidate_update(objective, candidate=candidate, decision=decision, tenant_id=tenant_id)

    if commit:
        await db.commit()
    return objective


async def intake_session_objectives(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    messages: list[dict[str, Any]] | None,
    tenant_id: uuid.UUID | None = None,
    source: str = "conversation",
    commit: bool = True,
) -> dict[str, Any]:
    """Extract and persist objective candidates from a user-facing session."""
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if agent is None:
        return {"created_or_updated": 0, "candidates": 0, "error": "agent_not_found"}
    effective_tenant_id = tenant_id or getattr(agent, "tenant_id", None)
    candidates = extract_candidates_from_messages(
        agent_id=agent_id,
        tenant_id=effective_tenant_id,
        messages=messages,
        source=source,
    )
    changed = 0
    for candidate in candidates:
        await upsert_objective_candidate(db, agent, candidate, commit=False)
        changed += 1
    if commit and changed:
        await db.commit()
    return {"created_or_updated": changed, "candidates": len(candidates), "error": None}


async def intake_internal_self_improvement(
    db: AsyncSession,
    agent: Any,
    *,
    description: str,
    evidence: dict[str, Any],
    objective_key: str | None = None,
    commit: bool = True,
) -> AgentObjective:
    candidate = ObjectiveCandidate(
        agent_id=getattr(agent, "id"),
        tenant_id=_agent_tenant_id(agent),
        description=description,
        source="self_evolution",
        autonomy_class="internal_self_improvement",
        risk_level="low",
        confidence=0.8,
        evidence=evidence,
        objective_key=objective_key,
        priority=1,
    )
    return await upsert_objective_candidate(db, agent, candidate, commit=commit)


async def safe_intake_session_objectives(**kwargs: Any) -> dict[str, Any]:
    try:
        return await intake_session_objectives(**kwargs)
    except Exception as exc:
        logger.debug("[ObjectiveIntake] session intake failed: {}", exc)
        return {"created_or_updated": 0, "candidates": 0, "error": str(exc)}
