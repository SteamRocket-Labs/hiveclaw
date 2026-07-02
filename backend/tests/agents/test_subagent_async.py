"""Tests for cut ④ P0: async background spawn + Signal-based completion (anti busy-poll)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agents.coordination import coordination_runtime
import app.agents.subagent as subagent_module
from app.agents.subagent import (
    SUBAGENT_COMPLETION_SIGNAL,
    SubagentSpawnContext,
    consume_subagent_signals,
    explorer_spec,
    spawn_subagent,
)


def _ctx(**overrides) -> SubagentSpawnContext:
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def _ok_invoke(content: str = "bg-digest", tokens: int = 3):
    async def invoke(_request):
        return SimpleNamespace(content=content, tokens_used=tokens)

    return invoke


@pytest.mark.asyncio
async def test_sync_spawn_is_default_and_resolved():
    handle = await spawn_subagent(_ctx(), explorer_spec("s"), "task", invoke=_ok_invoke())
    assert handle.result is not None
    assert handle.result.ok


@pytest.mark.asyncio
async def test_background_spawn_returns_unresolved_handle():
    coordination_runtime.reset()
    handle = await spawn_subagent(
        _ctx(trace_id="thr-bg"), explorer_spec("bg"), "task", run_in_background=True, invoke=_ok_invoke()
    )
    assert handle.result is None  # not resolved yet — runs in the background


@pytest.mark.asyncio
async def test_background_emits_completion_signal():
    coordination_runtime.reset()
    ctx = _ctx(trace_id="thr-1")
    await spawn_subagent(
        ctx, explorer_spec("bg"), "task", run_in_background=True, invoke=_ok_invoke(content="bg-result")
    )

    # let the background task finish and emit its Signal
    signals: list = []
    for _ in range(20):
        await asyncio.sleep(0.01)
        signals = consume_subagent_signals(ctx.parent_agent_id, thread_id="thr-1")
        if signals:
            break

    assert len(signals) == 1
    assert signals[0].signal_type == SUBAGENT_COMPLETION_SIGNAL
    assert "bg-result" in signals[0].content
    assert consume_subagent_signals(ctx.parent_agent_id, thread_id="thr-1") == []


@pytest.mark.asyncio
async def test_background_spawn_respects_subagent_budget(monkeypatch):
    coordination_runtime.reset()
    monkeypatch.setattr(subagent_module, "_background_subagent_capacity", lambda: 1, raising=False)
    subagent_module._BACKGROUND_SUBAGENT_SEMAPHORE = None
    subagent_module._BACKGROUND_SUBAGENT_SEMAPHORE_LIMIT = None

    active = 0
    max_active = 0

    async def invoke(_request):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.02)
            return SimpleNamespace(content="budgeted", tokens_used=1)
        finally:
            active -= 1

    ctx = _ctx(trace_id="thr-budget")
    for idx in range(3):
        await spawn_subagent(
            ctx,
            explorer_spec(f"bg-{idx}"),
            "task",
            run_in_background=True,
            invoke=invoke,
        )

    signals: list = []
    for _ in range(80):
        await asyncio.sleep(0.01)
        signals.extend(consume_subagent_signals(ctx.parent_agent_id))
        if len(signals) == 3:
            break

    assert len(signals) == 3
    assert max_active == 1


@pytest.mark.asyncio
async def test_consume_filters_foreign_signals():
    coordination_runtime.reset()
    parent = uuid.uuid4()
    # an unrelated signal to the same parent must not be returned
    coordination_runtime.send_signal(
        from_agent_id="someone",
        to_agent_id=str(parent),
        content="unrelated",
        signal_type="other_kind",
    )
    assert consume_subagent_signals(parent) == []


def test_consume_subagent_signals_is_read_once():
    coordination_runtime.reset()
    parent = uuid.uuid4()
    coordination_runtime.send_signal(
        from_agent_id="subagent:bg",
        to_agent_id=str(parent),
        content="done",
        signal_type=SUBAGENT_COMPLETION_SIGNAL,
        thread_id="thr-once",
    )

    first = consume_subagent_signals(parent, thread_id="thr-once")
    second = consume_subagent_signals(parent, thread_id="thr-once")

    assert len(first) == 1
    assert second == []
