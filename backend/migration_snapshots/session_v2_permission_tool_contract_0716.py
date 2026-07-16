"""Frozen SQL for Session V2 approval/tool pairing authority."""

from __future__ import annotations


UPGRADE_SQL = """
ALTER TABLE session_tool_invocations
  ADD COLUMN tool_name varchar(200) NOT NULL DEFAULT '',
  ADD COLUMN provider_arguments_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN effective_arguments_json jsonb,
  ADD COLUMN effective_args_hash varchar(64),
  ADD COLUMN permission_item_id uuid,
  ADD COLUMN permission_state varchar(32) NOT NULL DEFAULT 'not_required',
  ADD COLUMN permission_request_version integer NOT NULL DEFAULT 0,
  ADD COLUMN permission_authority_snapshot_hash varchar(64),
  ADD COLUMN permission_response_schema varchar(300),
  ADD COLUMN permission_expires_at timestamptz,
  ADD COLUMN permission_receipt_ref varchar(300),
  ADD CONSTRAINT ck_session_tool_permission_state CHECK (
    permission_state IN ('not_required','waiting','approved','denied','expired','cancelled')
  );

-- A Run waiting on an interactive permission remains the Session's active
-- Run.  Quarantine any pre-existing duplicate instead of letting the new
-- uniqueness predicate fail or silently preserve two effect owners.
DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session;

WITH ranked AS (
  SELECT id,
         row_number() OVER (
           PARTITION BY parent_agent_id, parent_session_id
           ORDER BY
             CASE status
               WHEN 'running' THEN 0
               WHEN 'resumable' THEN 1
               WHEN 'suspended' THEN 2
               ELSE 3
             END,
             created_at DESC,
             id DESC
         ) AS row_number
  FROM runtime_tasks
  WHERE task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')
    AND status IN ('pending', 'running', 'suspended', 'resumable')
    AND parent_agent_id IS NOT NULL
    AND parent_session_id IS NOT NULL
)
UPDATE runtime_tasks AS task
SET status='needs_reconciliation',
    result_summary=COALESCE(
      task.result_summary,
      'Duplicate active Session Run quarantined by Session V2 permission migration'
    ),
    metadata_json=(
      COALESCE(task.metadata_json, '{}'::json)::jsonb || jsonb_build_object(
        'needs_reconciliation', true,
        'reconciliation_reason', 'duplicate_active_session_run_during_permission_migration',
        'recovery_owner', 'session_v2_permission_tool_0716'
      )
    )::json
FROM ranked
WHERE task.id=ranked.id AND ranked.row_number > 1;

CREATE UNIQUE INDEX uq_runtime_tasks_active_web_chat_session
  ON runtime_tasks(parent_agent_id, parent_session_id)
  WHERE task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')
    AND status IN ('pending', 'running', 'suspended', 'resumable')
    AND parent_agent_id IS NOT NULL
    AND parent_session_id IS NOT NULL;
"""

UPGRADE_SQL_STATEMENTS = (
    """
    ALTER TABLE session_tool_invocations
      ADD COLUMN tool_name varchar(200) NOT NULL DEFAULT '',
      ADD COLUMN provider_arguments_json jsonb NOT NULL DEFAULT '{}'::jsonb,
      ADD COLUMN effective_arguments_json jsonb,
      ADD COLUMN effective_args_hash varchar(64),
      ADD COLUMN permission_item_id uuid,
      ADD COLUMN permission_state varchar(32) NOT NULL DEFAULT 'not_required',
      ADD COLUMN permission_request_version integer NOT NULL DEFAULT 0,
      ADD COLUMN permission_authority_snapshot_hash varchar(64),
      ADD COLUMN permission_response_schema varchar(300),
      ADD COLUMN permission_expires_at timestamptz,
      ADD COLUMN permission_receipt_ref varchar(300),
      ADD CONSTRAINT ck_session_tool_permission_state CHECK (
        permission_state IN ('not_required','waiting','approved','denied','expired','cancelled')
      )
    """,
    "DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session",
    """
    WITH ranked AS (
      SELECT id,
             row_number() OVER (
               PARTITION BY parent_agent_id, parent_session_id
               ORDER BY
                 CASE status
                   WHEN 'running' THEN 0
                   WHEN 'resumable' THEN 1
                   WHEN 'suspended' THEN 2
                   ELSE 3
                 END,
                 created_at DESC,
                 id DESC
             ) AS row_number
      FROM runtime_tasks
      WHERE task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')
        AND status IN ('pending', 'running', 'suspended', 'resumable')
        AND parent_agent_id IS NOT NULL
        AND parent_session_id IS NOT NULL
    )
    UPDATE runtime_tasks AS task
    SET status='needs_reconciliation',
        result_summary=COALESCE(
          task.result_summary,
          'Duplicate active Session Run quarantined by Session V2 permission migration'
        ),
        metadata_json=(
          COALESCE(task.metadata_json, '{}'::json)::jsonb || jsonb_build_object(
            'needs_reconciliation', true,
            'reconciliation_reason', 'duplicate_active_session_run_during_permission_migration',
            'recovery_owner', 'session_v2_permission_tool_0716'
          )
        )::json
    FROM ranked
    WHERE task.id=ranked.id AND ranked.row_number > 1
    """,
    """
    CREATE UNIQUE INDEX uq_runtime_tasks_active_web_chat_session
      ON runtime_tasks(parent_agent_id, parent_session_id)
      WHERE task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')
        AND status IN ('pending', 'running', 'suspended', 'resumable')
        AND parent_agent_id IS NOT NULL
        AND parent_session_id IS NOT NULL
    """,
)


DOWNGRADE_GUARD_SQL = """
LOCK TABLE session_tool_invocations IN SHARE MODE;

DO $session_v2_permission_tool_downgrade_guard$
BEGIN
  IF EXISTS (
    SELECT 1 FROM session_tool_invocations
    WHERE permission_state <> 'not_required'
       OR permission_request_version <> 0
       OR permission_item_id IS NOT NULL
       OR permission_receipt_ref IS NOT NULL
       OR effective_args_hash IS NOT NULL
  ) THEN
    RAISE EXCEPTION
      'session_v2_permission_tool_downgrade_blocked: permission/effective-argument evidence exists'
      USING ERRCODE='23514';
  END IF;
END;
$session_v2_permission_tool_downgrade_guard$;
"""

DOWNGRADE_GUARD_SQL_STATEMENTS = (
    "LOCK TABLE session_tool_invocations IN SHARE MODE",
    """
    DO $session_v2_permission_tool_downgrade_guard$
    BEGIN
      IF EXISTS (
        SELECT 1 FROM session_tool_invocations
        WHERE permission_state <> 'not_required'
           OR permission_request_version <> 0
           OR permission_item_id IS NOT NULL
           OR permission_receipt_ref IS NOT NULL
           OR effective_args_hash IS NOT NULL
      ) THEN
        RAISE EXCEPTION
          'session_v2_permission_tool_downgrade_blocked: permission/effective-argument evidence exists'
          USING ERRCODE='23514';
      END IF;
    END;
    $session_v2_permission_tool_downgrade_guard$
    """,
)


DOWNGRADE_SQL = """
DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session;

CREATE UNIQUE INDEX uq_runtime_tasks_active_web_chat_session
  ON runtime_tasks(parent_agent_id, parent_session_id)
  WHERE task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')
    AND status IN ('pending', 'running')
    AND parent_agent_id IS NOT NULL
    AND parent_session_id IS NOT NULL;

ALTER TABLE session_tool_invocations
  DROP CONSTRAINT ck_session_tool_permission_state,
  DROP COLUMN permission_receipt_ref,
  DROP COLUMN permission_expires_at,
  DROP COLUMN permission_response_schema,
  DROP COLUMN permission_authority_snapshot_hash,
  DROP COLUMN permission_request_version,
  DROP COLUMN permission_state,
  DROP COLUMN permission_item_id,
  DROP COLUMN effective_args_hash,
  DROP COLUMN effective_arguments_json,
  DROP COLUMN provider_arguments_json,
  DROP COLUMN tool_name;
"""

DOWNGRADE_SQL_STATEMENTS = (
    "DROP INDEX IF EXISTS uq_runtime_tasks_active_web_chat_session",
    """
    CREATE UNIQUE INDEX uq_runtime_tasks_active_web_chat_session
      ON runtime_tasks(parent_agent_id, parent_session_id)
      WHERE task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan')
        AND status IN ('pending', 'running')
        AND parent_agent_id IS NOT NULL
        AND parent_session_id IS NOT NULL
    """,
    """
    ALTER TABLE session_tool_invocations
      DROP CONSTRAINT ck_session_tool_permission_state,
      DROP COLUMN permission_receipt_ref,
      DROP COLUMN permission_expires_at,
      DROP COLUMN permission_response_schema,
      DROP COLUMN permission_authority_snapshot_hash,
      DROP COLUMN permission_request_version,
      DROP COLUMN permission_state,
      DROP COLUMN permission_item_id,
      DROP COLUMN effective_args_hash,
      DROP COLUMN effective_arguments_json,
      DROP COLUMN provider_arguments_json,
      DROP COLUMN tool_name
    """,
)


__all__ = [
    "DOWNGRADE_GUARD_SQL",
    "DOWNGRADE_GUARD_SQL_STATEMENTS",
    "DOWNGRADE_SQL",
    "DOWNGRADE_SQL_STATEMENTS",
    "UPGRADE_SQL",
    "UPGRADE_SQL_STATEMENTS",
]
