"""A stopped run preserves the true outcome of its already-started tool."""

import asyncio
import json

import pytest
from httpx import Response

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("stop_phase,delete_while_pending", [
    ("preflight", False), ("executing", False), ("executing", True), ("lost_owner", True),
])
async def test_stopped_permission_execution_keeps_its_original_receipt_without_resuming(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, stop_phase, delete_while_pending,
):
    from app.services import agent_tools, runtime_task_service, runtime_task_worker
    from app.services.approval_ticket import hash_tool_input

    seed = await _seed_waiting_batch(owner_sessionmaker)
    paused, release = asyncio.Event(), asyncio.Event()
    effects = tmp_path / "effects.txt"
    notifications = []

    async def execute(tool_name, arguments, **kwargs):
        if stop_phase == "preflight":
            paused.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        with effects.open("a") as out:
            out.write("one effect\n")
        if stop_phase in {"executing", "lost_owner"}:
            paused.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        kwargs["trace_metadata_sink"].update({
            "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "local-stopped-effect",
                              "outcome": "allow", "input_hash": hash_tool_input(tool_name, arguments),
                              "policy_snapshot_hash": "a" * 64},
            "tool_execution_frame": {"status": "completed", "output_hash": "b" * 64},
        })
        return "Known original local result"

    async def notify(**_kwargs):
        notifications.append(_kwargs)

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    monkeypatch.setattr(runtime_task_service, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    path = f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}"
    deleted = None
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = asyncio.create_task(client.post(path + f"/permissions/{seed.permission_ids[0]}/resolve",
                                               json={"action": "allow_once"}))
        try:
            await asyncio.wait_for(paused.wait(), timeout=15)
            assert await runtime_task_service.update_runtime_task_record(str(seed.run_id), status="killed")
            if stop_phase == "lost_owner":
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            if delete_while_pending:
                deleted = await asyncio.wait_for(client.delete(path), timeout=5)
        finally:
            release.set()
            result = (await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), timeout=15))[0]
        original_result = result
        if stop_phase == "lost_owner":
            result = await client.post(path + f"/permissions/{seed.permission_ids[0]}/resolve",
                                       json={"action": "allow_once"})
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            task = await db.get(RuntimeTask, seed.run_id)
            invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
            session = await db.get(ChatSession, seed.session_id)
            (tmp_path / "stopped-effect-observation.json").write_text(json.dumps({
                "deleted_status": deleted.status_code if deleted is not None else None,
                "original_response": {"status": original_result.status_code, "body": original_result.text}
                    if isinstance(original_result, Response) else {"exception": type(original_result).__name__, "message": str(original_result)},
                "recovery_response": {"status": result.status_code, "body": result.text}
                    if stop_phase == "lost_owner" else None,
                "run_status": task.status, "session_exists": session is not None,
                "effect_state": invocation.effect_state if invocation else None,
                "result_event_id": str(invocation.result_event_id) if invocation else None,
                "effects": effects.read_text().splitlines() if effects.exists() else [],
            }, indent=2) + "\n")
            if deleted is not None:
                assert deleted.status_code == 409, deleted.text
            expected_effects = [] if stop_phase == "preflight" else ["one effect"]
            assert (effects.read_text().splitlines() if effects.exists() else []) == expected_effects
            assert isinstance(result, Response), repr(result)
            assert result.status_code == 200, result.text
            expected_status = "needs_reconciliation" if stop_phase == "lost_owner" else "killed"
            assert result.json()["run_id"] == str(seed.run_id) and result.json()["run_status"] == expected_status
            assert notifications == [] and task.status == expected_status and session is not None
            assert any(entry.get("status") == "killed" and entry.get("schema") == "runtime_completion_journal.v1"
                       for entry in task.metadata_json["completion_journal"])
            assert invocation is not None
            if stop_phase == "lost_owner":
                assert result.json()["status"] == "needs_reconciliation"
                assert result.json()["recovery_action"] == "reconcile_tool_effect"
                assert invocation.effect_state == "effect_started" and invocation.result_event_id is None
                assert result.json()["result_event_id"] is None
            else:
                assert result.json()["recovery_action"] is None
                assert invocation.effect_state == ("prepared_not_started" if stop_phase == "preflight" else "effect_committed")
                assert str(invocation.result_event_id) == result.json()["result_event_id"]


@pytest.mark.parametrize("tool_count", [1, 2])
async def test_channel_stop_during_permission_execution_reports_the_stopped_run(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool_count,
):
    from app.models.user import User
    from app.services import agent_tools, runtime_task_service, runtime_task_worker
    from app.services.approval_ticket import hash_tool_input
    from app.services.channel_agent_runtime import try_resolve_channel_session_permission_from_text

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_count=tool_count)
    paused, release = asyncio.Event(), asyncio.Event()
    notifications = []

    async def execute(tool_name, arguments, **kwargs):
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        paused.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        kwargs["trace_metadata_sink"].update({
            "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "local-channel-stop",
                              "outcome": "allow", "input_hash": hash_tool_input(tool_name, arguments),
                              "policy_snapshot_hash": "a" * 64},
            "tool_execution_frame": {"status": "completed", "output_hash": "b" * 64},
        })
        return "Known original channel result"

    async def notify(**kwargs):
        notifications.append(kwargs)

    async def resolve():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            user = await db.get(User, seed.user_id)
            return await try_resolve_channel_session_permission_from_text(db=db, agent_id=seed.agent_id,
                user=user, user_text=f"/allow {seed.permission_ids[0]}", session_id=str(seed.session_id), session_source="feishu")

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    monkeypatch.setattr(runtime_task_service, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    pending = asyncio.create_task(resolve())
    try:
        await asyncio.wait_for(paused.wait(), timeout=15)
        assert await runtime_task_service.update_runtime_task_record(str(seed.run_id), status="killed")
    finally:
        release.set()
        reply = await asyncio.wait_for(pending, timeout=15)
    (tmp_path / "stopped-channel-reply.txt").write_text(reply)
    assert "原运行已结束" in reply and "不会续跑" in reply, reply
    assert "将从同一工具轮继续" not in reply and "将继续处理" not in reply, reply
    assert notifications == []
    if tool_count == 2:
        assert "其他权限" in reply, reply
