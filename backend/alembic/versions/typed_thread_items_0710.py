"""Normalize historical transcript events to the typed ThreadItem contract.

Revision ID: typed_thread_items_0710
Revises: personal_kb_local_receipts_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections import defaultdict

from alembic import op


revision = "typed_thread_items_0710"
down_revision = "personal_kb_local_receipts_0710"
branch_labels = None
depends_on = None

# Frozen copy of the v1 runtime classifier. The migration test requires exact
# equality so a future runtime event cannot silently drift past historical data.
EVENT_THREAD_ITEM_TYPES: dict[str, str] = {
    "user_message": "user_message",
    "assistant_message": "agent_message",
    "thinking": "reasoning",
    "reasoning": "reasoning",
    "tool_call": "tool_call",
    "tool_result": "tool_result",
    "tool_success": "tool_result",
    "tool_failure": "tool_result",
    "permission_request": "approval_request",
    "session_permission_request": "approval_request",
    "approval_request": "approval_request",
    "permission": "approval_request",
    "permission_resolved": "approval_decision",
    "session_permission_decision": "approval_decision",
    "session_permission_expired": "approval_decision",
    "permission_profile_updated": "approval_decision",
    "approval.resolved": "approval_decision",
    "approval.resolved_via_feishu": "approval_decision",
    "plan": "plan",
    "advanced_plan": "plan",
    "plan_confirmed": "plan",
    "plan_failed": "plan",
    "workflow_run": "workflow_activity",
    "workflow_step": "workflow_activity",
    "workflow_started": "workflow_activity",
    "workflow_completed": "workflow_activity",
    "workflow_failed": "workflow_activity",
    "dynamic_workflow": "workflow_activity",
    "delegation_run": "subagent_activity",
    "child_session": "subagent_activity",
    "agent_task_notification": "subagent_activity",
    "subagent": "subagent_activity",
    "subagent_task_started": "subagent_activity",
    "subagent_task_completed": "subagent_activity",
    "team_member": "subagent_activity",
    "member_spawned": "subagent_activity",
    "member_idle": "subagent_activity",
    "member_message_queued": "subagent_activity",
    "member_message_rejected": "subagent_activity",
    "member_run_started": "subagent_activity",
    "session_compact": "context_compaction",
    "summary_turn": "context_compaction",
    "artifact_update": "artifact",
    "artifact_delivery": "artifact",
    "file_changes": "artifact",
    "run_queued": "boundary",
    "run_started": "boundary",
    "run_completed": "boundary",
    "run_cancelled": "boundary",
    "done": "boundary",
    "phase": "boundary",
    "segment_boundary": "boundary",
    "session_branch": "boundary",
    "session_rewind": "boundary",
    "session_workspace_rewind": "boundary",
    "session_clear": "boundary",
    "turn_steered": "boundary",
    "error": "error",
    "denial": "error",
    "expired": "error",
    "hard_stopped": "error",
    "circuit_break": "error",
    "loop": "error",
    "quota_exceeded": "error",
    "runtime_action_failed": "error",
    "runtime_action_blocked": "error",
}


def _sql_values(values: list[str] | tuple[str, ...] | set[str]) -> str:
    escaped = (value.replace("'", "''") for value in sorted(values))
    return ", ".join(f"'{value}'" for value in escaped)


def _backfill_sql() -> str:
    grouped: dict[str, list[str]] = defaultdict(list)
    for event_type, item_type in EVENT_THREAD_ITEM_TYPES.items():
        grouped[item_type].append(event_type)
    item_cases = "\n".join(
        f"            WHEN event_type IN ({_sql_values(grouped[item_type])}) THEN '{item_type}'"
        for item_type in sorted(grouped)
    )
    failed_events = {event for event, item_type in EVENT_THREAD_ITEM_TYPES.items() if item_type == "error"} | {
        "plan_failed",
        "tool_failure",
        "workflow_failed",
    }
    cancelled_events = {"run_cancelled"}
    running_events = {
        "member_run_started",
        "run_queued",
        "run_started",
        "subagent_task_started",
        "thinking",
        "workflow_started",
    }
    approval_events = {event for event, item_type in EVENT_THREAD_ITEM_TYPES.items() if item_type == "approval_request"}
    return f"""
        UPDATE chat_transcript_events
        SET schema_version = 1,
            item_type = CASE
{item_cases}
                WHEN COALESCE(metadata_json ->> 'role', '') = 'user'
                     OR actor_type = 'user' THEN 'user_message'
                WHEN COALESCE(metadata_json ->> 'role', '') = 'assistant'
                     OR actor_type IN ('agent', 'assistant') THEN 'agent_message'
                ELSE 'event'
            END,
            item_status = CASE
                WHEN lower(COALESCE(metadata_json ->> 'status', metadata_json ->> 'phase', ''))
                     IN ('blocked', 'capability_denied', 'denied', 'error', 'failed')
                     OR event_type IN ({_sql_values(failed_events)}) THEN 'failed'
                WHEN lower(COALESCE(metadata_json ->> 'status', metadata_json ->> 'phase', ''))
                     IN ('canceled', 'cancelled', 'killed')
                     OR event_type IN ({_sql_values(cancelled_events)}) THEN 'cancelled'
                WHEN lower(COALESCE(metadata_json ->> 'status', metadata_json ->> 'phase', ''))
                     IN ('awaiting_approval', 'awaiting_confirmation', 'session_permission_required', 'waiting_user')
                     OR event_type IN ({_sql_values(approval_events)}) THEN 'waiting_user'
                WHEN lower(COALESCE(metadata_json ->> 'status', metadata_json ->> 'phase', ''))
                     IN ('executing', 'in_progress', 'pending', 'queued', 'running', 'started')
                     OR event_type IN ({_sql_values(running_events)}) THEN 'running'
                ELSE 'succeeded'
            END
    """


def upgrade() -> None:
    op.execute(_backfill_sql())


def downgrade() -> None:
    # Data normalization is intentionally monotonic; the v0 inferred value is
    # not an authoritative state to reconstruct.
    pass
