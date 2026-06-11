"""Stage-2c: drop 4 orphan dead tables the production RLS audit flagged UNPROTECTED.

The deployed `audit_rls_coverage` (run inside the production container) reported
four tables carrying tenant_id/agent_id but with NO RLS policy — so under the
stage-3 non-owner role flip they would be fully readable cross-tenant. Unlike the
stage-2a/2b tables these have **no ORM model** (not in Base.metadata — which is
why the metadata-reflection-based stage-2a sweep never saw them) and **zero code
references** (no model, no raw SQL access). Production row counts: agent_credentials
0, daily_token_usage 0, published_pages 0, llm_provider_configs 1 — all dead
remnants of removed features whose tables an old migration left behind.

Owner decision (2026-06-11): DROP them — the clean, no-debt resolution that also
removes the leak surface, rather than dressing dead tables with policies. A fresh
DB never creates them (no model), so this only affects long-lived DBs; idempotent
DROP IF EXISTS. Irreversible (the 1 llm_provider_configs row is zero-referenced),
executed under an owner confirmation gate.

Revision ID: rls_stage2c_drop_orphan_tables_0611
Revises: rls_stage2b_agent_scoped_0610
Create Date: 2026-06-11
"""

from typing import Sequence, Union

from alembic import op

revision: str = "rls_stage2c_drop_orphan_tables_0611"
down_revision: Union[str, None] = "rls_stage2b_agent_scoped_0610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORPHAN_TABLES = (
    "agent_credentials",
    "daily_token_usage",
    "llm_provider_configs",
    "published_pages",
)


def upgrade() -> None:
    for table in _ORPHAN_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def downgrade() -> None:
    # No-op: these are orphan tables with no ORM model — their schema is not
    # known to the codebase, so there is nothing to recreate on downgrade.
    pass
