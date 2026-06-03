"""Tests for cut ④ P0: async background spawn + Signal-based completion (anti busy-poll)."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agents.coordination import coordination_runtime
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
