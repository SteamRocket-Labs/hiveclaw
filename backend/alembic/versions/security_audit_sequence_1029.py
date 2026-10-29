"""Restore sequence allocation without rewriting historical audit evidence."""

from alembic import op

from app.db_bootstrap import apply_security_audit_sequence

revision = "security_audit_sequence_1029"
down_revision = "terminal_route_bootstrap_1029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_security_audit_sequence(op.get_bind())


def downgrade() -> None:
    # Retain ordering and existing evidence when returning to the parent schema.
    pass
