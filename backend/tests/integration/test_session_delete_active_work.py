"""A conversation deletion cannot orphan recoverable permission work."""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_delete_preserves_the_original_permission_and_its_recovery(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import runtime_task_worker

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    seed = await _seed_waiting_batch(owner_sessionmaker)
    path = f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}"
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        deleted = await client.delete(path)
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            session = await db.get(ChatSession, seed.session_id)
            task = await db.get(RuntimeTask, seed.run_id)
            invocations = await db.scalar(select(func.count()).select_from(SessionToolInvocation).where(
                SessionToolInvocation.run_id == seed.run_id))
            (tmp_path / "delete-waiting-work-observation.json").write_text(json.dumps({
                "response_status": deleted.status_code, "response": deleted.text,
                "session_exists": session is not None, "run_status": task.status,
                "invocation_count": invocations,
            }, indent=2) + "\n")
            assert deleted.status_code == 409, deleted.text
            assert deleted.json()["detail"]["code"] == "session_work_unsettled"
            assert session is not None and task.status == "suspended" and invocations == 1
        resolved = await client.post(path + f"/permissions/{seed.permission_ids[0]}/resolve", json={"action": "deny"})
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["run_id"] == str(seed.run_id)
        assert resolved.json()["run_status"] == "resumable" and resolved.json()["result_event_id"]
        assert (await client.delete(path)).status_code == 409


async def test_delete_refuses_inflight_work_without_disrupting_the_original_effect(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services import agent_tools, runtime_task_worker
    from app.services.approval_ticket import hash_tool_input

    seed = await _seed_waiting_batch(owner_sessionmaker)
    paused, release = asyncio.Event(), asyncio.Event()
    effects = tmp_path / "effects.txt"

    async def execute(tool_name, arguments, **kwargs):
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        with effects.open("a") as out:
            out.write("one effect\n")
        paused.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        kwargs["trace_metadata_sink"].update({
            "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "local-delete-control",
                              "outcome": "allow", "input_hash": hash_tool_input(tool_name, arguments),
                              "policy_snapshot_hash": "a" * 64},
            "tool_execution_frame": {"status": "completed", "output_hash": "b" * 64},
        })
        return "Original local result"

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    path = f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}"
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = asyncio.create_task(client.post(path + f"/permissions/{seed.permission_ids[0]}/resolve",
                                               json={"action": "allow_once"}))
        try:
            await asyncio.wait_for(paused.wait(), timeout=15)
            deleted = await asyncio.wait_for(client.delete(path), timeout=5)
            assert deleted.status_code == 409, deleted.text
            assert not first.done() and effects.read_text().splitlines() == ["one effect"]
        finally:
            release.set()
            result = await asyncio.wait_for(first, timeout=15)
        assert result.status_code == 200, result.text
        assert result.json()["run_id"] == str(seed.run_id) and result.json()["run_status"] == "resumable"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        assert invocation.effect_state == "effect_committed" and invocation.result_event_id
        assert await db.get(ChatSession, seed.session_id) is not None
        assert effects.read_text().splitlines() == ["one effect"]
