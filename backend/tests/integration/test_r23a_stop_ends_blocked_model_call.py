"""A member stop abandons a blocked provider through the real run/control path."""

import asyncio
import json
import time

import pytest
from sqlalchemy import select

from tests.integration import test_r11c_colleague_lifecycle_postgres as colleagues
from tests.integration import test_unverified_t2_recovery_postgres as support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = support.t2_work
BUDGET_SECONDS = 3.0


class BlockingProviderClient(ScriptedLLMClient):
    def __init__(self, script, *, block_at=0):
        super().__init__(script)
        self.block_at = block_at
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()
        self.abandoned = asyncio.Event()
        self.returned = False

    async def stream(self, **kwargs):
        if len(self.requests) == self.block_at:
            self.in_flight.set()
            try:
                await asyncio.wait_for(self.release.wait(), timeout=30)
                self.returned = True
            except asyncio.CancelledError:
                self.abandoned.set()
                raise
        return await super().stream(**kwargs)


async def _dispatch(owner_sessionmaker, run_id):
    from app.models.runtime_task import RuntimeTask
    from app.services import runtime_task_worker, web_chat_runtime

    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert runtime_task_worker._dispatch_claimed_task(run)
    return web_chat_runtime._TASKS[run_id.hex]


async def _stop(owner_sessionmaker, seed):
    from app.api.chat_sessions import cancel_session_run
    from app.models.user import User

    async with owner_sessionmaker() as db:
        return await cancel_session_run(
            agent_id=seed["agent_id"],
            session_id=seed["session_id"],
            run_id=seed["run_id"],
            idempotency_key=f"r23a-stop:{seed['run_id']}",
            current_user=await db.get(User, seed["user_id"]),
            db=db,
        )


def _control_channel(monkeypatch, seed, *, remote):
    from app.services import runtime_control_bus as bus
    from app.services import web_chat_runtime as runtime

    published = []

    class Redis:
        async def publish(self, channel, payload):
            published.append(json.loads(payload))
            return 1

    async def redis():
        return Redis()

    monkeypatch.setattr(bus, "get_redis", redis)
    real_signal = runtime.signal_web_chat_cancel
    clock = {}

    async def signal(**kwargs):
        # submit_live_cancel_input has committed the applying control here.
        clock["accepted"] = time.monotonic()
        assert kwargs["run_id"] == seed["run_id"]
        event = runtime._CANCEL_EVENTS[seed["run_id"].hex]
        if remote:
            runtime._CANCEL_EVENTS.pop(seed["run_id"].hex)
        try:
            receipt = await real_signal(**kwargs)
            assert receipt.local_delivered is (not remote), receipt
            assert receipt.delivery_state == "delivered", receipt
        finally:
            if remote:
                runtime._CANCEL_EVENTS[seed["run_id"].hex] = event
        if remote:
            assert not event.is_set(), "The accepting process must not set the host event"
            messages = [item for item in published if item.get("type") == "web_chat_cancel"]
            assert len(messages) == 1
            assert messages[0]["run_id"] == seed["run_id"].hex
            assert await bus.handle_runtime_control_message(messages[0])
            assert event.is_set()
        return receipt

    monkeypatch.setattr(runtime, "signal_web_chat_cancel", signal)
    return clock


async def _assert_stops(owner, seed, provider, worker, clock, *, case):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult

    await asyncio.wait_for(provider.in_flight.wait(), timeout=20)
    assert len(provider.requests) == provider.block_at
    receipt = await _stop(owner, seed)
    assert receipt["accepted"] is True, receipt
    assert receipt["status"] in {"applying", "applied"}, receipt
    deadline = clock["accepted"] + BUDGET_SECONDS
    while True:
        async with owner() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            status, metadata = run.status, dict(run.metadata_json or {})
        if status not in {"pending", "running", "suspended", "resumable"} or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.01)
    elapsed = time.monotonic() - clock["accepted"]
    print(
        f"R23A case={case} accepted_to_terminal={elapsed:.3f}s status={status} abandoned={provider.abandoned.is_set()}"
    )
    assert status == "killed", (elapsed, status, metadata)
    assert elapsed < BUDGET_SECONDS, elapsed
    assert metadata["terminal_reason"] == "user_cancel", metadata
    assert metadata["cancel_state"] == "cancelled", metadata
    assert provider.abandoned.is_set(), "The provider await must be cancelled"
    assert not provider.returned and not provider.release.is_set()
    await asyncio.wait_for(asyncio.shield(worker), timeout=5)
    async with owner() as db:
        rounds = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
        assert len(rounds) == provider.block_at + 1
        assert sum(row.seal_json.get("error_class") == "cancelled" for row in rounds if row.seal_json) == 1
        assert all(row.state != "needs_reconciliation" for row in rounds)


async def _cleanup(provider, *workers):
    provider.release.set()
    for worker in workers:
        if worker is not None and not worker.done():
            worker.cancel()
    await asyncio.gather(*(worker for worker in workers if worker is not None), return_exceptions=True)


@pytest.mark.parametrize("remote", [False, True], ids=["a_plain", "c_cross_process"])
async def test_member_stop_abandons_blocked_model_call(t2_work, owner_sessionmaker, monkeypatch, remote):
    import app.runtime.invoker as invoker

    seed = t2_work
    provider = BlockingProviderClient([{"content": "Final answer"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    clock = _control_channel(monkeypatch, seed, remote=remote)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await _assert_stops(owner_sessionmaker, seed, provider, worker, clock, case="c" if remote else "a")
    finally:
        await _cleanup(provider, worker)


async def test_parent_stop_abandons_post_delegation_model_call(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    import app.runtime.invoker as invoker
    from app.models.runtime_task import RuntimeTask

    seed, parent, child_provider = await colleagues._setup(
        owner_sessionmaker,
        app_user_sessionmaker,
        monkeypatch,
        tmp_path,
        tool="delegate_to_agent",
    )
    provider = BlockingProviderClient(parent.script, block_at=1)
    monkeypatch.setattr(
        invoker,
        "create_llm_client",
        lambda **kwargs: child_provider if kwargs["model"] == "fake-colleague" else provider,
    )
    clock = _control_channel(monkeypatch, seed, remote=False)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    child_worker = None
    try:
        await asyncio.wait_for(provider.in_flight.wait(), timeout=20)
        child = await colleagues._child(owner_sessionmaker, seed)
        assert child is not None and child.root_runtime_task_id == seed["run_id"]
        child_worker = asyncio.create_task(
            colleagues._execute(
                child.id,
                owner_sessionmaker,
                worker="r23a-colleague",
                delegation=True,
            )
        )
        await asyncio.wait_for(child_provider.started.wait(), timeout=20)
        await _assert_stops(owner_sessionmaker, seed, provider, worker, clock, case="b")
        assert child_provider.cancelled.is_set()
        await asyncio.gather(child_worker, return_exceptions=True)
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, child.id)).status == "killed"
    finally:
        child_provider.release.set()
        await _cleanup(provider, worker, child_worker)


class PreparationGate:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.abandoned = asyncio.Event()
        self.finished = False

    async def wait(self):
        self.started.set()
        try:
            try:
                await asyncio.wait_for(self.release.wait(), timeout=30)
            except TimeoutError:
                pass
            self.finished = True
        except asyncio.CancelledError:
            self.abandoned.set()
            raise


@pytest.mark.parametrize(
    "stage",
    [
        "round_input_bind",
        "context_preflight",
        "model_request_prepare",
        "model_request_committed",
        "model_request_committed_effectful",
        "model_request_locked_query",
    ],
)
async def test_case_d_stop_during_next_round_preparation(
    t2_work,
    owner_sessionmaker,
    monkeypatch,
    stage,
):
    import app.kernel.engine as kernel
    import app.runtime.invoker as invoker
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round, web_chat_runtime
    from sqlalchemy.ext.asyncio import AsyncSession

    seed = t2_work
    provider = ScriptedLLMClient(
        [
            colleagues._tool("get_current_time", {}),
            {"content": "This round must not be sent after stop."},
        ]
    )
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    clock = _control_channel(monkeypatch, seed, remote=False)
    gate = PreparationGate()
    stop_committed = asyncio.Event()
    query_pid = None
    if stage == "model_request_locked_query":
        signal = web_chat_runtime.signal_web_chat_cancel

        async def signal_during_query(**kwargs):
            # The real stop transaction commits before signaling. Let the
            # prepare take the transcript lock only after that commit.
            stop_committed.set()
            from sqlalchemy import text

            async with owner_sessionmaker() as db:
                async with asyncio.timeout(5):
                    while query_pid is None or not await db.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:pid AND wait_event='PgSleep')"),
                        {"pid": query_pid},
                    ):
                        await asyncio.sleep(0.01)
            return await signal(**kwargs)

        monkeypatch.setattr(web_chat_runtime, "signal_web_chat_cancel", signal_during_query)
    async with owner_sessionmaker() as db:
        original_description = (await db.get(Agent, seed["agent_id"])).role_description

    async def uncommitted_probe(db):
        agent = await db.get(Agent, seed["agent_id"])
        agent.role_description = "R23B_UNCOMMITTED_PREPARATION"
        await db.flush()

    bind = session_model_round.bind_round_inputs
    prepare = session_model_round.prepare_model_request
    context = kernel.prepare_session_context_for_request
    commit = AsyncSession.commit

    async def bind_input(db, **kwargs):
        if stage == "round_input_bind" and kwargs["round_index"] == 2:
            await uncommitted_probe(db)
            await gate.wait()
        return await bind(db, **kwargs)

    async def prepare_request(db, **kwargs):
        nonlocal query_pid
        if kwargs["round_index"] == 2:
            if stage == "model_request_locked_query":
                from app.services.chat_transcript import lock_transcript_session
                from sqlalchemy import text

                gate.started.set()
                await stop_committed.wait()
                await lock_transcript_session(db, session_id=seed["session_id"])
                query_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                try:
                    await db.execute(text("SELECT pg_sleep(30)"))
                    gate.finished = True
                except asyncio.CancelledError:
                    gate.abandoned.set()
                    raise
            if stage == "model_request_prepare":
                await uncommitted_probe(db)
                await gate.wait()
            if stage == "model_request_committed_effectful":
                kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "local-unsent"}]}
        result = await prepare(db, **kwargs)
        if kwargs["round_index"] == 2 and stage.startswith("model_request_committed"):
            db.info["r23b_pause_after_commit"] = True
        return result

    async def prepare_context(**kwargs):
        if stage == "context_preflight" and len(provider.requests) == 1:
            await gate.wait()
        return await context(**kwargs)

    async def commit_request(db):
        await commit(db)
        if db.info.pop("r23b_pause_after_commit", False):
            # The real prepared transaction is committed and its locks are
            # released, but the caller has not returned to the kernel yet.
            await gate.wait()

    monkeypatch.setattr(session_model_round, "bind_round_inputs", bind_input)
    monkeypatch.setattr(session_model_round, "prepare_model_request", prepare_request)
    monkeypatch.setattr(kernel, "prepare_session_context_for_request", prepare_context)
    monkeypatch.setattr(AsyncSession, "commit", commit_request)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(gate.started.wait(), timeout=20)
        assert len(provider.requests) == 1
        from app.models.session_v2 import SessionToolInvocation

        async with owner_sessionmaker() as db:
            tool = await db.scalar(
                select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == seed["run_id"],
                    SessionToolInvocation.tool_name == "get_current_time",
                )
            )
            assert tool is not None and tool.result_event_id is not None
        receipt = await _stop(owner_sessionmaker, seed)
        assert receipt["accepted"] is True and not gate.finished
        # Observe the durable terminal boundary, independently of later hook
        # cleanup. The baseline is allowed to finish its full 30-second block.
        deadline = clock["accepted"] + 35
        while True:
            async with owner_sessionmaker() as db:
                status = (await db.get(RuntimeTask, seed["run_id"])).status
            if status not in {"pending", "running", "suspended", "resumable"} or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        elapsed = time.monotonic() - clock["accepted"]
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            metadata = dict(run.metadata_json or {})
            rounds = list(
                await db.scalars(
                    select(SessionModelResult)
                    .where(
                        SessionModelResult.run_id == seed["run_id"],
                    )
                    .order_by(SessionModelResult.round_id)
                )
            )
            description = (await db.get(Agent, seed["agent_id"])).role_description
        print(
            f"R23B stage={stage} event_to_terminal={elapsed:.3f}s status={run.status} provider_calls={len(provider.requests)}"
        )
        await asyncio.wait_for(asyncio.shield(worker), timeout=5)
        assert elapsed < 1.0, (stage, elapsed)
        assert run.status == "killed" and metadata["terminal_reason"] == "user_cancel", metadata
        assert metadata["cancel_state"] == "cancelled", metadata
        assert gate.abandoned.is_set() and not gate.finished
        assert len(provider.requests) == 1, "No provider request may be sent for round 2"
        assert description == original_description, "Interrupted preparation must roll back its transaction"
        if stage == "model_request_locked_query":
            from sqlalchemy import func

            async with owner_sessionmaker() as db:
                lock_key = int.from_bytes(seed["session_id"].bytes[:8], byteorder="big", signed=True)
                assert await db.scalar(select(func.pg_try_advisory_xact_lock(lock_key))), (
                    "The query lock must be released"
                )
        assert all(row.state not in {"prepared", "streaming", "needs_reconciliation"} for row in rounds)
        if stage != "round_input_bind":
            # Input binding already committed a placeholder before context
            # or request preparation; cancel seals it instead of deleting it.
            assert len(rounds) == 2 and rounds[1].state == "failed"
            assert rounds[1].seal_json["error_class"] == "cancelled"
            assert rounds[1].seal_json["retry_safe"] is False
        else:
            assert len(rounds) == 1
        before = [(row.id, row.state, row.version) for row in rounds]
        # Even explicit crash-recovery re-entry must not resend a cancelled run.
        await web_chat_runtime.execute_web_chat_run(seed["run_id"])
        assert len(provider.requests) == 1
        async with owner_sessionmaker() as db:
            after = list(
                await db.scalars(
                    select(SessionModelResult)
                    .where(
                        SessionModelResult.run_id == seed["run_id"],
                    )
                    .order_by(SessionModelResult.round_id)
                )
            )
            assert [(row.id, row.state, row.version) for row in after] == before
            assert (await db.get(RuntimeTask, seed["run_id"])).status == "killed"
    finally:
        gate.release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_preparation_cancel_requires_request_identity_for_a_full_snapshot(t2_work, owner_sessionmaker):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.session_model_round import fail_model_request
    from tests.services.test_r18a_ambiguous_send_classification import _prepare_round

    seed = t2_work
    common = dict(
        tenant_id=seed["tenant_id"],
        agent_id=seed["agent_id"],
        session_id=seed["session_id"],
        run_id=seed["run_id"],
        turn_id=seed["turn_id"],
        round_index=1,
    )
    async with owner_sessionmaker() as db:
        request_id = await _prepare_round(db, common, {"mcp_servers": [{"name": "possibly-sent"}]})
        outcome = await fail_model_request(
            db,
            **common,
            provider_request_id=None,
            error_class="cancelled",
            retry_safe=False,
            provider_request_sent=False,
        )
        assert outcome is None, "Only an input placeholder can be cancelled without an exact request identity"
        await db.commit()
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
        assert result.provider_request_id == request_id and result.state == "prepared" and result.seal_json is None
        assert (await db.get(RuntimeTask, seed["run_id"])).status == "running"
        # The exact identity plus the trusted no-send fact can close this
        # request. Also leave no active synthetic run for later claim tests.
        from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

        run = await db.get(RuntimeTask, seed["run_id"])
        fence = set_runtime_task_fence(task_id=run.id, worker_id=run.claimed_by, claim_version=run.claim_version)
        try:
            outcome = await fail_model_request(
                db,
                **common,
                provider_request_id=request_id,
                error_class="cancelled",
                retry_safe=False,
                provider_request_sent=False,
            )
            await db.commit()
        finally:
            reset_runtime_task_fence(fence)
        assert outcome == "failed" and (await db.get(RuntimeTask, seed["run_id"])).status == "killed"


async def test_output_continuation_cancel_does_not_erase_prior_provider_send(
    t2_work,
    owner_sessionmaker,
    monkeypatch,
):
    import app.kernel.engine as kernel
    import app.runtime.invoker as invoker
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round

    seed = t2_work
    provider = ScriptedLLMClient([{"content": "Partial answer", "finish_reason": "length"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    gate = PreparationGate()
    prepare = session_model_round.prepare_model_request
    continue_output = kernel._continue_after_output_cap
    root_requests, continuation_cancel = [], []

    async def prepare_request(db, **kwargs):
        root = not kwargs.get("continuation_index")
        if root:
            kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
        result = await prepare(db, **kwargs)
        if root:
            root_requests.append(result)
        return result

    async def continue_after_stop(**kwargs):
        # The first physical request has returned, but its logical round has
        # not sealed. The next physical request must not erase that send fact.
        await gate.wait()
        try:
            return await continue_output(**kwargs)
        except kernel._KernelCancelledError as exc:
            continuation_cancel.append(exc.provider_request_sent)
            raise

    monkeypatch.setattr(session_model_round, "prepare_model_request", prepare_request)
    monkeypatch.setattr(kernel, "_continue_after_output_cap", continue_after_stop)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(gate.started.wait(), timeout=20)
        assert len(provider.requests) == 1 and len(root_requests) == 1
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        gate.release.set()
        await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        # Stream call sites conservatively retain the logical root's send fact,
        # including a continuation precheck; preparation alone uses False.
        assert continuation_cancel == [True], "A continuation stop must preserve the original send fact"
        assert len(provider.requests) == 1
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            result = await db.scalar(
                select(SessionModelResult).where(
                    SessionModelResult.provider_request_id == root_requests[0],
                )
            )
            assert run.status == "needs_reconciliation", "The original possibly effectful request was already sent"
            assert result.state == "needs_reconciliation" and result.seal_json["error_class"] == "cancelled"
    finally:
        gate.release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def _reclaim_stopped_run(owner, seed, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _claim_run

    async with owner() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert run.status == "running" and run.metadata_json["cancel_state"] == "cancelling"
        run.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()
    claimed = await _claim_run(owner, seed["run_id"], "r23b-reclaimed-worker")
    assert len(claimed) == 1 and claimed[0].attempt_count == 2
    assert claimed[0].metadata_json["reclaimed_expired_claim"] is True
    loaded = []
    resume = web_chat_runtime._session_restart_resume_history

    async def observe_resume(db, task):
        loaded.append(task.attempt_count)
        return await resume(db, task)

    monkeypatch.setattr(web_chat_runtime, "_session_restart_resume_history", observe_resume)
    worker = await _dispatch(owner, seed["run_id"])
    await asyncio.wait_for(asyncio.shield(worker), timeout=15)
    assert loaded and set(loaded) == {2}, "The real claim must enter the restart loader"


async def _assert_sealed_once(owner, seed, result_id, expected):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult

    async with owner() as db:
        row = await db.get(SessionModelResult, result_id)
        run = await db.get(RuntimeTask, seed["run_id"])
        seals = list(
            await db.scalars(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.result_id == result_id,
                    ChatTranscriptEvent.item_kind == "result_commit",
                    ChatTranscriptEvent.lifecycle.in_(["failed", "needs_reconciliation"]),
                )
            )
        )
        assert row.state == expected and row.seal_json["error_class"] == "cancelled"
        assert run.status == ("killed" if expected == "failed" else "needs_reconciliation")
        assert len(seals) == 1, "A stopped request must have exactly one closing seal"
        return row.version


async def test_round2_prepare_and_cancel_finish_in_the_same_turn(t2_work, owner_sessionmaker, monkeypatch):
    import app.runtime.invoker as invoker
    from app.models.session_v2 import SessionModelResult
    from app.services import web_chat_run_orchestrator as orchestrator, web_chat_runtime

    seed = t2_work
    provider = ScriptedLLMClient([{"content": "Must not send"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    signal = web_chat_runtime.signal_web_chat_cancel
    prepare = orchestrator._prepare_session_model_request
    prepared, accepted, deliver = asyncio.Event(), asyncio.Event(), asyncio.Event()
    returned = []

    async def defer_signal(**kwargs):
        accepted.set()  # The real stop transaction has committed.
        await deliver.wait()
        return await signal(**kwargs)

    async def finish_with_stop(state, **payload):
        result = await prepare(state, **payload)
        prepared.set()
        await accepted.wait()
        returned.append(result)
        # No await: the successful preparation and event finish together.
        state.cancel_event.set()
        deliver.set()
        return result

    monkeypatch.setattr(web_chat_runtime, "signal_web_chat_cancel", defer_signal)
    monkeypatch.setattr(orchestrator, "_prepare_session_model_request", finish_with_stop)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(prepared.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        assert returned and provider.requests == []
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            result_id = row.id
            assert row.provider_request_id == returned[0]
        await _assert_sealed_once(owner_sessionmaker, seed, result_id, "failed")
    finally:
        deliver.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("snapshot", ["placeholder", "full", "full_effectful"])
async def test_round2_crash_after_accepted_stop_is_reclaimed_without_sending(
    t2_work, owner_sessionmaker, monkeypatch, snapshot
):
    import app.runtime.invoker as invoker
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round as rounds, web_chat_runtime
    from sqlalchemy.ext.asyncio import AsyncSession
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import SimulatedWorkerCrash

    seed = t2_work
    provider = ScriptedLLMClient([{"content": "A stopped run must not send"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    ready = asyncio.Event()
    bind, prepare, commit = rounds.bind_round_inputs, rounds.prepare_model_request, AsyncSession.commit

    async def bind_placeholder(db, **kwargs):
        result = await bind(db, **kwargs)
        if snapshot == "placeholder":
            db.info["r23b_crash_after_commit"] = True
        return result

    async def prepare_full(db, **kwargs):
        if snapshot == "full_effectful":
            kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
        result = await prepare(db, **kwargs)
        if snapshot != "placeholder":
            db.info["r23b_crash_after_commit"] = True
        return result

    async def die_before_seal(db):
        await commit(db)
        if db.info.pop("r23b_crash_after_commit", False):
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Abrupt process death runs no preparation-failure callback.
                raise SimulatedWorkerCrash()

    monkeypatch.setattr(rounds, "bind_round_inputs", bind_placeholder)
    monkeypatch.setattr(rounds, "prepare_model_request", prepare_full)
    monkeypatch.setattr(AsyncSession, "commit", die_before_seal)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            assert run.status == "running" and run.metadata_json["cancel_state"] == "cancelling"
            assert row.state == "prepared" and row.seal_json is None
            result_id, version = row.id, row.version
        assert provider.requests == [] and seed["run_id"].hex not in web_chat_runtime._CANCEL_EVENTS
        monkeypatch.setattr(rounds, "bind_round_inputs", bind)
        monkeypatch.setattr(rounds, "prepare_model_request", prepare)
        monkeypatch.setattr(AsyncSession, "commit", commit)
        await _reclaim_stopped_run(owner_sessionmaker, seed, monkeypatch)
        assert provider.requests == [], "A reclaimed stopped task must never send"
        expected = "needs_reconciliation" if snapshot == "full_effectful" else "failed"
        assert await _assert_sealed_once(owner_sessionmaker, seed, result_id, expected) == version + 1
        await web_chat_runtime.execute_web_chat_run(seed["run_id"])
        assert provider.requests == []
        assert await _assert_sealed_once(owner_sessionmaker, seed, result_id, expected) == version + 1
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_round2_asyncio_cancel_in_continuation_prepare_preserves_sent_root(
    t2_work, owner_sessionmaker, monkeypatch
):
    import app.runtime.invoker as invoker
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round as rounds
    from sqlalchemy.ext.asyncio import AsyncSession

    seed = t2_work
    provider = ScriptedLLMClient([{"content": "Partial", "finish_reason": "length"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    ready = asyncio.Event()
    prepare, commit = rounds.prepare_model_request, AsyncSession.commit
    root_id = []

    async def prepare_request(db, **kwargs):
        if not kwargs.get("continuation_index"):
            kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
        result = await prepare(db, **kwargs)
        if kwargs.get("continuation_index"):
            db.info["r23b_continuation_commit"] = True
        else:
            root_id.append(result)
        return result

    async def commit_continuation(db):
        await commit(db)
        if db.info.pop("r23b_continuation_commit", False):
            ready.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(rounds, "prepare_model_request", prepare_request)
    monkeypatch.setattr(AsyncSession, "commit", commit_continuation)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        worker.cancel()  # Process shutdown while the continuation prepare exits.
        await asyncio.gather(worker, return_exceptions=True)
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, seed["run_id"])).status == "running", "Do not kill a possibly-sent root"
            root = await db.scalar(
                select(SessionModelResult).where(SessionModelResult.provider_request_id == root_id[0])
            )
            result_id = root.id
        monkeypatch.setattr(rounds, "prepare_model_request", prepare)
        monkeypatch.setattr(AsyncSession, "commit", commit)
        await _reclaim_stopped_run(owner_sessionmaker, seed, monkeypatch)
        assert len(provider.requests) == 1
        await _assert_sealed_once(owner_sessionmaker, seed, result_id, "needs_reconciliation")
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("lane", ["web", "delegated"])
@pytest.mark.parametrize("cancel_count", [1, 2], ids=["one_cancel", "two_cancels"])
async def test_round2_reconciliation_commit_survives_cancel(
    t2_work, owner_sessionmaker, monkeypatch, lane, cancel_count
):
    from types import SimpleNamespace

    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.models.user import User
    from app.services import session_model_round as rounds, web_chat_run_orchestrator as orchestrator, web_chat_runtime
    from app.services.delegation_session_runtime import DelegationSessionRuntime
    from sqlalchemy.ext.asyncio import AsyncSession
    from tests.services.test_r18a_ambiguous_send_classification import _prepare_round

    seed = t2_work
    common = dict(
        tenant_id=seed["tenant_id"],
        agent_id=seed["agent_id"],
        session_id=seed["session_id"],
        run_id=seed["run_id"],
        turn_id=seed["turn_id"],
    )
    async with owner_sessionmaker() as db:
        request_id = await _prepare_round(
            db, {**common, "round_index": 1}, {"mcp_servers": [{"name": "possibly-sent"}]}
        )
        run, agent, user = (
            await db.get(RuntimeTask, seed["run_id"]),
            await db.get(Agent, seed["agent_id"]),
            await db.get(User, seed["user_id"]),
        )
    event = asyncio.Event()
    if lane == "web":
        state = SimpleNamespace(
            run_uuid=seed["run_id"],
            ports=web_chat_runtime._web_chat_run_ports(),
            cancel_event=event,
            agent=agent,
            user=user,
            runtime_task=run,
            session_id=str(seed["session_id"]),
            metadata={},
            active_provider_request_id=None,
        )

        async def prepare():
            return await orchestrator._prepare_session_model_request(state, round_index=1, messages=[])

        expected_error = rounds.ModelRoundNeedsReconciliation
    else:
        from app.kernel.contracts import SessionRestartRecoveryRequired

        runtime = DelegationSessionRuntime(
            scope=common,
            attempt_owner=f"{run.claimed_by}:{run.claim_version}:{run.attempt_count}",
            session_context=SimpleNamespace(),
        )

        async def prepare():
            return await runtime.prepare(round_index=1, messages=[])

        expected_error = SessionRestartRecoveryRequired
    ready, release = asyncio.Event(), asyncio.Event()
    commit = AsyncSession.commit
    commit_tasks = []

    async def require_reconciliation(db, **_kwargs):
        # Exercise the actual transaction's exception/commit boundary with
        # the same staged row transition as ambiguous prepare takeover.
        row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        row.state, row.reconciliation_owner = "needs_reconciliation", "session_model_round:ambiguous_prepare"
        row.version += 1
        db.info["r23b_reconciliation_commit"] = True
        raise rounds.ModelRoundNeedsReconciliation("model_round_provider_send_is_ambiguous")

    async def pause_commit(db):
        if db.info.pop("r23b_reconciliation_commit", False):
            commit_tasks.append(asyncio.current_task())
            ready.set()
            await release.wait()
        await commit(db)

    monkeypatch.setattr(rounds, "prepare_model_request", require_reconciliation)
    monkeypatch.setattr(AsyncSession, "commit", pause_commit)
    task = asyncio.create_task(prepare())
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        event.set()
        task.cancel()
        await asyncio.sleep(0)
        if cancel_count == 2:
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        async with owner_sessionmaker() as db:
            row = await db.scalar(
                select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id)
            )
            assert row.state == "needs_reconciliation", (lane, row.state, type(outcome).__name__)
        assert isinstance(outcome, expected_error), outcome
        assert len(commit_tasks) == 1, "Cancellation must keep awaiting the same single commit"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_round2_stream_error_during_stop_preserves_r18a(t2_work, owner_sessionmaker, monkeypatch):
    import app.runtime.invoker as invoker
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round as rounds

    seed = t2_work
    ready = asyncio.Event()

    class ErrorOnCancel(ScriptedLLMClient):
        async def stream(self, **kwargs):
            self.requests.append(kwargs)
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise ConnectionError("Provider socket closed during stop")

    provider = ErrorOnCancel([])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    prepare = rounds.prepare_model_request

    async def prepare_effectful(db, **kwargs):
        kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
        return await prepare(db, **kwargs)

    monkeypatch.setattr(rounds, "prepare_model_request", prepare_effectful)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            result_id = row.id
        assert len(provider.requests) == 1
        await _assert_sealed_once(owner_sessionmaker, seed, result_id, "needs_reconciliation")
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_round2_durable_stop_survives_death_before_any_prepared_row(t2_work, owner_sessionmaker, monkeypatch):
    import app.runtime.invoker as invoker
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round as rounds
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import SimulatedWorkerCrash

    seed = t2_work
    provider = ScriptedLLMClient([{"content": "A durable stop must prevent the first send"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    ready = asyncio.Event()
    bind = rounds.bind_round_inputs

    async def die_before_bind_commit(db, **_kwargs):
        await db.execute(select(1))  # A real transaction, with no prepared row yet.
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise SimulatedWorkerCrash()

    monkeypatch.setattr(rounds, "bind_round_inputs", die_before_bind_commit)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        async with owner_sessionmaker() as db:
            assert not list(
                await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            )
        monkeypatch.setattr(rounds, "bind_round_inputs", bind)
        await _reclaim_stopped_run(owner_sessionmaker, seed, monkeypatch)
        assert provider.requests == [], "Recreate the event from durable intent even without a prepared row"
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            assert run.status == "killed" and run.metadata_json["cancel_state"] == "cancelled"
            assert not list(
                await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            )
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_round2_stop_during_preparation_race_cleanup_is_still_unsent(t2_work, owner_sessionmaker, monkeypatch):
    import app.runtime.invoker as invoker
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round as rounds, web_chat_run_orchestrator as orchestrator

    seed = t2_work
    provider = ScriptedLLMClient([{"content": "Must not send"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    _control_channel(monkeypatch, seed, remote=False)
    returned, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    prepare, prepare_round, gather = (
        orchestrator._prepare_session_model_request,
        rounds.prepare_model_request,
        asyncio.gather,
    )
    held = False

    async def effectful_snapshot(db, **kwargs):
        kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "known-unsent"}]}
        return await prepare_round(db, **kwargs)

    async def record_return(state, **payload):
        result = await prepare(state, **payload)
        returned.set()
        return result

    def hold_race_cleanup(*tasks, **kwargs):
        nonlocal held
        joined = gather(*tasks, **kwargs)
        if returned.is_set() and not held and len(tasks) == 2 and all(isinstance(t, asyncio.Task) for t in tasks):
            held = True

            async def after_join():
                result = await joined
                cleanup.set()
                await release.wait()
                return result

            return after_join()
        return joined

    monkeypatch.setattr(rounds, "prepare_model_request", effectful_snapshot)
    monkeypatch.setattr(orchestrator, "_prepare_session_model_request", record_return)
    monkeypatch.setattr(asyncio, "gather", hold_race_cleanup)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(cleanup.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        release.set()
        await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        assert provider.requests == [], "Preparation cleanup is still before the provider handoff"
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            result_id = row.id
        await _assert_sealed_once(owner_sessionmaker, seed, result_id, "failed")
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await gather(worker, return_exceptions=True)


@pytest.mark.parametrize("sent", [True, False], ids=["stream", "prepare"])
async def test_round3_work_error_and_stop_during_cleanup(monkeypatch, sent):
    import app.kernel.engine as kernel
    from app.services.session_model_round import ModelRoundNeedsReconciliation

    event, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    error = ConnectionError("Provider socket failed") if sent else ModelRoundNeedsReconciliation("ambiguous prepare")
    gather = asyncio.gather

    class FailingClient:
        async def stream(self):
            raise error

    async def hold_cleanup(*tasks, **kwargs):
        result = await gather(*tasks, **kwargs)
        cleanup.set()
        await release.wait()
        return result

    monkeypatch.setattr(asyncio, "gather", hold_cleanup)
    task = asyncio.create_task(
        kernel._await_with_cancel(FailingClient().stream, cancel_event=event, provider_request_sent=sent)
    )
    try:
        await asyncio.wait_for(cleanup.wait(), timeout=5)
        event.set()
        release.set()
        outcome = (await gather(task, return_exceptions=True))[0]
        if sent:
            assert isinstance(outcome, kernel._KernelCancelledError), type(outcome).__name__
            assert outcome.provider_request_sent is True
        else:
            assert outcome is error, "Preparation must propagate the original reconciliation exception unchanged"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await gather(task, return_exceptions=True)


async def test_round3_stream_error_cleanup_seals_effectful_request_as_uncertain(
    t2_work, owner_sessionmaker, monkeypatch
):
    import app.runtime.invoker as invoker
    from app.models.session_v2 import SessionModelResult
    from app.services import session_model_round as rounds

    seed = t2_work
    raised, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    gather, prepare = asyncio.gather, rounds.prepare_model_request
    held = False

    class FailingProvider(ScriptedLLMClient):
        async def stream(self, **kwargs):
            self.requests.append(kwargs)
            raised.set()
            raise ConnectionError("Provider failed before the stop arrived")

    async def prepare_effectful(db, **kwargs):
        kwargs["wire_request"] = {**kwargs["wire_request"], "mcp_servers": [{"name": "possibly-sent"}]}
        return await prepare(db, **kwargs)

    def hold_stream_cleanup(*tasks, **kwargs):
        nonlocal held
        joined = gather(*tasks, **kwargs)
        if raised.is_set() and not held and len(tasks) == 2 and all(isinstance(t, asyncio.Task) for t in tasks):
            held = True

            async def after_join():
                result = await joined
                cleanup.set()
                await release.wait()
                return result

            return after_join()
        return joined

    provider = FailingProvider([])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    monkeypatch.setattr(rounds, "prepare_model_request", prepare_effectful)
    monkeypatch.setattr(asyncio, "gather", hold_stream_cleanup)
    _control_channel(monkeypatch, seed, remote=False)
    worker = await _dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(cleanup.wait(), timeout=20)
        assert (await _stop(owner_sessionmaker, seed))["accepted"] is True
        release.set()
        await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        assert len(provider.requests) == 1
        async with owner_sessionmaker() as db:
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            result_id = row.id
        await _assert_sealed_once(owner_sessionmaker, seed, result_id, "needs_reconciliation")
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await gather(worker, return_exceptions=True)
