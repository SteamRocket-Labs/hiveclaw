from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.memory import router
from app.core.security import get_current_admin, get_current_user


def _build_client(*, role: str, tmp_path):
    app = FastAPI()
    app.include_router(router)

    user = SimpleNamespace(id=uuid4(), role=role, tenant_id=uuid4(), is_active=True)

    async def override_user():
        return user

    async def override_admin():
        if user.role not in ("platform_admin", "org_admin"):
            raise HTTPException(status_code=403, detail="Admin access required")
        return user

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_current_admin] = override_admin
    return TestClient(app, raise_server_exceptions=False), user


def test_team_memory_api_supports_upsert_list_search_get_and_delete_over_http(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.team_memory.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client, _user = _build_client(role="org_admin", tmp_path=tmp_path)

    upsert = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Use a canary rollout and verify logs before promoting globally.",
            "mode": "replace",
            "base_revision": None,
        },
    )
    listed = client.get("/enterprise/memory/shared", params={"workspace_key": "workspace-alpha"})
    searched = client.get(
        "/enterprise/memory/shared/search",
        params={"workspace_key": "workspace-alpha", "query": "canary rollout"},
    )
    fetched = client.get(
        "/enterprise/memory/shared/deploy-playbook",
        params={"workspace_key": "workspace-alpha"},
    )
    deleted = client.delete(
        "/enterprise/memory/shared/deploy-playbook",
        params={"workspace_key": "workspace-alpha"},
    )

    assert upsert.status_code == 200
    assert listed.status_code == 200
    assert searched.status_code == 200
    assert fetched.status_code == 200
    assert deleted.status_code == 200
    assert upsert.json()["key"] == "deploy-playbook"
    assert upsert.json()["revision"] == 1
    assert upsert.json()["updated_by"] == str(_user.id)
    assert len(listed.json()) == 1
    assert searched.json()[0]["key"] == "deploy-playbook"
    assert "verify logs" in fetched.json()["content"]
    assert deleted.json() == {"deleted": True, "key": "deploy-playbook", "workspace_key": "workspace-alpha"}


def test_team_memory_api_rejects_invalid_mode_via_pydantic_over_http(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.team_memory.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client, _user = _build_client(role="org_admin", tmp_path=tmp_path)

    response = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Use a canary rollout and verify logs before promoting globally.",
            "mode": "apend",
        },
    )

    assert response.status_code == 422


def test_team_memory_delete_requires_admin_over_http(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.team_memory.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    admin_client, _admin = _build_client(role="org_admin", tmp_path=tmp_path)
    admin_client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Use a canary rollout and verify logs before promoting globally.",
            "mode": "replace",
            "base_revision": None,
        },
    )

    member_client, _member = _build_client(role="member", tmp_path=tmp_path)
    response = member_client.delete(
        "/enterprise/memory/shared/deploy-playbook",
        params={"workspace_key": "workspace-alpha"},
    )

    assert response.status_code == 403


def test_team_memory_api_returns_conflict_on_stale_revision(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.team_memory.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client, _user = _build_client(role="org_admin", tmp_path=tmp_path)

    created = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Use a canary rollout and verify logs before promoting globally.",
            "mode": "replace",
            "base_revision": None,
        },
    )

    response = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Overwrite with stale revision.",
            "mode": "replace",
            "base_revision": 0,
        },
    )

    assert created.status_code == 200
    assert response.status_code == 409
    assert response.json()["detail"]["current_entry"]["revision"] == 1


def test_team_memory_api_returns_server_wins_retry_metadata_for_stale_sync_token(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.team_memory.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client, _user = _build_client(role="org_admin", tmp_path=tmp_path)

    created = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Initial content.",
            "mode": "replace",
        },
    )
    created_token = created.json()["sync_token"]

    client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Server updated content.",
            "mode": "replace",
            "sync_token": created_token,
        },
    )

    stale = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Client stale content.",
            "mode": "replace",
            "sync_token": created_token,
        },
    )

    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["retry_action"] == "pull_latest_then_retry"
    assert detail["sync_hint"] == "server_wins_pull"
    assert detail["server_sync_token"] == detail["current_entry"]["sync_token"]


def test_team_memory_api_lists_tombstones_when_include_deleted_is_requested(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.team_memory.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    client, _user = _build_client(role="org_admin", tmp_path=tmp_path)

    created = client.put(
        "/enterprise/memory/shared",
        json={
            "workspace_key": "workspace-alpha",
            "key": "deploy-playbook",
            "title": "Deploy Playbook",
            "content": "Initial content.",
            "mode": "replace",
        },
    )
    delete_response = client.delete(
        "/enterprise/memory/shared/deploy-playbook",
        params={
            "workspace_key": "workspace-alpha",
            "sync_token": created.json()["sync_token"],
        },
    )
    listed = client.get(
        "/enterprise/memory/shared",
        params={"workspace_key": "workspace-alpha", "include_deleted": "true"},
    )
    fetched = client.get(
        "/enterprise/memory/shared/deploy-playbook",
        params={"workspace_key": "workspace-alpha", "include_deleted": "true"},
    )

    assert delete_response.status_code == 200
    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert listed.json()[0]["deleted"] is True
    assert listed.json()[0]["sync_token"]
    assert fetched.json()["deleted"] is True
