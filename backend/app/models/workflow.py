"""Workflow runtime journal + definition models (§9 P1).

Design (docs/workflow-source-capability.md §3.3 / §10):

* **A workflow run IS a ``RuntimeTask(task_type="workflow")``** — there is no
  separate run table (no fifth background ledger). Run-level metadata
  (``definition_source``, ``definition_hash``, ``args_hash``,
  ``confirmed_plan_id``, ``tenant_id`` mirror) lives in
  ``RuntimeTask.metadata_json``.
* The models here are the journal sub-tables hanging off that run record:
  ``RuntimeTask -> WorkflowStep -> WorkflowLeafCall`` (leaf-level journal,
  v1 decision 6), plus the per-run budget envelope ``WorkflowQuota`` and the
  registered-definition store ``WorkflowDefinitionRecord``.
* **Tenant-correct from day one** (§10 invariant ⑥): every table carries
  ``tenant_id`` and ships with RLS ENABLEd **and FORCEd** — unlike the legacy
  ENABLE-only tables, the owner connection cannot bypass these policies
  (P0 gap B). All access must therefore go through ``tenant_scoped_session``.
* ``definition_version`` + ``definition_hash`` are immutable once a version
  is active: content changes create a NEW version row (unique constraint on
  ``tenant_id, name, definition_version`` anchors this; the service layer in
  P6 enforces the no-in-place-update rule).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorkflowDefinitionRecord(Base):
    """A registered (persistent, versioned) workflow definition.

    Ephemeral definitions are NOT stored here — they live in the run's
    ``RuntimeTask.metadata_json`` archive. ``visibility_scope=platform`` rows
    are factory-curated read-only templates with ``tenant_id IS NULL``
    (visible to every tenant through the policy's NULL escape, never written
    by tenant code — P6 enforces)."""

    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "definition_version", name="uq_workflow_definition_version"),
        UniqueConstraint("promotion_proposal_id", name="uq_workflow_definition_promotion_proposal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    definition_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    definition_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    definition_json: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # draft | active | deprecated | revoked
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft", index=True)
    # agent | org | tenant | platform
    visibility_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="agent", server_default="agent")
    # allowed_agents / allowed_roles / allowed_orgs
    call_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # user | agent | org | tenant | platform
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user", server_default="user")
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # promote provenance: the ephemeral run this version was promoted from
    promoted_from_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    promotion_proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "workflow_promotion_proposals.id",
            ondelete="RESTRICT",
            name="fk_workflow_definitions_promotion_proposal_id",
        ),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowPromotionProposal(Base):
    """Immutable run evidence submitted by its session owner for two-person review.

    Only review lifecycle columns may change after insert. PostgreSQL enforces
    snapshot immutability as well, so an ORM bypass cannot rewrite what the
    reviewer approved.
    """

    __tablename__ = "workflow_promotion_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "proposal_hash", name="uq_workflow_promotion_proposal_hash"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','stale','withdrawn')",
            name="ck_workflow_promotion_proposal_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    root_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    run_evidence_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", server_default="pending")
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowStep(Base):
    """Step-level journal row for one workflow run (RuntimeTask).

    ``input_hash`` + ``definition_hash`` are what make resume safe: a done
    step is only reused when both still match (§3.3)."""

    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_id", name="uq_workflow_step_per_run"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # step identifier within the definition (NOT a row id)
    step_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # agent_step | fanout_step | gate_step | condition | wait_until
    step_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="agent_step", server_default="agent_step"
    )
    # pending | running | done | failed | skipped | suspended |
    # unknown_requires_reconciliation (P10 drain policy)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="pending", server_default="pending", index=True
    )

    input_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    definition_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowLeafCall(Base):
    """Leaf-level journal (v1 decision 6): one row per spawned leaf inside a
    step, so 8-of-8 fan-out with 7 done resumes exactly 1 — never the whole
    step. ``idempotency_key`` is unique per run for exactly-once spawn intent."""

    __tablename__ = "workflow_leaf_calls"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "leaf_id", name="uq_workflow_leaf_per_step"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_workflow_leaf_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # leaf identifier within the fanout (e.g. input index / item key)
    leaf_id: Mapped[str] = mapped_column(String(200), nullable=False)

    input_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    definition_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # pending | running | done | failed
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending", index=True
    )
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class WorkflowQuota(Base):
    """Per-run budget envelope (§3.4): pre-deducted at spawn under a Postgres
    advisory lock (P5); admission hard-rejects when exhausted. One row per run."""

    __tablename__ = "workflow_quotas"
    __table_args__ = (UniqueConstraint("run_id", name="uq_workflow_quota_per_run"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runtime_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allocated_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    consumed_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
