"""Schedule / trigger decision ledger helpers."""

from __future__ import annotations

from typing import Any

from app.runtime.decision_ledger import build_agent_cycle_decision_entry


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def build_schedule_decision_entry(
    *,
    command_origin: str,
    natural_vs_structured: str,
    plan_gate_decision: dict[str, Any] | None = None,
    confirmed_plan_ref: dict[str, Any] | None = None,
    trigger_id: str | None = None,
    next_fire: str | None = None,
    runtime_task_id: str | None = None,
) -> dict[str, Any]:
    plan_gate = _dict_or_empty(plan_gate_decision)
    confirmed_plan = _dict_or_empty(confirmed_plan_ref)
    decision = "enable_trigger" if trigger_id else "prepare_schedule"
    outcome = "scheduled" if next_fire else "pending"
    if plan_gate.get("result") in {"denied", "requires_confirmation"} and not confirmed_plan:
        decision = "block"
        outcome = str(plan_gate.get("result") or "requires_confirmation")
    permission_result = str(plan_gate.get("result") or ("confirmed_plan" if confirmed_plan else "not_applicable"))
    return {
        "schema": "hive.ccplus.schedule_decision.v1",
        "command_origin": str(command_origin or ""),
        "natural_vs_structured": str(natural_vs_structured or "unknown"),
        "plan_gate_decision": plan_gate,
        "confirmed_plan_ref": confirmed_plan,
        "trigger_id": str(trigger_id or "") or None,
        "next_fire": str(next_fire or "") or None,
        "runtime_task_id": str(runtime_task_id or "") or None,
        "agent_cycle_decision_entry": build_agent_cycle_decision_entry(
            subsystem="schedule",
            trigger=str(command_origin or "schedule_command"),
            judge="schedule_decision_ledger.build_schedule_decision_entry",
            decision=decision,
            outcome=outcome,
            next_action="wait_for_trigger_fire" if next_fire else "await_confirmation_or_configuration",
            model_interaction="runtime_wake" if trigger_id else "none",
            user_visible=True,
            permission_result=permission_result,
            budget_result="not_applicable",
            details={
                "natural_vs_structured": str(natural_vs_structured or "unknown"),
                "trigger_id": str(trigger_id or "") or None,
            },
        ),
    }


def confirmed_plan_ref_from_args(arguments: dict[str, Any]) -> dict[str, Any]:
    evidence = arguments.get("_plan_authorization")
    if not isinstance(evidence, dict):
        evidence = {}
    plan_id = evidence.get("plan_id") or arguments.get("confirmed_plan_id")
    if not plan_id:
        return {}
    return {
        "plan_id": str(plan_id),
        "plan_version": (
            evidence.get("plan_version")
            if evidence.get("plan_version") is not None
            else arguments.get("confirmed_plan_version")
        ),
        "plan_hash": evidence.get("plan_hash"),
    }
