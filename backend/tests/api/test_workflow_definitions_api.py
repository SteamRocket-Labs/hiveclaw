"""Registered workflow definition API authorization.

The service owns lifecycle semantics; the API shell must still enforce who may
manage or list definitions before it calls the service.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _user(*, role="member", tenant_id=None):
    return SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=tenant_id or uuid.uuid4())


def _record(**overrides):
    data = dict(
        id=uuid.uuid4(),
        name="wf",
        definition_version=1,
        definition_hash="hash",
        definition_json={"name": "wf", "description": "what this flow does", "steps": []},
        status="draft",
        visibility_scope="agent",
        owner_type="agent",
        owner_id=uuid.uuid4(),
        call_policy=None,
        promoted_from_run_id=None,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_record_payload_carries_description():
    """The asset view shows 说明 — the payload must surface the definition's
    description (and tolerate definitions that never set one)."""
    from app.api import workflow_definitions as api

    payload = api.record_payload(_record())
    assert payload["description"] == "what this flow does"

    bare = api.record_payload(_record(definition_json={"name": "wf", "steps": []}))
    assert bare["description"] == ""


class _FakeDefinitionService:
    def __init__(self, record=None):
        self.record = record or _record()
        self.create_calls: list[dict] = []
        self.list_calls: list[dict] = []

    async def create_draft(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.record

    async def list_definitions(self, **kwargs):
        self.list_calls.append(kwargs)
        return [self.record]


@pytest.mark.asyncio
async def test_member_cannot_create_tenant_scope_definition():
    from app.api import workflow_definitions as api

    service = _FakeDefinitionService()
    payload = api.DefinitionCreateRequest(
        definition={"name": "wf", "steps": []},
        visibility_scope="tenant",
    )

    with pytest.raises(HTTPException) as exc:
        await api.create_definition_draft(payload, current_user=_user(role="member"), db=object(), service=service)

    assert exc.value.status_code == 403
    assert service.create_calls == []


@pytest.mark.asyncio
async def test_agent_scope_create_requires_manage_access(monkeypatch):
    from app.api import workflow_definitions as api

    agent_id = uuid.uuid4()
    service = _FakeDefinitionService()

    async def fake_check_agent_access(db, user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "use"

    monkeypatch.setattr(api, "check_agent_access", fake_check_agent_access, raising=False)

    payload = api.DefinitionCreateRequest(
        definition={"name": "wf", "steps": []},
        visibility_scope="agent",
        owner_type="agent",
        owner_id=str(agent_id),
    )

    with pytest.raises(HTTPException) as exc:
        await api.create_definition_draft(payload, current_user=_user(role="member"), db=object(), service=service)

    assert exc.value.status_code == 403
    assert service.create_calls == []


@pytest.mark.asyncio
async def test_agent_scope_create_allows_agent_manager(monkeypatch):
    from app.api import workflow_definitions as api

    agent_id = uuid.uuid4()
    service = _FakeDefinitionService()

    async def fake_check_agent_access(db, user, requested_agent_id):
        assert requested_agent_id == agent_id
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(api, "check_agent_access", fake_check_agent_access, raising=False)

    payload = api.DefinitionCreateRequest(
        definition={"name": "wf", "steps": []},
        visibility_scope="agent",
        owner_type="agent",
        owner_id=str(agent_id),
    )

    result = await api.create_definition_draft(payload, current_user=_user(role="member"), db=object(), service=service)

    assert result["id"] == str(service.record.id)
    assert len(service.create_calls) == 1


@pytest.mark.asyncio
async def test_list_without_agent_context_requires_admin():
    from app.api import workflow_definitions as api

    service = _FakeDefinitionService()

    with pytest.raises(HTTPException) as exc:
        await api.list_definitions(agent_id=None, current_user=_user(role="member"), db=object(), service=service)

    assert exc.value.status_code == 403
    assert service.list_calls == []
