"""A selected-company requester consumes its saved remote result exactly once."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import tenant_scoped_session
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.user import User
from tests.integration.test_m3p_remote_artifacts_postgres import artifact_work, packet, recovery_frame  # noqa: F401
from tests.integration.test_m3o_remote_a2a_tasks_postgres import command, configure, execute
from tests.integration.test_m3o_remote_a2a_tasks_postgres import machine_api, remote_work  # noqa: F401
from tests.services.test_remote_a2a_transport import remote_server

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
BODY = "Saved remote bytes for this selected-company requester."


@pytest.mark.parametrize("home_kind", ["tenantless", "foreign_home"])
async def test_selected_company_requester_recovers_and_downloads_its_remote_artifact(
    artifact_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, home_kind,  # noqa: F811
):
    seed, api, session_id = artifact_work
    subject = uuid4()
    home = uuid4() if home_kind == "foreign_home" else None
    # Keep the employee's existing company owner; the work requester is a
    # different authenticated platform account selecting this company.
    async with owner_sessionmaker() as db:
        if home is not None:
            db.add(Tenant(id=home, name="Requester home", slug=f"requester-{home.hex}"))
            await db.flush()
        db.add(User(id=subject, tenant_id=home, username=f"requester-{subject.hex}",
            email=f"{subject}@fixture.test", display_name="Selected company requester",
            password_hash="synthetic-unused", role="platform_admin", is_active=True))
        await db.flush()
        (await db.get(ChatSession, session_id)).user_id = subject
        await db.commit()
    monkeypatch.setattr("app.services.audit_logger.async_session", app_user_sessionmaker)
    selected = {**seed, "owner": subject}
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        api.headers.update({"Authorization": "Bearer " + create_access_token(str(subject), "platform_admin",
            str(home) if home else None), "X-Tenant-Id": str(seed["tenant"])})
        remote.reply = lambda _request: packet(BODY)
        queued = await api.post(path + "/tasks", json=command(selected, session_id))
        assert queued.status_code == 201, queued.text
        task_id = queued.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        observed = await api.get(path + "/tasks/" + task_id)
        assert observed.status_code == 200 and observed.json()["status"] == "completed", observed.text
        assert len(remote.requests) == 2
        from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

        delivery = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="selected-company-remote-return")
        assert delivery["claimed"] == delivery["delivered"] == 1, delivery
        returned = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(selected))
        assert returned.status_code == 200 and len(returned.json()["parts"]) == 1, returned.text
        artifact_id = UUID(returned.json()["parts"][0]["artifact_id"])
        repeated = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(selected))
        assert repeated.status_code == 200 and repeated.json() == returned.json(), repeated.text
        assert len(remote.requests) == 2
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            artifacts = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == UUID(task_id))))
            assert len(artifacts) == 1 and artifacts[0].id == artifact_id
            assert (artifacts[0].tenant_id, artifacts[0].session_id, artifacts[0].owner_user_id) == (
                seed["tenant"], session_id, subject)
        url = f"/agents/{seed['agent']}/files/artifacts/{artifact_id}"
        content = await api.get(url + "/content")
        download = await api.get(url + "/download")
        assert content.status_code == 200 and content.json()["content"] == BODY, content.text
        assert content.json()["operator_view"] is False
        assert download.status_code == 200 and download.content == BODY.encode(), download.text
        async with owner_sessionmaker() as db:
            user = await db.get(User, subject)
            assert (user.tenant_id, user.role) == (home, "platform_admin")
            user.role = "member"
            await db.commit()
        for suffix in ("/content", "/download"):
            denied = await api.get(url + suffix)
            assert denied.status_code in {403, 404} and BODY.encode() not in denied.content, denied.text
        denied = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(selected))
        assert denied.status_code in {403, 404} and BODY.encode() not in denied.content, denied.text
        assert len(remote.requests) == 2
    async with owner_sessionmaker() as db:
        user = await db.get(User, subject)
        assert (user.tenant_id, user.role) == (home, "member")
