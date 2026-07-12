"""Quarantine unreviewed MCP metadata outside model-visible tool schemas.

Revision ID: mcp_metadata_trust_0712
Revises: local_agent_action_gov_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "mcp_metadata_trust_0712"
down_revision = "local_agent_action_gov_0712"
branch_labels = None
depends_on = None


_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("mcp_raw_description", sa.Text(), nullable=True),
    sa.Column("mcp_raw_schema", sa.JSON(), nullable=True),
    sa.Column("mcp_metadata_fingerprint", sa.String(length=64), nullable=True),
    sa.Column("mcp_metadata_risk_flags", sa.JSON(), nullable=True),
    sa.Column("mcp_trust_status", sa.String(length=32), nullable=True),
    sa.Column("mcp_trust_tier", sa.String(length=40), nullable=True),
    sa.Column("mcp_reviewed_fingerprint", sa.String(length=64), nullable=True),
    sa.Column(
        "mcp_reviewed_by",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", name="fk_tools_mcp_reviewed_by_users"),
        nullable=True,
    ),
    sa.Column("mcp_reviewed_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = {column["name"] for column in sa.inspect(bind).get_columns("tools")}
    for column in _COLUMNS:
        if column.name not in existing_columns:
            op.add_column("tools", column)
    existing_indexes = {index["name"] for index in sa.inspect(bind).get_indexes("tools")}
    if "ix_tools_mcp_metadata_fingerprint" not in existing_indexes:
        op.create_index("ix_tools_mcp_metadata_fingerprint", "tools", ["mcp_metadata_fingerprint"])
    if "ix_tools_mcp_trust_status" not in existing_indexes:
        op.create_index("ix_tools_mcp_trust_status", "tools", ["mcp_trust_status"])
    agent_tool_columns = {column["name"] for column in sa.inspect(bind).get_columns("agent_tools")}
    if "mcp_trust_requested_enabled" not in agent_tool_columns:
        op.add_column(
            "agent_tools",
            sa.Column("mcp_trust_requested_enabled", sa.Boolean(), nullable=True),
        )

    # Existing descriptions/schema annotations may already be hostile. Preserve
    # them only as administrator evidence, replace the model surface with a
    # neutral platform statement, and require an explicit fresh review.
    bind.execute(
        sa.text(
            """
            UPDATE tools
            SET mcp_raw_description = COALESCE(description, ''),
                mcp_raw_schema = COALESCE(parameters_schema, CAST('{"type":"object","properties":{}}' AS json)),
                mcp_metadata_fingerprint = encode(sha256(convert_to(
                    COALESCE(mcp_server_name, '') || '|' || COALESCE(mcp_server_url, '') || '|' ||
                    COALESCE(mcp_tool_name, name) || '|' || COALESCE(description, '') || '|' ||
                    COALESCE(parameters_schema::jsonb::text, ''),
                    'UTF8'
                )), 'hex'),
                mcp_metadata_risk_flags = CAST('["legacy_unreviewed"]' AS json),
                mcp_trust_status = 'pending_review',
                mcp_trust_tier = 'legacy_quarantined',
                mcp_reviewed_fingerprint = NULL,
                mcp_reviewed_by = NULL,
                mcp_reviewed_at = NULL,
                description = 'External MCP operation. Remote metadata is untrusted and omitted pending administrator review.',
                parameters_schema = CAST('{"type":"object","properties":{}}' AS json),
                enabled = false
            WHERE type = 'mcp' AND mcp_trust_status IS NULL
            """
        )
    )
    bind.execute(
        sa.text(
            """
            UPDATE agent_tools
            SET mcp_trust_requested_enabled = COALESCE(mcp_trust_requested_enabled, enabled),
                enabled = false
            WHERE tool_id IN (
                SELECT id FROM tools WHERE type = 'mcp' AND mcp_trust_status = 'pending_review'
            )
            """
        )
    )


def downgrade() -> None:
    # Secure downgrade: raw external prose is never copied back into the
    # model-visible description/schema and quarantined tools are never
    # re-enabled. Columns and review evidence intentionally remain in place;
    # upgrade() is idempotent so a forward migration remains possible.
    pass
