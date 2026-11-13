"""Real worker lanes with production-sized history and scripted auxiliary latency."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import and_, func, or_, select, text, update

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_result import RuntimeResultObject
from app.models.runtime_task import RuntimeTask
from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
from app.models.user import User
from tests.integration.test_failed_delegation_continuation import _execute, _failed_child
from tests.integration.test_r19a_consumed_result import auxiliary_models
from tests.integration.test_r19a_terminal_boundary_race import _route_lanes
from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient,
    SimulatedWorkerCrash,
    _seed_run,
)
from tests.services.test_runtime_terminal_boundary_outbox import _binding, _enqueue, _seed_terminal_task


# The measured wait's hang guard; the settle below uses the same horizon so
# it only ever settles work the worker could really take during the test.
MEASUREMENT_GUARD_SECONDS = 300


async def _history(owner):
    async with owner() as db:
        exists = await db.scalar(select(RuntimeTask.id).where(
            RuntimeTask.root_idempotency_key == "r19a-r2-history-1"))
    if exists is None:
        seeds = [await _seed_terminal_task(owner, tenant_name=f"R19a round 2 history {i}",
            task_type="delegation" if i % 2 else "web_chat_turn") for i in range(32)]
        ids = [seed.task_id for seed in seeds]
        async with owner() as db:
            await db.execute(text("""
                UPDATE runtime_tasks SET terminal_boundary_enqueued_at=now(),
                    completion_outbox_settled_at=now() WHERE id=ANY(CAST(:ids AS uuid[]))
            """), {"ids": ids})
            await db.execute(text("""
                INSERT INTO runtime_tasks (id, tenant_id, task_type, status, depth, parent_agent_id,
                    parent_session_id, root_session_id, root_user_id, prompt, result_summary,
                    terminal_boundary_generation, terminal_boundary_enqueued_at, completion_outbox_settled_at,
                    root_idempotency_key, config_snapshot_hash, policy_snapshot_hash, created_at, completed_at)
                SELECT md5('r19a-r2-history-' || n)::uuid, t.tenant_id, t.task_type, 'completed', 1,
                    t.parent_agent_id, t.parent_session_id, t.root_session_id, t.root_user_id,
                    repeat('historical work ', 100), 'completed', 1, now(), now(),
                    'r19a-r2-history-' || n, t.config_snapshot_hash, t.policy_snapshot_hash,
                    now()-interval '2 days', now()-interval '2 days'
                FROM generate_series(1,500000) n
                JOIN unnest(CAST(:ids AS uuid[])) WITH ORDINALITY s(id,rn) ON s.rn=1+(n%32)
                JOIN runtime_tasks t ON t.id=s.id
            """), {"ids": ids})
            await db.execute(text("""
                INSERT INTO runtime_terminal_boundary_outbox (id,tenant_id,runtime_task_id,task_type,
                    agent_id,session_id,event_kind,terminal_status,authority_ref,authority_id,
                    binding_json,binding_sha256,idempotency_key,status,available_at,delivered_at,created_at)
                SELECT md5('r19a-r2-boundary-'||id)::uuid,tenant_id,id,task_type,parent_agent_id,
                    parent_session_id,'turn_stop','completed','runtime_task',id::text,
                    '{}'::jsonb,repeat('a',64),md5(id::text)||md5(id::text),'delivered',
                    created_at,completed_at,created_at FROM runtime_tasks
                WHERE root_idempotency_key LIKE 'r19a-r2-history-%' OR id=ANY(CAST(:ids AS uuid[]))
            """), {"ids": ids})
            await db.commit()
            await db.execute(text("ANALYZE runtime_tasks"))
            await db.execute(text("ANALYZE runtime_terminal_boundary_outbox"))
            await db.commit()
    async with owner() as db:
        counts = {table: await db.scalar(text("SELECT count(*) FROM " + table))
            for table in ("runtime_tasks", "runtime_terminal_boundary_outbox")}
        assert min(counts.values()) >= 500000
        assert await db.scalar(select(func.count(func.distinct(RuntimeTask.tenant_id))).where(
            RuntimeTask.root_idempotency_key.like("r19a-r2-history-%"))) == 32
    return counts


async def _stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _settle_foreign_work(owner):
    """Make the shared database hold no work the worker could take from it.

    The production runtime-task worker claims any tenant's work, so leftovers
    from earlier tests in this shared database are claimed and dispatched by
    this test's worker and then run through its monkeypatched scripted
    clients. Called before either test seeds anything, so every row it
    touches predates the test: it can only settle foreign leftovers, never
    this test's own chain. The enqueued and settled stamps mirror the shape
    `_history` writes for its settled rows, so no reconcile lane re-enqueues
    work for the settled tasks.
    """
    from app.services.runtime_task_claim_service import _runtime_task_claim_conditions

    # Only what the claim could really take during the measurement: the
    # production claim conditions evaluated at the guard horizon, so a lease
    # or schedule that matures inside the hang guard is settled too, while a
    # future scheduled_at, an inactive tenant, or a lease that cannot expire
    # within the guard is left untouched.
    guard_horizon = datetime.now(UTC) + timedelta(seconds=MEASUREMENT_GUARD_SECONDS)
    claim_conditions = _runtime_task_claim_conditions(claim_now=guard_horizon)
    # The input-dispatch lane runs once per second and dispatches admitted
    # inputs (state='admitted' with dispatch_state='pending', or 'dispatching'
    # with an expired lease), spawning runs this test never seeded.
    admitted_inputs = and_(SessionInputAdmission.state == "admitted", or_(
        SessionInputAdmission.dispatch_state == "pending",
        and_(SessionInputAdmission.dispatch_state == "dispatching",
            SessionInputAdmission.lease_expires_at <= func.now())))

    async with owner() as db:
        settled_tasks = (await db.execute(update(RuntimeTask).where(*claim_conditions).values(
            status="skipped", completed_at=func.now(),
            terminal_boundary_enqueued_at=func.coalesce(
                RuntimeTask.terminal_boundary_enqueued_at, func.now()),
            completion_outbox_settled_at=func.coalesce(
                RuntimeTask.completion_outbox_settled_at, func.now()),
        ))).rowcount
        # Undelivered boundary and notification rows are marked delivered, so
        # the measured lanes only ever carry this test's own chain.
        settled_boundaries = (await db.execute(text("""
            UPDATE runtime_terminal_boundary_outbox SET status='delivered',
                delivered_at=coalesce(delivered_at, now())
            WHERE status = ANY(CAST(:undelivered AS text[]))
        """), {"undelivered": ["pending", "processing"]})).rowcount
        settled_notifications = (await db.execute(text("""
            UPDATE runtime_notification_outbox SET status='delivered',
                delivered_at=coalesce(delivered_at, now())
            WHERE status = ANY(CAST(:undelivered AS text[]))
        """), {"undelivered": ["pending", "processing"]})).rowcount
        settled_admission_ids = (await db.execute(
            update(SessionInputAdmission).where(admitted_inputs)
            .values(dispatch_state="dispatched")
            .returning(SessionInputAdmission.input_id))).scalars().all()
        if settled_admission_ids:
            await db.execute(update(SessionTurnInput)
                .where(SessionTurnInput.id.in_(settled_admission_ids),
                    SessionTurnInput.status != "applied")
                .values(status="applied"))
        await db.commit()
    # Stated precondition for the measurement, in the claim service's and the
    # dispatch lane's own terms: nothing they could take remains.
    async with owner() as db:
        assert await db.scalar(select(func.count()).select_from(RuntimeTask)
            .where(*claim_conditions)) == 0
        assert await db.scalar(select(func.count()).select_from(SessionInputAdmission)
            .where(admitted_inputs)) == 0
        for model in (RuntimeTerminalBoundaryOutbox, RuntimeNotificationOutbox):
            assert await db.scalar(select(func.count()).select_from(model).where(
                model.status.in_(("pending", "processing")))) == 0
    return {"runtime_tasks": settled_tasks, "boundaries": settled_boundaries,
        "notifications": settled_notifications, "admissions": len(settled_admission_ids)}


async def _settle_origin(owner, app_factory, monkeypatch, seed):
    # The initiating turn has already finished while the colleague is still running.
    with monkeypatch.context() as phase_patch:
        worker = _route_lanes(phase_patch, app_factory)
        running = asyncio.create_task(worker.start_terminal_boundary_worker_loop(worker_id="r19a-r2-origin"))
        try:
            # Hang guard for an unmeasured precondition on a shared machine.
            async with asyncio.timeout(120):
                while True:
                    async with owner() as db:
                        row = await db.scalar(select(RuntimeTerminalBoundaryOutbox).where(
                            RuntimeTerminalBoundaryOutbox.runtime_task_id == seed["run_id"]))
                        if row and row.delivered_at:
                            break
                    await asyncio.sleep(0.1)
        finally:
            await _stop(running)


def _auxiliary(monkeypatch, delayed_sessions, seconds):
    from app.services import session_memory, memory_service

    observations = []

    async def config(*_args):
        return {"provider": "openai", "model": "scripted", "max_output_tokens": 1000}

    class Client(ScriptedLLMClient):
        def __init__(self, model_config):
            self.config = model_config
            source = model_config.get("_usage_source")
            self.phase = {"session_memory_writer": "session_memory",
                "fast_reflection_learning_brain": "fast_reflection"}.get(source)
            content = {"current_state": "The scripted result is committed."}
            if self.phase == "fast_reflection":
                content = {"signal_type": "user_preference_correction", "lesson": "Keep summaries concise.",
                    "confidence": 0.9, "container": "session_learning", "promotion_intent": "project_only",
                    "evidence_refs": ["message:0"], "boundary_checks": {"not_one_off": True,
                        "no_credentials": True, "not_direct_memory_write": True}}
            super().__init__([{"content": json.dumps(content)}])

        async def stream(self, **kwargs):
            session = str((self.config.get("_usage_metadata") or {}).get("session_id") or "")
            if self.phase and not session:
                payload = json.loads(kwargs["messages"][-1].content)
                session = str((payload.get("metadata") or {}).get("session_id") or "")
            if self.phase and session:
                session = str(UUID(session))
            started = datetime.now(UTC)
            if self.phase and session in delayed_sessions:
                await asyncio.sleep(seconds)
            result = await super().stream(**kwargs)
            if self.phase:
                observations.append({"phase": self.phase, "session_id": session,
                    "started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat()})
            return result

        async def complete(self, **kwargs):
            return await self.stream(**kwargs)

    monkeypatch.setattr(session_memory, "_get_session_memory_model_config", config)
    monkeypatch.setattr(memory_service, "_get_summary_model_config", config)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", Client)
    monkeypatch.setattr("app.services.fast_reflection_learning_brain.create_llm_client_from_config", Client)
    return observations


async def _seed(owner, app_factory, monkeypatch, tmp_path):
    async def settle(seed):
        await _settle_origin(owner, app_factory, monkeypatch, seed)

    seed, _ = await _failed_child(owner, app_factory, monkeypatch, tmp_path,
        child_success=True, deliver=False, before_child_completion=settle)
    async with owner() as db:
        child = await db.get(RuntimeTask, seed["child_id"])
        seed["child_session_id"] = str(UUID(child.child_session_id))
    return seed


async def _state(owner, seed, web_run_id=None):
    async with owner() as db:
        child = await db.get(RuntimeTask, seed["child_id"])
        notification = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(child.id)))
        parents = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext.is_not(None))))
        ids = [child.id, *(row.id for row in parents), *([web_run_id] if web_run_id else [])]
        boundaries = list(await db.scalars(select(RuntimeTerminalBoundaryOutbox).where(
            RuntimeTerminalBoundaryOutbox.runtime_task_id.in_(ids))))
        return child, notification, parents, boundaries


@pytest.mark.parametrize("sample", range(5))
async def test_parent_starts_before_memory_and_web_joins_both_projections(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models, sample,
):
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.services.web_chat_runtime import start_web_chat_run

    # Before any of this test's rows exist, every claimable or undelivered
    # row still in the shared database is foreign; settle it so the worker
    # loop below only ever carries this test's own chain.
    await _settle_foreign_work(owner_sessionmaker)
    counts = await _history(owner_sessionmaker)
    seed = await _seed(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    web_session_id = uuid4()
    async with owner_sessionmaker() as db:
        session = ChatSession(id=web_session_id, agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
            user_id=seed["user_id"], title="Independent ordinary Web turn")
        db.add(session)
        await db.flush()
        run = await start_web_chat_run(db=db, agent=await db.get(Agent, seed["agent_id"]),
            user=await db.get(User, seed["user_id"]), session=session, content="Say hello.",
            extra_metadata={"source": "websocket_message"})
        web_run_id = UUID(run["run_id"])
        await db.commit()
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: ScriptedLLMClient([{"content": "Hello."}]))
    await _execute(owner_sessionmaker, web_run_id, "r19a-r2-web")
    client = ScriptedLLMClient([{"content": "The colleague's result has arrived."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr("app.services.web_chat_runtime.get_settings", get_settings)
    observations = _auxiliary(monkeypatch, {seed["child_session_id"], str(web_session_id)}, 30)
    worker = _route_lanes(monkeypatch, app_user_sessionmaker)
    assert worker._settings().RUNTIME_TASK_CLAIM_POLL_SECONDS == 1.0
    running = asyncio.create_task(worker.start_runtime_task_worker_loop())
    try:
        # Hang guard only; the measured 5 s and 30–40 s assertions below are unchanged.
        async with asyncio.timeout(MEASUREMENT_GUARD_SECONDS):
            while True:
                child, notice, parents, boundaries = await _state(owner_sessionmaker, seed, web_run_id)
                if (notice and notice.delivered_at and len(parents) == 1 and parents[0].status == "completed"
                    and len(boundaries) == 3 and all(row.delivered_at for row in boundaries)):
                    break
                await asyncio.sleep(0.1)
        by_task = {row.runtime_task_id: row for row in boundaries}
        async with owner_sessionmaker() as db:
            web = await db.get(RuntimeTask, web_run_id)
        parent = parents[0]
        chain = {"sample": sample + 1, "counts": counts, "child_completed_at": child.completed_at.isoformat(),
            "child_boundary_ready_at": by_task[child.id].available_at.isoformat(),
            "child_boundary_delivered_at": by_task[child.id].delivered_at.isoformat(),
            "notification_created_at": notice.created_at.isoformat(),
            "notification_delivered_at": notice.delivered_at.isoformat(),
            "parent_started_at": parent.started_at.isoformat(), "web_completed_at": web.completed_at.isoformat(),
            "web_boundary_ready_at": by_task[web.id].available_at.isoformat(),
            "web_boundary_delivered_at": by_task[web.id].delivered_at.isoformat(),
            "child_to_parent": (parent.started_at - child.completed_at).total_seconds(),
            "web_to_delivered": (by_task[web.id].delivered_at - web.completed_at).total_seconds(),
            "model_calls": observations}
        print("R19A_R2_CHAIN", json.dumps(chain), flush=True)
        assert len(client.requests) == 1
        assert all(row.attempt_count == 1 for row in boundaries)
        delayed = [call for call in observations if call["session_id"] in {
            seed["child_session_id"], str(web_session_id)}]
        assert len(delayed) == 4
        assert all((datetime.fromisoformat(call["finished_at"]) - datetime.fromisoformat(
            call["started_at"])).total_seconds() >= 30 for call in delayed)
        assert {"parent_within_5s": chain["child_to_parent"] <= 5,
            "web_joins_30s": 30 <= chain["web_to_delivered"] < 40} == {
                "parent_within_5s": True, "web_joins_30s": True}
    finally:
        await _stop(running)


async def _plant_foreign_leftovers(owner, tmp_path):
    """Plant the leftover shapes earlier tests leave in a shared database.

    Four shapes, each in its own foreign tenant so none of them can belong
    to the measured chain: a web_chat_turn a dead worker left ``running``
    with an expired lease (the claim lane reclaims and dispatches it), a
    queued claimable web turn whose admitted input is still pending dispatch
    (the input-dispatch lane picks it up once per second), a completed web
    turn whose terminal boundary row was never delivered, and the expired
    run's input bookkeeping already marked dispatched/applied — exactly what
    a dead worker leaves behind mid-turn.
    """
    seeded = await _seed_terminal_task(owner, tenant_name="R19a iso foreign boundary",
        task_type="web_chat_turn")
    await _enqueue(owner, seeded, event_kind="turn_stop", binding=_binding())
    expired = await _seed_run(owner, tmp_path / "foreign-expired-lease",
        prompt="Foreign leftover turn the dead worker never finished.")
    queued = await _seed_run(owner, tmp_path / "foreign-queued",
        prompt="Foreign queued turn no worker ever claimed.")
    async with owner() as db:
        await db.execute(text("""
            UPDATE runtime_tasks SET claim_expires_at = now() - interval '5 minutes'
                WHERE id = :expired
        """), {"expired": expired["run_id"]})
        await db.execute(text("""
            UPDATE runtime_tasks SET status='pending', claimed_by=NULL, claim_version=0,
                attempt_count=0, claim_expires_at=NULL WHERE id = :queued
        """), {"queued": queued["run_id"]})
        # Only the dead worker's run has consumed its input; the queued turn's
        # admission stays 'admitted'/'pending' with no pre-marking, the exact
        # shape the input-dispatch lane takes.
        await db.execute(text("""
            UPDATE session_input_admissions SET dispatch_state='dispatched'
                WHERE session_id = :session
        """), {"session": expired["session_id"]})
        await db.execute(text("""
            UPDATE session_turn_inputs SET status='applied'
                WHERE session_id = :session AND status <> 'applied'
        """), {"session": expired["session_id"]})
        await db.commit()
    return expired, queued, seeded


async def test_foreign_leftover_work_never_joins_the_measured_chain(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    """Reproduction for the shared-database failure, independent of suite order.

    The measured test's worker claims any tenant's work, so without settling
    first it reclaims the foreign expired-lease web turn, claims the foreign
    queued web turn, and serves them through this test's scripted parent
    client (a second request) and delayed auxiliary clients. Planting the
    leftovers here and running the unchanged measurement keeps that failure
    reproducible in one pytest process on a fresh database.
    """
    await _plant_foreign_leftovers(owner_sessionmaker, tmp_path)
    await test_parent_starts_before_memory_and_web_joins_both_projections(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        auxiliary_models, 0,
    )


async def test_crash_after_parent_projection_replays_without_duplicate_outputs(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, auxiliary_models,
):
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor

    # Same shared-database precondition as the latency test: settle foreign
    # claimable work before the first real worker loop starts.
    await _settle_foreign_work(owner_sessionmaker)
    seed = await _seed(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    observations = _auxiliary(monkeypatch, set(), 0)
    client = ScriptedLLMClient([{"content": "The colleague's result is ready."}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr("app.services.web_chat_runtime.get_settings", get_settings)
    crashed = asyncio.Event()
    native = DirectInvocationTerminalBoundaryProcessor._project_response

    async def crash_before_response(self, item, material):
        if item.runtime_task_id == seed["child_id"] and not crashed.is_set():
            async with owner_sessionmaker() as db:
                assert await db.scalar(select(func.count()).select_from(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(seed["child_id"]))) == 1
                assert await db.scalar(select(func.count()).select_from(RuntimeResultObject).where(
                    RuntimeResultObject.source_run_id == str(seed["child_id"]))) == 1
            crashed.set()
            raise SimulatedWorkerCrash()
        return await native(self, item, material)

    monkeypatch.setattr(DirectInvocationTerminalBoundaryProcessor, "_project_response", crash_before_response)
    worker = _route_lanes(monkeypatch, app_user_sessionmaker)
    first = asyncio.create_task(worker.start_runtime_task_worker_loop())
    try:
        await asyncio.wait_for(crashed.wait(), 10)
        async with asyncio.timeout(10):
            while True:
                _, notice, parents, boundaries = await _state(owner_sessionmaker, seed)
                if notice.delivered_at and len(parents) == 1 and parents[0].status == "completed":
                    break
                await asyncio.sleep(0.1)
        child_boundary = next(row for row in boundaries if row.runtime_task_id == seed["child_id"])
        assert child_boundary.delivered_at is None and child_boundary.attempt_count == 1
        assert not [call for call in observations if call["session_id"] == seed["child_session_id"]]
    finally:
        await _stop(first)
    async with owner_sessionmaker() as db:
        row = await db.get(RuntimeTerminalBoundaryOutbox, child_boundary.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    second = asyncio.create_task(worker.start_runtime_task_worker_loop())
    try:
        async with asyncio.timeout(15):
            while True:
                _, notice, parents, boundaries = await _state(owner_sessionmaker, seed)
                if len(boundaries) == 2 and all(row.delivered_at for row in boundaries):
                    break
                await asyncio.sleep(0.1)
        async with owner_sessionmaker() as db:
            assert await db.scalar(select(func.count()).select_from(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.source_run_id == str(seed["child_id"]))) == 1
            assert await db.scalar(select(func.count()).select_from(RuntimeResultObject).where(
                RuntimeResultObject.source_run_id == str(seed["child_id"]))) == 1
        child_boundary = next(row for row in boundaries if row.runtime_task_id == seed["child_id"])
        assert child_boundary.attempt_count == 2 and child_boundary.delivery_receipt_json["response_projection_sha256"]
        assert len(parents) == 1 and len(client.requests) == 1
        calls = [call["phase"] for call in observations if call["session_id"] == seed["child_session_id"]]
        assert sorted(calls) == ["fast_reflection", "session_memory"]
        from app.services.agent_asset_transaction import AgentAssetTransaction
        from app.services.evolution_ledger import load_evolution_ledger
        from app.services.fast_reflection_service import load_fast_reflection_disposition
        from app.services.session_learning import load_session_learning_projections

        root = seed["data_root"] / str(seed["child_agent_id"])
        with AgentAssetTransaction(root, operation="response_complete_session_projection",
            idempotency_key=f"session-memory:{child_boundary.idempotency_key}") as transaction:
            assert transaction.is_replay, "Session memory must have its original committed receipt"
            session_receipt = transaction.commit()
        disposition = load_fast_reflection_disposition(data_root=seed["data_root"],
            agent_id=seed["child_agent_id"], session_id=child_boundary.session_id,
            response_idempotency_key=child_boundary.idempotency_key)
        assert disposition["status"] == "candidate_created" and disposition["idempotent_replay"] is True
        candidate_id = disposition["candidate_id"]
        assert len([entry for entry in load_evolution_ledger(root)
            if entry.get("event") == "candidate" and entry.get("candidate_id") == candidate_id]) == 1
        assert len([entry for entry in load_session_learning_projections(data_root=seed["data_root"],
            agent_id=seed["child_agent_id"], session_id=child_boundary.session_id)
            if entry.get("candidate_id") == candidate_id]) == 1
        print("R19A_R2_CRASH_REPLAY", json.dumps({"boundary_attempts": child_boundary.attempt_count,
            "notifications": 1, "result_objects": 1, "continuations": len(parents), "child_model_calls": calls,
            "session_transaction_id": session_receipt.transaction_id, "fast_reflection_candidate_id": candidate_id}))
    finally:
        await _stop(second)
