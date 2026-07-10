"""Runtime budget control-plane models.

These tables are the run-level safety envelope for autonomous work. Account
token quotas remain outer caps; runtime budgets bound one active continuation
chain so background subagents, delegation, workflow leaves, and parent wakes
cannot amplify without a shared budget.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeBudgetPolicy(Base):
    """Tenant/platform policy template resolved when a root budget run starts."""

    __tablename__ = "runtime_budget_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # platform_default | tenant_default | source_profile | agent | trigger | agent_trigger
    scope_type: Mapped[str] = mapped_column(String(40), nullable=False, default="tenant_default", index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    profile: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)

    enforcement_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="enforce", server_default="enforce"
    )
    fail_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fail_closed", server_default="fail_closed"
    )

    max_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_cache_miss_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_subagents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_team_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_delegations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_background_tasks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_continuation_wakes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_provider_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # §10 circuit-breaker dimensions (failure / reconciliation / parent-wake guards)
    max_failures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_needs_reconciliation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_child_failure_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_parent_invocations: Mapped[int | None] = mapped_column(Integer, nullable=True)

    default_child_token_reservation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=50_000, server_default="50000"
    )
    default_llm_call_token_reservation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=50_000, server_default="50000"
    )
    policy_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RuntimeBudgetRun(Base):
    """One active continuation-chain budget envelope."""

    __tablename__ = "runtime_budget_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "root_run_kind", "root_run_key", name="uq_runtime_budget_root_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_budget_policies.id"), nullable=True, index=True
    )

    root_run_kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    root_run_key: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    root_runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    root_session_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    root_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    root_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    profile: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    # active | completed | exhausted | hard_stopped | expired | cancelled
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="active", server_default="active", index=True
    )
    enforcement_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="enforce", server_default="enforce"
    )
    fail_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fail_closed", server_default="fail_closed"
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    max_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_cache_miss_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    max_subagents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_team_sessions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_delegations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_background_tasks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_continuation_wakes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_provider_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # §10 circuit-breaker dimensions, snapshotted from policy at run creation
    max_failures: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_needs_reconciliation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_child_failure_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_parent_invocations: Mapped[int | None] = mapped_column(Integer, nullable=True)

    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    used_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_cache_miss_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    used_cache_miss_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_subagents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    used_subagents: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_team_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    used_team_sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_delegations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    used_delegations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_background_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    used_background_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_continuation_wakes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    used_continuation_wakes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    used_provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # §10 parent-wake counter (real write point). failures / needs_reconciliation
    # are NOT persisted: they are derived from ground-truth child-task statuses at
    # the wake boundary (query-derivation), so a denormalized column would only
    # risk drift. See the conformance audit for this §7.2 field deviation.
    parent_invocations: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    policy_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # §2 finalization lane state (summary_turn_state / summary_run_id / ...)
    # and other run-scoped control metadata.
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuntimeBudgetEvent(Base):
    """Append-only runtime budget event stream."""

    __tablename__ = "runtime_budget_events"
    __table_args__ = (
        UniqueConstraint("budget_run_id", "reservation_key", "event_type", name="uq_runtime_budget_event_key_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    budget_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_budget_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    reservation_key: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    would_deny: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    amounts_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    runtime_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
