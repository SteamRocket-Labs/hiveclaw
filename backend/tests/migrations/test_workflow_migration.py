"""§9 P1 red tests: workflow tables exist, tenant-scoped, RLS ENABLEd+FORCEd.

Both deployment paths are exercised against real PostgreSQL:

* upgrade path (``chain_migrated_pg_url``) — ``add_workflow_tables_0604``
  actually executes its DDL;
* bootstrap path (``migrated_pg_url``) — ``db_bootstrap.apply_rls_policies``
  covers the create_all+stamp shortcut every fresh deployment takes.

FORCE matters (P0 gap B): production connects as the table owner, so
ENABLE-only policies are inert there. These tables are the first family
where the owner connection is *also* policy-bound — proven below by the
owner seeing nothing on an empty GUC.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import BACKEND_ROOT

_WORKFLOW_TABLES = (
    "workflow_definitions",
    "workflow_steps",
    "workflow_leaf_calls",
    "workflow_quotas",
    "workflow_quota_reservations",
    "workflow_completion_outbox",
)
_COORDINATION_TABLES = ("coordination_leases", "coordination_signals", "coordination_checkpoints")
_FEEDBACK_TABLES = ("session_feedback_events",)
_INVOCATION_TRACE_TABLES = ("invocation_spans",)
_INVOCATION_IDENTITY_COLUMNS = (
    "execution_identity_type",
    "execution_identity_id",
    "execution_identity_label",
)
_TENANT_TOKEN_QUOTA_COLUMNS = (
    "quota_tokens_per_day",
    "quota_tokens_per_month",
    "tokens_used_today",
    "tokens_used_month",
    "tokens_used_total",
    "tokens_reset_at",
)
_AGENT_TOKEN_QUOTA_COLUMNS = (
    "quota_tokens_per_day",
    "quota_tokens_per_month",
)
_CURRENT_CLOSURE_HEAD = "runtime_notification_source_kind_0713"
_WORKFLOW_QUOTA_RESERVATION_STATE_COLUMNS = {
    "state",
    "step_id",
    "leaf_id",
    "input_hash",
    "execution_started_at",
    "reconciliation_required_at",
    "reconciliation_reason",
    "reconciliation_operation_id",
    "settlement_reason",
    "repair_deferred_at",
}


async def _seed_tenant(owner_engine, tenant_id: uuid.UUID, label: str) -> None:
    """Seed via the ORM so Python-side column defaults apply (tenants has
    NOT NULL columns without server defaults)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.models.tenant import Tenant

    factory = async_sessionmaker(owner_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(Tenant(id=tenant_id, name=label, slug=f"{label.lower()}-{tenant_id.hex[:8]}"))
        await session.commit()


def test_alembic_single_head_is_current_closure_head():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=BACKEND_ROOT,
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-500:]
    heads = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(heads) == 1, f"expected single head, got: {heads}"
    assert _CURRENT_CLOSURE_HEAD in heads[0]


async def _assert_workflow_tables_forced_rls(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"
                    ),
                    {"names": list(_WORKFLOW_TABLES)},
                )
            ).all()
            policies = (
                await conn.execute(
                    text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                    {"names": list(_WORKFLOW_TABLES)},
                )
            ).all()
    finally:
        await engine.dispose()

    found = {row.relname: row for row in rows}
    assert set(found) == set(_WORKFLOW_TABLES), f"missing workflow tables: {set(_WORKFLOW_TABLES) - set(found)}"
    for table in _WORKFLOW_TABLES:
        assert found[table].relrowsecurity is True, f"{table}: RLS not enabled"
        assert found[table].relforcerowsecurity is True, f"{table}: RLS not FORCEd (P0 gap B regression)"
    assert {t for t, _ in policies} == set(_WORKFLOW_TABLES)
    assert all(name == f"tenant_isolation_{table}" for table, name in policies)


async def _assert_coordination_tables_forced_rls(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"
                    ),
                    {"names": list(_COORDINATION_TABLES)},
                )
            ).all()
            policies = (
                await conn.execute(
                    text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                    {"names": list(_COORDINATION_TABLES)},
                )
            ).all()
    finally:
        await engine.dispose()

    found = {row.relname: row for row in rows}
    assert set(found) == set(_COORDINATION_TABLES), (
        f"missing coordination tables: {set(_COORDINATION_TABLES) - set(found)}"
    )
    for table in _COORDINATION_TABLES:
        assert found[table].relrowsecurity is True, f"{table}: RLS not enabled"
        assert found[table].relforcerowsecurity is True, f"{table}: RLS not FORCEd"
    assert {t for t, _ in policies} == set(_COORDINATION_TABLES)
    assert all(name == f"tenant_isolation_{table}" for table, name in policies)


async def _assert_feedback_tables_forced_rls(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"
                    ),
                    {"names": list(_FEEDBACK_TABLES)},
                )
            ).all()
            policies = (
                await conn.execute(
                    text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                    {"names": list(_FEEDBACK_TABLES)},
                )
            ).all()
    finally:
        await engine.dispose()

    found = {row.relname: row for row in rows}
    assert set(found) == set(_FEEDBACK_TABLES), f"missing feedback tables: {set(_FEEDBACK_TABLES) - set(found)}"
    for table in _FEEDBACK_TABLES:
        assert found[table].relrowsecurity is True, f"{table}: RLS not enabled"
        assert found[table].relforcerowsecurity is True, f"{table}: RLS not FORCEd"
    assert {t for t, _ in policies} == set(_FEEDBACK_TABLES)
    assert all(name == f"tenant_isolation_{table}" for table, name in policies)


async def _assert_invocation_trace_tables_forced_rls(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:names)"
                    ),
                    {"names": list(_INVOCATION_TRACE_TABLES)},
                )
            ).all()
            policies = (
                await conn.execute(
                    text("SELECT tablename, policyname FROM pg_policies WHERE tablename = ANY(:names)"),
                    {"names": list(_INVOCATION_TRACE_TABLES)},
                )
            ).all()
    finally:
        await engine.dispose()

    found = {row.relname: row for row in rows}
    assert set(found) == set(_INVOCATION_TRACE_TABLES), (
        f"missing invocation trace tables: {set(_INVOCATION_TRACE_TABLES) - set(found)}"
    )
    for table in _INVOCATION_TRACE_TABLES:
        assert found[table].relrowsecurity is True, f"{table}: RLS not enabled"
        assert found[table].relforcerowsecurity is True, f"{table}: RLS not FORCEd"
    assert {t for t, _ in policies} == set(_INVOCATION_TRACE_TABLES)
    assert all(name == f"tenant_isolation_{table}" for table, name in policies)


async def _assert_invocation_span_identity_columns(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = 'invocation_spans'
                          AND column_name = ANY(:names)
                        """
                    ),
                    {"names": list(_INVOCATION_IDENTITY_COLUMNS)},
                )
            ).all()
    finally:
        await engine.dispose()

    found = {row.column_name for row in rows}
    assert found == set(_INVOCATION_IDENTITY_COLUMNS)


async def _assert_token_quota_hard_cap_columns(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            tenant_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = 'tenants'
                          AND column_name = ANY(:names)
                        """
                    ),
                    {"names": list(_TENANT_TOKEN_QUOTA_COLUMNS)},
                )
            ).all()
            agent_rows = (
                await conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_name = 'agents'
                          AND column_name = ANY(:names)
                        """
                    ),
                    {"names": list(_AGENT_TOKEN_QUOTA_COLUMNS)},
                )
            ).all()
    finally:
        await engine.dispose()

    assert {row.column_name for row in tenant_rows} == set(_TENANT_TOKEN_QUOTA_COLUMNS)
    assert {row.column_name for row in agent_rows} == set(_AGENT_TOKEN_QUOTA_COLUMNS)


async def _assert_workflow_quota_reservation_state_machine(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            columns = (
                await conn.execute(
                    text(
                        """
                        SELECT column_name, column_default, is_nullable
                        FROM information_schema.columns
                        WHERE table_name = 'workflow_quota_reservations'
                          AND column_name = ANY(:names)
                        """
                    ),
                    {"names": sorted(_WORKFLOW_QUOTA_RESERVATION_STATE_COLUMNS)},
                )
            ).all()
            constraints = (
                await conn.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition
                        FROM pg_constraint
                        WHERE conrelid = 'workflow_quota_reservations'::regclass
                        """
                    )
                )
            ).all()
            indexes = (
                await conn.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE tablename = 'workflow_quota_reservations'
                        """
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    by_name = {row.column_name: row for row in columns}
    assert set(by_name) == _WORKFLOW_QUOTA_RESERVATION_STATE_COLUMNS
    assert by_name["state"].is_nullable == "NO"
    assert "reserved" in str(by_name["state"].column_default)
    assert any(
        row.conname == "ck_workflow_quota_reservation_state"
        and all(state in row.definition for state in ("reserved", "executing", "needs_reconciliation", "settled"))
        for row in constraints
    )
    index_names = {row.indexname for row in indexes}
    assert "ix_workflow_quota_reservations_run_state" in index_names
    assert "ix_workflow_quota_reservations_state_created" in index_names
    assert "ix_workflow_quota_reservations_repair_scan" in index_names


async def test_upgrade_path_creates_workflow_tables_with_forced_rls(chain_migrated_pg_url):
    """The migration's own DDL (executed, not stamped) must produce the contract."""
    await _assert_workflow_tables_forced_rls(chain_migrated_pg_url)


async def test_bootstrap_path_creates_workflow_tables_with_forced_rls(migrated_pg_url):
    """The fresh-deployment path (create_all + apply_rls_policies) must
    produce the same contract."""
    await _assert_workflow_tables_forced_rls(migrated_pg_url)


async def test_upgrade_path_creates_coordination_tables_with_forced_rls(chain_migrated_pg_url):
    await _assert_coordination_tables_forced_rls(chain_migrated_pg_url)


async def test_bootstrap_path_creates_coordination_tables_with_forced_rls(migrated_pg_url):
    await _assert_coordination_tables_forced_rls(migrated_pg_url)


async def test_upgrade_path_creates_session_feedback_events_with_forced_rls(chain_migrated_pg_url):
    await _assert_feedback_tables_forced_rls(chain_migrated_pg_url)


async def test_bootstrap_path_creates_session_feedback_events_with_forced_rls(migrated_pg_url):
    await _assert_feedback_tables_forced_rls(migrated_pg_url)


async def test_upgrade_path_creates_invocation_spans_with_forced_rls(chain_migrated_pg_url):
    await _assert_invocation_trace_tables_forced_rls(chain_migrated_pg_url)


async def test_bootstrap_path_creates_invocation_spans_with_forced_rls(migrated_pg_url):
    await _assert_invocation_trace_tables_forced_rls(migrated_pg_url)


async def test_upgrade_path_adds_invocation_span_execution_identity_columns(chain_migrated_pg_url):
    await _assert_invocation_span_identity_columns(chain_migrated_pg_url)


async def test_bootstrap_path_adds_invocation_span_execution_identity_columns(migrated_pg_url):
    await _assert_invocation_span_identity_columns(migrated_pg_url)


async def test_upgrade_path_adds_token_quota_hard_cap_columns(chain_migrated_pg_url):
    await _assert_token_quota_hard_cap_columns(chain_migrated_pg_url)


async def test_bootstrap_path_adds_token_quota_hard_cap_columns(migrated_pg_url):
    await _assert_token_quota_hard_cap_columns(migrated_pg_url)


async def test_upgrade_path_adds_workflow_quota_reservation_state_machine(chain_migrated_pg_url):
    await _assert_workflow_quota_reservation_state_machine(chain_migrated_pg_url)


async def test_bootstrap_path_adds_workflow_quota_reservation_state_machine(migrated_pg_url):
    await _assert_workflow_quota_reservation_state_machine(migrated_pg_url)


async def test_upgrade_path_adds_chat_message_thinking_signature(chain_migrated_pg_url):
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'chat_messages'
                              AND column_name = 'thinking_signature'
                        )
                        """
                    )
                )
            ).scalar_one()
        assert exists is True
    finally:
        await engine.dispose()


async def _assert_no_workflow_step_phase(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            exists = (
                await conn.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'workflow_steps'
                              AND column_name = 'phase'
                        )
                        """
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert exists is False, "workflow_steps.phase must be dropped (Step 10 dead column)"


async def test_upgrade_path_drops_workflow_step_phase(chain_migrated_pg_url):
    """Step 10: the migration's DROP COLUMN removes the dead phase column."""
    await _assert_no_workflow_step_phase(chain_migrated_pg_url)


async def test_bootstrap_path_has_no_workflow_step_phase(migrated_pg_url):
    """Fresh create_all deployments never build phase (the model no longer maps it)."""
    await _assert_no_workflow_step_phase(migrated_pg_url)


async def test_workflow_tables_cross_tenant_invisible(migrated_pg_url, owner_engine, app_user_engine):
    """Tenant isolation through the non-superuser role — the only kind of
    connection RLS actually filters. (The container's ``test`` user, like
    production's ``clawith``, is a SUPERUSER and bypasses every policy even
    under FORCE — switching the app to a non-superuser role is the P15
    deployment task; FORCE makes that switch sufficient.)"""
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    # Seed two tenants (FK target). tenants is ENABLE-only → owner may write.
    await _seed_tenant(owner_engine, tenant_a, "A")
    await _seed_tenant(owner_engine, tenant_b, "B")

    async with app_user_engine.connect() as conn:
        # Writing tenant A's definition requires tenant A's GUC (policy is
        # also the WITH CHECK for inserts).
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a}'"))
        await conn.execute(
            text(
                "INSERT INTO workflow_definitions (id, tenant_id, name, definition_hash, definition_json) "
                "VALUES (:id, :tid, 'wf-a', 'hash-a', '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "tid": tenant_a},
        )
        await conn.commit()

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_a}'"))
        own = (await conn.execute(text("SELECT count(*) FROM workflow_definitions WHERE name='wf-a'"))).scalar()
    assert own == 1, "tenant A must see its own definition"

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant_b}'"))
        cross = (await conn.execute(text("SELECT count(*) FROM workflow_definitions WHERE name='wf-a'"))).scalar()
    assert cross == 0, "tenant B must NOT see tenant A's definition"

    async with app_user_engine.connect() as conn:
        await conn.execute(text("SET LOCAL app.current_tenant_id = ''"))
        blind = (await conn.execute(text("SELECT count(*) FROM workflow_definitions WHERE name='wf-a'"))).scalar()
    assert blind == 0, "empty GUC must be blind on workflow tables"


async def test_workflow_insert_without_matching_guc_rejected(migrated_pg_url, owner_engine, app_user_engine):
    """The USING clause doubles as WITH CHECK: inserting a row for tenant X
    while the session GUC is empty (or another tenant) is rejected —
    background code without tenant_scoped_session cannot write."""
    import pytest
    from sqlalchemy.exc import DBAPIError

    tenant = uuid.uuid4()
    await _seed_tenant(owner_engine, tenant, "X")

    async with app_user_engine.connect() as conn:
        await conn.execute(text("SET LOCAL app.current_tenant_id = ''"))
        with pytest.raises(DBAPIError):
            await conn.execute(
                text(
                    "INSERT INTO workflow_definitions (id, tenant_id, name, definition_hash, definition_json) "
                    "VALUES (:id, :tid, 'wf-x', 'hash-x', '{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "tid": tenant},
            )


async def test_superuser_bypasses_even_forced_rls_documented_gap(migrated_pg_url, owner_engine, app_user_engine):
    """Documented gap (deepens P0 gap B): a SUPERUSER connection bypasses
    RLS entirely — FORCE included. Production currently connects as the
    POSTGRES_USER init superuser, so FORCE alone does not protect it; the
    P15 rollout must switch the app DSN to a non-superuser role."""
    tenant = uuid.uuid4()
    await _seed_tenant(owner_engine, tenant, "S")

    async with app_user_engine.connect() as conn:
        await conn.execute(text(f"SET LOCAL app.current_tenant_id = '{tenant}'"))
        await conn.execute(
            text(
                "INSERT INTO workflow_definitions (id, tenant_id, name, definition_hash, definition_json) "
                "VALUES (:id, :tid, 'wf-s', 'hash-s', '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "tid": tenant},
        )
        await conn.commit()

    async with owner_engine.connect() as conn:  # superuser, empty GUC
        await conn.execute(text("SET LOCAL app.current_tenant_id = ''"))
        count = (await conn.execute(text("SELECT count(*) FROM workflow_definitions WHERE name='wf-s'"))).scalar()
    assert count == 1, "superuser sees everything despite FORCE — the gap P15 closes by switching roles"


async def _read_workflow_closure_schema_contract(database_url: str) -> dict[str, dict[str, set[str]]]:
    tables = ("workflow_quota_reservations", "workflow_completion_outbox")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            contracts: dict[str, dict[str, set[str]]] = {}
            for table in tables:
                index_rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT indexname
                            FROM pg_indexes
                            WHERE schemaname = current_schema()
                              AND tablename = :table
                            """
                        ),
                        {"table": table},
                    )
                ).all()
                constraint_rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT conname, pg_get_constraintdef(oid) AS definition
                            FROM pg_constraint
                            WHERE conrelid = to_regclass(:table)
                            """
                        ),
                        {"table": table},
                    )
                ).all()
                contracts[table] = {
                    "indexes": {row.indexname for row in index_rows},
                    "constraints": {f"{row.conname}:{row.definition}" for row in constraint_rows},
                }
            return contracts
    finally:
        await engine.dispose()


async def test_workflow_closure_migration_matches_create_all_schema(
    chain_migrated_pg_url: str,
    migrated_pg_url: str,
) -> None:
    def run_alembic(*arguments: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=BACKEND_ROOT,
            env={**os.environ, "DATABASE_URL": chain_migrated_pg_url},
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, result.stderr[-2000:]

    await asyncio.to_thread(run_alembic, "downgrade", "system_plan_runtime_task_0713")
    try:
        await asyncio.to_thread(run_alembic, "upgrade", "head")
        migration_contract = await _read_workflow_closure_schema_contract(chain_migrated_pg_url)
        bootstrap_contract = await _read_workflow_closure_schema_contract(migrated_pg_url)
    finally:
        await asyncio.to_thread(run_alembic, "upgrade", "head")

    assert migration_contract == bootstrap_contract
    assert any(
        constraint.startswith("ck_workflow_completion_outbox_terminal_status:CHECK")
        for constraint in migration_contract["workflow_completion_outbox"]["constraints"]
    )
