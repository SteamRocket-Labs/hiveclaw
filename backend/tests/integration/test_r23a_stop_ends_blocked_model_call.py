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
        if kwargs["round_index"] == 2:
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
        assert continuation_cancel == [False], "The continuation's own provider request did not start"
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
