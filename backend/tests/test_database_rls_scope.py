from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


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
