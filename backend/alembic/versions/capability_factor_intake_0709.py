"""Add capability factor intake tables.

Revision ID: capability_factor_intake_0709
Revises: external_marketplace_sources_0709
Create Date: 2026-07-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "capability_factor_intake_0709"
down_revision = "external_marketplace_sources_0709"
branch_labels = None
depends_on = None


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
        "capability_factors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "originating_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "originating_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("factor_kind", sa.String(length=60), nullable=False),
        sa.Column(
            "source_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "trace_refs_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("artifact_ref", sa.Text(), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=128), nullable=True),
        sa.Column("upstream_source_ref", sa.Text(), nullable=True),
        sa.Column("upstream_content_sha256", sa.String(length=128), nullable=True),
        sa.Column(
            "license_report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(length=260), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "authoring_contract_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "declared_components_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "declared_permissions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "sensitivity_report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "reuse_score_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("suggested_scope", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="captured", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_tenant_id"),
        "capability_factors",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_originating_agent_id"),
        "capability_factors",
        ["originating_agent_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_originating_user_id"),
        "capability_factors",
        ["originating_user_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_factor_kind"),
        "capability_factors",
        ["factor_kind"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_suggested_scope"),
        "capability_factors",
        ["suggested_scope"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_status"),
        "capability_factors",
        ["status"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factors_created_at"),
        "capability_factors",
        ["created_at"],
        if_not_exists=True,
    )
    _enable_rls("capability_factors")

    op.create_table(
        "capability_factor_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "factor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capability_factors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "automated_report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "admission_report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "findings_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "dedupe_report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "eval_report_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factor_reviews_tenant_id"),
        "capability_factor_reviews",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factor_reviews_factor_id"),
        "capability_factor_reviews",
        ["factor_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factor_reviews_decision"),
        "capability_factor_reviews",
        ["decision"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_factor_reviews_created_at"),
        "capability_factor_reviews",
        ["created_at"],
        if_not_exists=True,
    )
    _enable_rls("capability_factor_reviews")

    op.create_table(
        "capability_promotion_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "factor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capability_factors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("capability_factor_reviews.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("proposed_snapshot_kind", sa.String(length=40), nullable=False),
        sa.Column("proposed_catalog_scope", sa.String(length=40), server_default="workspace", nullable=False),
        sa.Column("proposed_activation_policy", sa.String(length=60), server_default="requestable", nullable=False),
        sa.Column(
            "proposed_selector_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "approver_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("decision", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column(
            "resulting_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("external_capability_snapshots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_tenant_id"),
        "capability_promotion_proposals",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_factor_id"),
        "capability_promotion_proposals",
        ["factor_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_review_id"),
        "capability_promotion_proposals",
        ["review_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_approver_id"),
        "capability_promotion_proposals",
        ["approver_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_decision"),
        "capability_promotion_proposals",
        ["decision"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_resulting_snapshot_id"),
        "capability_promotion_proposals",
        ["resulting_snapshot_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_capability_promotion_proposals_created_at"),
        "capability_promotion_proposals",
        ["created_at"],
        if_not_exists=True,
    )
    _enable_rls("capability_promotion_proposals")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_capability_promotion_proposals ON capability_promotion_proposals"
    )
    op.execute("ALTER TABLE capability_promotion_proposals DISABLE ROW LEVEL SECURITY")
    op.drop_table("capability_promotion_proposals")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_capability_factor_reviews ON capability_factor_reviews")
    op.execute("ALTER TABLE capability_factor_reviews DISABLE ROW LEVEL SECURITY")
    op.drop_table("capability_factor_reviews")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_capability_factors ON capability_factors")
    op.execute("ALTER TABLE capability_factors DISABLE ROW LEVEL SECURITY")
    op.drop_table("capability_factors")
