"""Unified runtime decision ledger.

One home for the runtime's decision-entry builders (F slimming: the former
cache_decision_ledger / runtime_decision_ledger / authorization_decision
single-builder modules folded in here — same schemas, same behavior).
"""

from __future__ import annotations

import hashlib
from typing import Any

AGENT_CYCLE_DECISION_SCHEMA = "hive.ccplus.agent_cycle_decision.v1"
AGENT_CYCLE_DECISION_MATRIX_SCHEMA = "hive.ccplus.agent_cycle_decision_matrix.v1"

_REQUIRED_FIELDS = [
    "trigger",
    "judge",
    "decision",
    "outcome",
    "next_action",
    "model_interaction",
    "user_visible",
    "permission_result",
    "budget_result",
]


def _text(value: Any, *, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def build_agent_cycle_decision_entry(
    *,
    subsystem: str,
    trigger: str,
    judge: str,
    decision: str,
    outcome: str,
    next_action: str,
    model_interaction: str,
    user_visible: bool,
    permission_result: str = "not_applicable",
    budget_result: str = "not_applicable",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": AGENT_CYCLE_DECISION_SCHEMA,
        "subsystem": _text(subsystem, fallback="unknown"),
        "trigger": _text(trigger, fallback="unknown"),
        "judge": _text(judge, fallback="unknown"),
        "decision": _text(decision, fallback="unknown"),
        "outcome": _text(outcome, fallback="unknown"),
        "next_action": _text(next_action, fallback="none"),
        "model_interaction": _text(model_interaction, fallback="none"),
        "user_visible": bool(user_visible),
        "permission_result": _text(permission_result, fallback="not_applicable"),
        "budget_result": _text(budget_result, fallback="not_applicable"),
        "details": dict(details or {}),
    }


def build_agent_cycle_decision_matrix() -> dict[str, Any]:
    return {
        "schema": AGENT_CYCLE_DECISION_MATRIX_SCHEMA,
        "required_fields": list(_REQUIRED_FIELDS),
        "subsystems": [
            {
                "subsystem": "compaction",
                "trigger": "context_threshold|prompt_too_long|manual_compact|tool_result_budget",
                "judge": "session_context_controller|kernel_compaction_hooks",
                "outcome": "completed|skipped|failed",
                "next_action": "continue|pause|surface_error",
                "permission_boundary": "unchanged",
            },
            {
                "subsystem": "loop_guard",
                "trigger": "repeated_text|repeated_tool|failed_tool_loop|round_pressure",
                "judge": "kernel.loop_guard",
                "outcome": "warn|abort",
                "next_action": "inject_runtime_reminder|stop_turn",
                "permission_boundary": "unchanged",
            },
            {
                "subsystem": "goal",
                "trigger": "terminal_web_turn",
                "judge": "session_goal_runtime.should_continue_goal",
                "outcome": "active|paused|blocked|budget_limited|usage_limited|complete",
                "next_action": "schedule_continuation|ask_user|stop",
                "permission_boundary": "inherits_web_chat_runtime",
            },
            {
                "subsystem": "schedule",
                "trigger": "set_trigger|plan_mode_schedule|confirmed_plan",
                "judge": "schedule_decision_ledger",
                "outcome": "scheduled|pending|requires_confirmation|denied",
                "next_action": "wait_for_trigger_fire|await_confirmation",
                "permission_boundary": "plan_gate+tool_policy",
            },
            {
                "subsystem": "trigger",
                "trigger": "daemon_tick|external_event",
                "judge": "trigger_daemon",
                "outcome": "wake|skipped|failed",
                "next_action": "start_runtime_task|backoff|record_skip",
                "permission_boundary": "trigger_policy+budget",
            },
            {
                "subsystem": "workflow",
                "trigger": "preview_workflow|start_workflow|workflow_completion",
                "judge": "workflow_runtime_service|dynamic_workflow",
                "outcome": "previewed|running|completed|failed|repairable",
                "next_action": "continue|repair|promote|ask_user",
                "permission_boundary": "workflow_admission+tool_governance",
            },
            {
                "subsystem": "agent_team",
                "trigger": "team_create|member_completion|team_close",
                "judge": "agent_team_runtime_service",
                "outcome": "running|idle|failed|closed",
                "next_action": "wait_for_members|review_failed_members|close_or_continue_team",
                "permission_boundary": "member_runtime_inherited",
            },
            {
                "subsystem": "subagent",
                "trigger": "spawn_subagent|completion_wake|resume_reconciliation",
                "judge": "subagent_run_service|subagent_decision_entry",
                "outcome": "queued|running|completed|failed|needs_reconciliation",
                "next_action": "observe_result|approve_retry|manual_reconcile_or_abandon",
                "permission_boundary": "parent_runtime+subagent_budget+replay_risk",
            },
        ],
    }


def append_agent_cycle_decision_entry(session_context: Any | None, entry: dict[str, Any], *, limit: int = 100) -> None:
    if session_context is None:
        return
    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_agent_cycle_decision(entry, limit=limit)


# ── Cache decisions (formerly cache_decision_ledger.py) ─────────────────────


def _hash_cache_key(cache_key: str) -> str:
    return hashlib.sha256(str(cache_key or "").encode("utf-8")).hexdigest()[:16]


def build_cache_decision_entry(
    *,
    cache_surface: str,
    cache_key: str = "",
    decision: str,
    invalidation_reason: str = "",
    shared_with_parent: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "hive.ccplus.cache_decision.v1",
        "cache_surface": str(cache_surface or "none"),
        "cache_key": "[redacted]" if cache_key else "",
        "cache_key_hash": _hash_cache_key(cache_key) if cache_key else "",
        "decision": str(decision or "none"),
        "invalidation_reason": str(invalidation_reason or ""),
        "shared_with_parent": bool(shared_with_parent),
    }


def append_cache_decision_entry(session_context: Any | None, entry: dict[str, Any], *, limit: int = 50) -> None:
    if session_context is None:
        return
    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_cache_decision(entry, limit=limit)


# ── Runtime control decisions (formerly runtime_decision_ledger.py) ─────────


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


# ── Authorization decisions (formerly authorization_decision.py) ────────────

AUTHORIZATION_DECISION_SCHEMA = "hive.ccplus.authorization_decision.v1"


def _auth_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _auth_reason_text(reason: Any) -> str:
    if isinstance(reason, (list, tuple, set)):
        return ",".join(str(item) for item in reason if str(item or "").strip())
    return str(reason or "").strip()


def build_authorization_decision_entry(
    *,
    resource: Any,
    action: Any,
    result: Any,
    reason: Any = None,
    principal: Any = None,
    company: Any = None,
    sensitivity: Any = None,
    policy: Any = None,
    model_visible_message: Any = None,
    source: str = "runtime",
) -> dict[str, Any]:
    return {
        "schema": AUTHORIZATION_DECISION_SCHEMA,
        "resource": _auth_text(resource),
        "action": _auth_text(action),
        "principal": _auth_text(principal),
        "company": _auth_text(company),
        "sensitivity": _auth_text(sensitivity),
        "policy": _auth_text(policy),
        "result": _auth_text(result),
        "reason": _auth_reason_text(reason) or None,
        "model_visible_message": _auth_text(model_visible_message),
        "source": source,
    }


__all__ = [
    "AGENT_CYCLE_DECISION_SCHEMA",
    "AUTHORIZATION_DECISION_SCHEMA",
    "build_agent_cycle_decision_matrix",
    "append_agent_cycle_decision_entry",
    "build_agent_cycle_decision_entry",
    "build_cache_decision_entry",
    "append_cache_decision_entry",
    "build_runtime_decision_entry",
    "append_runtime_decision_entry",
    "build_authorization_decision_entry",
]
