from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql


class _Scalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _Result:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return _Scalars(self._values)


class _CaptureDB:
    def __init__(self, values):
        self.values = values
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.values)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()


@pytest.mark.asyncio
async def test_activity_authority_is_filtered_in_sql_before_a_single_limit(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    owner_id = uuid4()
    owned = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        owner_user_id=owner_id,
        root_session_id=None,
        authority_state="owned",
        action_type="tool_call",
        summary="bounded row",
        detail_json={},
        related_id=None,
        created_at=datetime.now(UTC),
    )
    user = SimpleNamespace(id=owner_id, tenant_id=uuid4(), department_id=None, role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=user.tenant_id)
    db = _CaptureDB([owned])

    async def fake_access(*_args):
        return agent, "use"

    async def no_grants(*_args, **_kwargs):
        return set()

    async def forbidden_python_filter(*_args, **_kwargs):
        raise AssertionError("row-by-row authority filtering must not run")

    monkeypatch.setattr(activity_api, "check_agent_access", fake_access)
    monkeypatch.setattr(activity_api, "load_explicit_resource_grant_ids", no_grants, raising=False)
    monkeypatch.setattr(activity_api, "filter_authorized_resources", forbidden_python_filter, raising=False)

    payload = await activity_api.get_agent_activity(
        agent_id=agent_id,
        limit=5,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=db,
    )

    assert [row["summary"] for row in payload] == ["bounded row"]
    assert payload[0]["authority_source"] == "resource_owner"
    assert len(db.statements) == 1
    sql = _sql(db.statements[0])
    assert "agent_activity_logs.authority_state" in sql
    assert "agent_activity_logs.owner_user_id" in sql
    assert "exists (select" in sql
    assert "chat_sessions" in sql
    assert "limit" in sql
    assert "offset" not in sql


@pytest.mark.asyncio
async def test_activity_explicit_grants_are_loaded_once_and_reported_without_n_plus_one(monkeypatch):
    import app.api.activity as activity_api

    agent_id = uuid4()
    user = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), department_id=None, role="member")
    granted = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        owner_user_id=uuid4(),
        root_session_id=None,
        authority_state="owned",
        action_type="error",
        summary="granted row",
        detail_json={"tool_name": "web_search"},
        related_id=None,
        created_at=datetime.now(UTC),
    )
    agent = SimpleNamespace(id=agent_id, tenant_id=user.tenant_id)
    db = _CaptureDB([granted])
    grant_calls = 0

    async def fake_access(*_args):
        return agent, "use"

    async def one_grant(*_args, **_kwargs):
        nonlocal grant_calls
        grant_calls += 1
        return {granted.id}

    monkeypatch.setattr(activity_api, "check_agent_access", fake_access)
    monkeypatch.setattr(activity_api, "load_explicit_resource_grant_ids", one_grant, raising=False)

    payload = await activity_api.get_agent_activity(
        agent_id=agent_id,
        limit=5,
        operator_view=False,
        operator_reason=None,
        current_user=user,
        db=db,
    )

    assert grant_calls == 1
    assert len(db.statements) == 1
    assert payload[0]["authority_source"] == "resource_grant"
