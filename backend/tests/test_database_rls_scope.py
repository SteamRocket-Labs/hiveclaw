from __future__ import annotations

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

    async def execute(self, statement) -> None:
        self.statements.append(str(statement))


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
