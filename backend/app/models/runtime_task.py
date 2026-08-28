"""Runtime task model for persistent subagent lifecycle tracking.

Tracks agent-to-agent delegation tasks with full lifecycle:
spawn → running → completed/failed/killed.

This is separate from the business-layer Task model (models/task.py)
which tracks user-facing tasks. RuntimeTask tracks the internal
agent execution machinery.
"""

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.services.channel_secret_storage import EncryptedDeliveryTargetJSON


COMPLETION_OUTBOX_GENERATION = 1
COMPLETION_OUTBOX_RETRY_SECONDS = 30
COMPLETION_OUTBOX_TASK_TYPES = (
    "subagent",
    "team_member",
    "workflow",
    "delegation",
    "a2a_delegation",
    "a2a_continuation",
    "trigger",
    "approval_execution",
)
COMPLETION_OUTBOX_TERMINAL_STATUSES = (
    "completed",
    "failed",
    "killed",
    "skipped",
    "needs_reconciliation",
)
COMPLETION_OUTBOX_PENDING_SQL = (
    "completion_outbox_generation IS NOT NULL "
    "AND completion_outbox_settled_at IS NULL "
    "AND task_type IN ('subagent', 'team_member', 'workflow', 'delegation', 'a2a_delegation', "
    "'a2a_continuation', 'trigger', 'approval_execution') "
    "AND status IN ('completed', 'failed', 'killed', 'skipped', 'needs_reconciliation') "
    "AND NOT (task_type = 'trigger' AND status = 'skipped') "
    "AND parent_agent_id IS NOT NULL"
)


class RuntimeTask(Base):
    """Persistent record of a subagent delegation task."""

    __tablename__ = "runtime_tasks"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan', "
            "'workflow', 'delegation', 'business_task', 'subagent', 'trigger', 'heartbeat', "
            "'coordinator_worker', 'harness_canary', 'a2a_delegation', 'approval_execution', "
            "'hr_provisioning', 'dream', 'a2a_continuation')",
            name="ck_runtime_tasks_task_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'killed', 'skipped', "
            "'needs_reconciliation', 'resumable', 'suspended')",
            name="ck_runtime_tasks_status",
        ),
        UniqueConstraint("root_idempotency_key", name="uq_runtime_tasks_root_idempotency_key"),
        Index(
            "ix_runtime_tasks_root_authority",
            "tenant_id",
            "parent_agent_id",
            "root_user_id",
            "root_session_id",
        ),
        Index(
            "uq_runtime_tasks_active_web_chat_session",
            "parent_agent_id",
            "parent_session_id",
            unique=True,
            postgresql_where=text(
                "task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan', "
                "'a2a_continuation') "
                "AND status IN ('pending', 'running', 'suspended', 'resumable') "
                "AND parent_agent_id IS NOT NULL "
                "AND parent_session_id IS NOT NULL"
            ),
            sqlite_where=text(
                "task_type IN ('web_chat_turn', 'goal_continuation', 'team_member', 'advanced_plan', "
                "'a2a_continuation') "
                "AND status IN ('pending', 'running', 'suspended', 'resumable') "
                "AND parent_agent_id IS NOT NULL "
                "AND parent_session_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_runtime_tasks_completion_outbox_pending",
            text("(completion_outbox_attempted_at IS NOT NULL)"),
            text("completion_outbox_attempted_at ASC"),
            text("created_at ASC"),
            postgresql_where=text(COMPLETION_OUTBOX_PENDING_SQL),
            sqlite_where=text(COMPLETION_OUTBOX_PENDING_SQL),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Task type: delegation, heartbeat, trigger, coordinator_worker
    task_type: Mapped[str] = mapped_column(String(50), nullable=False, default="delegation")

    # Parent-child relationship
    parent_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    child_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    child_agent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Tenant scope (RLS): derived from the durable run authority and never global.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )

    # Lifecycle
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        index=True,
    )  # pending → running → completed | failed | killed
    writer_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    # Context
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Tracing
    trace_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    parent_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    child_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    root_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    root_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    delegation_chain_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default=text("'[]'"))
    depth: Mapped[int] = mapped_column(default=1)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Durable recovery eligibility and fairness ledger for the narrow crash
    # gap between a terminal RuntimeTask write and its completion outbox row.
    # Historical terminal rows intentionally retain NULL generation and are
    # never mistaken for fresh user-visible notifications.
    completion_outbox_generation: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
        default=COMPLETION_OUTBOX_GENERATION,
        server_default=text(str(COMPLETION_OUTBOX_GENERATION)),
    )
    completion_outbox_settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_outbox_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_outbox_attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    completion_outbox_last_error: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Cross-process queue claim / lease metadata
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"), index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    claim_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    root_idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    config_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Runtime budget admission metadata
    budget_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_budget_runs.id"), nullable=True, index=True
    )
    root_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    budget_reservation_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    budget_admission_status: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    budget_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    budget_terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Metadata
    metadata_json: Mapped[dict | None] = mapped_column(
        EncryptedDeliveryTargetJSON(),
        nullable=True,
    )

    __mapper_args__ = {
        "version_id_col": claim_version,
        "version_id_generator": False,
    }


@event.listens_for(RuntimeTask, "before_insert")
def _set_runtime_task_root_idempotency_key(_mapper, _connection, target: RuntimeTask) -> None:
    if target.id is None:
        target.id = uuid.uuid4()
    if not str(getattr(target, "root_idempotency_key", "") or "").strip():
        target.root_idempotency_key = f"{target.task_type}:{target.id}"
    metadata = dict(getattr(target, "metadata_json", None) or {})
    if not str(getattr(target, "config_snapshot_hash", "") or "").strip():
        config_snapshot = {
            "task_type": target.task_type,
            "parent_agent_id": str(target.parent_agent_id) if target.parent_agent_id else None,
            "child_agent_id": str(target.child_agent_id) if target.child_agent_id else None,
            "parent_session_id": target.parent_session_id,
            "child_session_id": target.child_session_id,
            "root_user_id": str(target.root_user_id) if target.root_user_id else None,
            "root_session_id": target.root_session_id,
            "delegation_chain": target.delegation_chain_json,
            "depth": target.depth,
            "prompt": target.prompt,
            "source": metadata.get("source"),
            "definition_hash": metadata.get("definition_hash"),
            "model_id": metadata.get("model_id"),
        }
        target.config_snapshot_hash = hashlib.sha256(
            json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    if not str(getattr(target, "policy_snapshot_hash", "") or "").strip():
        policy_snapshot = {
            "tenant_id": str(target.tenant_id) if target.tenant_id else None,
            "permission_mode": metadata.get("permission_mode"),
            "permission_profile": metadata.get("permission_profile"),
            "budget_run_id": str(target.budget_run_id) if target.budget_run_id else None,
            "root_user_id": str(target.root_user_id) if target.root_user_id else None,
            "root_session_id": target.root_session_id,
            "delegation_chain": target.delegation_chain_json,
            "budget_snapshot": target.budget_snapshot_json,
            "guard_policy": metadata.get("guard_policy"),
        }
        target.policy_snapshot_hash = hashlib.sha256(
            json.dumps(policy_snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


@event.listens_for(RuntimeTask, "load")
def _check_loaded_runtime_task_fence(target: RuntimeTask, _context) -> None:
    from app.services.runtime_task_fence import assert_runtime_task_fence

    assert_runtime_task_fence(target)


# Keep isolated RuntimeTask imports mapper-safe for tests and maintenance tools.
from app.models.runtime_budget import RuntimeBudgetRun  # noqa: E402, F401
