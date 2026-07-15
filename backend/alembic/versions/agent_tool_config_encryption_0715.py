"""Encrypt every persisted per-agent tool configuration document.

Revision ID: agent_tool_config_encryption_0715
Revises: audit_evidence_immutability_0715
Create Date: 2026-07-15
"""

from __future__ import annotations

import os

from alembic import op

from app.services.agent_tool_config_storage import (
    inspect_agent_tool_config_rows,
    migrate_agent_tool_config_rows,
)
from app.services.secrets_provider import FernetSecretsProvider


revision = "agent_tool_config_encryption_0715"
down_revision = "audit_evidence_immutability_0715"
branch_labels = None
depends_on = None


def _provider_from_environment() -> FernetSecretsProvider:
    master_key = os.environ.get("SECRETS_MASTER_KEY", "").strip()
    if not master_key:
        raise RuntimeError("SECRETS_MASTER_KEY is required to encrypt legacy AgentTool configs")
    previous = tuple(key.strip() for key in os.environ.get("SECRETS_MASTER_KEY_PREVIOUS", "").split(",") if key.strip())
    return FernetSecretsProvider(master_key, previous_master_keys=previous)


def upgrade() -> None:
    bind = op.get_bind()
    inventory = inspect_agent_tool_config_rows(bind)
    totals = inventory["totals"]
    if totals["malformed"]:
        raise RuntimeError("malformed AgentTool config envelope blocks migration")
    if not totals["non_empty"]:
        return

    report = migrate_agent_tool_config_rows(
        bind,
        provider=_provider_from_environment(),
        apply=True,
    )
    verified = report["totals"]
    if verified["plaintext"] or verified["non_current"] or verified["malformed"]:
        raise RuntimeError("legacy AgentTool config plaintext remains after migration")


def downgrade() -> None:
    # Secure rollback: the application revision may move back, but encrypted
    # configuration is never converted back into plaintext.
    pass
