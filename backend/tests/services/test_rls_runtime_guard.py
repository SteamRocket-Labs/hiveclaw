from __future__ import annotations

import logging

import pytest


class _FakeResult:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeResult:
        return self

    def one_or_none(self) -> dict[str, object] | None:
        return self._row


class _FakeSession:
    def __init__(self, row: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.row = row
        self.error = error
        self.statements: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement):
        self.statements.append(str(statement))
        if self.error:
            raise self.error
        return _FakeResult(self.row)


class _FakeSessionFactory:
    def __init__(self, row: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.session = _FakeSession(row=row, error=error)
        self.opened = 0

    def __call__(self) -> _FakeSession:
        self.opened += 1
        return self.session


@pytest.mark.asyncio
async def test_strict_runtime_role_guard_rejects_superuser_roles() -> None:
    from app.services.rls_runtime_guard import (
        check_runtime_rls_role,
        latest_runtime_rls_role_health,
        reset_runtime_rls_role_guard_for_tests,
    )

    reset_runtime_rls_role_guard_for_tests()
    factory = _FakeSessionFactory(
        {
            "role_name": "postgres",
            "rolsuper": True,
            "rolbypassrls": False,
        }
    )

    with pytest.raises(RuntimeError, match="superuser"):
        await check_runtime_rls_role(session_factory=factory, enforcement="strict")

    health = latest_runtime_rls_role_health()
    assert health["runtime_role_checked"] is True
    assert health["role_name"] == "postgres"
    assert health["superuser"] is True
    assert health["bypassrls"] is False
    assert health["status"] == "critical"


@pytest.mark.asyncio
async def test_warn_runtime_role_guard_logs_and_degrades_for_bypassrls(caplog: pytest.LogCaptureFixture) -> None:
    from app.services.rls_runtime_guard import (
        check_runtime_rls_role,
        latest_runtime_rls_role_health,
        reset_runtime_rls_role_guard_for_tests,
    )

    reset_runtime_rls_role_guard_for_tests()
    caplog.set_level(logging.WARNING)
    factory = _FakeSessionFactory(
        {
            "role_name": "app_with_bypass",
            "rolsuper": False,
            "rolbypassrls": True,
        }
    )

    snapshot = await check_runtime_rls_role(session_factory=factory, enforcement="warn")

    assert snapshot.violations == ("bypassrls",)
    assert "bypassrls" in caplog.text
    health = latest_runtime_rls_role_health()
    assert health["status"] == "degraded"
    assert health["runtime_role_checked"] is True
    assert health["bypassrls"] is True


@pytest.mark.asyncio
async def test_off_runtime_role_guard_does_not_open_database_session() -> None:
    from app.services.rls_runtime_guard import (
        check_runtime_rls_role,
        latest_runtime_rls_role_health,
        reset_runtime_rls_role_guard_for_tests,
    )

    reset_runtime_rls_role_guard_for_tests()
    factory = _FakeSessionFactory(
        {
            "role_name": "postgres",
            "rolsuper": True,
            "rolbypassrls": True,
        }
    )

    snapshot = await check_runtime_rls_role(session_factory=factory, enforcement="off")

    assert factory.opened == 0
    assert snapshot.checked is False
    health = latest_runtime_rls_role_health()
    assert health["status"] == "disabled"
    assert health["runtime_role_checked"] is False


@pytest.mark.asyncio
async def test_strict_runtime_role_guard_rejects_unverifiable_database_role() -> None:
    from app.services.rls_runtime_guard import check_runtime_rls_role, reset_runtime_rls_role_guard_for_tests

    reset_runtime_rls_role_guard_for_tests()
    factory = _FakeSessionFactory(error=ConnectionError("database unavailable"))

    with pytest.raises(RuntimeError, match="could not verify"):
        await check_runtime_rls_role(session_factory=factory, enforcement="strict")


@pytest.mark.asyncio
async def test_safe_runtime_role_guard_reports_ok_health() -> None:
    from app.services.rls_runtime_guard import (
        check_runtime_rls_role,
        latest_runtime_rls_role_health,
        reset_runtime_rls_role_guard_for_tests,
    )

    reset_runtime_rls_role_guard_for_tests()
    factory = _FakeSessionFactory(
        {
            "role_name": "app_rls",
            "rolsuper": False,
            "rolbypassrls": False,
        }
    )

    snapshot = await check_runtime_rls_role(session_factory=factory, enforcement="strict")

    assert snapshot.violations == ()
    assert "pg_roles" in factory.session.statements[0]
    health = latest_runtime_rls_role_health()
    assert health["status"] == "ok"
    assert health["runtime_role_checked"] is True
    assert health["superuser"] is False
    assert health["bypassrls"] is False
