"""Make canonical audit evidence append-only at the database boundary.

Revision ID: audit_evidence_immutability_0715
Revises: personal_kb_authority_0715
"""

from alembic import op


revision = "audit_evidence_immutability_0715"
down_revision = "personal_kb_authority_0715"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("fk_audit_logs_external_principal_id", "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        "fk_audit_logs_external_principal_id",
        "audit_logs",
        "external_principals",
        ["external_principal_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_audit_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit evidence is append-only: %', TG_TABLE_NAME
                USING ERRCODE = '55000';
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs")
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_immutable
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION reject_audit_evidence_mutation()
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_security_audit_events_immutable ON security_audit_events")
    op.execute(
        """
        CREATE TRIGGER trg_security_audit_events_immutable
        BEFORE UPDATE OR DELETE ON security_audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_evidence_mutation()
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_truncate ON audit_logs")
    op.execute(
        """
        CREATE TRIGGER trg_audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_evidence_mutation()
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_security_audit_events_no_truncate ON security_audit_events")
    op.execute(
        """
        CREATE TRIGGER trg_security_audit_events_no_truncate
        BEFORE TRUNCATE ON security_audit_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_evidence_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS trg_security_audit_events_immutable ON security_audit_events")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_truncate ON audit_logs")
    op.execute("DROP TRIGGER IF EXISTS trg_security_audit_events_no_truncate ON security_audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_evidence_mutation()")
    op.drop_constraint("fk_audit_logs_external_principal_id", "audit_logs", type_="foreignkey")
    op.create_foreign_key(
        "fk_audit_logs_external_principal_id",
        "audit_logs",
        "external_principals",
        ["external_principal_id"],
        ["id"],
        ondelete="SET NULL",
    )
