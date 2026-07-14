"""Tests for worker/critic type presets + the binary fork (none/all).

Exec/automation CC-alignment §5.2: fork is binary (CC fresh vs full-fork); the
old ``brief`` middle level was removed and legacy definitions coerce to ``all``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.subagent import (
    SUBAGENT_TYPE_CRITIC,
    SUBAGENT_TYPE_WORKER,
    SubagentJob,
    SubagentSpawnContext,
    SubagentSpec,
    _build_subagent_messages,
    _spawn_one,
    resolve_subagent_tools,
)
from app.agents.subagent_definition import _coerce_isolation


def _ctx(**overrides) -> SubagentSpawnContext:
    ctx = SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# --- type presets -----------------------------------------------------------


def test_worker_preset_allows_writes():
    allowed, excluded = resolve_subagent_tools(SubagentSpec(name="w", type=SUBAGENT_TYPE_WORKER))
    assert "write_file" in allowed
    assert "edit_file" in allowed
    assert "read_file" in allowed
    assert "spawn_subagent" not in excluded  # depth/cycle/budget govern recursion


def test_critic_preset_is_read_only():
    allowed, _ = resolve_subagent_tools(SubagentSpec(name="c", type=SUBAGENT_TYPE_CRITIC))
    assert "read_file" in allowed
    # critic verifies, never mutates ("只验不改")
    assert "write_file" not in allowed
    assert "edit_file" not in allowed


def test_unknown_type_has_no_preset():
    allowed, _ = resolve_subagent_tools(SubagentSpec(name="x", type="mystery"))
    assert allowed == ()


# --- binary fork ------------------------------------------------------------


def test_fork_none_is_task_only():
    msgs = _build_subagent_messages("task", fork="none", parent_messages=[{"role": "user", "content": "ctx"}])
    assert msgs == [{"role": "user", "content": "task"}]


def test_fork_all_extends_parent_verbatim():
    parent = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    msgs = _build_subagent_messages("task", fork="all", parent_messages=parent)
    assert msgs == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "task"},
    ]


def test_explicit_context_brief_wins_over_parent_messages_for_all():
    msgs = _build_subagent_messages(
        "task",
        fork="all",
        context_brief="EXPLICIT",
        parent_messages=[{"role": "user", "content": "ignored"}],
    )
    assert msgs[0] == {"role": "user", "content": "EXPLICIT"}
    assert "ignored" not in msgs[0]["content"]


def test_legacy_brief_isolation_coerces_to_all():
    # A stored definition with the retired ``brief`` level upgrades to full context.
    assert _coerce_isolation("brief") == "all"
    assert _coerce_isolation("none") == "none"
    assert _coerce_isolation("all") == "all"


# --- integration: _spawn_one threads ctx.parent_messages --------------------


@pytest.mark.asyncio
async def test_spawn_threads_parent_messages_verbatim_for_all():
    captured: list = []

    async def invoke(request):
        captured.append(request)
        return SimpleNamespace(content="ok", tokens_used=1)

    ctx = _ctx(parent_messages=[{"role": "user", "content": "prior context"}])
    job = SubagentJob(spec=SubagentSpec(name="w", type=SUBAGENT_TYPE_WORKER), task="do")
    await _spawn_one(ctx, job, fork="all", invoke=invoke)
    req = captured[0]
    # parent messages prepended verbatim before the task (full-context fork)
    assert len(req.messages) == 2
    assert req.messages[0] == {"role": "user", "content": "prior context"}
    assert req.messages[1] == {"role": "user", "content": "do"}
