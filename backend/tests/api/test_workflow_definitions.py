"""§9 P6 red tests: workflow-definitions REST shell (service stubbed).

Service behaviour is covered on real PG in tests/services/; this file pins
the HTTP contract: payload mapping, error → status code mapping, and that
the authenticated user's tenant/identity always flow into the service.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.workflow_definitions as wfdef_api
from app.core.security import get_current_user
from app.database import get_db
from app.services.workflow_definitions import WorkflowDefinitionError


def _user():
    return SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4(), username="u")


def _record(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        name="weekly-report",
        definition_version=1,
        definition_hash="hash-1",
        status="draft",
        visibility_scope="agent",
        owner_type="user",
        owner_id=None,
        call_policy=None,
        promoted_from_run_id=None,
        definition_json={"name": "weekly-report"},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _StubService:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.raises: Exception | None = None

    def _maybe_raise(self):
        if self.raises is not None:
            raise self.raises

    async def create_draft(self, **kwargs):
        self.calls.append(("create_draft", kwargs))
        self._maybe_raise()
        return _record()

    async def list_definitions(self, **kwargs):
        self.calls.append(("list_definitions", kwargs))
        return [_record()]

    async def activate(self, definition_id, **kwargs):
        self.calls.append(("activate", {"definition_id": definition_id, **kwargs}))
        self._maybe_raise()
        return _record(status="active")

    async def deprecate(self, definition_id, **kwargs):
        self.calls.append(("deprecate", {"definition_id": definition_id, **kwargs}))
        self._maybe_raise()
        return _record(status="deprecated")

    async def revoke(self, definition_id, **kwargs):
        self.calls.append(("revoke", {"definition_id": definition_id, **kwargs}))
        self._maybe_raise()
        return _record(status="revoked")

    async def approve_promotion(self, definition_id, **kwargs):
        self.calls.append(("approve_promotion", {"definition_id": definition_id, **kwargs}))
        self._maybe_raise()
        return _record(status="active")

    async def get_record(self, definition_id, **kwargs):
        self.calls.append(("get_record", {"definition_id": definition_id, **kwargs}))
        self._maybe_raise()
        return _record()

    async def fork_to_ephemeral(self, **kwargs):
        self.calls.append(("fork_to_ephemeral", kwargs))
        self._maybe_raise()
        return {"name": "weekly-report", "steps": []}


def _client(user, stub: _StubService):
    api = FastAPI()
    api.include_router(wfdef_api.router)

    async def override_user():
        return user

    async def override_db():
        yield SimpleNamespace()

    api.dependency_overrides[get_current_user] = override_user
    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[wfdef_api.get_workflow_definition_service] = lambda: stub
    return TestClient(api)


def test_create_draft_threads_tenant_and_user():
    user, stub = _user(), _StubService()
    client = _client(user, stub)
    resp = client.post("/workflow-definitions", json={"definition": {"name": "x"}})
    assert resp.status_code == 200
    name, kwargs = stub.calls[0]
    assert name == "create_draft"
    assert kwargs["tenant_id"] == user.tenant_id
    assert kwargs["created_by_user_id"] == user.id


def test_lifecycle_endpoints_map_conflicts_to_409():
    user, stub = _user(), _StubService()
    stub.raises = WorkflowDefinitionError("cannot move definition from 'revoked' to 'active'")
    client = _client(user, stub)
    resp = client.post(f"/workflow-definitions/{uuid.uuid4()}/activate")
    assert resp.status_code == 409


def test_not_found_maps_to_404():
    user, stub = _user(), _StubService()
    stub.raises = WorkflowDefinitionError("definition deadbeef not found")
    client = _client(user, stub)
    resp = client.post(f"/workflow-definitions/{uuid.uuid4()}/revoke")
    assert resp.status_code == 404


def test_approve_promotion_threads_human_approver():
    user, stub = _user(), _StubService()
    client = _client(user, stub)
    resp = client.post(f"/workflow-definitions/{uuid.uuid4()}/approve-promotion")
    assert resp.status_code == 200
    name, kwargs = stub.calls[0]
    assert name == "approve_promotion"
    assert kwargs["approver_user_id"] == user.id


def test_missing_approver_maps_to_403():
    user, stub = _user(), _StubService()
    stub.raises = PermissionError("promotion approval requires a human approver")
    client = _client(user, stub)
    resp = client.post(f"/workflow-definitions/{uuid.uuid4()}/approve-promotion")
    assert resp.status_code == 403


def test_fork_returns_ephemeral_definition():
    user, stub = _user(), _StubService()
    client = _client(user, stub)
    resp = client.post(
        f"/workflow-definitions/{uuid.uuid4()}/fork",
        json={"agent_id": str(uuid.uuid4()), "patch": {"description": "tweak"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["definition"]["name"] == "weekly-report"
    fork_call = next(c for c in stub.calls if c[0] == "fork_to_ephemeral")
    assert fork_call[1]["patch"] == {"description": "tweak"}


def test_list_returns_records():
    user, stub = _user(), _StubService()
    client = _client(user, stub)
    resp = client.get("/workflow-definitions")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "weekly-report"
