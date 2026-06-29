"""Retire Atlassian Rovo integration.

Revision ID: retire_atlassian_rovo_0629
Revises: invocation_span_truth_evidence_0628
Create Date: 2026-06-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "retire_atlassian_rovo_0629"
down_revision = "invocation_span_truth_evidence_0628"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(
        """
        DELETE FROM agent_tools
        WHERE tool_id IN (
            SELECT id FROM tools
            WHERE name = 'atlassian_rovo'
               OR name LIKE 'atlassian_rovo_%'
               OR mcp_server_name = 'Atlassian Rovo'
               OR category = 'atlassian'
        )
        """
    )
    op.execute(
        """
        DELETE FROM tools
        WHERE name = 'atlassian_rovo'
           OR name LIKE 'atlassian_rovo_%'
           OR mcp_server_name = 'Atlassian Rovo'
           OR category = 'atlassian'
        """
    )
    op.execute("DELETE FROM channel_configs WHERE channel_type::text = 'atlassian'")
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE channel_type_enum RENAME TO channel_type_enum_old")
        op.execute(
            """
            CREATE TYPE channel_type_enum AS ENUM (
                'feishu',
                'wecom',
                'dingtalk',
                'slack',
                'discord',
                'microsoft_teams',
                'telegram',
                'wechat_personal'
            )
            """
        )
        op.execute(
            """
            ALTER TABLE channel_configs
            ALTER COLUMN channel_type TYPE channel_type_enum
            USING channel_type::text::channel_type_enum
            """
        )
        op.execute("DROP TYPE channel_type_enum_old")
    op.execute(
        """
        DELETE FROM skill_files
        WHERE skill_id IN (
            SELECT id FROM skills
            WHERE folder_name = 'atlassian-rovo'
               OR name = 'Atlassian Rovo'
        )
        """
    )
    op.execute(
        """
        DELETE FROM skills
        WHERE folder_name = 'atlassian-rovo'
           OR name = 'Atlassian Rovo'
        """
    )


def downgrade() -> None:
    pass
