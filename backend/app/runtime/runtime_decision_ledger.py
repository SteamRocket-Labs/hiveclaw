"""Unified runtime decision ledger helpers."""

from __future__ import annotations

from typing import Any

from app.runtime.decision_ledger import append_agent_cycle_decision_entry, build_agent_cycle_decision_entry


def _budget_result(*, trigger: str, status: str, details: dict[str, Any]) -> str:
    if details.get("tool_result_trimmed"):
        return "tool_result_trimmed"
    if details.get("budget_result"):
        return str(details["budget_result"])
    if trigger == "tool_result_budget":
        return "within_budget" if status != "failed" else "over_budget"
    return "not_applicable"


def build_runtime_decision_entry(
    *,
    kind: str,
    status: str,
    trigger: str = "",
    reason: str = "",
    next_action: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details_payload = dict(details or {})
    agent_cycle_decision_entry = build_agent_cycle_decision_entry(
        subsystem=str(kind or "unknown"),
        trigger=str(trigger or "runtime"),
        judge=f"{str(kind or 'runtime')}_controller",
        decision=str(status or "unknown"),
        outcome=str(status or "unknown"),
        next_action=str(next_action or ""),
        model_interaction="runtime_control",
        user_visible=False,
        permission_result=str(details_payload.get("permission_result") or "unchanged"),
        budget_result=_budget_result(trigger=str(trigger or ""), status=str(status or ""), details=details_payload),
        details=details_payload,
    )
    return {
        "schema": "hive.ccplus.runtime_decision.v1",
        "kind": str(kind or "unknown"),
        "trigger": str(trigger or ""),
        "status": str(status or "unknown"),
        "reason": str(reason or ""),
        "next_action": str(next_action or ""),
        "judge": agent_cycle_decision_entry["judge"],
        "decision": agent_cycle_decision_entry["decision"],
        "outcome": agent_cycle_decision_entry["outcome"],
        "model_interaction": agent_cycle_decision_entry["model_interaction"],
        "user_visible": agent_cycle_decision_entry["user_visible"],
        "permission_result": agent_cycle_decision_entry["permission_result"],
        "budget_result": agent_cycle_decision_entry["budget_result"],
        "agent_cycle_decision_entry": agent_cycle_decision_entry,
        "details": details_payload,
    }


def append_runtime_decision_entry(session_context: Any | None, entry: dict[str, Any], *, limit: int = 100) -> None:
    if session_context is None:
        return
    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_runtime_decision(entry, limit=limit)
    agent_cycle_entry = entry.get("agent_cycle_decision_entry")
    if isinstance(agent_cycle_entry, dict):
        append_agent_cycle_decision_entry(session_context, agent_cycle_entry, limit=limit)
