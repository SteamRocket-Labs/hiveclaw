"""Pin the real RLS frame once without losing later transaction reassertion."""

import uuid

import pytest
from sqlalchemy import event, select, text

from app.database import pin_rls_tenant_context
from app.models.tenant import Tenant
from app.models.user import User
from tests.integration.test_m1e_business_teams_postgres import _user

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def reader_rows(owner_sessionmaker):
    tenants, users = [uuid.uuid4() for _ in range(2)], [uuid.uuid4() for _ in range(2)]
    async with owner_sessionmaker() as db:
        for tenant in tenants:
            db.add(Tenant(id=tenant, name="Scoped reader", slug=f"reader-{tenant}"))
        await db.flush()
        for tenant, user in zip(tenants, users, strict=True):
            _user(db, id=user, tenant_id=tenant, username=f"reader-{user.hex}", email=f"{user}@scope.test")
        await db.commit()
    return tenants, users


@pytest.mark.parametrize("start", ["fresh", "logical_transaction", "committed", "rolled_back", "empty"])
async def test_initial_pin_sets_the_current_frame_once_and_reapplies_after_commit(
    reader_rows, app_user_engine, app_user_sessionmaker, start
):
    tenants, users = reader_rows
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SET LOCAL app.current_tenant_id"):
            statements.append(statement)

    async with app_user_sessionmaker() as db:
        if start == "logical_transaction":
            await db.begin()
        elif start in {"committed", "rolled_back"}:
            await pin_rls_tenant_context(db, tenants[1])
            if start == "committed":
                await db.commit()
            else:
                await db.rollback()
        scope = None if start == "empty" else tenants[0]
        expected_scope = "" if scope is None else str(scope)
        expected_users = [] if scope is None else [users[0]]
        event.listen(app_user_engine.sync_engine, "before_cursor_execute", observe)
        try:
            assert await pin_rls_tenant_context(db, scope) == scope
            assert await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == expected_scope
            role = (
                await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            ).one()
            assert role == (False, False)
            assert list(await db.scalars(select(User.id).where(User.id.in_(users)))) == expected_users
            initial_statements = len(statements)
            await db.commit()
            assert await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == expected_scope
            assert list(await db.scalars(select(User.id).where(User.id.in_(users)))) == expected_users
            assert len(statements) == initial_statements + 1
        finally:
            event.remove(app_user_engine.sync_engine, "before_cursor_execute", observe)
    assert initial_statements == 1, statements


async def test_active_transaction_pin_reasserts_the_frame_after_a_direct_guc_change(
    reader_rows, app_user_engine, app_user_sessionmaker
):
    tenants, users = reader_rows
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SET LOCAL app.current_tenant_id"):
            statements.append(statement)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenants[0])
        await db.execute(text(f"SET LOCAL app.current_tenant_id = '{tenants[1]}'"))
        assert list(await db.scalars(select(User.id).where(User.id.in_(users)))) == [users[1]]
        event.listen(app_user_engine.sync_engine, "before_cursor_execute", observe)
        try:
            assert await pin_rls_tenant_context(db, tenants[0]) == tenants[0]
            assert await db.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == str(tenants[0])
            assert list(await db.scalars(select(User.id).where(User.id.in_(users)))) == [users[0]]
        finally:
            event.remove(app_user_engine.sync_engine, "before_cursor_execute", observe)
    assert len(statements) == 1
