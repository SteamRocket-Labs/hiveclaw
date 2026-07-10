"""Bind approvals to immutable execution tickets.

Revision ID: approval_ticket_governance_0710
Revises: runtime_event_fencing_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

import ast
from datetime import timedelta
import hashlib
import json
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "approval_ticket_governance_0710"
down_revision = "runtime_event_fencing_0710"
branch_labels = None
depends_on = None


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _arguments(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                parsed = {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def upgrade() -> None:
    op.add_column("approval_requests", sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("approval_requests", sa.Column("decision_id", sa.String(length=128), nullable=True))
    op.add_column("approval_requests", sa.Column("tool_name", sa.String(length=100), nullable=True))
    op.add_column("approval_requests", sa.Column("normalized_arguments", postgresql.JSON(), nullable=True))
    op.add_column("approval_requests", sa.Column("input_hash", sa.String(length=64), nullable=True))
    op.add_column("approval_requests", sa.Column("policy_snapshot", postgresql.JSON(), nullable=True))
    op.add_column("approval_requests", sa.Column("policy_snapshot_hash", sa.String(length=64), nullable=True))
    op.add_column("approval_requests", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("approval_requests", sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "approval_requests",
        sa.Column("execution_status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
    )
    op.add_column("approval_requests", sa.Column("execution_idempotency_key", sa.String(length=200), nullable=True))
    op.add_column("approval_requests", sa.Column("execution_result", sa.Text(), nullable=True))
    op.add_column("approval_requests", sa.Column("execution_receipt", postgresql.JSON(), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, agent_id, action_type, details, status, created_at, resolved_by "
            "FROM approval_requests"
        )
    ).mappings()
    for row in rows:
        details = dict(row["details"] or {})
        tool_name = str(details.get("tool") or "").strip() or None
        arguments = _arguments(details.get("args")) if tool_name else None
        requested_by = None
        try:
            requested_by = uuid.UUID(str(details.get("requested_by"))) if details.get("requested_by") else None
        except ValueError:
            requested_by = None
        policy_snapshot = {
            "capability": row["action_type"],
            "tenant_id": str(row["tenant_id"]) if row["tenant_id"] else None,
            "agent_id": str(row["agent_id"]),
            "reason": details.get("reason"),
            "origin": details.get("origin"),
        }
        input_hash = (
            hashlib.sha256(_canonical({"tool_name": tool_name, "arguments": arguments}).encode("utf-8")).hexdigest()
            if tool_name
            else None
        )
        policy_hash = hashlib.sha256(_canonical(policy_snapshot).encode("utf-8")).hexdigest()
        execution_status = (
            "needs_reapproval"
            if row["status"] == "approved" and tool_name
            else {"approved": "approved", "rejected": "rejected"}.get(row["status"], "pending")
        )
        bind.execute(
            sa.text(
                "UPDATE approval_requests SET requested_by=:requested_by, tool_name=:tool_name, "
                "decision_id=:decision_id, "
                "normalized_arguments=:arguments, input_hash=:input_hash, policy_snapshot=:policy_snapshot, "
                "policy_snapshot_hash=:policy_hash, expires_at=:expires_at, execution_status=:execution_status, "
                "execution_idempotency_key=:idempotency_key WHERE id=:approval_id"
            ),
            {
                "approval_id": row["id"],
                "requested_by": requested_by,
                "decision_id": f"legacy-approval:{row['id']}",
                "tool_name": tool_name,
                "arguments": json.dumps(arguments) if arguments is not None else None,
                "input_hash": input_hash,
                "policy_snapshot": json.dumps(policy_snapshot),
                "policy_hash": policy_hash,
                "expires_at": row["created_at"] + timedelta(days=7),
                "execution_status": execution_status,
                "idempotency_key": f"approval:{row['id']}",
            },
        )

    op.create_foreign_key(
        "fk_approval_requests_requested_by_users",
        "approval_requests",
        "users",
        ["requested_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_approval_requests_requested_by", "approval_requests", ["requested_by"])
    op.create_index("ix_approval_requests_decision_id", "approval_requests", ["decision_id"])
    op.create_index("ix_approval_requests_input_hash", "approval_requests", ["input_hash"])
    op.create_index("ix_approval_requests_expires_at", "approval_requests", ["expires_at"])
    op.create_unique_constraint(
        "uq_approval_requests_execution_idempotency_key",
        "approval_requests",
        ["execution_idempotency_key"],
    )
    op.create_check_constraint(
        "ck_approval_requests_execution_status",
        "approval_requests",
        "execution_status IN ('pending','approved','rejected','executing','succeeded','failed',"
        "'needs_reconciliation','needs_reapproval')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_approval_requests_execution_status", "approval_requests", type_="check")
    op.drop_constraint("uq_approval_requests_execution_idempotency_key", "approval_requests", type_="unique")
    op.drop_index("ix_approval_requests_expires_at", table_name="approval_requests")
    op.drop_index("ix_approval_requests_input_hash", table_name="approval_requests")
    op.drop_index("ix_approval_requests_requested_by", table_name="approval_requests")
    op.drop_index("ix_approval_requests_decision_id", table_name="approval_requests")
    op.drop_constraint("fk_approval_requests_requested_by_users", "approval_requests", type_="foreignkey")
    for column in (
        "execution_receipt",
        "execution_result",
        "execution_idempotency_key",
        "execution_status",
        "consumed_at",
        "expires_at",
        "policy_snapshot_hash",
        "policy_snapshot",
        "input_hash",
        "normalized_arguments",
        "tool_name",
        "requested_by",
        "decision_id",
    ):
        op.drop_column("approval_requests", column)
