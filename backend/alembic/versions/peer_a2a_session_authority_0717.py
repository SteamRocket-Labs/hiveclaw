"""Bind Peer A2A evidence to the exact RuntimeTask child Session.

Revision ID: peer_a2a_session_authority_0717
Revises: runtime_result_fanin_0717
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

from migration_snapshots.peer_a2a_session_authority_contract_0717 import (
    build_session_event_contract_function_sql,
)


revision = "peer_a2a_session_authority_0717"
down_revision = "runtime_result_fanin_0717"
branch_labels = None
depends_on = None


def _install_peer_a2a_session_authority() -> None:
    op.execute(build_session_event_contract_function_sql())
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_event_v2_contract() FROM PUBLIC")


def upgrade() -> None:
    _install_peer_a2a_session_authority()


def downgrade() -> None:
    # A compatibility downgrade must not make already-bound child evidence
    # unwritable or reopen cross-task authority. Keep the exact parent/child
    # RuntimeTask binding while older application artifacts drain.
    _install_peer_a2a_session_authority()
