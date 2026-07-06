"""Schedule / trigger decision ledger helpers."""

from __future__ import annotations

from typing import Any


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
    return {
        "schema": "hive.ccplus.schedule_decision.v1",
        "command_origin": str(command_origin or ""),
        "natural_vs_structured": str(natural_vs_structured or "unknown"),
        "plan_gate_decision": _dict_or_empty(plan_gate_decision),
        "confirmed_plan_ref": _dict_or_empty(confirmed_plan_ref),
        "trigger_id": str(trigger_id or "") or None,
        "next_fire": str(next_fire or "") or None,
        "runtime_task_id": str(runtime_task_id or "") or None,
    }


def confirmed_plan_ref_from_args(arguments: dict[str, Any]) -> dict[str, Any]:
    plan_id = arguments.get("confirmed_plan_id")
    if not plan_id:
        return {}
    return {
        "plan_id": str(plan_id),
        "plan_version": arguments.get("confirmed_plan_version"),
        "plan_hash": arguments.get("confirmed_plan_hash"),
    }
