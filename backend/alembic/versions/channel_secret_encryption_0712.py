"""Encrypt all bot and enterprise channel credentials at rest.

Revision ID: channel_secret_encryption_0712
Revises: approval_continuation_outbox_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa

from app.services.channel_secret_storage import (
    CHANNEL_SECRET_PREFIX,
    inspect_channel_secret_rows,
    migrate_channel_secret_rows,
)
from app.services.secrets_provider import FernetSecretsProvider


revision = "channel_secret_encryption_0712"
down_revision = "approval_continuation_outbox_0712"
branch_labels = None
depends_on = None


_TABLES = ("channel_configs", "tenant_channel_configs")
_COLUMNS = ("app_secret", "encrypt_key", "verification_token")


def _provider_from_environment() -> FernetSecretsProvider:
    master_key = os.environ.get("SECRETS_MASTER_KEY", "").strip()
    if not master_key:
        raise RuntimeError("SECRETS_MASTER_KEY is required to encrypt legacy channel credentials")
    previous = tuple(key.strip() for key in os.environ.get("SECRETS_MASTER_KEY_PREVIOUS", "").split(",") if key.strip())
    return FernetSecretsProvider(master_key, previous_master_keys=previous)


def upgrade() -> None:
    for table_name in _TABLES:
        for column_name in _COLUMNS:
            op.alter_column(
                table_name,
                column_name,
                existing_type=sa.String(length=255),
                type_=sa.String(length=1024),
                existing_nullable=True,
            )

    bind = op.get_bind()
    inventory = inspect_channel_secret_rows(bind)
    if inventory["totals"]["plaintext"]:
        report = migrate_channel_secret_rows(bind, provider=_provider_from_environment(), apply=True)
        if report["totals"]["plaintext"]:
            raise RuntimeError(
                f"legacy channel credential plaintext remains; expected {CHANNEL_SECRET_PREFIX} envelopes"
            )


def downgrade() -> None:
    # Secure rollback contract: revision state may move back, but ciphertext is
    # never decrypted and 1024-byte columns are never shrunk to a truncating
    # legacy width. The previous application can be restored only together
    # with the forward-compatible encrypted model adapter.
    pass
