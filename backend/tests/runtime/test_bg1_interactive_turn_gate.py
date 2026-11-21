import asyncio

import pytest


@pytest.fixture
def gate():
    from app.runtime import interactive_turn_gate

    return interactive_turn_gate


async def test_idle_wait_returns_without_yielding(gate):
    callback = asyncio.Event()
    asyncio.get_running_loop().call_soon(callback.set)
    assert await gate.wait_for_no_interactive_turns() == 0
    assert not callback.is_set()


async def test_wait_resumes_when_turn_exits(gate):
    async with gate.interactive_turn():
        waiter = asyncio.create_task(gate.wait_for_no_interactive_turns())
        await asyncio.sleep(0)
        assert not waiter.done()
    assert await asyncio.wait_for(waiter, timeout=1) > 0


async def test_wait_resumes_at_cap_with_turn_still_held(gate, monkeypatch):
    monkeypatch.setattr(gate, "BACKGROUND_WAIT_CAP_SECONDS", 0.02)
    async with gate.interactive_turn():
        elapsed = await asyncio.wait_for(gate.wait_for_no_interactive_turns(), timeout=1)
        assert elapsed >= 0.019


async def test_budget_expired_while_idle_returns_without_yielding(gate):
    budget = gate.BackgroundWaitBudget(0.02)
    await asyncio.sleep(0.04)
    callback = asyncio.Event()
    async with gate.interactive_turn():
        asyncio.get_running_loop().call_soon(callback.set)
        await budget.wait()
        assert not callback.is_set()


async def test_wait_spans_nested_and_concurrent_turns(gate):
    entered, release = asyncio.Event(), asyncio.Event()

    async def other_turn():
        async with gate.interactive_turn():
            entered.set()
            await release.wait()

    async with gate.interactive_turn():
        async with gate.interactive_turn():
            other = asyncio.create_task(other_turn())
            await entered.wait()
            waiter = asyncio.create_task(gate.wait_for_no_interactive_turns())
            await asyncio.sleep(0)
            assert not waiter.done()
        assert not waiter.done()
    try:
        await asyncio.sleep(0)
        assert not waiter.done()
    finally:
        release.set()
        await other
        await asyncio.wait_for(waiter, timeout=1)


async def test_exception_releases_turn(gate):
    with pytest.raises(ValueError, match="turn failed"):
        async with gate.interactive_turn():
            raise ValueError("turn failed")
    assert await gate.wait_for_no_interactive_turns() == 0


async def test_cancellation_releases_turn(gate):
    entered = asyncio.Event()

    async def turn():
        async with gate.interactive_turn():
            entered.set()
            await asyncio.Event().wait()

    held = asyncio.create_task(turn())
    await entered.wait()
    held.cancel()
    with pytest.raises(asyncio.CancelledError):
        await held
    assert await gate.wait_for_no_interactive_turns() == 0
