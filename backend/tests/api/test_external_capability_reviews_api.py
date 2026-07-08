from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.external_capabilities as external_mod
from app.api.external_capabilities import router
from app.core.security import get_current_admin, get_current_user
from app.database import get_db


class _FakeDB:
    async def execute(self, _stmt):
        raise AssertionError("Unexpected execute() call")


def _build_client(*, tenant_id=None):
    app = FastAPI()
    app.include_router(router)
    fake_db = _FakeDB()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=tenant_id or uuid4(), is_active=True)

    async def override_admin():
        return current_user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_admin] = override_admin
    app.dependency_overrides[get_current_user] = override_admin
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, current_user


def test_stage_external_capability_review_api_uses_tenant_and_normalized_bundle(monkeypatch):
    client, fake_db, current_user = _build_client()
    expected_review = {
        "id": str(uuid4()),
        "status": "review_required",
        "admission_class": "governed_runtime",
    }

    async def fake_stage(db_session, *, tenant_id, created_by_user_id, bundle):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        assert created_by_user_id == current_user.id
        assert bundle.plugin_name == "review-pack"
        assert bundle.components[0].qualified_name == "review-pack:check"
        return expected_review

    monkeypatch.setattr(external_mod, "stage_external_capability_review", fake_stage)

    resp = client.post(
        "/enterprise/external-capabilities/reviews",
        json={
            "source_format": "cc_plugin",
            "source_uri": "github:acme/review-pack",
            "plugin_name": "review-pack",
            "manifest_sha256": "manifest-hash",
            "components": [
                {
                    "component_type": "slash_command",
                    "local_name": "check",
                    "qualified_name": "review-pack:check",
                    "source_path": "commands/check.md",
                    "content_sha256": "cmd-hash",
                    "runtime_projection": {"description": "Run checks"},
                }
            ],
        },
    )

    assert resp.status_code == 200
    assert resp.json() == expected_review


def test_approve_external_capability_review_api_returns_snapshot(monkeypatch):
    target_review_id = uuid4()
    client, fake_db, current_user = _build_client()
    expected = {"id": str(uuid4()), "review_id": str(target_review_id), "status": "approved"}

    async def fake_approve(db_session, *, tenant_id, review_id, approved_by_user_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        assert str(review_id) == str(target_review_id)
        assert approved_by_user_id == current_user.id
        return expected

    monkeypatch.setattr(external_mod, "approve_external_capability_snapshot", fake_approve)

    resp = client.post(f"/enterprise/external-capabilities/reviews/{target_review_id}/approve")

    assert resp.status_code == 200
    assert resp.json() == expected


def test_revoke_external_capability_snapshot_api_threads_admin(monkeypatch):
    target_snapshot_id = uuid4()
    client, fake_db, current_user = _build_client()
    expected = {"snapshot_id": str(target_snapshot_id), "status": "revoked", "catalog_entries_revoked": 1}

    async def fake_revoke(db_session, *, tenant_id, snapshot_id, revoked_by_user_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        assert snapshot_id == target_snapshot_id
        assert revoked_by_user_id == current_user.id
        return expected

    monkeypatch.setattr(external_mod, "revoke_external_capability_snapshot", fake_revoke)

    resp = client.post(f"/enterprise/external-capabilities/snapshots/{target_snapshot_id}/revoke")

    assert resp.status_code == 200
    assert resp.json() == expected


def test_list_external_extension_catalog_entries_api_uses_tenant(monkeypatch):
    client, fake_db, current_user = _build_client()
    expected = [
        {
            "id": str(uuid4()),
            "snapshot_id": str(uuid4()),
            "component_type": "skill",
            "component_name": "audit",
            "qualified_name": "review-pack:audit",
            "policy": "optional",
            "status": "available",
        }
    ]

    async def fake_list_catalog(db_session, *, tenant_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        return expected

    monkeypatch.setattr(external_mod, "list_external_extension_catalog_entries", fake_list_catalog)

    resp = client.get("/enterprise/external-capabilities/catalog")

    assert resp.status_code == 200
    assert resp.json() == expected


def test_deactivate_agent_external_extension_checks_agent_access(monkeypatch):
    agent_id = uuid4()
    snapshot_id = uuid4()
    client, fake_db, current_user = _build_client()
    expected = {"snapshot_id": str(snapshot_id), "status": "inactive", "deactivated_components": []}

    async def fake_check_agent_access(db_session, user, requested_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(tenant_id=current_user.tenant_id), "owner"

    async def fake_deactivate(db_session, *, tenant_id, agent_id, snapshot_id, workspace, deactivated_by_user_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        assert deactivated_by_user_id == current_user.id
        assert str(workspace).endswith(str(agent_id))
        return expected

    monkeypatch.setattr(external_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(external_mod, "deactivate_external_extension_for_agent", fake_deactivate)

    resp = client.post(f"/agents/{agent_id}/external-extensions/{snapshot_id}/deactivate")

    assert resp.status_code == 200
    assert resp.json() == expected


def test_list_agent_external_extension_catalog_checks_agent_access(monkeypatch):
    agent_id = uuid4()
    client, fake_db, current_user = _build_client()
    expected = [{"id": str(uuid4()), "snapshot_id": str(uuid4()), "component_type": "subagent"}]

    async def fake_check_agent_access(db_session, user, requested_agent_id):
        assert db_session is fake_db
        assert user is current_user
        assert requested_agent_id == agent_id
        return SimpleNamespace(tenant_id=current_user.tenant_id), "owner"

    async def fake_list_catalog(db_session, *, tenant_id):
        assert db_session is fake_db
        assert tenant_id == current_user.tenant_id
        return expected

    monkeypatch.setattr(external_mod, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(external_mod, "list_external_extension_catalog_entries", fake_list_catalog)

    resp = client.get(f"/agents/{agent_id}/external-extensions/catalog")

    assert resp.status_code == 200
    assert resp.json() == expected
