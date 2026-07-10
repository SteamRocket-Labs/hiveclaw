from __future__ import annotations

from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, tenant_id, calls):
        self._tenant_id = tenant_id
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        if "app.current_tenant_id" in str(statement):
            return _ScalarResult(None)
        self._calls.append(statement)
        return _ScalarResult(self._tenant_id)


@pytest.mark.asyncio
async def test_resolve_tenant_for_agent_caches_successful_agent_lookup(monkeypatch) -> None:
    from app.services import tenant_resolver

    tenant_resolver.clear_tenant_resolution_cache()
    agent_id = uuid4()
    tenant_id = uuid4()
    calls = []

    monkeypatch.setattr(tenant_resolver, "async_session", lambda: _FakeSession(tenant_id, calls))

    first = await tenant_resolver.resolve_tenant_for_agent(agent_id)
    second = await tenant_resolver.resolve_tenant_for_agent(str(agent_id))

    assert first == tenant_id
    assert second == tenant_id
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resolve_tenant_for_runtime_task_is_a_narrow_locator(monkeypatch) -> None:
    from app.services import tenant_resolver

    task_id = uuid4()
    tenant_id = uuid4()
    calls = []
    monkeypatch.setattr(tenant_resolver, "async_session", lambda: _FakeSession(tenant_id, calls))

    resolved = await tenant_resolver.resolve_tenant_for_runtime_task(task_id)

    assert resolved == tenant_id
    assert len(calls) == 1
    assert "runtime_tasks.tenant_id" in str(calls[0])
    assert "runtime_tasks.id" in str(calls[0])


@pytest.mark.asyncio
async def test_resolve_tenant_for_user_is_a_narrow_locator(monkeypatch) -> None:
    from app.services import tenant_resolver

    user_id = uuid4()
    tenant_id = uuid4()
    calls = []
    monkeypatch.setattr(tenant_resolver, "async_session", lambda: _FakeSession(tenant_id, calls))

    resolved = await tenant_resolver.resolve_tenant_for_user(user_id)

    assert resolved == tenant_id
    assert len(calls) == 1
    assert "users.tenant_id" in str(calls[0])
    assert "users.id" in str(calls[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolver_name", "table_name"),
    [
        ("resolve_tenant_for_chat_session", "chat_sessions"),
        ("resolve_tenant_for_transcript_event", "chat_transcript_events"),
    ],
)
async def test_runtime_control_tenant_resolvers_are_narrow_locators(
    monkeypatch,
    resolver_name,
    table_name,
) -> None:
    from app.services import tenant_resolver

    resource_id = uuid4()
    tenant_id = uuid4()
    calls = []
    monkeypatch.setattr(tenant_resolver, "async_session", lambda: _FakeSession(tenant_id, calls))

    resolved = await getattr(tenant_resolver, resolver_name)(resource_id)

    assert resolved == tenant_id
    assert len(calls) == 1
    assert f"{table_name}.tenant_id" in str(calls[0])
    assert f"{table_name}.id" in str(calls[0])
