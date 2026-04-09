"""Add security_audit_events and resource_permissions tables.

Revision ID: add_security_audit_and_rbac
Revises: add_agent_classification
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY, INET

revision = "add_security_audit_and_rbac"
down_revision = "add_agent_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Append-only security audit log with hash chain
    audit_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'security_audit_events')")
    ).scalar()
    if not audit_exists:
        op.create_table(
            "security_audit_events",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("sequence_num", sa.BigInteger, autoincrement=True, nullable=False, unique=True),
            sa.Column("event_type", sa.String(50), nullable=False, index=True),
            sa.Column("severity", sa.String(10), nullable=False, server_default="info"),
            sa.Column("actor_type", sa.String(20), nullable=False),
            sa.Column("actor_id", UUID(as_uuid=True), nullable=True),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
            sa.Column("resource_type", sa.String(30), nullable=True),
            sa.Column("resource_id", UUID(as_uuid=True), nullable=True),
            sa.Column("action", sa.String(100), nullable=False),
            sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("ip_address", INET, nullable=True),
            sa.Column("user_agent", sa.Text, nullable=True),
            sa.Column("request_id", UUID(as_uuid=True), nullable=True, index=True),
            sa.Column("prev_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("event_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_audit_events_actor", "security_audit_events", ["actor_type", "actor_id"])
        op.create_index("ix_audit_events_created", "security_audit_events", ["tenant_id", "created_at"])

    # Resource-level permissions (RBAC + ABAC)
    rbac_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'resource_permissions')")
    ).scalar()
    if not rbac_exists:
        op.create_table(
            "resource_permissions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("principal_type", sa.String(20), nullable=False),
            sa.Column("principal_id", UUID(as_uuid=True), nullable=False),
            sa.Column("resource_type", sa.String(30), nullable=False),
            sa.Column("resource_id", UUID(as_uuid=True), nullable=False),
            sa.Column("actions", ARRAY(sa.Text), nullable=False),
            sa.Column("conditions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_rp_principal", "resource_permissions", ["principal_type", "principal_id"])
        op.create_index("ix_rp_resource", "resource_permissions", ["resource_type", "resource_id"])


def downgrade() -> None:
    conn = op.get_bind()

    rbac_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'resource_permissions')")
    ).scalar()
    if rbac_exists:
        op.drop_table("resource_permissions")

    audit_exists = conn.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'security_audit_events')")
    ).scalar()
    if audit_exists:
        op.drop_table("security_audit_events")
