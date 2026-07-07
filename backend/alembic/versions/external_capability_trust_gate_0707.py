"""Add external capability Trust Gate ledger.

Revision ID: external_capability_trust_gate_0707
Revises: runtime_budget_team_sessions_0704
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "external_capability_trust_gate_0707"
down_revision = "runtime_budget_team_sessions_0704"
branch_labels = None
depends_on = None


_TABLES = ("external_capability_reviews", "external_capability_snapshots")


def _tenant_predicate(table: str) -> str:
    return f"""
        current_setting('app.current_tenant_id', true) = 'BYPASS'
        OR {table}.tenant_id::text = current_setting('app.current_tenant_id', true)
    """


def _enable_rls(table: str) -> None:
    predicate = _tenant_predicate(table)
    policy_name = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy_name} ON {table}
            USING ({predicate})
            WITH CHECK ({predicate})
        """
    )


def upgrade() -> None:
    op.create_table(
        "external_capability_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_format", sa.String(length=60), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(length=300), nullable=True),
        sa.Column("source_hash", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="review_required", nullable=False),
        sa.Column("admission_class", sa.String(length=40), server_default="governed_runtime", nullable=False),
        sa.Column("admission_report_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("governance_projection_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("normalized_manifest_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_format", "source_uri", "source_hash", name="uq_external_review_source"),
    )
    op.create_index(op.f("ix_external_capability_reviews_tenant_id"), "external_capability_reviews", ["tenant_id"])
    op.create_index(op.f("ix_external_capability_reviews_source_format"), "external_capability_reviews", ["source_format"])
    op.create_index(op.f("ix_external_capability_reviews_source_hash"), "external_capability_reviews", ["source_hash"])
    op.create_index(op.f("ix_external_capability_reviews_normalized_name"), "external_capability_reviews", ["normalized_name"])
    op.create_index(op.f("ix_external_capability_reviews_status"), "external_capability_reviews", ["status"])
    op.create_index(op.f("ix_external_capability_reviews_admission_class"), "external_capability_reviews", ["admission_class"])
    op.create_index(op.f("ix_external_capability_reviews_created_by_user_id"), "external_capability_reviews", ["created_by_user_id"])

    op.create_table(
        "external_capability_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_key", sa.String(length=260), nullable=False),
        sa.Column("source_format", sa.String(length=60), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(length=300), nullable=True),
        sa.Column("source_hash", sa.String(length=128), nullable=False),
        sa.Column("normalized_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="approved", nullable=False),
        sa.Column("admission_class", sa.String(length=40), nullable=False),
        sa.Column("admission_report_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("governance_projection_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("component_manifest_json", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "snapshot_key", name="uq_external_snapshot_key"),
    )
    op.create_index(op.f("ix_external_capability_snapshots_tenant_id"), "external_capability_snapshots", ["tenant_id"])
    op.create_index(op.f("ix_external_capability_snapshots_review_id"), "external_capability_snapshots", ["review_id"])
    op.create_index(op.f("ix_external_capability_snapshots_source_format"), "external_capability_snapshots", ["source_format"])
    op.create_index(op.f("ix_external_capability_snapshots_source_hash"), "external_capability_snapshots", ["source_hash"])
    op.create_index(op.f("ix_external_capability_snapshots_normalized_name"), "external_capability_snapshots", ["normalized_name"])
    op.create_index(op.f("ix_external_capability_snapshots_status"), "external_capability_snapshots", ["status"])
    op.create_index(op.f("ix_external_capability_snapshots_admission_class"), "external_capability_snapshots", ["admission_class"])
    op.create_index(op.f("ix_external_capability_snapshots_approved_by_user_id"), "external_capability_snapshots", ["approved_by_user_id"])

    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("external_capability_snapshots")
    op.drop_table("external_capability_reviews")
