"""Tests for cut ③: worker/critic type presets + fork three-way (none/brief/all)."""

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
    _build_brief_from_messages,
    _build_subagent_messages,
    _spawn_one,
    resolve_subagent_tools,
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


# --- type presets -----------------------------------------------------------


def test_worker_preset_allows_writes():
    allowed, excluded = resolve_subagent_tools(SubagentSpec(name="w", type=SUBAGENT_TYPE_WORKER))
    assert "write_file" in allowed
    assert "edit_file" in allowed
    assert "read_file" in allowed
    assert "spawn_subagent" in excluded  # recursion guard still applies


def test_critic_preset_is_read_only():
    allowed, _ = resolve_subagent_tools(SubagentSpec(name="c", type=SUBAGENT_TYPE_CRITIC))
    assert "read_file" in allowed
    # critic verifies, never mutates ("只验不改")
    assert "write_file" not in allowed
    assert "edit_file" not in allowed


def test_unknown_type_has_no_preset():
    allowed, _ = resolve_subagent_tools(SubagentSpec(name="x", type="mystery"))
    assert allowed == ()


# --- brief construction -----------------------------------------------------


def test_build_brief_from_messages_formats_roles():
    brief = _build_brief_from_messages([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}])
    assert "User: hello" in brief
    assert "Assistant: hi" in brief


def test_build_brief_keeps_last_n_and_truncates():
    msgs = [{"role": "user", "content": f"m{i}-" + "x" * 1000} for i in range(20)]
    brief = _build_brief_from_messages(msgs)
    assert len(brief) <= 4000 + 4  # cap + leading "...\n"
    assert brief.startswith("...")


def test_build_brief_empty():
    assert _build_brief_from_messages([]) == ""


# --- fork three-way ---------------------------------------------------------


def test_fork_none_is_task_only():
    msgs = _build_subagent_messages("task", fork="none", parent_messages=[{"role": "user", "content": "ctx"}])
    assert msgs == [{"role": "user", "content": "task"}]


def test_fork_brief_auto_compresses_parent():
    parent = [{"role": "user", "content": "earlier question"}]
    msgs = _build_subagent_messages("task", fork="brief", parent_messages=parent)
    assert len(msgs) == 2
    assert "earlier question" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "task"}


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


def test_explicit_brief_wins_over_parent_messages():
    msgs = _build_subagent_messages(
        "task",
        fork="brief",
        context_brief="EXPLICIT",
        parent_messages=[{"role": "user", "content": "ignored"}],
    )
    assert msgs[0] == {"role": "user", "content": "EXPLICIT"}
    assert "ignored" not in msgs[0]["content"]


# --- integration: _spawn_one threads ctx.parent_messages --------------------


@pytest.mark.asyncio
async def test_spawn_threads_parent_messages_into_brief():
    captured: list = []

    async def invoke(request):
        captured.append(request)
        return SimpleNamespace(content="ok", tokens_used=1)

    ctx = _ctx(parent_messages=[{"role": "user", "content": "prior context"}])
    job = SubagentJob(spec=SubagentSpec(name="w", type=SUBAGENT_TYPE_WORKER), task="do")
    await _spawn_one(ctx, job, fork="brief", invoke=invoke)
    req = captured[0]
    # brief auto-built from ctx.parent_messages, prepended before the task
    assert len(req.messages) == 2
    assert "prior context" in req.messages[0]["content"]
    assert req.messages[1] == {"role": "user", "content": "do"}
