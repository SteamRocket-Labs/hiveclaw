"""Tests for cut C2 (§12.7): subagent configuration API — 7 endpoints.

Agent-level surface (check_agent_access guarded, writes need manage) +
tenant-level enterprise library (org admin guarded). Style mirrors
test_agent_capability_installs_api.py: real router on a bare FastAPI app,
dependency overrides for auth/db, real stores on a tmp AGENT_DATA_DIR.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api.agent_subagents as subagents_mod
from app.agents.subagent import SubagentSpec
from app.agents.subagent_definition import (
    definition_store_for_agent,
    definition_store_for_tenant,
    render_subagent_definition,
)
from app.api.agent_subagents import enterprise_router, router
from app.config import get_settings
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_client(*, role: str = "member", tenant_id: uuid.UUID | None = None):
    app = FastAPI()
    app.include_router(router)
    app.include_router(enterprise_router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid.uuid4(), role=role, tenant_id=tenant_id or uuid.uuid4(), is_active=True)

    async def override_user():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def _grant_access(monkeypatch, agent_id: uuid.UUID, tenant_id: uuid.UUID, *, level: str = "manage"):
    async def fake_check_agent_access(_db, _user, target_agent_id):
        if target_agent_id != agent_id:
            raise HTTPException(status_code=404, detail="Agent not found")
        return SimpleNamespace(id=agent_id, tenant_id=tenant_id), level

    monkeypatch.setattr(subagents_mod, "check_agent_access", fake_check_agent_access)


def _md(name: str, prompt: str, *, type_: str = "explorer") -> str:
    return render_subagent_definition(SubagentSpec(name=name, type=type_, system_prompt=prompt))


@pytest.fixture
def data_root(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    return tmp_path


# --- agent-level: list + detail ----------------------------------------------


def test_list_merges_scopes(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="mine", type="explorer", system_prompt="agent def")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="ours", type="critic", system_prompt="tenant def")
    )

    resp = client.get(f"/agents/{agent_id}/subagents")
    assert resp.status_code == 200
    rows = {row["name"]: row["scope"] for row in resp.json()["subagents"]}
    assert rows["mine"] == "agent"
    assert rows["ours"] == "tenant"
    assert rows["explorer"] == "builtin"


def test_detail_returns_definition_scope_and_memory(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="mine", type="explorer", system_prompt="agent def body")
    )

    resp = client.get(f"/agents/{agent_id}/subagents/mine")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scope"] == "agent"
    assert "agent def body" in payload["definition"]
    assert payload["spec"]["type"] == "explorer"
    assert payload["memory"]["exists"] is False


def test_detail_404_when_absent(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    assert client.get(f"/agents/{agent_id}/subagents/ghost").status_code == 404


def test_detail_builtin_template_row(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.get(f"/agents/{agent_id}/subagents/explorer")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["scope"] == "builtin"
    assert payload["spec"]["type"] == "explorer"


# --- agent-level: write path --------------------------------------------------


def test_put_creates_agent_definition(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={"definition": _md("my-scout", "scout prompt")},
    )
    assert resp.status_code == 200
    assert resp.json()["scope"] == "agent"
    saved = definition_store_for_agent(agent_id, agent_data_dir=data_root).load("my-scout")
    assert saved is not None
    assert saved.system_prompt == "scout prompt"


def test_put_requires_manage(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id, level="use")

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={"definition": _md("my-scout", "scout prompt")},
    )
    assert resp.status_code == 403


def test_put_rejects_invalid_frontmatter(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={"definition": "---\ntype: explorer\n---\nno name field"},
    )
    assert resp.status_code == 422
    assert "name" in resp.json()["detail"]


def test_put_rejects_invalid_contract_field_types(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={"definition": "---\nname: my-scout\ntype: explorer\nmax_tool_rounds: nope\n---\nprompt"},
    )
    assert resp.status_code == 422
    assert "max_tool_rounds" in resp.json()["detail"]

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={"definition": "---\nname: my-scout\ntype: explorer\nallowed_tools: read_file\n---\nprompt"},
    )
    assert resp.status_code == 422
    assert "allowed_tools" in resp.json()["detail"]


def test_put_rejects_name_mismatch(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={"definition": _md("other-name", "prompt")},
    )
    assert resp.status_code == 422
    assert "mismatch" in resp.json()["detail"].lower()


def test_put_rejects_invalid_url_name(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.put(
        f"/agents/{agent_id}/subagents/%2e%2e%2fescape",
        json={"definition": _md("x", "prompt")},
    )
    assert resp.status_code in (404, 422)  # path-reject or name-guard reject, never 2xx


def test_delete_agent_definition_falls_back_to_tenant(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="dup", type="explorer", system_prompt="agent version")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="dup", type="critic", system_prompt="tenant version")
    )

    resp = client.delete(f"/agents/{agent_id}/subagents/dup")
    assert resp.status_code == 200

    detail = client.get(f"/agents/{agent_id}/subagents/dup").json()
    assert detail["scope"] == "tenant"
    assert "tenant version" in detail["definition"]


def test_delete_404_when_no_agent_definition(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    # tenant-level definition alone does not make DELETE on agent scope a hit
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="ours", type="critic", system_prompt="tenant def")
    )
    assert client.delete(f"/agents/{agent_id}/subagents/ours").status_code == 404


def test_cross_tenant_agent_404(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    other_agent = uuid.uuid4()  # check_agent_access raises 404 for unknown/foreign agents
    assert client.get(f"/agents/{other_agent}/subagents").status_code == 404


# --- tenant-level enterprise library ------------------------------------------


def test_enterprise_crud_for_org_admin(monkeypatch, data_root):
    tenant_id = uuid.uuid4()
    client, _db, _user = _build_client(role="org_admin", tenant_id=tenant_id)

    resp = client.put(
        "/enterprise/subagents/shared-critic",
        json={"definition": _md("shared-critic", "shared critic prompt", type_="critic")},
    )
    assert resp.status_code == 200
    assert resp.json()["scope"] == "tenant"

    listed = client.get("/enterprise/subagents")
    assert listed.status_code == 200
    names = {row["name"] for row in listed.json()["subagents"]}
    assert "shared-critic" in names

    # Detail returns the full definition text — the edit flow must round-trip
    # the body, never reconstruct frontmatter from list rows (C4 fix).
    detail = client.get("/enterprise/subagents/shared-critic")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["scope"] == "tenant"
    assert "shared critic prompt" in payload["definition"]
    assert payload["memory"]["exists"] is False

    assert client.delete("/enterprise/subagents/shared-critic").status_code == 200
    saved = definition_store_for_tenant(tenant_id, agent_data_dir=data_root).load("shared-critic")
    assert saved is None


def test_enterprise_detail_404_when_absent(monkeypatch, data_root):
    client, _db, _user = _build_client(role="org_admin")
    assert client.get("/enterprise/subagents/ghost").status_code == 404


def test_enterprise_forbidden_for_member(monkeypatch, data_root):
    client, _db, _user = _build_client(role="member")

    assert client.get("/enterprise/subagents").status_code == 403
    assert client.get("/enterprise/subagents/x").status_code == 403
    assert (
        client.put(
            "/enterprise/subagents/x",
            json={"definition": _md("x", "p")},
        ).status_code
        == 403
    )
    assert client.delete("/enterprise/subagents/x").status_code == 403
