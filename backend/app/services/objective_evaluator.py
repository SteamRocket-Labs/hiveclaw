"""Evaluate objective attempts and advance ledger state using evidence."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.objective import AgentObjective
from app.services.focus_state import normalize_focus_task_id

_STATUS_RE = re.compile(r"\[OBJECTIVE_STATUS:\s*(completed|failed|blocked|running)\s*\]", re.IGNORECASE)
_EVIDENCE_RE = re.compile(r"\[OBJECTIVE_EVIDENCE:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)


def extract_attempt_status_and_evidence(
    *,
    result_summary: str | None,
    metadata_json: dict[str, Any] | None,
) -> tuple[str | None, str]:
    metadata = dict(metadata_json or {})
    status = metadata.get("objective_status")
    evidence = str(
        metadata.get("objective_evidence")
        or metadata.get("completion_evidence")
        or ""
    ).strip()
    text = result_summary or ""
    if not status:
        match = _STATUS_RE.search(text)
        if match:
            status = match.group(1).lower()
    if not evidence:
        evidence_match = _EVIDENCE_RE.search(text)
        if evidence_match:
            evidence = evidence_match.group(1).strip()[:1000]
    return (str(status).lower() if status else None), evidence


def apply_attempt_evaluation(
    objective: Any,
    *,
    attempted_status: str | None,
    evidence: str,
    result_summary: str,
) -> bool:
    status = str(attempted_status or "").lower()
    evidence = str(evidence or "").strip()
    metadata = dict(getattr(objective, "metadata_json", None) or {})
    if status == "completed":
        if not evidence:
            metadata["completion_rejected_reason"] = "completion_requires_evidence"
            objective.metadata_json = metadata
            return False
        objective.status = "completed"
        objective.completed_at = datetime.now(timezone.utc)
        metadata["completion_evidence"] = evidence
        metadata["last_result_summary"] = result_summary[:2000]
        objective.metadata_json = metadata
        return True
    if status in {"failed", "blocked"}:
        objective.status = "blocked"
        objective.blocked_reason = evidence or result_summary[:500] or "Objective attempt failed."
        metadata["last_failure_summary"] = result_summary[:2000]
        metadata["last_failure_evidence"] = evidence
        objective.metadata_json = metadata
        return True
    if status == "running" and getattr(objective, "status", None) not in {"completed", "blocked"}:
        objective.status = "running"
        metadata["last_result_summary"] = result_summary[:2000]
        objective.metadata_json = metadata
        return True
    return False


async def evaluate_trigger_attempt_by_session_key(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID | None = None,
    objective_session_key: str | None,
    result_summary: str,
    metadata_json: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    if not objective_session_key:
        return {"evaluated": False, "reason": "no_objective_session_key"}
    objective = None
    if objective_session_key.startswith("objective:focus:"):
        focus_key = normalize_focus_task_id(objective_session_key.removeprefix("objective:focus:"))
        stmt = select(AgentObjective).where(AgentObjective.objective_key == focus_key)
        if agent_id is not None:
            stmt = stmt.where(AgentObjective.agent_id == agent_id)
        result = await db.execute(stmt)
        objective = result.scalar_one_or_none()
    elif objective_session_key.startswith("objective:"):
        raw = objective_session_key.removeprefix("objective:")
        try:
            stmt = select(AgentObjective).where(AgentObjective.id == uuid.UUID(raw))
            if agent_id is not None:
                stmt = stmt.where(AgentObjective.agent_id == agent_id)
            result = await db.execute(stmt)
            objective = result.scalar_one_or_none()
        except ValueError:
            objective = None
    if objective is None:
        return {"evaluated": False, "reason": "objective_not_found"}

    status, evidence = extract_attempt_status_and_evidence(
        result_summary=result_summary,
        metadata_json=metadata_json,
    )
    changed = apply_attempt_evaluation(
        objective,
        attempted_status=status,
        evidence=evidence,
        result_summary=result_summary,
    )
    if changed and commit:
        await db.commit()
    return {
        "evaluated": True,
        "changed": changed,
        "objective_id": str(getattr(objective, "id")),
        "status": getattr(objective, "status", None),
    }


async def safe_evaluate_trigger_attempt_by_session_key(**kwargs: Any) -> dict[str, Any]:
    try:
        return await evaluate_trigger_attempt_by_session_key(**kwargs)
    except Exception as exc:
        logger.debug("[ObjectiveEvaluator] trigger attempt evaluation failed: {}", exc)
        return {"evaluated": False, "reason": str(exc)}
