"""Native delegated controls retain exact database authority and lossless migration."""

import uuid

import pytest
from tests.integration import conftest as integration
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionControlInput
from tests.integration.test_business_task_child_binding import _migrate

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
def pg_container():
    # DDL cases own fresh databases; unrelated grants and receipts remain intact.
    yield from integration.pg_container.__wrapped__()


@pytest.fixture()
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


async def test_empty_delegation_permission_contract_round_trips_exact_guards(
    migrated_pg_url, owner_sessionmaker, tmp_path
):
    from app.services.session_event_contract import build_session_tenant_binding_function_sql as live_sql
    from migration_snapshots.delegation_permissions_1022 import build_session_tenant_binding_function_sql as frozen_sql

    assert live_sql() == frozen_sql()
    async with owner_sessionmaker() as db:
        before = await db.scalar(
            text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
        )
        original_revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    assert "NEW.kind='permission_response' AND run.task_type='delegation'" in before
    try:
        previous = _migrate(migrated_pg_url, "downgrade", "business_permissions_1022", tmp_path)
        assert previous.returncode == 0, previous.stderr
        async with owner_sessionmaker() as db:
            restored = await db.scalar(
                text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
            )
            assert "NEW.kind='permission_response' AND run.task_type='delegation'" not in restored
            assert "NEW.kind='permission_response' AND run.task_type='business_task'" in restored
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "business_permissions_1022"
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
    finally:
        restored_head = _migrate(migrated_pg_url, "upgrade", original_revision, tmp_path)
        assert restored_head.returncode == 0, restored_head.stderr
    async with owner_sessionmaker() as db:
        assert (
            await db.scalar(
                text("SELECT pg_get_functiondef('public.enforce_session_v2_tenant_binding()'::regprocedure)")
            )
            == before
        )
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == original_revision


async def verify_delegated_permission_binding(
    *, owner, factory, tenant_id, session_id, control_id, parent_agent_id, url, tmp_path
):
    for field, value in [
        ("peer_agent_id", None),
        ("parent_session_id", None),
        ("runtime_task_id", None),
        ("user_id", None),
        ("agent_id", parent_agent_id),
        ("session_kind", "user_chat"),
    ]:
        async with owner() as db:
            session = await db.get(ChatSession, uuid.UUID(session_id))
            previous = getattr(session, field)
            setattr(session, field, value)
            await db.commit()
        try:
            async with factory() as db:
                await pin_rls_tenant_context(db, tenant_id)
                control = await db.get(SessionControlInput, control_id)
                control.version += 1
                with pytest.raises(DBAPIError, match="session_v2_authority_binding_mismatch"):
                    await db.flush()
                await db.rollback()
        finally:
            async with owner() as db:
                setattr(await db.get(ChatSession, uuid.UUID(session_id)), field, previous)
                await db.commit()
    async with factory() as db:
        await pin_rls_tenant_context(db, tenant_id)
        control = await db.get(SessionControlInput, control_id)
        control.kind = "cancel_run"
        with pytest.raises(DBAPIError, match="session_v2_authority_binding_mismatch"):
            await db.flush()
        await db.rollback()
    async with owner() as db:
        original_revision = await db.scalar(text("SELECT version_num FROM alembic_version"))
    try:
        refused = _migrate(url, "downgrade", "business_permissions_1022", tmp_path)
        assert (
            refused.returncode != 0
            and "Retain native delegation permission responses before downgrade" in refused.stderr
        )
        async with owner() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "delegation_permissions_1022"
            assert await db.get(SessionControlInput, control_id) is not None
    finally:
        restored_head = _migrate(url, "upgrade", original_revision, tmp_path)
        assert restored_head.returncode == 0, restored_head.stderr
    async with owner() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == original_revision
        assert await db.get(SessionControlInput, control_id) is not None
