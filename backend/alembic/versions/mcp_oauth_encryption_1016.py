"""Encrypt legacy MCP OAuth credentials without changing connection or policy state."""

from __future__ import annotations

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.services.mcp_oauth import encrypt_legacy_oauth_config
from app.services.secrets_provider import FernetSecretsProvider

revision = "mcp_oauth_encryption_1016"
down_revision = "m1g_direct_creation_1016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    servers = sa.table("mcp_servers", sa.column("id", UUID), sa.column("config_json", JSONB))
    rows = bind.execute(sa.select(servers.c.id, servers.c.config_json).with_for_update()).all()
    provider = None
    for server_id, config in rows:
        if not (config or {}).get("oauth"):
            continue
        if provider is None:
            key = os.environ.get("SECRETS_MASTER_KEY", "").strip()
            if not key:
                raise RuntimeError("SECRETS_MASTER_KEY is required to encrypt legacy MCP OAuth credentials")
            previous = tuple(
                key.strip() for key in os.environ.get("SECRETS_MASTER_KEY_PREVIOUS", "").split(",") if key.strip()
            )
            provider = FernetSecretsProvider(key, previous_master_keys=previous)
        encrypted = encrypt_legacy_oauth_config(config, provider)
        if encrypted != config:
            bind.execute(servers.update().where(servers.c.id == server_id).values(config_json=encrypted))


def downgrade() -> None:
    # Secure rollback keeps credentials encrypted and requires the compatible reader.
    pass
