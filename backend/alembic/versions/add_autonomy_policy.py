"""Add autonomy_policy column to agents and default_autonomy_policy to agent_templates.

Revision ID: add_autonomy_policy
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "add_autonomy_policy"
down_revision = None
branch_labels = None
depends_on = None

_DEFAULT_POLICY = {
    "read_files": "L1",
    "write_workspace_files": "L2",
    "send_feishu_message": "L2",
    "send_external_message": "L3",
    "modify_soul": "L3",
    "access_business_system_read": "L2",
    "access_business_system_write": "L3",
    "delete_files": "L3",
    "create_calendar_event": "L2",
    "financial_operations": "L3",
}


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # agents.autonomy_policy
    cols = [c["name"] for c in inspector.get_columns("agents")]
    if "autonomy_policy" not in cols:
        default_json = json.dumps(_DEFAULT_POLICY)
        op.add_column(
            "agents",
            sa.Column(
                "autonomy_policy",
                sa.JSON,
                nullable=False,
                server_default=sa.text(f"'{default_json}'::json"),
            ),
        )

    # agent_templates.default_autonomy_policy
    if conn.dialect.has_table(conn, "agent_templates"):
        tmpl_cols = [c["name"] for c in inspector.get_columns("agent_templates")]
        if "default_autonomy_policy" not in tmpl_cols:
            op.add_column(
                "agent_templates",
                sa.Column(
                    "default_autonomy_policy",
                    sa.JSON,
                    nullable=False,
                    server_default=sa.text("'{}'::json"),
                ),
            )


def downgrade():
    op.drop_column("agents", "autonomy_policy")
    conn = op.get_bind()
    if conn.dialect.has_table(conn, "agent_templates"):
        op.drop_column("agent_templates", "default_autonomy_policy")
