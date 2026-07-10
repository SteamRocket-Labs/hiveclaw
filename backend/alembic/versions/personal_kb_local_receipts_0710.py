"""Close Personal KB proposals and Local/A2A receipt recovery.

Revision ID: personal_kb_local_receipts_0710
Revises: ai_asset_control_plane_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "personal_kb_local_receipts_0710"
down_revision = "ai_asset_control_plane_0710"
branch_labels = None
depends_on = None

_PERSONAL_KB_LOCAL_RLS_TABLES = (
    "personal_knowledge_proposals",
    "local_agent_capability_snapshots",
)


def _enable_strict_tenant_rls(table: str) -> None:
    policy = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy} ON {table}
        USING (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
            current_setting('app.current_tenant_id', true) = 'BYPASS'
            OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def upgrade() -> None:
    op.create_table(
        "personal_knowledge_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proposed_by_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "delegated_by_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("delegation_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("proposed_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "baseline_document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "baseline_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("baseline_content_hash", sa.String(length=64), nullable=True),
        sa.Column("diff_unified", sa.Text(), nullable=False),
        sa.Column("target_collection", sa.String(length=120), server_default="inbox", nullable=False),
        sa.Column(
            "source_refs_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("sensitivity", sa.String(length=30), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("policy_outcome", sa.String(length=20), nullable=False),
        sa.Column(
            "policy_reason_codes_json",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column(
            "reviewed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("config_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rollback_ref", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_personal_kb_proposal_idempotency"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','committed','failed')",
            name="ck_personal_kb_proposal_status",
        ),
        sa.CheckConstraint(
            "policy_outcome IN ('approve','ask','reject')",
            name="ck_personal_kb_proposal_policy_outcome",
        ),
    )
    for name, columns in (
        ("ix_personal_knowledge_proposals_tenant_id", ["tenant_id"]),
        ("ix_personal_knowledge_proposals_owner_user_id", ["owner_user_id"]),
        ("ix_personal_knowledge_proposals_proposed_by_agent_id", ["proposed_by_agent_id"]),
        ("ix_personal_knowledge_proposals_delegated_by_agent_id", ["delegated_by_agent_id"]),
        ("ix_personal_knowledge_proposals_content_hash", ["content_hash"]),
        ("ix_personal_knowledge_proposals_baseline_document_id", ["baseline_document_id"]),
        ("ix_personal_knowledge_proposals_dedupe_key", ["dedupe_key"]),
        ("ix_personal_knowledge_proposals_status", ["status"]),
        ("ix_personal_knowledge_proposals_document_id", ["document_id"]),
        ("ix_personal_knowledge_proposals_revision_id", ["revision_id"]),
        ("ix_personal_kb_proposals_owner_status", ["owner_user_id", "status"]),
        ("ix_personal_kb_proposals_agent_created", ["proposed_by_agent_id", "created_at"]),
    ):
        op.create_index(name, "personal_knowledge_proposals", columns)

    op.create_table(
        "local_agent_capability_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("local_agent_channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("local_agent_bridge_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(length=120), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reported_capabilities_json", postgresql.JSONB(), nullable=False),
        sa.Column("server_capabilities_json", postgresql.JSONB(), nullable=False),
        sa.Column("agent_capabilities_json", postgresql.JSONB(), nullable=False),
        sa.Column("effective_capabilities_json", postgresql.JSONB(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("channel_id", "version", name="uq_local_agent_capability_snapshot_version"),
        sa.UniqueConstraint("snapshot_hash", name="uq_local_agent_capability_snapshot_hash"),
    )
    for name, columns in (
        ("ix_local_agent_capability_snapshots_tenant_id", ["tenant_id"]),
        ("ix_local_agent_capability_snapshots_channel_id", ["channel_id"]),
        ("ix_local_agent_capability_snapshots_connection_id", ["connection_id"]),
        ("ix_local_agent_capability_snapshots_subject_agent_id", ["subject_agent_id"]),
        ("ix_local_agent_capability_snapshots_expires_at", ["expires_at"]),
        ("ix_local_agent_capability_snapshot_active", ["channel_id", "expires_at", "revoked_at"]),
    ):
        op.create_index(name, "local_agent_capability_snapshots", columns)

    op.add_column("local_agent_channel_events", sa.Column("sequence", sa.BigInteger(), nullable=True))
    op.execute(
        """
        WITH numbered AS (
            SELECT id,
                   row_number() OVER (PARTITION BY session_id ORDER BY created_at, id) AS sequence
            FROM local_agent_channel_events
        )
        UPDATE local_agent_channel_events AS event
        SET sequence = numbered.sequence
        FROM numbered
        WHERE event.id = numbered.id
        """
    )
    op.alter_column("local_agent_channel_events", "sequence", nullable=False)
    op.create_unique_constraint(
        "uq_local_agent_channel_events_session_sequence",
        "local_agent_channel_events",
        ["session_id", "sequence"],
    )
    op.create_index(
        "ix_local_agent_channel_events_session_sequence",
        "local_agent_channel_events",
        ["session_id", "sequence"],
    )

    for column in (
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=True),
        sa.Column("capability_snapshot_hash", sa.String(length=64), nullable=True),
        sa.Column("replay_key", sa.String(length=200), nullable=True),
        sa.Column("receipt_trace_id", sa.String(length=255), nullable=True),
        sa.Column("receipt_span_id", sa.String(length=80), nullable=True),
    ):
        op.add_column("local_agent_channel_messages", column)
    op.execute(
        """
        UPDATE local_agent_channel_messages
        SET idempotency_key = 'legacy:' || id::text,
            replay_key = 'legacy:' || id::text,
            request_hash = md5(id::text || ':' || content) || md5('hive:' || id::text || ':' || content),
            receipt_trace_id = 'local-agent:' || session_id::text,
            receipt_span_id = 'remote-action:' || id::text
        WHERE idempotency_key IS NULL
        """
    )
    op.alter_column("local_agent_channel_messages", "idempotency_key", nullable=False)
    op.alter_column("local_agent_channel_messages", "replay_key", nullable=False)
    op.create_unique_constraint(
        "uq_local_agent_channel_messages_tenant_idempotency",
        "local_agent_channel_messages",
        ["tenant_id", "idempotency_key"],
    )
    op.create_index("ix_local_agent_channel_messages_request_hash", "local_agent_channel_messages", ["request_hash"])
    op.create_index(
        "ix_local_agent_channel_messages_capability_snapshot_hash",
        "local_agent_channel_messages",
        ["capability_snapshot_hash"],
    )
    op.create_index("ix_local_agent_channel_messages_replay_key", "local_agent_channel_messages", ["replay_key"])

    # Unsigned legacy self-reports are retained only as diagnostic input. A
    # reconnect must mint a new signed snapshot before work is delivered.
    op.execute(
        """
        UPDATE local_agent_channels
        SET status = CASE WHEN status = 'online' THEN 'stale' ELSE status END,
            capabilities_json = jsonb_build_object(
                'legacy_reported', COALESCE(capabilities_json, '{}'::jsonb),
                'snapshot_required', true
            )
        """
    )

    if op.get_bind().dialect.name == "postgresql":
        for table in _PERSONAL_KB_LOCAL_RLS_TABLES:
            _enable_strict_tenant_rls(table)


def downgrade() -> None:
    op.drop_constraint(
        "uq_local_agent_channel_messages_tenant_idempotency",
        "local_agent_channel_messages",
        type_="unique",
    )
    op.drop_index("ix_local_agent_channel_messages_replay_key", table_name="local_agent_channel_messages")
    op.drop_index(
        "ix_local_agent_channel_messages_capability_snapshot_hash",
        table_name="local_agent_channel_messages",
    )
    op.drop_index("ix_local_agent_channel_messages_request_hash", table_name="local_agent_channel_messages")
    for column in (
        "receipt_span_id",
        "receipt_trace_id",
        "replay_key",
        "capability_snapshot_hash",
        "request_hash",
        "idempotency_key",
    ):
        op.drop_column("local_agent_channel_messages", column)

    op.drop_index("ix_local_agent_channel_events_session_sequence", table_name="local_agent_channel_events")
    op.drop_constraint(
        "uq_local_agent_channel_events_session_sequence",
        "local_agent_channel_events",
        type_="unique",
    )
    op.drop_column("local_agent_channel_events", "sequence")
    op.drop_table("local_agent_capability_snapshots")
    op.drop_table("personal_knowledge_proposals")
