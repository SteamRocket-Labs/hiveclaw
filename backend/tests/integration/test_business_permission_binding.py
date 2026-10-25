"""Business permission authority changes migrate without dropping receipts."""

import pytest
from tests.integration import conftest as integration

from sqlalchemy import text

from tests.integration.test_business_task_child_binding import _migrate

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
def pg_container():
    # DDL cases own fresh databases; unrelated business receipts remain intact.
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


async def test_empty_business_permission_contract_restores_previous_and_current_guards(
    migrated_pg_url,
    owner_sessionmaker,
    tmp_path,
):
    from app.services.session_event_contract import build_session_tenant_binding_function_sql as live_sql
    from migration_snapshots.delegation_permissions_1022 import build_session_tenant_binding_function_sql as frozen_sql

    assert live_sql() == frozen_sql()
    async with owner_sessionmaker() as db:
        before_revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
        before = await db.scalar(
            text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
        )
    assert "NEW.kind='permission_response' AND run.task_type='business_task'" in before
    previous = _migrate(migrated_pg_url, "downgrade", "business_task_child_1022", tmp_path)
    assert previous.returncode == 0, previous.stderr
    async with owner_sessionmaker() as db:
        old = await db.scalar(
            text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
        )
        assert "NEW.kind='permission_response' AND run.task_type='business_task'" not in old
        assert old.count("run.task_type='business_task'") == 6
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "business_task_child_1022"
    upgraded = _migrate(migrated_pg_url, "upgrade", "delegation_permissions_1022", tmp_path)
    assert upgraded.returncode == 0, upgraded.stderr
    async with owner_sessionmaker() as db:
        assert (
            await db.scalar(
                text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
            )
            == before
        )
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "delegation_permissions_1022"

    restored_head = _migrate(migrated_pg_url, "upgrade", "head", tmp_path)
    assert restored_head.returncode == 0, restored_head.stderr
    async with owner_sessionmaker() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == before_revision
        assert (
            await db.scalar(
                text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
            )
            == before
        )
