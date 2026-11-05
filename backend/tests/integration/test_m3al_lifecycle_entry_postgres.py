"""Alternate lifecycle entries preserve real external work and expose recovery."""

from uuid import UUID, uuid4
from datetime import datetime, timezone

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError
from tests.integration import test_m3aj_employee_retirement_postgres as retirement

app_user_engine = retirement.app_user_engine
app_user_sessionmaker = retirement.app_user_sessionmaker
machine_api = retirement.machine_api
migrated_pg_url = retirement.migrated_pg_url
owner_engine = retirement.owner_engine
owner_sessionmaker = retirement.owner_sessionmaker
pg_container = retirement.pg_container
remote_work = retirement.remote_work


async def test_desktop_sub_agent_retires_through_the_main_deactivate_entry(
    remote_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import agent_manager

    seed, api, session_id = remote_work
    parent_id = uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=parent_id, tenant_id=seed["tenant"], creator_id=seed["owner"],
            owner_user_id=seed["owner"], name="Desktop parent"))
        await db.flush()
        agent = await db.get(Agent, seed["agent"])
        agent.parent_agent_id = parent_id
        await db.commit()
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    with retirement.withdrawal.remote_server() as remote:
        path = await retirement.remote_tasks.configure(seed, api, remote)
        remote.reply = lambda request: ({"id": "desktop-retirement", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "desktop-retirement", "status": {"state": "TASK_STATE_WORKING"}}})
        created = await api.post(path + "/tasks", json=retirement.remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        await retirement.remote_tasks.execute(seed, task_id, app_user_sessionmaker)
        rpc = RemoteA2AEndpoint.rpc

        async def lose_cancel(endpoint, method, *args, **kwargs):
            result = await rpc(endpoint, method, *args, **kwargs)
            if method == "CancelTask":
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")
            return result

        monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lose_cancel)
        deactivating = f"/agents/{seed['agent']}/deactivate"
        held = await api.post(deactivating)
        assert held.status_code == 409 and held.json()["detail"]["code"] == "agent_retirement_pending", held.text
        assert directory.is_dir()
        again = await api.post(deactivating)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "agent_retirement_pending", again.text
        ended = await api.post(path + f"/tasks/{task_id}/operations", json={
            **retirement.withdrawal.request_frame(seed, 1), "operation_id": str(uuid4()), "action": "abandon", "params": {}})
        assert ended.status_code == 200, ended.text
        removed = await api.post(deactivating)
        assert removed.status_code == 204, removed.text
        # G1: a settled deactivation keeps the employee and its files.
        assert directory.is_dir()
        assert (await api.post(deactivating)).status_code == 204
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, UUID(task_id))
            employee = await db.get(Agent, seed["agent"])
            assert employee.deactivated_at is not None and employee.deleted_at is None
            assert task.root_user_id == seed["owner"] and task.metadata_json["remote_a2a"]["abandoned"]
            assert task.metadata_json["remote_a2a"]["error_code"] == "remote_a2a_response_lost"
        assert [r[3]["method"] for r in remote.requests if r[0] == "POST"] == ["SendMessage", "CancelTask"]


async def test_hr_abandon_exposes_management_recovery_and_rolls_back_its_provisioning_fence(
    remote_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.api import hr_creation
    from app.models.chat_session import ChatSession
    from app.models.hr_creation import HrCreationDraft
    from app.services import agent_manager
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task
    from app.services.hr_creation_service import canonical_hr_blueprint_hash

    seed, api, session_id = remote_work
    api._transport.app.include_router(hr_creation.router)
    hr_id, hr_session_id, draft_id = uuid4(), uuid4(), uuid4()
    async with tenant_scoped_session(seed["tenant"], session_factory=owner_sessionmaker) as db:
        db.add(Agent(id=hr_id, tenant_id=seed["tenant"], creator_id=seed["owner"], owner_user_id=seed["owner"],
            sponsor_user_id=seed["owner"], agent_class="internal_system", name="__system_hr__", status="running"))
        await db.flush()
        db.add(ChatSession(id=hr_session_id, tenant_id=seed["tenant"], agent_id=hr_id, user_id=seed["owner"]))
        await db.flush()
        draft = HrCreationDraft(id=draft_id, tenant_id=seed["tenant"], hr_agent_id=hr_id, session_id=hr_session_id,
            requested_by_user_id=seed["owner"], status="failed", blueprint_version=1,
            blueprint_hash=canonical_hr_blueprint_hash({"name": "Interrupted employee"}),
            blueprint_json={"name": "Interrupted employee"}, created_agent_id=seed["agent"],
            confirmed_by_user_id=seed["owner"], confirmed_at=datetime.now(timezone.utc))
        db.add(draft)
        await db.flush()
        provisioning = build_hr_provisioning_runtime_task(draft)
        provisioning.claim_version = 4
        db.add(provisioning)
        await db.flush()
        draft.provisioning_task_id = provisioning.id
        provisioning_id = provisioning.id
        await db.commit()
    monkeypatch.setattr(agent_manager.settings, "AGENT_DATA_DIR", str(tmp_path))
    directory = tmp_path / str(seed["agent"])
    directory.mkdir()
    with retirement.withdrawal.remote_server() as remote:
        path = await retirement.remote_tasks.configure(seed, api, remote)
        # The remote worker never stops, so the abandon's deactivation commits
        # but its recall cannot settle: the typed pending state with the
        # governed management path comes back and nothing is deleted.
        remote.reply = lambda request: {"id": "hr-retirement", "status": {"state": "TASK_STATE_WORKING"}} \
            if request["method"] == "CancelTask" else {"task": {"id": "hr-retirement", "status": {"state": "TASK_STATE_WORKING"}}}
        created = await api.post(path + "/tasks", json=retirement.remote_tasks.command(seed, session_id))
        assert created.status_code == 201, created.text
        await retirement.remote_tasks.execute(seed, created.json()["id"], app_user_sessionmaker)
        deleting = f"/agents/{hr_id}/hr-creation-drafts/{draft_id}"
        held = await api.delete(deleting)
        assert held.status_code == 409 and held.json()["detail"]["code"] == "agent_retirement_pending", held.text
        assert directory.is_dir()
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            job = await db.get(RuntimeTask, provisioning_id)
            draft = await db.get(HrCreationDraft, draft_id)
            employee = await db.get(Agent, seed["agent"])
            # prepare's commit carries the fence and the deactivation; the
            # draft itself rolls back and stays abandonable.
            assert job.status == "killed" and job.claim_version == 4
            assert draft.status == "failed" and draft.provisioning_task_id == provisioning_id
            assert employee.deactivated_at is not None and employee.status == "stopped"
            assert employee.deleted_at is None
        assert held.json()["detail"]["recovery"] == {
            "employee_id": str(seed["agent"]), "management_path": "/enterprise/digital-employees",
            "can_manage_retirement": True, "retry_after_retirement": True,
        }
        # Once the remote settles its cancel, the retried abandon completes:
        # the employee stays deactivated — never deleted, files never moved.
        remote.reply = lambda request: ({"id": "hr-retirement", "status": {"state": "TASK_STATE_CANCELED"}}
            if request["method"] == "CancelTask" else {"task": {"id": "hr-retirement", "status": {"state": "TASK_STATE_WORKING"}}})
        retried = await api.delete(deleting)
        assert retried.status_code == 200 and retried.json()["draft_status"] == "superseded", retried.text
        assert directory.is_dir()
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            draft = await db.get(HrCreationDraft, draft_id)
            employee = await db.get(Agent, seed["agent"])
            assert draft.status == "superseded"
            assert employee.deactivated_at is not None and employee.deleted_at is None
        # No cleanup-only branch remains: a repeat abandon is an invalid status.
        repeat = await api.delete(deleting)
        assert repeat.status_code == 409 and repeat.json()["detail"]["error"] == "invalid_status", repeat.text
        assert directory.is_dir()
        methods = [r[3]["method"] for r in remote.requests if r[0] == "POST"]
        assert "SendMessage" in methods and "CancelTask" in methods
