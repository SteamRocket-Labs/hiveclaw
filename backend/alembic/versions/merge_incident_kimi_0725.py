"""Merge the production incident and 2026-07-24 remediation heads.

The PostgreSQL resource-exhaustion repairs were applied to production before
the independent Kimi remediation chain was recovered onto the current Git
history.  Both branches therefore describe real schema history and must meet
at a no-op merge revision instead of rewriting either branch's ancestry.

Revision ID: merge_incident_kimi_0725
Revises: completion_outbox_index_0721, retire_agent_agent_relationships_table_0724
Create Date: 2026-07-25
"""

revision = "merge_incident_kimi_0725"
down_revision = (
    "completion_outbox_index_0721",
    "retire_agent_agent_relationships_table_0724",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
