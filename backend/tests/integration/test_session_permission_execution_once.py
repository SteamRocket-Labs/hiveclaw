"""Mounted permission retries with real PostgreSQL and a local synthetic executor."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text

from app.api.chat_sessions import router
from app.core.security import create_access_token
from app.database import get_db, pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionControlInput, SessionToolInvocation
from tests.services.test_session_permission_runtime import _event_count, _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _mounted_client(sessionmaker, seed):
    async def database():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, seed.tenant_id)
            role = (await db.execute(text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ))).one()
            assert tuple(role) == (False, False)
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = database
    token = create_access_token(str(seed.user_id), "member", tenant_id=str(seed.tenant_id))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://permission",
                       headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("paused_phase", ["preflight", "executing"])
async def test_original_permission_api_retry_does_not_repeat_a_pending_tool(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, paused_phase,
):
    from app.services import agent_tools, runtime_task_worker
    from app.services.approval_ticket import hash_tool_input

    seed = await _seed_waiting_batch(owner_sessionmaker)
    release_preflight, release_effect = asyncio.Event(), asyncio.Event()
    first_paused, another_entry = asyncio.Event(), asyncio.Event()
    entries: list[str] = []
    effect_file: Path = tmp_path / "effects.txt"

    async def execute(tool_name, arguments, **kwargs):
        entries.append(str(kwargs["tool_call_id"]))
        first = len(entries) == 1
        if not first:
            another_entry.set()
        if first and paused_phase == "preflight":
            first_paused.set()
            await asyncio.wait_for(release_preflight.wait(), timeout=15)
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        with effect_file.open("a") as out:
            out.write("one effect\n")
        if first and paused_phase == "executing":
            first_paused.set()
        await asyncio.wait_for(release_effect.wait(), timeout=15)
        kwargs["trace_metadata_sink"].update({
            "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "local-permitted-effect",
                              "outcome": "allow", "input_hash": hash_tool_input(tool_name, arguments),
                              "policy_snapshot_hash": "a" * 64},
            "tool_execution_frame": {"status": "completed", "output_hash": "b" * 64},
        })
        return "Original local execution result"

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    path = (f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}"
            f"/permissions/{seed.permission_ids[0]}/resolve")
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = asyncio.create_task(client.post(path, json={"action": "allow_once"}))
        replay = observed = None
        waiting_receipt = None
        try:
            await asyncio.wait_for(first_paused.wait(), timeout=15)
            replay = asyncio.create_task(client.post(path, json={"action": "allow_once"}))
            observed = asyncio.create_task(another_entry.wait())
            done, _pending = await asyncio.wait({replay, observed}, timeout=10,
                                              return_when=asyncio.FIRST_COMPLETED)
            assert done, "The repeated permission response neither returned nor reached its boundary"
            if replay.done():
                response = replay.result()
                assert response.status_code == 200, response.text
                waiting_receipt = response.json()
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed.tenant_id)
                invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
                task = await db.get(RuntimeTask, seed.run_id)
                assert invocation.result_event_id is None and task.status == "suspended"
        finally:
            release_preflight.set()
            release_effect.set()
            tasks = [task for task in (first, replay) if task is not None]
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            if observed is not None:
                observed.cancel()
                await asyncio.gather(observed, return_exceptions=True)
        (tmp_path / "effect-observation.json").write_text(json.dumps({
            "entries": entries, "effects": effect_file.read_text().splitlines(),
            "responses": [{"status": r.status_code, "body": r.json()} for r in responses],
            "waiting_receipt": waiting_receipt,
        }, indent=2) + "\n")
        assert effect_file.read_text().splitlines() == ["one effect"]
        assert len(entries) == 1
        assert waiting_receipt is not None and waiting_receipt["status"] == "awaiting_tool_result"
        assert waiting_receipt["result_event_id"] is None and waiting_receipt["retryable"] is False
        assert waiting_receipt["run_id"] == str(seed.run_id)
        assert waiting_receipt["recovery_action"] == "read_tool_result"
        assert responses[0].status_code == 200 and responses[0].json()["status"] == "resolved"
        repeated = await client.post(path, json={"action": "allow_once"})
        assert repeated.status_code == 200 and repeated.json()["status"] == "resolved"
        assert repeated.json()["result_event_id"] == responses[0].json()["result_event_id"]
        assert len(entries) == 1 and effect_file.read_text().splitlines() == ["one effect"]

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        task = await db.get(RuntimeTask, seed.run_id)
        assert invocation.effect_state == "effect_committed" and invocation.result_event_id is not None
        assert task.status == "resumable"
        assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 1
        assert await db.scalar(select(func.count()).select_from(SessionControlInput).where(
            SessionControlInput.session_id == seed.session_id, SessionControlInput.kind == "permission_response",
        )) == 1


@pytest.mark.parametrize("loss_phase", ["before_fence", "after_fence"])
async def test_owner_connection_loss_preserves_the_effect_fence_and_safe_recovery(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, loss_phase,
):
    from app.services import agent_tools, runtime_task_worker, session_permission_runtime
    from app.services.approval_ticket import hash_tool_input

    seed = await _seed_waiting_batch(owner_sessionmaker)
    original_lease = session_permission_runtime.permission_execution_lease
    leases = []

    @asynccontextmanager
    async def observe_lease(db, permission_id):
        async with original_lease(db, permission_id) as lease:
            if lease.acquired:
                leases.append(lease)
            yield lease

    release, paused = asyncio.Event(), asyncio.Event()
    effects = tmp_path / "owner-loss-effects.txt"
    entries = []

    async def execute(tool_name, arguments, **kwargs):
        entries.append(kwargs["tool_call_id"])
        first = len(entries) == 1
        if first and loss_phase == "before_fence":
            paused.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        with effects.open("a") as out:
            out.write("one effect\n")
        if first and loss_phase == "after_fence":
            paused.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        kwargs["trace_metadata_sink"].update({
            "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "local-recovered-effect",
                              "outcome": "allow", "input_hash": hash_tool_input(tool_name, arguments),
                              "policy_snapshot_hash": "a" * 64},
            "tool_execution_frame": {"status": "completed", "output_hash": "b" * 64},
        })
        return "Recovered local execution result"

    async def notify(**_kwargs):
        return None

    monkeypatch.setattr(session_permission_runtime, "permission_execution_lease", observe_lease)
    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    path = (f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}"
            f"/permissions/{seed.permission_ids[0]}/resolve")
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = asyncio.create_task(client.post(path, json={"action": "allow_once"}))
        try:
            await asyncio.wait_for(paused.wait(), timeout=15)
            assert len(leases) == 1
            async with owner_sessionmaker() as control:
                assert await control.scalar(text("SELECT pg_terminate_backend(:pid, 5000)"),
                                            {"pid": leases[0].pid}) is True
            if loss_phase == "after_fence":
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            repeated = await client.post(path, json={"action": "allow_once"})
            assert repeated.status_code == 200, repeated.text
            if loss_phase == "before_fence":
                assert repeated.json()["status"] == "resolved"
                release.set()
                failed_owner = await asyncio.wait_for(first, timeout=15)
                assert failed_owner.status_code == 503
                assert failed_owner.json()["detail"]["code"] == "permission_execution_unavailable"
            else:
                assert repeated.json()["status"] == "needs_reconciliation"
                assert repeated.json()["run_status"] == "needs_reconciliation"
                assert repeated.json()["result_event_id"] is None
                assert repeated.json()["retryable"] is False
                assert len(entries) == 1
            assert effects.read_text().splitlines() == ["one effect"]
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        task = await db.get(RuntimeTask, seed.run_id)
        if loss_phase == "before_fence":
            assert invocation.effect_state == "effect_committed" and task.status == "resumable"
            assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 1
        else:
            assert invocation.effect_state in {"effect_started", "needs_reconciliation"}
            assert invocation.result_event_id is None and task.status == "needs_reconciliation"
            assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 0
