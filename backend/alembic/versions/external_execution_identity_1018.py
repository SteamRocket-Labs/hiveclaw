"""Retain the full existing external-principal execution identity in audit evidence."""

from alembic import op
import sqlalchemy as sa

revision = "external_execution_identity_1018"
down_revision = "delegation_session_rounds_1017"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("security_audit_events", "invocation_spans"):
        op.alter_column(table, "execution_identity_type", existing_type=sa.String(20), type_=sa.String(40))


def downgrade():
    # Older readers can retain the wider column; shrinking would lose audit evidence.
    pass
