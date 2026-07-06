"""Unified Agent Cycle decision matrix entries."""

from __future__ import annotations

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


__all__ = [
    "AGENT_CYCLE_DECISION_SCHEMA",
    "build_agent_cycle_decision_matrix",
    "append_agent_cycle_decision_entry",
    "build_agent_cycle_decision_entry",
]
