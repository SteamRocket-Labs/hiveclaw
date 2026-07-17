"""Close Agent Team surface and RuntimeRoot terminal drift.

Revision ID: collaboration_runtime_closure_0717
Revises: peer_a2a_session_authority_0717
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op


revision = "collaboration_runtime_closure_0717"
down_revision = "peer_a2a_session_authority_0717"
branch_labels = None
depends_on = None


def build_team_member_surface_backfill_sql() -> str:
    return """
        UPDATE chat_sessions
        SET listed_surface = 'parent'
        WHERE listed_surface IS DISTINCT FROM 'parent'
          AND (
            session_kind = 'team_member'
            OR runtime_source = 'team_member'
            OR source_channel = 'agent_team'
          )
    """


def build_peer_a2a_session_surface_backfill_sql() -> str:
    return """
        UPDATE chat_sessions AS target_session
        SET session_kind = 'delegation_run',
            runtime_source = 'delegation',
            listed_surface = 'chat'
        FROM runtime_tasks AS task
        WHERE task.tenant_id = target_session.tenant_id
          AND task.task_type IN ('delegation', 'a2a_delegation')
          AND task.child_session_id IS NOT NULL
          AND replace(lower(task.child_session_id), '-', '')
                = replace(lower(target_session.id::text), '-', '')
          AND (
            target_session.session_kind IS DISTINCT FROM 'delegation_run'
            OR target_session.runtime_source IS DISTINCT FROM 'delegation'
            OR target_session.listed_surface IS DISTINCT FROM 'chat'
          )
    """


def build_terminal_root_backfill_sql() -> str:
    return """
        UPDATE runtime_root_items AS item
        SET state = CASE task.status
              WHEN 'completed' THEN 'completed'
              WHEN 'failed' THEN 'failed'
              WHEN 'killed' THEN 'killed'
              WHEN 'cancelled' THEN 'cancelled'
              WHEN 'canceled' THEN 'cancelled'
              WHEN 'skipped' THEN 'not_admitted'
              ELSE item.state
            END,
            admission_disposition = CASE
              WHEN task.status = 'skipped' THEN 'not_admitted'
              ELSE 'admitted'
            END,
            reason_code = 'session_v2_terminal_backfill_0717',
            metadata_json = COALESCE(item.metadata_json, '{}'::jsonb) || jsonb_build_object(
              'terminal_backfill', jsonb_build_object(
                'schema', 'hive.runtime_root_terminal_backfill.v1',
                'runtime_task_id', task.id::text,
                'runtime_task_status', task.status,
                'source', 'collaboration_runtime_closure_0717'
              )
            ),
            terminal_at = COALESCE(item.terminal_at, task.completed_at, NOW()),
            version = item.version + 1
        FROM runtime_tasks AS task
        WHERE task.id = item.runtime_task_id
          AND task.status IN ('completed', 'failed', 'killed', 'cancelled', 'canceled', 'skipped')
          AND item.state NOT IN ('completed', 'failed', 'killed', 'skipped', 'cancelled', 'not_admitted')
    """


def build_collaboration_thread_item_backfill_sql() -> str:
    return """
        UPDATE chat_transcript_events
        SET item_type = CASE
              WHEN event_type IN (
                'team_member', 'team_created', 'team_close_requested', 'team_close_delivery_failed',
                'member_spawned', 'member_idle', 'member_message_queued',
                'member_message_rejected', 'member_run_started'
              )
                OR lower(COALESCE(metadata_json ->> 'runtime_task_type', '')) = 'team_member'
                OR lower(COALESCE(metadata_json ->> 'runtime_source', '')) IN ('agent_team', 'agent_team_member')
                OR lower(COALESCE(metadata_json ->> 'source', '')) IN ('agent_team', 'agent_team_member')
                THEN 'agent_team_activity'
              WHEN event_type = 'delegation_run'
                OR lower(COALESCE(metadata_json ->> 'action_kind', '')) = 'a2a_delegation'
                OR lower(COALESCE(metadata_json ->> 'notification_source', '')) IN ('a2a', 'peer_a2a')
                OR lower(COALESCE(metadata_json ->> 'interaction_type', '')) = 'delegation'
                THEN 'peer_a2a_activity'
              WHEN event_type IN (
                'subagent', 'subagent_task_started', 'subagent_task_completed', 'subagent_task_failed'
              )
                OR (
                  event_type = 'child_session'
                  AND lower(COALESCE(metadata_json ->> 'source', '')) = 'subagent'
                )
                THEN 'subagent_activity'
              ELSE item_type
            END
        WHERE event_type IN (
                'team_member', 'team_created', 'team_close_requested', 'team_close_delivery_failed',
                'member_spawned', 'member_idle', 'member_message_queued',
                'member_message_rejected', 'member_run_started', 'delegation_run', 'subagent_task_failed'
              )
           OR lower(COALESCE(metadata_json ->> 'runtime_task_type', '')) = 'team_member'
           OR lower(COALESCE(metadata_json ->> 'runtime_source', '')) IN ('agent_team', 'agent_team_member')
           OR lower(COALESCE(metadata_json ->> 'source', '')) IN ('agent_team', 'agent_team_member')
           OR lower(COALESCE(metadata_json ->> 'action_kind', '')) = 'a2a_delegation'
           OR lower(COALESCE(metadata_json ->> 'notification_source', '')) IN ('a2a', 'peer_a2a')
           OR lower(COALESCE(metadata_json ->> 'interaction_type', '')) = 'delegation'
    """


def upgrade() -> None:
    op.execute(build_team_member_surface_backfill_sql())
    op.execute(build_peer_a2a_session_surface_backfill_sql())
    op.execute(build_terminal_root_backfill_sql())
    op.execute(build_collaboration_thread_item_backfill_sql())


def downgrade() -> None:
    # These rows are mechanical truth repairs. Re-exposing Team member
    # implementation sessions or reopening terminal root items would recreate
    # the production defect, so a compatibility downgrade intentionally keeps
    # the repaired data while older application artifacts drain.
    pass
