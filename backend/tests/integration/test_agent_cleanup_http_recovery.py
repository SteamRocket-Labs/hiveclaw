"""Authenticated route consumption of committed deletion cleanup and retries."""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.security_audit import SecurityAuditEvent
from app.models.tenant import Tenant
from app.models.user import User
from tests.integration.test_agent_delete_conflict_recovery import (
    _seed_admin_agent,
    _seed_hr_abandon_fixture,
)


def _client(owner_sessionmaker, user_id):
    from app.api.agents import router as agents_router
    from app.api.hr_creation import router as hr_router
    from app.core.security import get_current_user
    from app.database import get_db

    app = FastAPI()
    app.include_router(hr_router)
    app.include_router(agents_router)

    async def authenticated_user():
        # Only authentication is substituted; tenant/role/resource gates run.
        async with owner_sessionmaker() as db:
            return await db.get(User, user_id)

    async def request_db():
        async with owner_sessionmaker() as db:
            yield db

    app.dependency_overrides[get_current_user] = authenticated_user
    app.dependency_overrides[get_db] = request_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("crash_before_archive", [False, True])
async def test_deleted_agent_cleanup_is_discoverable_after_reload_and_retry(
    owner_sessionmaker, monkeypatch, tmp_path, crash_before_archive
):
    import app.services.agent_manager as manager

    tenant_id, agent_id, admin_id = await _seed_admin_agent(owner_sessionmaker)
    monkeypatch.setattr(manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    agent_dir = tmp_path / str(agent_id)
    agent_dir.mkdir()
    (agent_dir / "evidence.txt").write_text("retained evidence")
    archive = manager.agent_manager.archive_agent_files

    if crash_before_archive:
        from app.services.agent_identity_lifecycle import soft_delete_agent

        async with owner_sessionmaker() as db:
            await soft_delete_agent(db, await db.get(Agent, agent_id), actor_id=admin_id)
            await db.commit()
    else:

        async def unavailable(_agent_id):
            raise OSError("injected archive outage")

        monkeypatch.setattr(manager.agent_manager, "archive_agent_files", unavailable)
        async with _client(owner_sessionmaker, admin_id) as client:
            response = await client.delete(f"/agents/{agent_id}")
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "agent_cleanup_pending"
        monkeypatch.setattr(manager.agent_manager, "archive_agent_files", archive)

    async with owner_sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(SecurityAuditEvent))
    async with _client(owner_sessionmaker, admin_id) as client:
        assert (await client.get(f"/agents/{agent_id}")).status_code == 404
        response = await client.get("/agents/cleanup-pending", params={"tenant_id": str(tenant_id)})
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()] == [str(agent_id)]
        assert set(response.json()[0]) == {"id", "name", "deleted_at"}
        assert (await client.delete(f"/agents/{agent_id}")).status_code == 204
        assert (await client.get("/agents/cleanup-pending")).json() == []
        assert (await client.delete(f"/agents/{agent_id}")).status_code == 204
    assert not agent_dir.exists()
    assert [path.read_text() for path in (tmp_path / "_archived").glob("*/evidence.txt")] == ["retained evidence"]
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SecurityAuditEvent)) == before


async def test_cleanup_inventory_preserves_role_tenant_and_live_company_gates(
    owner_sessionmaker, monkeypatch, tmp_path
):
    import app.services.agent_manager as manager

    tenant_id, agent_id, foreign_admin_id = await _seed_admin_agent(owner_sessionmaker, shared_tenant=False)
    monkeypatch.setattr(manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    (tmp_path / str(agent_id)).mkdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, agent_id)
        agent.deleted_at = datetime.now(UTC)
        await db.commit()
    async with _client(owner_sessionmaker, foreign_admin_id) as client:
        assert (await client.get("/agents/cleanup-pending")).json() == []
        assert (await client.get("/agents/cleanup-pending", params={"tenant_id": str(tenant_id)})).status_code == 403
    async with owner_sessionmaker() as db:
        user = await db.get(User, foreign_admin_id)
        user.role = "member"
        await db.commit()
    async with _client(owner_sessionmaker, foreign_admin_id) as client:
        assert (await client.get("/agents/cleanup-pending")).status_code == 403
    async with owner_sessionmaker() as db:
        user = await db.get(User, foreign_admin_id)
        user.role = "platform_admin"
        user.tenant_id = tenant_id
        tenant = await db.get(Tenant, tenant_id)
        tenant.is_active = False
        await db.commit()
    async with _client(owner_sessionmaker, foreign_admin_id) as client:
        assert (await client.get("/agents/cleanup-pending")).status_code in {403, 404}
    assert (tmp_path / str(agent_id)).exists()


@pytest.mark.parametrize("crash_before_archive", [False, True])
async def test_hr_abandon_cleanup_survives_new_client_and_disappears_after_retry(
    owner_sessionmaker, monkeypatch, tmp_path, crash_before_archive
):
    import app.services.agent_manager as manager

    _tenant_id, user_id, hr_agent_id, draft_id, employee_id = await _seed_hr_abandon_fixture(owner_sessionmaker)
    monkeypatch.setattr(manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    (tmp_path / str(employee_id)).mkdir()
    archive = manager.agent_manager.archive_agent_files

    async def unavailable(_agent_id):
        raise OSError("injected archive outage")

    path = f"/agents/{hr_agent_id}/hr-creation-drafts"
    if crash_before_archive:
        from app.services.hr_creation_recovery import abandon_hr_creation

        async with owner_sessionmaker() as db:
            await abandon_hr_creation(db, await db.get(HrCreationDraft, draft_id), actor_id=user_id, task=None)
            await db.commit()
    else:
        monkeypatch.setattr(manager.agent_manager, "archive_agent_files", unavailable)
        async with _client(owner_sessionmaker, user_id) as client:
            response = await client.delete(f"{path}/{draft_id}")
            assert response.status_code == 503, response.text
            assert response.json()["detail"]["code"] == "hr_abandon_cleanup_pending"
    monkeypatch.setattr(manager.agent_manager, "archive_agent_files", archive)

    async with _client(owner_sessionmaker, user_id) as client:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        rows = response.json()
        assert [row["blueprint_id"] for row in rows] == [str(draft_id)]
        assert rows[0]["draft_status"] == "superseded"
        assert rows[0]["recovery"]["cleanup_pending"] is True
        assert rows[0]["recovery"]["can_abandon"] is True
        assert rows[0]["recovery"]["can_retry"] is False
        response = await client.delete(f"{path}/{draft_id}")
        assert response.status_code == 200, response.text
        assert (await client.get(path)).json() == []
    assert not (tmp_path / str(employee_id)).exists()


async def test_superseded_hr_revision_cannot_authorize_cleanup_of_separately_deleted_employee(
    owner_sessionmaker, monkeypatch, tmp_path
):
    import app.services.agent_manager as manager

    _tenant_id, user_id, hr_agent_id, draft_id, employee_id = await _seed_hr_abandon_fixture(owner_sessionmaker)
    monkeypatch.setattr(manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    (tmp_path / str(employee_id)).mkdir()
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        draft.status = "superseded"
        employee = await db.get(Agent, employee_id)
        employee.deleted_at = datetime.now(UTC)
        await db.commit()
    async with _client(owner_sessionmaker, user_id) as client:
        path = f"/agents/{hr_agent_id}/hr-creation-drafts"
        assert (await client.get(path)).json() == []
        response = await client.delete(f"{path}/{draft_id}")
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "invalid_status"
    assert (tmp_path / str(employee_id)).exists()


async def test_hr_cleanup_inventory_keeps_the_combined_result_limit(owner_sessionmaker, monkeypatch, tmp_path):
    import app.services.agent_manager as manager

    tenant_id, user_id, hr_agent_id, draft_id, employee_id = await _seed_hr_abandon_fixture(owner_sessionmaker)
    monkeypatch.setattr(manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    (tmp_path / str(employee_id)).mkdir()
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        draft.status = "superseded"
        draft.failure_code = "abandoned_by_requester"
        employee = await db.get(Agent, employee_id)
        employee.deleted_at = datetime.now(UTC)
        active = HrCreationDraft(
            tenant_id=tenant_id,
            hr_agent_id=hr_agent_id,
            session_id=draft.session_id,
            requested_by_user_id=user_id,
            blueprint_hash="active-preview",
        )
        db.add(active)
        await db.commit()
        active_id = active.id
    async with _client(owner_sessionmaker, user_id) as client:
        path = f"/agents/{hr_agent_id}/hr-creation-drafts"
        response = await client.get(path, params={"limit": 1})
        assert response.status_code == 200, response.text
        assert [row["blueprint_id"] for row in response.json()] == [str(draft_id)]
        response = await client.get(path, params={"limit": 2})
        assert [row["blueprint_id"] for row in response.json()] == [str(draft_id), str(active_id)]
