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

    async def commit(self):
        return None


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
    return render_subagent_definition(
        SubagentSpec(name=name, description=f"{name} helper definition", type=type_, system_prompt=prompt)
    )


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
        SubagentSpec(name="mine", description="d", type="explorer", system_prompt="agent def")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="ours", description="d", type="critic", system_prompt="tenant def")
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
        SubagentSpec(name="mine", description="d", type="explorer", system_prompt="agent def body")
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
    # The builtin template is a real template: its body must be the same
    # baseline prompt the runtime injects at spawn time — never an empty doc.
    assert "READ-ONLY" in payload["definition"]
    assert payload["spec"]["description"]  # whenToUse surfaces for the edit flow


def test_detail_builtin_templates_all_carry_baseline_prompt(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    # Anchors from the CC built-in agent ports (Explore / general-purpose / verification)
    for name, anchor in (("general-purpose", "agent for Hive"), ("explorer", "READ-ONLY"), ("critic", "VERDICT")):
        payload = client.get(f"/agents/{agent_id}/subagents/{name}").json()
        body = payload["definition"].split("---", 2)[-1]
        assert anchor in body, f"builtin {name} template body must carry its baseline prompt"
        assert payload["spec"]["description"], f"builtin {name} must carry a whenToUse description"


# --- AI generation (CC /agents "generate" method, vendor-neutral) -------------


def _patch_generation(monkeypatch, *, definition: str = "GENERATED-MD", capture: dict | None = None):
    async def fake_resolve_model(*args, **kwargs):
        return {"provider": "openai", "api_key": "k", "model": "m", "base_url": None}

    async def fake_generate(request, *, model_config, existing_names=None, **_kwargs):
        if capture is not None:
            capture.update(request=request, model_config=model_config, existing_names=existing_names)
        return definition

    monkeypatch.setattr(subagents_mod, "_resolve_generation_model_config", fake_resolve_model)
    monkeypatch.setattr(subagents_mod, "generate_subagent_definition", fake_generate)


def test_generate_agent_subagent_returns_definition(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    capture: dict = {}
    _patch_generation(monkeypatch, capture=capture)

    resp = client.post(
        f"/agents/{agent_id}/subagents/generate",
        json={"description": "track DeFi market movements"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"] == "GENERATED-MD"
    assert capture["request"] == "track DeFi market movements"
    # existing names (incl. builtins) reach the generator so it avoids collisions
    assert "explorer" in (capture["existing_names"] or [])


def test_generate_agent_subagent_requires_manage(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id, level="use")
    _patch_generation(monkeypatch)

    resp = client.post(f"/agents/{agent_id}/subagents/generate", json={"description": "x"})
    assert resp.status_code == 403


def test_generate_agent_subagent_requires_description(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    _patch_generation(monkeypatch)

    resp = client.post(f"/agents/{agent_id}/subagents/generate", json={"description": "   "})
    assert resp.status_code == 422


def test_generate_surfaces_generation_failure(monkeypatch, data_root):
    from app.services.subagent_generator import SubagentGenerationError

    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    async def fake_resolve_model(*args, **kwargs):
        return {"provider": "openai", "api_key": "k", "model": "m", "base_url": None}

    async def fake_generate(request, *, model_config, existing_names=None, **_kwargs):
        raise SubagentGenerationError("model returned an empty response")

    monkeypatch.setattr(subagents_mod, "_resolve_generation_model_config", fake_resolve_model)
    monkeypatch.setattr(subagents_mod, "generate_subagent_definition", fake_generate)

    resp = client.post(f"/agents/{agent_id}/subagents/generate", json={"description": "x"})
    assert resp.status_code == 502
    assert "empty response" in resp.json()["detail"]


def test_generate_enterprise_subagent_org_admin_only(monkeypatch, data_root):
    client, _db, _user = _build_client(role="org_admin")
    _patch_generation(monkeypatch)
    resp = client.post("/enterprise/subagents/generate", json={"description": "company-wide reviewer"})
    assert resp.status_code == 200
    assert resp.json()["definition"] == "GENERATED-MD"

    member_client, _db2, _user2 = _build_client(role="member")
    resp = member_client.post("/enterprise/subagents/generate", json={"description": "x"})
    assert resp.status_code == 403


# --- evolution loop: proposals + approval mode (§4.3) --------------------------


def _seed_pending_proposal(agent_id, data_root, *, body: str = "You are a scout. Verify twice."):
    from app.agents.subagent_definition import render_subagent_definition as render
    from app.agents.subagent_evolution import EvolutionProposal, definition_sha, proposal_store_for_agent

    base = definition_store_for_agent(agent_id, agent_data_dir=data_root).load("scout")
    proposal = EvolutionProposal(
        name="scout",
        status="pending",
        base_definition_sha=definition_sha(render(base)),
        absorbed_entry_ids=["e1"],
        rationale="mature lesson",
        created_at="2026-06-05T00:00:00+00:00",
        proposal_id="prop-api-1",
        body=body,
    )
    proposal_store_for_agent(agent_id, agent_data_dir=data_root).save(proposal)
    return proposal


def _seed_agent_definition(agent_id, data_root):
    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="scout", description="d", type="explorer", system_prompt="You are a scout.")
    )


def test_list_marks_pending_proposal_and_mode(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    _seed_agent_definition(agent_id, data_root)
    _seed_pending_proposal(agent_id, data_root)

    payload = client.get(f"/agents/{agent_id}/subagents").json()
    rows = {row["name"]: row for row in payload["subagents"]}
    assert rows["scout"]["pending_proposal"] is True
    assert rows["explorer"]["pending_proposal"] is False
    assert payload["evolution_auto_approve"] is False  # fake agent has no column → default off


def test_detail_carries_pending_proposal(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    _seed_agent_definition(agent_id, data_root)
    proposal = _seed_pending_proposal(agent_id, data_root)

    payload = client.get(f"/agents/{agent_id}/subagents/scout").json()
    assert payload["proposal"]["proposal_id"] == proposal.proposal_id
    assert payload["proposal"]["body"] == proposal.body
    assert "Verify twice" in payload["proposal"]["body"]


def test_approve_proposal_applies_definition(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    _seed_agent_definition(agent_id, data_root)
    _seed_pending_proposal(agent_id, data_root)

    resp = client.post(f"/agents/{agent_id}/subagents/scout/proposal/approve")
    assert resp.status_code == 200
    assert resp.json()["applied"] is True

    updated = definition_store_for_agent(agent_id, agent_data_dir=data_root).load("scout")
    assert updated.system_prompt == "You are a scout. Verify twice."
    # idempotence: a second approve finds nothing pending
    assert client.post(f"/agents/{agent_id}/subagents/scout/proposal/approve").status_code == 404


def test_approve_requires_manage_and_409_on_stale(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id, level="use")
    assert client.post(f"/agents/{agent_id}/subagents/scout/proposal/approve").status_code == 403

    client2, _db2, _user2 = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    _seed_agent_definition(agent_id, data_root)
    _seed_pending_proposal(agent_id, data_root)
    # definition edited after drafting → stale
    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="scout", description="d", type="explorer", system_prompt="edited meanwhile")
    )
    assert client2.post(f"/agents/{agent_id}/subagents/scout/proposal/approve").status_code == 409


def test_reject_proposal_endpoint(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)
    _seed_agent_definition(agent_id, data_root)
    _seed_pending_proposal(agent_id, data_root)

    assert client.post(f"/agents/{agent_id}/subagents/scout/proposal/reject").json()["rejected"] is True
    assert client.post(f"/agents/{agent_id}/subagents/scout/proposal/reject").status_code == 404
    # definition untouched
    assert (
        definition_store_for_agent(agent_id, agent_data_dir=data_root).load("scout").system_prompt == "You are a scout."
    )


def test_evolution_mode_switch(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    resp = client.post(f"/agents/{agent_id}/subagents/evolution-mode", json={"auto_approve": True})
    assert resp.status_code == 200
    assert resp.json()["evolution_auto_approve"] is True

    use_client, _db2, _user2 = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id, level="use")
    assert (
        use_client.post(f"/agents/{agent_id}/subagents/evolution-mode", json={"auto_approve": True}).status_code == 403
    )


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
        json={"definition": "---\nname: my-scout\ndescription: d\ntype: explorer\nmax_tool_rounds: nope\n---\nprompt"},
    )
    assert resp.status_code == 422
    assert "max_tool_rounds" in resp.json()["detail"]

    resp = client.put(
        f"/agents/{agent_id}/subagents/my-scout",
        json={
            "definition": "---\nname: my-scout\ndescription: d\ntype: explorer\nallowed_tools: read_file\n---\nprompt"
        },
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


def test_member_manage_cannot_delete_agent_definition_asset(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="dup", description="d", type="explorer", system_prompt="agent version")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="dup", description="d", type="critic", system_prompt="tenant version")
    )

    resp = client.delete(f"/agents/{agent_id}/subagents/dup")
    assert resp.status_code == 403

    detail = client.get(f"/agents/{agent_id}/subagents/dup").json()
    assert detail["scope"] == "agent"
    assert "agent version" in detail["definition"]


def test_admin_delete_agent_definition_falls_back_to_tenant(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(role="org_admin", tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    definition_store_for_agent(agent_id, agent_data_dir=data_root).save(
        SubagentSpec(name="dup", description="d", type="explorer", system_prompt="agent version")
    )
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="dup", description="d", type="critic", system_prompt="tenant version")
    )

    resp = client.delete(f"/agents/{agent_id}/subagents/dup")
    assert resp.status_code == 200

    detail = client.get(f"/agents/{agent_id}/subagents/dup").json()
    assert detail["scope"] == "tenant"
    assert "tenant version" in detail["definition"]


def test_delete_404_when_no_agent_definition(monkeypatch, data_root):
    agent_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    client, _db, _user = _build_client(role="org_admin", tenant_id=tenant_id)
    _grant_access(monkeypatch, agent_id, tenant_id)

    # tenant-level definition alone does not make DELETE on agent scope a hit
    definition_store_for_tenant(tenant_id, agent_data_dir=data_root).save(
        SubagentSpec(name="ours", description="d", type="critic", system_prompt="tenant def")
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
