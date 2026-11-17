"""Completed handoffs remain stoppable through the member's real session API."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionControlInput, SessionTurnInput
from app.models.user import User
from tests.integration import test_a4b_company_approval_resumption as approvals
from tests.integration import test_r11c_colleague_lifecycle_postgres as colleagues
from tests.integration import test_r29a_member_stop_colleague_continuation as stop_path

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _terminal(task):
    return (task.status, task.result_summary, task.completed_at,
        {key: value for key, value in task.metadata_json.items() if key.startswith("terminal_")})


@pytest.mark.parametrize("running", [False, True], ids=["queued-colleague", "running-colleague"])
async def test_handoff_active_api_and_cancel_cover_outstanding_colleague(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, running,
):
    seed, _parent, provider = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool="delegate_to_agent"
    )
    await colleagues._execute(seed["run_id"], owner_sessionmaker, worker="x8-parent")
    child = await colleagues._child(owner_sessionmaker, seed)
    assert child is not None
    async with owner_sessionmaker() as db:
        parent = await db.get(RuntimeTask, seed["run_id"])
        assert parent.status == "completed"
        terminal = _terminal(parent)
    before = await stop_path._counts(owner_sessionmaker, seed)
    worker = None
    if running:
        worker = asyncio.create_task(colleagues._execute(
            child.id, owner_sessionmaker, worker="x8-colleague", delegation=True
        ))
        await asyncio.wait_for(provider.started.wait(), timeout=20)
    app, headers = approvals._mounted_app(seed, app_user_sessionmaker, user_id=seed["user_id"])
    prefix = f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x8", headers=headers) as api:
            active = await api.get(f"{prefix}/runs/active")
            assert active.status_code == 200, active.text
            assert active.json() is not None, "The completed handoff must remain a stoppable active run"
            assert active.json()["run_id"] == seed["run_id"].hex
            assert active.json()["status"] == "running"
            workbench = await api.get(f"{prefix}/workbench")
            assert workbench.status_code == 200, workbench.text
            assert workbench.json()["controls"]["can_stop_active_run"] is True
            response = await api.post(f"{prefix}/runs/{seed['run_id']}/cancel",
                headers={"Idempotency-Key": f"x8-stop:{seed['run_id']}"})
            assert response.status_code == 200, response.text
            assert response.json()["accepted"] is True, response.json()
            if worker is not None:
                outcomes = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), timeout=20)
                assert isinstance(outcomes[0], asyncio.CancelledError)
                assert provider.cancelled.is_set()
            async with owner_sessionmaker() as db:
                parent = await db.get(RuntimeTask, seed["run_id"])
                stopped = await db.get(RuntimeTask, child.id)
                assert _terminal(parent) == terminal, "Stopping the handoff must not rewrite the parent's terminal record"
                assert parent.metadata_json["cancel_state"] == "cancelled"
                assert stopped.status == "killed"
            assert (await api.get(f"{prefix}/runs/active")).json() is None
            events = (await api.get(f"{prefix}/transcript", params={"schema_version": 2, "limit": 1000})).json()
            cancels = {event["item_id"] for event in events if event.get("item_kind") == "control_input"
                and event["lifecycle"] == "accepted" and event["payload"].get("kind") == "cancel_run"}
            applied = [event for event in events if event.get("item_kind") == "control_input"
                and event["lifecycle"] == "applied" and event["item_id"] in cancels]
            assert len(applied) == 1
            assert "kind" not in applied[0]["payload"], "Reuse the accepted control; do not add a persisted payload field"
            assert not any(event.get("item_kind") == "run" and event["lifecycle"] == "cancelled" for event in events)
            assert (await api.get(f"{prefix}/workbench")).json()["controls"]["can_stop_active_run"] is False
        receipt = await stop_path._deliver(owner_sessionmaker, app_user_sessionmaker,
            SimpleNamespace(seed=seed, child=child), continuation=False)
        assert receipt["consumer"] == "session_projection", receipt
        assert await stop_path._counts(owner_sessionmaker, seed) == before
        print(f"X8 {running=}: active=running can_stop=true; parent=completed child=killed; "
            "cancel=applied; late result recorded; no new parent")
    finally:
        provider.release.set()
        if worker is not None and not worker.done():
            await asyncio.wait_for(worker, timeout=20)


async def test_handoff_cancel_retains_member_session_and_latest_target_authorization(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed, _parent, _provider = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool="delegate_to_agent")
    await colleagues._execute(seed["run_id"], owner_sessionmaker, worker="x8-authority")
    other_id, other_session_id, latest_id = (uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(User(id=other_id, tenant_id=seed["tenant_id"], username=f"x8-{other_id.hex}",
            email=f"{other_id.hex}@x8.test", password_hash="x", display_name="Other member", role="member"))
        db.add(ChatSession(id=other_session_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            user_id=seed["user_id"], source_channel="web"))
        await db.commit()
    for user_id, session_id in [(other_id, seed["session_id"]), (seed["user_id"], other_session_id)]:
        app, headers = approvals._mounted_app(seed, app_user_sessionmaker, user_id=user_id)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x8", headers=headers) as api:
            response = await api.post(f"/api/agents/{seed['agent_id']}/sessions/{session_id}/runs/{seed['run_id']}/cancel")
            assert response.status_code in {403, 404} or response.json().get("accepted") is False, response.text
    async with owner_sessionmaker() as db:
        db.add(RuntimeTask(id=latest_id, tenant_id=seed["tenant_id"], task_type="web_chat_turn", status="completed",
            parent_agent_id=seed["agent_id"], parent_session_id=str(seed["session_id"]), root_user_id=seed["user_id"]))
        await db.commit()
    app, headers = approvals._mounted_app(seed, app_user_sessionmaker, user_id=seed["user_id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x8", headers=headers) as api:
        prefix = f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
        assert (await api.get(f"{prefix}/runs/active")).json() is None
        response = await api.post(f"{prefix}/runs/{seed['run_id']}/cancel")
        assert response.status_code == 200 and response.json()["accepted"] is False, response.text
    async with owner_sessionmaker() as db:
        child = await colleagues._child(owner_sessionmaker, seed)
        assert child.status == "pending"
        assert not (await db.get(RuntimeTask, seed["run_id"])).metadata_json.get("cancel_state")
    print("X8 authorization: another member, another session and stale target refused; colleague unchanged")


@pytest.mark.parametrize("caller", ["active_reader", "message", "workbench", "rewind", "legacy_steer", "payload"])
async def test_handoff_only_changes_stop_surfaces(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, caller,
):
    from app.services import session_command_runtime, web_chat_runtime

    seed, _parent, _provider = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool="delegate_to_agent")
    await colleagues._execute(seed["run_id"], owner_sessionmaker, worker="x8-caller")
    child = await colleagues._child(owner_sessionmaker, seed)
    if caller == "message":
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

        processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker)
        delivered = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="x8-parent-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert delivered["delivered"] == 1, delivered
    async with owner_sessionmaker() as db:
        if caller == "active_reader":
            assert await web_chat_runtime.get_active_web_chat_run(
                db=db, agent_id=seed["agent_id"], session_id=seed["session_id"]) is None
        elif caller == "rewind":
            cancel = AsyncMock()
            monkeypatch.setattr(session_command_runtime, "submit_live_cancel_input", cancel)
            result = await session_command_runtime._prepare_rewind_mutation(
                db=db, agent=await db.get(Agent, seed["agent_id"]), user=await db.get(User, seed["user_id"]),
                session=await db.get(ChatSession, seed["session_id"]), arguments={}, observed_last_sequence=0)
            assert result["interrupted_active_run"] is False
            cancel.assert_not_awaited()
    app, headers = approvals._mounted_app(seed, app_user_sessionmaker, user_id=seed["user_id"])
    prefix = f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x8", headers=headers) as api:
        if caller == "message":
            input_id = uuid4()
            response = await api.post(f"{prefix}/runs", json={"content": "Start a separate request.",
                "input_id": str(input_id), "idempotency_key": f"x8-new:{input_id}"})
            assert response.status_code == 201, response.text
            assert response.json()["run_id"] != seed["run_id"].hex
            async with owner_sessionmaker() as db:
                item = await db.get(SessionTurnInput, input_id)
                assert item.intent == "start_turn"
                assert (await db.get(RuntimeTask, child.id)).status == "pending"
        elif caller == "workbench":
            response = await api.get(f"{prefix}/workbench")
            assert response.status_code == 200, response.text
            controls = response.json()["controls"]
            assert controls["can_start_turn"] is True
            assert controls["can_start_goal"] is True
            assert controls["can_stop_active_run"] is True
            assert response.json()["active_run"]["run_id"] == seed["run_id"].hex
            assert response.json()["active_turn"] is None
        elif caller == "legacy_steer":
            response = await api.post(f"{prefix}/turns/steer", json={"content": "A new instruction."})
            assert response.status_code == 404, response.text
        elif caller == "payload":
            response = await api.get(f"{prefix}/runs/active")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["status"] == "running"
            assert payload["completed_at"] is None
            assert "result_summary" not in payload
            assert "terminal_reason" not in payload


async def test_legacy_handoff_without_terminal_fence_is_not_stoppable(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed, _parent, _provider = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool="delegate_to_agent")
    await colleagues._execute(seed["run_id"], owner_sessionmaker, worker="x8-legacy")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        task.metadata_json = {key: value for key, value in task.metadata_json.items()
            if key != "terminal_execution_fence_ref"}
        await db.commit()
    app, headers = approvals._mounted_app(seed, app_user_sessionmaker, user_id=seed["user_id"])
    prefix = f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x8", headers=headers) as api:
        assert (await api.get(f"{prefix}/runs/active")).json() is None
        response = await api.post(f"{prefix}/runs/{seed['run_id']}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is False
    async with owner_sessionmaker() as db:
        assert not (await db.get(RuntimeTask, seed["run_id"])).metadata_json.get("cancel_state")
        assert (await colleagues._child(owner_sessionmaker, seed)).status == "pending"


async def test_stale_handoff_cancel_backs_off_after_a_newer_turn(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from tests.services.test_session_input_control_v2 import _authority
    from app.services.session_control_input import (
        accept_cancel_control_input, begin_cancel_control_input, recover_stale_cancel_control_inputs_once,
    )

    seed, _parent, _provider = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool="delegate_to_agent")
    await colleagues._execute(seed["run_id"], owner_sessionmaker, worker="x8-stale")
    control_id = uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=seed["user_id"], agent_id=seed["agent_id"], session_id=seed["session_id"])
        accepted = await accept_cancel_control_input(db, authority=authority, control_id=control_id,
            idempotency_key=f"x8-stale:{control_id}", expected_run_id=seed["run_id"])
        await begin_cancel_control_input(db, authority=authority, control_id=control_id, worker_id="lost-stop")
        command = await db.get(SessionCommand, accepted.command_id)
        command.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.add(RuntimeTask(id=uuid4(), tenant_id=seed["tenant_id"], task_type="web_chat_turn", status="completed",
            parent_agent_id=seed["agent_id"], parent_session_id=str(seed["session_id"]), root_user_id=seed["user_id"]))
        await db.commit()
        first = await recover_stale_cancel_control_inputs_once(db, worker_id="x8-recovery",
            tenant_id=seed["tenant_id"], stale_after=timedelta(minutes=1))
        assert first["claimed"] == 1 and first["settled"] == 0
        second = await recover_stale_cancel_control_inputs_once(db, worker_id="x8-recovery",
            tenant_id=seed["tenant_id"], stale_after=timedelta(minutes=1))
        assert second["claimed"] == 0, "An outstanding old handoff must use the existing recovery backoff"
        child = await colleagues._child(owner_sessionmaker, seed)
        child_row = await db.get(RuntimeTask, child.id)
        child_row.status = "killed"
        child_row.completed_at = datetime.now(timezone.utc)
        command = await db.get(SessionCommand, accepted.command_id, populate_existing=True)
        command.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await db.commit()
        final = await recover_stale_cancel_control_inputs_once(db, worker_id="x8-recovery",
            tenant_id=seed["tenant_id"], stale_after=timedelta(minutes=1))
        assert final["settled"] == 1
        assert (await db.get(SessionControlInput, control_id, populate_existing=True)).status == "applied"
