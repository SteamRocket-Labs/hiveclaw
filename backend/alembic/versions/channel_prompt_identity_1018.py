"""Keep repeated questions in one runtime task independently deliverable."""

from alembic import op

revision = "channel_prompt_identity_1018"
down_revision = "external_execution_identity_1018"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE channel_delivery_outbox "
        "ADD COLUMN IF NOT EXISTS prompt_key varchar(36) NOT NULL DEFAULT ''")
    op.drop_constraint("uq_channel_delivery_outbox_runtime_target", "channel_delivery_outbox", type_="unique")
    op.create_unique_constraint("uq_channel_delivery_outbox_runtime_target", "channel_delivery_outbox",
        ["tenant_id", "runtime_task_id", "delivery_kind", "target_hash", "prompt_key"])


def downgrade():
    # Older writers use the empty key; collapsing distinct prompt receipts would lose evidence.
    pass
