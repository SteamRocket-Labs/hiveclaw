"""Allow truthful employee-creation results in the existing completion mailbox."""

from alembic import op

revision = "m1g_direct_creation_1016"
down_revision = "m1f_assistant_bindings_1016"
branch_labels = None
depends_on = None

_KINDS = "'subagent','agent_team','workflow','trigger','delegation','a2a_delegation','a2a_continuation','runtime_budget','approval'"


def _constraint(kinds):
    op.drop_constraint("ck_runtime_notification_outbox_source_kind", "runtime_notification_outbox", type_="check")
    op.create_check_constraint("ck_runtime_notification_outbox_source_kind", "runtime_notification_outbox",
                               f"source_kind IN ({kinds})")


def upgrade():
    _constraint(_KINDS + ",'employee_creation'")
    op.execute("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'delegation_queued'")


def downgrade():
    # PostgreSQL refuses this downgrade while new-kind receipts exist; preserve their evidence.
    _constraint(_KINDS)
    # PostgreSQL retains the additive activity enum value, as with earlier delegation migrations.
