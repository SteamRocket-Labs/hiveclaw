"""Tests for the subagent source capability (cut ①, runtime-only).

Style mirrors tests/deep_research/test_worker.py: inject a fake ``invoke`` in
place of the real kernel and assert on the AgentInvocationRequest fields + the
returned SubagentResult. No DB, no mocks of the functional core.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agents.subagent import (
    SUBAGENT_TYPE_EXPLORER,
    SubagentBudget,
    SubagentHandle,
    SubagentJob,
    SubagentResult,
    SubagentSpawnContext,
    SubagentSpec,
    _build_subagent_messages,
    _spawn_one,
    explorer_spec,
    fanout_subagents,
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


def _ok_invoke(content: str = "digest", tokens: int = 10, capture: list | None = None):
    async def invoke(request):
        if capture is not None:
            capture.append(request)
        return SimpleNamespace(content=content, tokens_used=tokens)

    return invoke


# --- contracts --------------------------------------------------------------


def test_subagent_result_ok_property():
    assert SubagentResult(name="a", type="explorer", status="completed").ok is True
    assert SubagentResult(name="a", type="explorer", status="failed").ok is False
    assert SubagentResult(name="a", type="explorer", status="timed_out").ok is False


def test_subagent_spec_defaults():
    spec = SubagentSpec(name="x")
    assert spec.type == SUBAGENT_TYPE_EXPLORER
    assert spec.isolation == "none"
    assert spec.has_own_memory is True  # schema complete even though cut ① ignores it
    assert spec.soul is False


def test_subagent_handle_defaults():
    handle = SubagentHandle(name="a", trace_id="tr", depth=1)
    assert handle.result is None


def test_explorer_spec_constructs_explorer():
    spec = explorer_spec("scout", max_tool_rounds=12)
    assert spec.type == SUBAGENT_TYPE_EXPLORER
    assert spec.isolation == "none"
    assert spec.max_tool_rounds == 12


# --- tool resolution --------------------------------------------------------


def test_resolve_tools_explorer_uses_preset():
    allowed, excluded = resolve_subagent_tools(SubagentSpec(name="e", type="explorer"))
    assert "web_search" in allowed
    assert "read_file" in allowed
    # recursion guard + delegation always denied
    assert "delegate_to_agent" in excluded
    assert "spawn_subagent" in excluded
    assert "fanout_subagents" in excluded


def test_resolve_tools_explicit_allowed_overrides_preset():
    allowed, _ = resolve_subagent_tools(SubagentSpec(name="w", type="worker", allowed_tools=("read_file",)))
    assert allowed == ("read_file",)


def test_resolve_tools_dedups_exclusions():
    _, excluded = resolve_subagent_tools(
        SubagentSpec(name="e", type="explorer", excluded_tools=("delegate_to_agent", "custom_tool"))
    )
    assert excluded.count("delegate_to_agent") == 1
    assert "custom_tool" in excluded


# --- message assembly (fork) ------------------------------------------------


def test_build_messages_fork_none_ignores_brief():
    msgs = _build_subagent_messages("task", fork="none", context_brief="BRIEF")
    assert msgs == [{"role": "user", "content": "task"}]


def test_build_messages_fork_brief_prepends():
    msgs = _build_subagent_messages("task", fork="brief", context_brief="BRIEF")
    assert msgs == [
        {"role": "user", "content": "BRIEF"},
        {"role": "user", "content": "task"},
    ]


def test_build_messages_fork_all_prepends():
    msgs = _build_subagent_messages("task", fork="all", context_brief="HISTORY")
    assert msgs == [
        {"role": "user", "content": "HISTORY"},
        {"role": "user", "content": "task"},
    ]


def test_build_messages_brief_without_context_is_task_only():
    msgs = _build_subagent_messages("task", fork="brief", context_brief=None)
    assert msgs == [{"role": "user", "content": "task"}]


# --- _spawn_one -------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_builds_governed_request():
    captured: list = []
    token = object()
    ctx = _ctx(delegation_token=token, trace_id="tr-1", parent_agent_name="HR")
    spec = SubagentSpec(name="market-explorer", type="explorer", max_tool_rounds=6)

    result = await _spawn_one(ctx, SubagentJob(spec=spec, task="investigate X"), invoke=_ok_invoke(capture=captured))

    assert result.ok
    assert result.content == "digest"
    assert result.tokens_used == 10
    req = captured[0]
    assert req.core_tools_only is False
    assert req.expand_tools is False
    assert req.agent_id == ctx.parent_agent_id  # subagent reuses the parent identity
    assert req.user_id == ctx.parent_user_id
    assert req.model is ctx.model  # child inherits the parent model
    assert req.max_tool_rounds == 6  # spec override wins over budget default
    assert req.delegation_token is token  # governance token threaded through
    assert "delegate_to_agent" in req.excluded_tool_names
    assert "spawn_subagent" in req.excluded_tool_names  # recursion guard
    assert "web_search" in req.allowed_tool_names  # explorer preset applied
    assert req.messages == [{"role": "user", "content": "investigate X"}]  # fork=none
    assert req.session_context.source == "subagent"
    assert req.session_context.channel == "internal"


@pytest.mark.asyncio
async def test_spawn_budget_rounds_when_spec_unset():
    captured: list = []
    ctx = _ctx()
    await _spawn_one(
        ctx,
        SubagentJob(spec=explorer_spec("e"), task="t"),  # max_tool_rounds=None
        budget=SubagentBudget(max_tool_rounds=15),
        invoke=_ok_invoke(capture=captured),
    )
    assert captured[0].max_tool_rounds == 15


@pytest.mark.asyncio
async def test_spawn_success_returns_completed():
    ctx = _ctx()
    result = await _spawn_one(ctx, SubagentJob(spec=explorer_spec("e"), task="t"), invoke=_ok_invoke(content="  hi  "))
    assert result.status == "completed"
    assert result.content == "hi"  # stripped


@pytest.mark.asyncio
async def test_spawn_isolates_exception():
    async def invoke(request):
        raise RuntimeError("boom")

    result = await _spawn_one(_ctx(), SubagentJob(spec=explorer_spec("e"), task="t"), invoke=invoke)
    assert result.status == "failed"
    assert "RuntimeError" in (result.error or "")
    assert "boom" in (result.error or "")


@pytest.mark.asyncio
async def test_spawn_times_out():
    async def invoke(request):
        await asyncio.sleep(1)
        return SimpleNamespace(content="late", tokens_used=0)

    result = await _spawn_one(
        _ctx(),
        SubagentJob(spec=explorer_spec("e"), task="t"),
        budget=SubagentBudget(timeout_seconds=0.01),
        invoke=invoke,
    )
    assert result.status == "timed_out"
    assert "timed out" in (result.error or "")


@pytest.mark.asyncio
async def test_spawn_depth_limited_does_not_invoke():
    called = False

    async def invoke(request):
        nonlocal called
        called = True
        return SimpleNamespace(content="x", tokens_used=0)

    ctx = _ctx(depth=2, max_depth=2)  # child_depth = 3 > 2
    result = await _spawn_one(ctx, SubagentJob(spec=explorer_spec("e"), task="t"), invoke=invoke)
    assert result.status == "depth_limited"
    assert called is False  # the kernel must never be invoked past the depth limit


@pytest.mark.asyncio
async def test_spawn_truncates_output():
    result = await _spawn_one(
        _ctx(),
        SubagentJob(spec=explorer_spec("e"), task="t"),
        budget=SubagentBudget(max_output_chars=5),
        invoke=_ok_invoke(content="0123456789"),
    )
    assert result.content == "01234"


# --- fanout_subagents -------------------------------------------------------


@pytest.mark.asyncio
async def test_fanout_empty_returns_empty():
    assert await fanout_subagents(_ctx(), [], invoke=_ok_invoke()) == []


@pytest.mark.asyncio
async def test_fanout_preserves_order():
    async def invoke(request):
        task = request.messages[-1]["content"]
        await asyncio.sleep(0.001 * (5 - int(task)))  # later tasks finish sooner
        return SimpleNamespace(content=task, tokens_used=1)

    jobs = [SubagentJob(spec=explorer_spec(f"e{i}"), task=str(i)) for i in range(5)]
    results = await fanout_subagents(_ctx(), jobs, max_concurrency=5, invoke=invoke)
    assert [r.content for r in results] == ["0", "1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_fanout_respects_concurrency():
    active = 0
    peak = 0

    async def invoke(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return SimpleNamespace(content="x", tokens_used=1)

    jobs = [SubagentJob(spec=explorer_spec(f"e{i}"), task="t") for i in range(8)]
    results = await fanout_subagents(_ctx(), jobs, max_concurrency=3, invoke=invoke)
    assert len(results) == 8
    assert all(r.ok for r in results)
    assert peak <= 3  # semaphore caps concurrency


@pytest.mark.asyncio
async def test_fanout_isolates_partial_failure():
    async def invoke(request):
        if request.messages[-1]["content"] == "fail":
            raise RuntimeError("nope")
        return SimpleNamespace(content="ok", tokens_used=1)

    jobs = [
        SubagentJob(spec=explorer_spec("a"), task="ok-1"),
        SubagentJob(spec=explorer_spec("b"), task="fail"),
        SubagentJob(spec=explorer_spec("c"), task="ok-2"),
    ]
    results = await fanout_subagents(_ctx(), jobs, invoke=invoke)
    assert [r.status for r in results] == ["completed", "failed", "completed"]


@pytest.mark.asyncio
async def test_fanout_abort_stops_remaining():
    ran: list[str] = []

    async def invoke(request):
        task = request.messages[-1]["content"]
        ran.append(task)
        if task == "fail":
            raise RuntimeError("nope")
        await asyncio.sleep(0.01)
        return SimpleNamespace(content="ok", tokens_used=1)

    jobs = [SubagentJob(spec=explorer_spec("x"), task="fail")]
    jobs += [SubagentJob(spec=explorer_spec(f"y{i}"), task=f"ok{i}") for i in range(5)]

    results = await fanout_subagents(_ctx(), jobs, max_concurrency=1, on_partial_failure="abort", invoke=invoke)
    assert results[0].status == "failed"
    aborted = [r for r in results[1:] if r.error and "aborted" in r.error]
    assert len(aborted) >= 1  # later lanes short-circuit once a sibling fails
    assert len(ran) < 6  # not every follower actually invoked the kernel
