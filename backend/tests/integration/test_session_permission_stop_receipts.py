"""A stopped run preserves the true outcome of its already-started tool."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import Response

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("run_status", ["suspended", "resumable"])
@pytest.mark.parametrize("cancel_started", [False, True])
@pytest.mark.parametrize("expired_lease", [False, True])
async def test_cancelled_waiting_run_settles_without_a_live_executor(
    owner_sessionmaker, app_user_sessionmaker, run_status, cancel_started, expired_lease,
):
    from sqlalchemy import func, select

    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionControlInput
    from app.models.user import User
    from app.services.session_control_input import (
        accept_cancel_control_input, begin_cancel_control_input, recover_stale_cancel_control_inputs_once,
    )
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.services.web_chat_runtime import get_active_web_chat_run

    seed = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.status = run_status
        task.claimed_by = task.claim_expires_at = None
        if expired_lease:
            task.claimed_by = "expired-worker"
            task.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            task.claim_version = 7
        await db.commit()
    signals = []

    async def no_executor(**kwargs):
        signals.append(kwargs)

    control_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        user = await db.get(User, seed.user_id)
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=seed.agent_id,
            session_id=seed.session_id, action="mutate_session_control")
        accepted = await accept_cancel_control_input(db, authority=authority, control_id=control_id,
            idempotency_key=f"stop-wait:{control_id}", expected_run_id=seed.run_id)
        if cancel_started:
            await begin_cancel_control_input(db, authority=authority, control_id=control_id, worker_id="lost-ingress")
        await db.commit()
        for _ in range(2):
            await pin_rls_tenant_context(db, seed.tenant_id)
            await recover_stale_cancel_control_inputs_once(db, worker_id="restart-worker",
                stale_after=timedelta(0), tenant_id=seed.tenant_id, signal_callback=no_executor)
        await pin_rls_tenant_context(db, seed.tenant_id)
        task = await db.get(RuntimeTask, seed.run_id, populate_existing=True)
        control = await db.get(SessionControlInput, control_id, populate_existing=True)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0], populate_existing=True)
        assert task.status == "killed" and task.completed_at is not None
        assert task.claimed_by is None and task.claim_expires_at is None
        assert task.claim_version > (7 if expired_lease else 0)
        assert task.metadata_json["terminal_execution_fence_ref"].startswith("runtime-task-terminal:")
        assert control.status == "applied" and signals == []
        assert invocation.effect_state == "prepared_not_started" and invocation.result_event_id is None
        assert await get_active_web_chat_run(db=db, agent_id=seed.agent_id, session_id=seed.session_id) is None
        for event_type in ("run.cancelled", "control_input.applied"):
            count = await db.scalar(select(func.count()).select_from(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == seed.session_id, ChatTranscriptEvent.event_type == event_type))
            assert count == 1
        assert accepted.command_id == control.command_id


@pytest.mark.parametrize("lease_expiry", ["live", "unknown"])
async def test_waiting_cancel_does_not_take_over_an_existing_executor_lease(
    owner_sessionmaker, app_user_sessionmaker, lease_expiry,
):
    from app.models.session_v2 import SessionControlInput
    from app.models.user import User
    from app.services.session_control_input import accept_cancel_control_input, recover_stale_cancel_control_inputs_once
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    seed = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.status = "suspended"
        task.claimed_by = "existing-executor"
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5) if lease_expiry == "live" else None
        await db.commit()
    signalled = []

    async def signal(**kwargs):
        signalled.append(kwargs["run_id"])

    control_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        user = await db.get(User, seed.user_id)
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=seed.agent_id,
            session_id=seed.session_id, action="mutate_session_control")
        await accept_cancel_control_input(db, authority=authority, control_id=control_id,
            idempotency_key=f"stop-owned:{control_id}", expected_run_id=seed.run_id)
        await db.commit()
        await pin_rls_tenant_context(db, seed.tenant_id)
        await recover_stale_cancel_control_inputs_once(db, worker_id="another-worker", stale_after=timedelta(0),
            tenant_id=seed.tenant_id, signal_callback=signal)
        await pin_rls_tenant_context(db, seed.tenant_id)
        task = await db.get(RuntimeTask, seed.run_id, populate_existing=True)
        control = await db.get(SessionControlInput, control_id, populate_existing=True)
        assert task.status == "suspended" and task.completed_at is None
        assert task.claimed_by == "existing-executor" and control.status == "applying"
        assert signalled == [seed.run_id]


async def test_cancel_recovery_refreshes_a_claim_that_arrived_after_its_cached_snapshot(
    owner_sessionmaker, app_user_sessionmaker,
):
    from app.models.user import User
    from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority
    from app.services.session_control_input import accept_cancel_control_input, recover_stale_cancel_control_inputs_once
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    seed = await _seed_waiting_batch(owner_sessionmaker)
    signalled = []

    async def signal(**kwargs):
        signalled.append(kwargs["run_id"])

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        user = await db.get(User, seed.user_id)
        authority = await resolve_session_mutation_authority(db, user=user, agent_id=seed.agent_id,
            session_id=seed.session_id, action="mutate_session_control")
        control_id = uuid.uuid4()
        await accept_cancel_control_input(db, authority=authority, control_id=control_id,
            idempotency_key=f"stop-cache:{control_id}", expected_run_id=seed.run_id)
        cached = await db.get(RuntimeTask, seed.run_id)
        assert cached.status == "suspended" and cached.claimed_by is None
        await db.commit()
        async with owner_sessionmaker() as writer:
            task = await lock_runtime_task_with_session_authority(writer, task_id=seed.run_id)
            task.status = "running"
            task.claimed_by = "new-executor"
            task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            task.claim_version += 1
            await writer.commit()
        assert cached.status == "suspended"
        await pin_rls_tenant_context(db, seed.tenant_id)
        await recover_stale_cancel_control_inputs_once(db, worker_id="cached-recovery", stale_after=timedelta(0),
            tenant_id=seed.tenant_id, signal_callback=signal)
        await pin_rls_tenant_context(db, seed.tenant_id)
        task = await db.get(RuntimeTask, seed.run_id, populate_existing=True)
        assert task.status == "running" and task.claimed_by == "new-executor" and task.completed_at is None
        assert signalled == [seed.run_id]


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
