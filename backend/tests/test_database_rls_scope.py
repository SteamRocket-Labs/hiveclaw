from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def exec_driver_sql(self, statement: str) -> None:
        self.statements.append(statement)


class _FakeAsyncSession:
    def __init__(self) -> None:
        self.sync_session = SimpleNamespace(info={})
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement) -> None:
        self.statements.append(str(statement))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _FakeSessionContext:
    def __init__(self, session: _FakeAsyncSession) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def test_rls_tenant_context_is_reapplied_for_each_transaction_begin():
    from app import database

    tenant_id = uuid4()
    connection = _FakeConnection()
    session = SimpleNamespace(info={database._RLS_TENANT_INFO_KEY: str(tenant_id)})

    database._apply_rls_tenant_for_transaction(session, object(), connection)
    database._apply_rls_tenant_for_transaction(session, object(), connection)

    assert connection.statements == [
        f"SET LOCAL app.current_tenant_id = '{tenant_id}'",
        f"SET LOCAL app.current_tenant_id = '{tenant_id}'",
    ]


def test_rls_tenant_context_pins_empty_scope_when_tenant_missing():
    from app import database

    connection = _FakeConnection()
    session = SimpleNamespace(info={database._RLS_TENANT_INFO_KEY: ""})

    database._apply_rls_tenant_for_transaction(session, object(), connection)

    assert connection.statements == ["SET LOCAL app.current_tenant_id = ''"]


async def test_pin_rls_tenant_context_updates_current_and_future_transactions():
    from app import database

    tenant_id = uuid4()
    session = _FakeAsyncSession()

    pinned = await database.pin_rls_tenant_context(session, tenant_id)

    assert pinned == tenant_id
    assert session.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(tenant_id)
    assert session.statements == [f"SET LOCAL app.current_tenant_id = '{tenant_id}'"]


async def test_tenant_scoped_session_restores_previous_context_after_exit():
    from app import database

    previous_tenant = str(uuid4())
    scoped_tenant = str(uuid4())
    session = _FakeAsyncSession()

    def fake_session_factory():
        return _FakeSessionContext(session)

    database.set_current_tenant(previous_tenant)
    try:
        async with database.tenant_scoped_session(scoped_tenant, session_factory=fake_session_factory):
            assert database.get_current_tenant_id() == scoped_tenant
        assert database.get_current_tenant_id() == previous_tenant
    finally:
        database.set_current_tenant(None)


def test_app_tenant_context_setters_restore_tokens():
    app_root = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []

    for path in app_root.rglob("*.py"):
        if path.name == "database.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "set_current_tenant(" in text and "reset_current_tenant(" not in text:
            offenders.append(str(path.relative_to(app_root.parent)))

    assert offenders == []


@pytest.mark.asyncio
async def test_bypass_exit_skips_restore_on_failed_transaction_and_preserves_original_error():
    """C3: a DB error inside the bypass scope leaves the transaction invalid;
    the finally-restore must not fire a second error that masks the first."""
    from types import SimpleNamespace

    from app.database import enter_rls_bypass

    executed: list[str] = []

    class _FailedTxSession:
        is_active = False  # SQLAlchemy: failed transaction pending rollback

        async def execute(self, stmt):
            executed.append(str(stmt))
            return SimpleNamespace()

    session = _FailedTxSession()
    session.is_active = True  # entering the scope succeeds

    with pytest.raises(RuntimeError, match="boom"):
        async with enter_rls_bypass(session, reason="unit-test bypass failure"):
            session.is_active = False  # simulate the tx dying inside the scope
            raise RuntimeError("boom")

    bypass_statements = [stmt for stmt in executed if "BYPASS" in stmt]
    restore_statements = [stmt for stmt in executed if "BYPASS" not in stmt]
    assert len(bypass_statements) == 1
    assert restore_statements == [], "no GUC restore may run on a failed transaction"


@pytest.mark.asyncio
async def test_bypass_exit_restores_tenant_scope_on_healthy_transaction():
    from types import SimpleNamespace

    from app.database import enter_rls_bypass, reset_current_tenant, set_current_tenant

    executed: list[str] = []

    class _HealthySession:
        is_active = True

        async def execute(self, stmt):
            executed.append(str(stmt))
            return SimpleNamespace()

    tenant_id = str(uuid4())
    token = set_current_tenant(tenant_id)
    try:
        async with enter_rls_bypass(_HealthySession(), reason="unit-test bypass restore"):
            pass
    finally:
        reset_current_tenant(token)

    assert any("BYPASS" in stmt for stmt in executed)
    assert any(tenant_id in stmt for stmt in executed), "healthy exit must re-pin the tenant scope"


@pytest.mark.asyncio
async def test_bypass_exit_restore_failure_is_logged_not_raised():
    """Even if the restore itself dies, the caller must see the body's result,
    not a masked secondary exception."""
    from types import SimpleNamespace

    from app.database import enter_rls_bypass

    class _FlakySession:
        is_active = True

        def __init__(self):
            self.calls = 0

        async def execute(self, stmt):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("connection lost during restore")
            return SimpleNamespace()

    async with enter_rls_bypass(_FlakySession(), reason="unit-test flaky restore"):
        pass  # must not raise


@pytest.mark.asyncio
async def test_bypass_scope_reapplies_bypass_after_commit_transaction_begin():
    from app import database

    session = _FakeAsyncSession()
    connection = _FakeConnection()

    async with database.enter_rls_bypass(session, reason="unit-test bypass transaction continuity"):
        assert session.sync_session.info[database._RLS_TENANT_INFO_KEY] == "BYPASS"

        database._apply_rls_tenant_for_transaction(session.sync_session, object(), connection)

    assert "SET LOCAL app.current_tenant_id = 'BYPASS'" in connection.statements
    assert database._RLS_TENANT_INFO_KEY not in session.sync_session.info


@pytest.mark.asyncio
async def test_bypass_scope_restores_previous_session_info_after_exit():
    from app import database

    previous_tenant = str(uuid4())
    session = _FakeAsyncSession()
    session.sync_session.info[database._RLS_TENANT_INFO_KEY] = previous_tenant

    async with database.enter_rls_bypass(session, reason="unit-test bypass previous restore"):
        assert session.sync_session.info[database._RLS_TENANT_INFO_KEY] == "BYPASS"

    assert session.sync_session.info[database._RLS_TENANT_INFO_KEY] == previous_tenant
