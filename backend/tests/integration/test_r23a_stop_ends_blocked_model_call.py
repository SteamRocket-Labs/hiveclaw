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
