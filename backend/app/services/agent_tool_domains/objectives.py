"""Agent-facing objective ledger tools."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.objective import AgentObjective
from app.services import objective_intake
from app.services.focus_state import normalize_focus_task_id
from app.tools.result_envelope import render_tool_error


def _objective_error(tool_name: str, error_class: str, message: str, *, actionable_hint: str | None = None) -> str:
    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=message,
        provider="objectives",
        actionable_hint=actionable_hint,
    )


def _objective_row(objective: Any) -> str:
    metadata = dict(getattr(objective, "metadata_json", None) or {})
    autonomy_class = metadata.get("autonomy_class", "")
    return (
        f"| {getattr(objective, 'objective_key', '')} "
        f"| {getattr(objective, 'status', '')} "
        f"| {getattr(objective, 'priority', 0)} "
        f"| {autonomy_class} "
        f"| {str(getattr(objective, 'description', '') or '')[:120]} |"
    )


async def list_objectives_for_tool(agent_id: uuid.UUID, status: str | None = None) -> list[AgentObjective]:
    async with async_session() as db:
        stmt = select(AgentObjective).where(AgentObjective.agent_id == agent_id)
        if status:
            stmt = stmt.where(AgentObjective.status == status)
        stmt = stmt.order_by(AgentObjective.priority.desc(), AgentObjective.created_at.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())


async def _handle_list_objectives(agent_id: uuid.UUID, arguments: dict) -> str:
    status = str(arguments.get("status") or "").strip() or None
    objectives = await list_objectives_for_tool(agent_id, status=status)
    if not objectives:
        return "No objectives found."
    lines = [
        "| Key | Status | Priority | Autonomy | Description |",
        "|-----|--------|----------|----------|-------------|",
    ]
    lines.extend(_objective_row(objective) for objective in objectives)
    return "\n".join(lines)


async def _load_agent(db, agent_id: uuid.UUID):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    return result.scalar_one_or_none()


async def _handle_propose_objective(agent_id: uuid.UUID, arguments: dict) -> str:
    description = str(arguments.get("description") or "").strip()
    if not description:
        return _objective_error("propose_objective", "bad_arguments", "description is required.")
    autonomy_class = str(arguments.get("autonomy_class") or "implicit_inference").strip()
    risk_level = str(arguments.get("risk_level") or "medium").strip()
    confidence = float(arguments.get("confidence") or 0.7)
    evidence = arguments.get("evidence") if isinstance(arguments.get("evidence"), dict) else {}
    wake_policy = arguments.get("wake_policy") if isinstance(arguments.get("wake_policy"), dict) else {}

    async with async_session() as db:
        agent = await _load_agent(db, agent_id)
        if not agent:
            return _objective_error("propose_objective", "not_found", "Agent not found.")
        candidate = objective_intake.ObjectiveCandidate(
            agent_id=agent_id,
            tenant_id=getattr(agent, "tenant_id", None),
            description=description,
            source=str(arguments.get("source") or "agent_tool"),
            autonomy_class=autonomy_class,
            risk_level=risk_level,
            confidence=confidence,
            evidence=evidence,
            objective_key=str(arguments.get("objective_key") or "").strip() or None,
            success_criteria=str(arguments.get("success_criteria") or "").strip() or None,
            priority=int(arguments.get("priority") or 0),
            wake_policy=wake_policy,
        )
        objective = await objective_intake.upsert_objective_candidate(db, agent, candidate)
    return f"✅ Objective '{objective.objective_key}' saved with status={objective.status}."


async def _find_objective(db, agent_id: uuid.UUID, *, objective_id: str | None, objective_key: str | None):
    if objective_id:
        try:
            result = await db.execute(
                select(AgentObjective).where(AgentObjective.id == uuid.UUID(objective_id), AgentObjective.agent_id == agent_id)
            )
            return result.scalar_one_or_none()
        except ValueError:
            return None
    key = normalize_focus_task_id(objective_key or "")
    if not key:
        return None
    result = await db.execute(
        select(AgentObjective).where(AgentObjective.agent_id == agent_id, AgentObjective.objective_key == key)
    )
    return result.scalar_one_or_none()


async def _handle_update_objective(agent_id: uuid.UUID, arguments: dict) -> str:
    objective_id = str(arguments.get("objective_id") or "").strip() or None
    objective_key = str(arguments.get("objective_key") or "").strip() or None
    requested_status = str(arguments.get("status") or "").strip().lower()
    if requested_status == "completed":
        return _objective_error(
            "update_objective",
            "bad_arguments",
            "Objective completion requires concrete evidence and must use complete_objective.",
            actionable_hint="Call complete_objective with an artifact path, delivery confirmation, test result, or other verifiable evidence.",
        )
    async with async_session() as db:
        objective = await _find_objective(db, agent_id, objective_id=objective_id, objective_key=objective_key)
        if not objective:
            return _objective_error("update_objective", "not_found", "Objective not found.")
        if "status" in arguments and arguments["status"] is not None:
            objective.status = str(arguments["status"]).strip()
        if "description" in arguments and arguments["description"]:
            objective.description = str(arguments["description"]).strip()
        if "success_criteria" in arguments:
            objective.success_criteria = str(arguments.get("success_criteria") or "").strip() or None
        if "blocked_reason" in arguments:
            objective.blocked_reason = str(arguments.get("blocked_reason") or "").strip() or None
        if "priority" in arguments and arguments["priority"] is not None:
            objective.priority = int(arguments["priority"])
        metadata = dict(objective.metadata_json or {})
        if isinstance(arguments.get("metadata"), dict):
            metadata.update(arguments["metadata"])
        if isinstance(arguments.get("wake_policy"), dict):
            metadata["wake_policy"] = arguments["wake_policy"]
        objective.metadata_json = metadata
        await db.commit()
    return f"✅ Objective '{objective.objective_key}' updated."


async def _handle_complete_objective(agent_id: uuid.UUID, arguments: dict) -> str:
    evidence = str(arguments.get("evidence") or "").strip()
    if not evidence:
        return _objective_error(
            "complete_objective",
            "bad_arguments",
            "evidence is required before an objective can be completed.",
            actionable_hint="Provide a concrete artifact path, delivery confirmation, test result, or other verifiable evidence.",
        )
    objective_id = str(arguments.get("objective_id") or "").strip() or None
    objective_key = str(arguments.get("objective_key") or "").strip() or None
    async with async_session() as db:
        objective = await _find_objective(db, agent_id, objective_id=objective_id, objective_key=objective_key)
        if not objective:
            return _objective_error("complete_objective", "not_found", "Objective not found.")
        if getattr(objective, "status", None) == "completed":
            return (
                f"ℹ️ Objective '{objective.objective_key}' is already completed; no state change was made.\n\n"
                "Do not call `complete_objective` for this objective again in this turn. "
                "Use the recorded evidence and produce the final user-facing answer now."
            )
        from app.services.objective_evaluator import apply_attempt_evaluation

        apply_attempt_evaluation(
            objective,
            attempted_status="completed",
            evidence=evidence,
            result_summary=str(arguments.get("result_summary") or evidence),
        )
        await db.commit()
    return (
        f"✅ Objective '{objective.objective_key}' completed with evidence recorded.\n\n"
        "Do not call `complete_objective` for this objective again in this turn. "
        "Produce the final user-facing answer now."
    )
