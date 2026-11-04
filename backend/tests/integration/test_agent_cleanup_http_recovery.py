"""Authenticated route consumption of committed HR-creation cleanup and retries.

G1 removed the bot DELETE endpoint and its cleanup-pending inventory; the
HTTP cleanup retry surface that remains is the HR abandon route.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.user import User
from tests.integration.test_agent_delete_conflict_recovery import (
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
