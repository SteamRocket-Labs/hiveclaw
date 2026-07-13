"""Close RuntimeTask fencing and typed transcript projection contracts.

Revision ID: runtime_event_fencing_0710
Revises: external_capability_strict_rls_0709
Create Date: 2026-07-10
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "runtime_event_fencing_0710"
down_revision = "external_capability_strict_rls_0709"
branch_labels = None
depends_on = None


_TASK_TYPES = (
    "web_chat_turn",
    "goal_continuation",
    "team_member",
    "advanced_plan",
    "workflow",
    "delegation",
    "business_task",
    "subagent",
    "trigger",
    "heartbeat",
    "coordinator_worker",
    "harness_canary",
    "a2a_delegation",
)
_TASK_STATUSES = (
    "pending",
    "running",
    "completed",
    "failed",
    "killed",
    "skipped",
    "needs_reconciliation",
    "resumable",
    "suspended",
)
_LEGACY_DEEP_RESEARCH_TERMINAL_STATUSES = (
    "completed",
    "failed",
    "killed",
    "skipped",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _assert_runtime_task_domain(column: str, allowed: tuple[str, ...]) -> None:
    bind = op.get_bind()
    invalid = {
        str(row[0])
        for row in bind.execute(
            sa.text(f"SELECT DISTINCT {column} FROM runtime_tasks WHERE {column} NOT IN ({_quoted(allowed)})")
        )
    }
    if invalid:
        raise RuntimeError(f"runtime_tasks.{column} has unsupported values before typed constraint: {sorted(invalid)}")


def _migrate_terminal_legacy_deep_research_tasks() -> None:
    """Preserve retired Deep Research evidence under its workflow-native type.

    Only terminal rows are safe to normalize. A non-terminal legacy row remains
    visible to the domain assertion below so the release fails closed instead
    of replaying an obsolete executor through the workflow worker.
    """

    op.execute(
        sa.text(
            f"""
            UPDATE runtime_tasks
            SET task_type = 'workflow',
                metadata_json = jsonb_set(
                    COALESCE(metadata_json::jsonb, '{{}}'::jsonb),
                    '{{runtime_type_migration}}',
                    COALESCE(
                        metadata_json::jsonb -> 'runtime_type_migration',
                        '{{}}'::jsonb
                    ) || jsonb_build_object(
                        'source_type', 'deep_research',
                        'target_type', 'workflow',
                        'migration_revision', 'runtime_event_fencing_0710',
                        'execution_replayed', false
                    ),
                    true
                )::json
            WHERE task_type = 'deep_research'
              AND status IN ({_quoted(_LEGACY_DEEP_RESEARCH_TERMINAL_STATUSES)})
            """
        )
    )


def upgrade() -> None:
    _migrate_terminal_legacy_deep_research_tasks()
    _assert_runtime_task_domain("task_type", _TASK_TYPES)
    _assert_runtime_task_domain("status", _TASK_STATUSES)

    op.add_column(
        "runtime_tasks",
        sa.Column("claim_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("runtime_tasks", sa.Column("root_idempotency_key", sa.String(length=200), nullable=True))
    op.add_column("runtime_tasks", sa.Column("config_snapshot_hash", sa.String(length=64), nullable=True))
    op.add_column("runtime_tasks", sa.Column("policy_snapshot_hash", sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE runtime_tasks SET root_idempotency_key = task_type || ':' || id::text "
        "WHERE root_idempotency_key IS NULL"
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, task_type, parent_agent_id, child_agent_id, parent_session_id, child_session_id, "
            "depth, prompt, tenant_id, budget_run_id, budget_snapshot_json, metadata_json FROM runtime_tasks"
        )
    ).mappings()
    for row in rows:
        metadata = dict(row["metadata_json"] or {})
        config_snapshot = {
            "task_type": row["task_type"],
            "parent_agent_id": str(row["parent_agent_id"]) if row["parent_agent_id"] else None,
            "child_agent_id": str(row["child_agent_id"]) if row["child_agent_id"] else None,
            "parent_session_id": row["parent_session_id"],
            "child_session_id": row["child_session_id"],
            "depth": row["depth"],
            "prompt": row["prompt"],
            "source": metadata.get("source"),
            "definition_hash": metadata.get("definition_hash"),
            "model_id": metadata.get("model_id"),
        }
        policy_snapshot = {
            "tenant_id": str(row["tenant_id"]) if row["tenant_id"] else None,
            "permission_mode": metadata.get("permission_mode"),
            "permission_profile": metadata.get("permission_profile"),
            "budget_run_id": str(row["budget_run_id"]) if row["budget_run_id"] else None,
            "budget_snapshot": row["budget_snapshot_json"],
            "guard_policy": metadata.get("guard_policy"),
        }
        bind.execute(
            sa.text(
                "UPDATE runtime_tasks SET config_snapshot_hash = :config_hash, "
                "policy_snapshot_hash = :policy_hash WHERE id = :task_id"
            ),
            {
                "task_id": row["id"],
                "config_hash": hashlib.sha256(
                    json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
                "policy_hash": hashlib.sha256(
                    json.dumps(policy_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            },
        )
    op.alter_column("runtime_tasks", "config_snapshot_hash", nullable=False)
    op.alter_column("runtime_tasks", "policy_snapshot_hash", nullable=False)
    op.alter_column("runtime_tasks", "root_idempotency_key", nullable=False)
    op.create_unique_constraint(
        "uq_runtime_tasks_root_idempotency_key",
        "runtime_tasks",
        ["root_idempotency_key"],
    )
    op.create_check_constraint(
        "ck_runtime_tasks_task_type",
        "runtime_tasks",
        f"task_type IN ({_quoted(_TASK_TYPES)})",
    )
    op.create_check_constraint(
        "ck_runtime_tasks_status",
        "runtime_tasks",
        f"status IN ({_quoted(_TASK_STATUSES)})",
    )

    op.add_column(
        "chat_transcript_events",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "chat_transcript_events",
        sa.Column("item_type", sa.String(length=64), nullable=False, server_default=sa.text("'event'")),
    )
    op.add_column(
        "chat_transcript_events",
        sa.Column("item_status", sa.String(length=32), nullable=False, server_default=sa.text("'succeeded'")),
    )
    op.add_column("chat_transcript_events", sa.Column("turn_id", sa.String(length=200), nullable=True))
    op.add_column("chat_transcript_events", sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("chat_transcript_events", sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "chat_transcript_events",
        sa.Column("projection_status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
    )
    op.add_column(
        "chat_transcript_events",
        sa.Column("projection_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("chat_transcript_events", sa.Column("projection_error", sa.Text(), nullable=True))
    op.add_column("chat_transcript_events", sa.Column("projected_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        """
        UPDATE chat_transcript_events
        SET item_type = CASE
            WHEN event_type = 'user_message' OR actor_type = 'user' THEN 'user_message'
            WHEN event_type = 'assistant_message' OR actor_type = 'assistant' THEN 'agent_message'
            WHEN event_type LIKE '%permission_request%' OR event_type LIKE '%approval_request%' THEN 'approval_request'
            WHEN event_type LIKE 'tool_%result%' THEN 'tool_result'
            WHEN event_type LIKE 'tool_%' THEN 'tool_call'
            WHEN event_type LIKE '%workflow%' THEN 'workflow_activity'
            WHEN event_type LIKE '%subagent%' OR event_type LIKE '%delegation%' THEN 'subagent_activity'
            WHEN event_type LIKE '%plan%' THEN 'plan'
            WHEN event_type LIKE '%compact%' THEN 'context_compaction'
            WHEN event_type LIKE 'run_%' THEN 'boundary'
            ELSE 'event'
        END,
        item_status = CASE
            WHEN event_type LIKE '%failed%' THEN 'failed'
            WHEN event_type LIKE '%cancelled%' OR event_type LIKE '%killed%' THEN 'cancelled'
            WHEN event_type LIKE '%started%' THEN 'running'
            WHEN event_type LIKE '%permission_request%' OR event_type LIKE '%approval_request%' THEN 'waiting_user'
            ELSE 'succeeded'
        END,
        projection_status = CASE
            WHEN COALESCE((metadata_json ->> 't0_bridge_pending')::boolean, false) THEN 'pending'
            ELSE 'projected'
        END,
        projected_at = CASE
            WHEN COALESCE((metadata_json ->> 't0_bridge_pending')::boolean, false) THEN NULL
            ELSE created_at
        END
        """
    )
    op.create_index("ix_chat_transcript_events_turn_id", "chat_transcript_events", ["turn_id"])
    op.create_index("ix_chat_transcript_events_causation_id", "chat_transcript_events", ["causation_id"])
    op.create_index("ix_chat_transcript_events_correlation_id", "chat_transcript_events", ["correlation_id"])
    op.create_index("ix_chat_transcript_events_projection_status", "chat_transcript_events", ["projection_status"])

    op.add_column("invocation_spans", sa.Column("decision_id", sa.String(length=128), nullable=True))
    op.add_column("invocation_spans", sa.Column("input_hash", sa.String(length=64), nullable=True))
    op.add_column("invocation_spans", sa.Column("claim_version", sa.Integer(), nullable=True))
    op.add_column("invocation_spans", sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.add_column(
        "invocation_spans",
        sa.Column(
            "side_effect_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_index("ix_invocation_spans_decision_id", "invocation_spans", ["decision_id"])
    op.create_index("ix_invocation_spans_idempotency_key", "invocation_spans", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index("ix_invocation_spans_idempotency_key", table_name="invocation_spans")
    op.drop_index("ix_invocation_spans_decision_id", table_name="invocation_spans")
    op.drop_column("invocation_spans", "side_effect_refs")
    op.drop_column("invocation_spans", "idempotency_key")
    op.drop_column("invocation_spans", "claim_version")
    op.drop_column("invocation_spans", "input_hash")
    op.drop_column("invocation_spans", "decision_id")

    op.drop_index("ix_chat_transcript_events_projection_status", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_correlation_id", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_causation_id", table_name="chat_transcript_events")
    op.drop_index("ix_chat_transcript_events_turn_id", table_name="chat_transcript_events")
    op.drop_column("chat_transcript_events", "projected_at")
    op.drop_column("chat_transcript_events", "projection_error")
    op.drop_column("chat_transcript_events", "projection_attempts")
    op.drop_column("chat_transcript_events", "projection_status")
    op.drop_column("chat_transcript_events", "correlation_id")
    op.drop_column("chat_transcript_events", "causation_id")
    op.drop_column("chat_transcript_events", "turn_id")
    op.drop_column("chat_transcript_events", "item_status")
    op.drop_column("chat_transcript_events", "item_type")
    op.drop_column("chat_transcript_events", "schema_version")

    op.drop_constraint("ck_runtime_tasks_status", "runtime_tasks", type_="check")
    op.drop_constraint("ck_runtime_tasks_task_type", "runtime_tasks", type_="check")
    op.execute(
        sa.text(
            """
            UPDATE runtime_tasks
            SET task_type = 'deep_research'
            WHERE task_type = 'workflow'
              AND metadata_json::jsonb -> 'runtime_type_migration' ->> 'source_type' = 'deep_research'
              AND metadata_json::jsonb -> 'runtime_type_migration' ->> 'migration_revision'
                    = 'runtime_event_fencing_0710'
            """
        )
    )
    op.drop_constraint("uq_runtime_tasks_root_idempotency_key", "runtime_tasks", type_="unique")
    op.drop_column("runtime_tasks", "policy_snapshot_hash")
    op.drop_column("runtime_tasks", "config_snapshot_hash")
    op.drop_column("runtime_tasks", "root_idempotency_key")
    op.drop_column("runtime_tasks", "claim_version")
