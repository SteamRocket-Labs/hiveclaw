"""A committed parent stop survives a crash before delegated children terminate."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.integration import test_r11c_colleague_lifecycle_postgres as colleagues
from tests.integration import test_r23a_stop_ends_blocked_model_call as stopped
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, SimulatedWorkerCrash
from tests.services.test_session_input_control_v2 import _authority, _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _accept_stop(owner, seed, monkeypatch, accept_path):
    from app.services import web_chat_runtime
    from app.services.session_control_input import accept_cancel_control_input, recover_stale_cancel_control_inputs_once

    signalled = []

    async def stop_before_child_cancel(**kwargs):
        # Both real ingress paths commit cancelling before this effect boundary.
        signalled.append(kwargs["run_id"])
        async with owner() as db:
            parent = await db.get(RuntimeTask, seed["run_id"])
            assert parent.metadata_json["cancel_state"] == "cancelling"

    monkeypatch.setattr(web_chat_runtime, "signal_web_chat_cancel", stop_before_child_cancel)
    if accept_path == "live":
        assert (await stopped._stop(owner, seed))["accepted"] is True
    else:
        async with owner() as db:
            authority = await _authority(
                db, user_id=seed["user_id"], agent_id=seed["agent_id"], session_id=seed["session_id"]
            )
            accepted = await accept_cancel_control_input(
                db,
                authority=authority,
                control_id=uuid4(),
                expected_run_id=seed["run_id"],
                idempotency_key=f"r23c-accepted:{seed['run_id']}",
            )
            assert accepted.status == "accepted"
            await db.commit()
            recovered = await recover_stale_cancel_control_inputs_once(
                db,
                worker_id="r23c-control-recovery",
                signal_callback=stop_before_child_cancel,
                stale_after=timedelta(0),
                tenant_id=seed["tenant_id"],
            )
            assert recovered["started"] == recovered["signalled"] == 1
    assert signalled == [seed["run_id"]]


async def _claim_and_dispatch_child(owner, child_id, *, attempt):
    from app.services import runtime_task_worker
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService

    async with owner() as db:
        claimed = await RuntimeTaskClaimService(
            db=db, worker_id=f"r23c-child-{attempt}", task_types=("delegation",), lease_seconds=60
        ).claim_available(batch_size=25)
    child = next(task for task in claimed if task.id == child_id)
    assert child.attempt_count == attempt
    if attempt == 2:
        assert child.metadata_json["reclaimed_expired_claim"] is True
    assert runtime_task_worker._dispatch_claimed_task(child)
    return runtime_task_worker._DISPATCHED_TASKS[child_id.hex][1]


@pytest.mark.parametrize("accept_path", ["live", "recovered"])
@pytest.mark.parametrize("snapshot", ["placeholder", "full", "full_effectful"])
async def test_parent_stop_crash_reclaims_prepared_child_without_sending(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, accept_path, snapshot
):
    import app.runtime.invoker as invoker
    from app.services import session_model_round as rounds, web_chat_runtime

    seed, parent, _ = await colleagues._setup(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool="delegate_to_agent", paused=True
    )
    # Use a genuinely restart-eligible delegation rather than the peer default,
    # whose existing mutating-profile gate stops before model-round recovery.
    parent.script[0] = colleagues._tool(
        "delegate_to_agent",
        {
            "agent_name": colleagues.COLLEAGUE,
            "message": "Review the supplied business facts.",
            "tool_profile": "review_readonly",
        },
    )
    provider = ScriptedLLMClient([{"content": "A stopped delegated run must not send"}])
    monkeypatch.setattr(
        invoker, "create_llm_client", lambda **kwargs: provider if kwargs["model"] == "fake-colleague" else parent
    )
    parent_worker = await stopped._dispatch(owner_sessionmaker, seed["run_id"])
    child_worker = None
    ready = asyncio.Event()
    bind, prepare, commit = rounds.bind_round_inputs, rounds.prepare_model_request, AsyncSession.commit
    try:
        await asyncio.wait_for(parent.started.wait(), timeout=20)
        child = await colleagues._child(owner_sessionmaker, seed)
        assert child is not None and child.root_runtime_task_id == seed["run_id"]
        assert child.metadata_json["tool_profile"] == "review_readonly"
        assert child.metadata_json["resume_after_restart"] and child.metadata_json["resumable_delegation"]

        async def bind_placeholder(db, **kwargs):
            result = await bind(db, **kwargs)
            if kwargs["run_id"] == child.id and snapshot == "placeholder":
                db.info["r23c_crash_after_commit"] = True
            return result

        async def prepare_full(db, **kwargs):
            if kwargs["run_id"] == child.id and snapshot == "full_effectful":
                kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
            result = await prepare(db, **kwargs)
            if kwargs["run_id"] == child.id and snapshot != "placeholder":
                db.info["r23c_crash_after_commit"] = True
            return result

        async def die_before_seal(db):
            await commit(db)
            if db.info.pop("r23c_crash_after_commit", False):
                ready.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Process death cannot run the preparation-failure callback.
                    raise SimulatedWorkerCrash()

        monkeypatch.setattr(rounds, "bind_round_inputs", bind_placeholder)
        monkeypatch.setattr(rounds, "prepare_model_request", prepare_full)
        monkeypatch.setattr(AsyncSession, "commit", die_before_seal)
        child_worker = await _claim_and_dispatch_child(owner_sessionmaker, child.id, attempt=1)
        await asyncio.wait_for(ready.wait(), timeout=20)
        await _accept_stop(owner_sessionmaker, seed, monkeypatch, accept_path)
        child_worker.cancel()
        await asyncio.gather(child_worker, return_exceptions=True)
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, child.id)
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == child.id))
            assert task.status == "running", "Child cancellation must not run before the simulated crash"
            assert row.state == "prepared" and row.seal_json is None
            assert (row.model_request_snapshot_json.get("phase") == "awaiting_exact_snapshot") is (
                snapshot == "placeholder"
            )
            result_id, version, request_id = row.id, row.version, row.provider_request_id
            task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        assert provider.requests == []
        monkeypatch.setattr(rounds, "bind_round_inputs", bind)
        monkeypatch.setattr(rounds, "prepare_model_request", prepare)
        monkeypatch.setattr(AsyncSession, "commit", commit)
        loaded, sealed = [], []
        resume, fail = web_chat_runtime._session_restart_resume_history, rounds.fail_model_request

        async def observe_resume(db, task):
            if task.id == child.id:
                loaded.append(task.attempt_count)
            return await resume(db, task)

        async def observe_seal(db, **kwargs):
            if kwargs["run_id"] == child.id:
                sealed.append(kwargs)
            return await fail(db, **kwargs)

        monkeypatch.setattr(web_chat_runtime, "_session_restart_resume_history", observe_resume)
        monkeypatch.setattr(rounds, "fail_model_request", observe_seal)
        child_worker = await _claim_and_dispatch_child(owner_sessionmaker, child.id, attempt=2)
        await asyncio.wait_for(asyncio.shield(child_worker), timeout=20)
        assert loaded == [2], "The real delegation worker must reach the R23b recovery loader"
        assert provider.requests == [], "A reclaimed descendant of a stopped parent must never send"
        assert len(sealed) == 1 and sealed[0]["provider_request_id"] == request_id
        assert sealed[0]["agent_id"] == child.child_agent_id and sealed[0]["run_id"] == child.id
        assert sealed[0]["provider_request_sent"] is (snapshot != "placeholder")
        expected = "needs_reconciliation" if snapshot == "full_effectful" else "failed"
        assert await stopped._assert_sealed_once(owner_sessionmaker, {"run_id": child.id}, result_id, expected) == (
            version + 1
        )
        async with owner_sessionmaker() as db:
            row = await db.get(SessionModelResult, result_id)
            assert row.seal_json == {
                "delivery_state": "rejected" if snapshot == "placeholder" else "unknown",
                "error_class": "cancelled",
                "retry_safe": False,
            }
        print(f"R23C accept={accept_path} snapshot={snapshot} provider_requests=0 seal={expected}")
    finally:
        parent.release.set()
        for worker in (parent_worker, child_worker):
            if worker is not None and not worker.done():
                worker.cancel()
        await asyncio.gather(*(worker for worker in (parent_worker, child_worker) if worker), return_exceptions=True)


@pytest.mark.parametrize("accept_path", ["live", "recovered"])
async def test_parent_stop_accept_leaves_all_descendant_rows_unchanged(owner_sessionmaker, monkeypatch, accept_path):
    from app.models.tenant import Tenant

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    seed = dict(tenant_id=tenant_id, user_id=user_id, agent_id=agent_id, session_id=session_id, run_id=run_id)
    active, unchanged = {}, {}
    async with owner_sessionmaker() as db:
        foreign_id = uuid4()
        db.add(Tenant(id=foreign_id, name="Other stop scope", slug=f"r23c-{foreign_id.hex}"))
        await db.flush()

        def add(status, *, root_id=run_id, task_type="delegation", tenant=tenant_id):
            task_id = uuid4()
            metadata = {"kept": str(task_id), "nested": {"payload": [1, 2]}}
            if status in {"completed", "failed", "killed", "skipped", "needs_reconciliation"}:
                metadata.update(cancel_state="cancelled", cancel_control_id=str(uuid4()))
            db.add(
                RuntimeTask(
                    id=task_id,
                    tenant_id=tenant,
                    task_type=task_type,
                    status=status,
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    root_runtime_task_id=root_id,
                    metadata_json=metadata,
                )
            )
            return task_id, metadata

        for status in ("running", "pending", "suspended", "resumable"):
            task_id, metadata = add(status)
            active[task_id] = (status, metadata)
        child_id = next(iter(active))
        grandchild_id, metadata = add("running", root_id=child_id)
        active[grandchild_id] = ("running", metadata)
        for status in ("completed", "failed", "killed", "skipped", "needs_reconciliation"):
            task_id, metadata = add(status)
            unchanged[task_id] = (status, metadata)
        terminal_id = next(iter(unchanged))
        task_id, metadata = add("pending", root_id=terminal_id)
        active[task_id] = ("pending", metadata)
        for kwargs in ({"root_id": uuid4()}, {"task_type": "subagent"}, {"tenant": foreign_id}):
            task_id, metadata = add("running", **kwargs)
            unchanged[task_id] = ("running", metadata)
        await db.commit()
    await _accept_stop(owner_sessionmaker, seed, monkeypatch, accept_path)
    async with owner_sessionmaker() as db:
        parent = await db.get(RuntimeTask, run_id)
        fields = {
            key: parent.metadata_json[key]
            for key in (
                "cancel_state",
                "cancel_control_id",
                "cancel_command_id",
                "cancel_requested_by",
                "cancel_worker_id",
            )
        }
        assert fields["cancel_state"] == "cancelling" and fields["cancel_requested_by"] == str(user_id)
        for task_id, (status, metadata) in active.items():
            child = await db.get(RuntimeTask, task_id)
            # Round 2 reads ancestors at execution admission; accept must not
            # acquire descendant row locks or overwrite independent control ids.
            assert child.status == status and child.metadata_json == metadata
        for task_id, (status, metadata) in unchanged.items():
            child = await db.get(RuntimeTask, task_id)
            assert child.status == status and child.metadata_json == metadata
