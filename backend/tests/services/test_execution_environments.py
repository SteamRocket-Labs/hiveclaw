"""Environment failures preserve safe alternatives and unknown execution truth."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services import execution_environments as environments


def scope():
    return environments.EnvironmentCommandScope(uuid4(), uuid4(), uuid4(), "synthetic-command", None)


async def test_noncommand_scope_does_not_require_identity_or_database(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise AssertionError("Non-command tools must retain their existing capabilities")

    monkeypatch.setattr(environments, "tenant_scoped_session", unavailable)
    async with environments.environment_command_scope(
        SimpleNamespace(tenant_id=None, agent_id="legacy", user_id=None, runtime_task_id=None), "read"
    ):
        assert environments.current_environment_scope() is not None
    assert environments.current_environment_scope() is None


async def test_database_unavailable_refuses_execution_with_typed_status(monkeypatch):
    @asynccontextmanager
    async def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("synthetic-sensitive-database-detail")
        yield

    monkeypatch.setattr(environments, "tenant_scoped_session", unavailable)

    async def operation():
        raise AssertionError("An unavailable resource frame cannot start compute")

    result = await scope().execute(operation, provider="local_os_sandbox", timeout=1)
    assert result.evidence["reason_code"] == "environment_state_unavailable"
    assert result.evidence["status"] == "unavailable" and "synthetic-sensitive" not in result.error


async def test_unconfirmed_cleanup_cannot_start_another_command_in_same_tool(monkeypatch):
    current = scope()
    current.lease_id = uuid4()
    current.selected_provider = "vercel_sandbox"
    current.resource_state = "unknown"

    async def valid_frame():
        pass

    monkeypatch.setattr(current, "_check", valid_frame)

    async def operation():
        raise AssertionError("An unconfirmed resource must not be hidden by the next command")

    result = await current.execute(operation, provider="vercel_sandbox", timeout=1)
    assert result.evidence["reason_code"] == "environment_reconciliation_required"
    assert current.resource_state == "unknown"


async def test_release_database_failure_is_unknown_without_private_error_text(monkeypatch):
    current = scope()

    async def unavailable():
        raise SQLAlchemyError("synthetic-sensitive-database-detail")

    monkeypatch.setattr(current, "_release", unavailable)
    with pytest.raises(environments.EnvironmentError, match="^environment_release_unknown$") as error:
        await current.release()
    assert error.value.status == 503
