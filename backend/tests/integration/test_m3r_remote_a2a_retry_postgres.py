"""Only locally proven pre-HTTP denials permit an explicit saved-operation retry."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.database import tenant_scoped_session
from app.models.agent import Agent
from tests.integration import test_m3o_remote_a2a_tasks_postgres as original
from tests.integration.test_m3n_remote_a2a_connections_postgres import request_frame
from tests.services.test_remote_a2a_transport import remote_server

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("action", ["initial", "send_message", "refresh", "cancel"])
async def test_local_authority_repair_retries_exact_saved_operation_in_original_work(
    remote_work, app_user_sessionmaker, action
):
    seed, api, session_id = remote_work
    with remote_server() as remote:
        path = await original.configure(seed, api, remote)
        remote.reply = lambda request: ({"task": {"id": "existing-task", "contextId": "existing-context",
            "status": {"state": "TASK_STATE_WORKING"}}} if request["method"] == "SendMessage"
            else {"id": "existing-task", "contextId": "existing-context", "status": {"state": "TASK_STATE_COMPLETED"}})
        created = await api.post(path + "/tasks", json=original.command(seed, session_id))
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        operations_path = path + f"/tasks/{task_id}/operations"
        if action != "initial":
            await original.execute(seed, task_id, app_user_sessionmaker)
            body = {**request_frame(seed, 1), "operation_id": str(uuid4()), "action": action}
            if action == "send_message":
                body["params"] = original.message("Complete unchanged follow-up.", task_id="existing-task", context_id="existing-context")
            queued = await api.post(operations_path, json=body)
            assert queued.status_code == 200, queued.text
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            agent.deactivated_at = datetime.now(timezone.utc)
            await db.commit()
        count = len(remote.requests)
        await original.execute(seed, task_id, app_user_sessionmaker)
        denied = (await api.get(path + "/tasks/" + task_id)).json()
        assert denied["status"] == "needs_reconciliation" and denied["operations"][-1]["phase"] == "not_started"
        assert len(remote.requests) == count
        assert "retry" in denied["recovery_actions"], denied
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            agent = await db.get(Agent, seed["agent"])
            agent.deactivated_at = None
            await db.commit()
        retry = {**request_frame(seed, 1), "operation_id": str(uuid4()), "action": "retry"}
        queued = await api.post(operations_path, json=retry)
        assert queued.status_code == 200, queued.text
        saved = queued.json()
        assert saved["id"] == task_id and saved["source_session_id"] == str(session_id) and saved["status"] == "pending"
        assert saved["operations"][:-1] == denied["operations"]
        previous, latest = saved["operations"][-2:]
        assert latest["retry_of"] == previous["id"] and latest["params"] == previous["params"]
        assert latest["method"] == previous["method"] and latest["request_hash"] == previous["request_hash"]
        assert (await api.post(operations_path, json=retry)).json()["operations"] == saved["operations"]
        assert (await api.post(operations_path, json={**retry, "params": {"changed": True}})).status_code == 422
        await original.execute(seed, task_id, app_user_sessionmaker)
        completed = (await api.get(path + "/tasks/" + task_id)).json()
        assert completed["operations"][-1]["phase"] == "received" and "retry" not in completed["recovery_actions"]
        assert len(remote.requests) == count + 1
        assert (await api.post(operations_path, json=retry)).json()["operations"] == completed["operations"]
        assert len(remote.requests) == count + 1


async def test_original_work_ui_repairs_and_retries_through_real_http_and_worker(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path
):
    import asyncio
    from fastapi import FastAPI
    from sqlalchemy import select
    from app.api import agents
    from app.database import get_db
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_task_worker
    from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

    seed, api, _session_id = remote_work
    web = FastAPI()
    web.include_router(agents.router, prefix="/api")
    async def dependency():
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            yield db
    web.dependency_overrides[get_db] = dependency
    queued, finished = asyncio.Queue(), asyncio.Queue()
    failures = []
    async def wake(**kwargs):
        queued.put_nowait(str(kwargs["runtime_task_id"]))
    async def consume():
        for attempt in range(2):
            try:
                task_id = await asyncio.wait_for(queued.get(), 30)
                if attempt == 0:
                    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                        (await db.get(Agent, seed["agent"])).deactivated_at = datetime.now(timezone.utc)
                        await db.commit()
                await original.execute(seed, task_id, app_user_sessionmaker)
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.put_nowait(True)
            if failures:
                return
    @web.get("/__remote-test/wait")
    async def wait_for_worker():
        await asyncio.wait_for(finished.get(), 20)
        assert not failures, failures
        return {"worker_finished": True}
    @web.post("/__remote-test/repair")
    async def repair():
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant"],
                RuntimeTask.task_type == "remote_a2a"))
            assert task.status == "needs_reconciliation" and task.metadata_json["remote_a2a"]["operations"][-1]["phase"] == "not_started"
            (await db.get(Agent, seed["agent"])).deactivated_at = None
            await db.commit()
        return {"repaired": True}
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", wake)
    with remote_server() as remote:
        await original.configure(seed, api, remote)
        remote.reply = lambda request: {"task": {"id": "repaired-task", "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "report", "parts": [{"text": "NATIVE_REMOTE_FULL_RESULT\n" * 4000}]}]}}
        worker = asyncio.create_task(consume())
        try:
            await _exercise_dom_bridge(web, {"user_id": seed["owner"], "tenant_id": seed["tenant"], "agent_id": seed["agent"]},
                tmp_path, entry_file="e2e/remote-a2a-work-native.tsx",
                extra_env={"HIVE_M3N_TOKEN": seed["user_token"], "HIVE_M3O_SESSION_ID": str(_session_id), "HIVE_M3R_RETRY": "1"})
            await asyncio.wait_for(worker, 5)
            assert not failures
            posts = [row[3] for row in remote.requests if row[0] == "POST"]
            assert len(posts) == 1 and posts[0]["params"]["message"]["parts"][0]["text"] == "Prepare the complete remote report."
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                task = await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant"], RuntimeTask.task_type == "remote_a2a"))
                assert task.status == "completed" and len(task.metadata_json["remote_a2a"]["operations"]) == 2
        finally:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
