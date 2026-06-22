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
    assert spec.has_own_memory is True
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


# --- type baseline prompts (CC built-in agent ports) -------------------------


def test_builtin_type_prompts_exist_and_nonempty():
    from app.agents.subagent import _TYPE_PRESETS, builtin_type_prompt

    anchors = {
        "explorer": "READ-ONLY",  # CC Explore port
        "worker": "agent for Hive",  # CC general-purpose port
        "critic": "VERDICT",  # CC verification port
    }
    for builtin_type in _TYPE_PRESETS:
        prompt = builtin_type_prompt(builtin_type)
        assert prompt and prompt.strip(), f"builtin type {builtin_type!r} must ship a baseline prompt"
        assert anchors[builtin_type] in prompt


def test_builtin_type_descriptions_exist_and_nonempty():
    from app.agents.subagent import _TYPE_PRESETS, builtin_type_description

    for builtin_type in _TYPE_PRESETS:
        description = builtin_type_description(builtin_type)
        assert description and description.strip(), f"builtin type {builtin_type!r} must ship a whenToUse"
    assert builtin_type_description("made-up-type") == ""


def test_builtin_type_prompt_unknown_type_is_empty():
    from app.agents.subagent import builtin_type_prompt

    assert builtin_type_prompt("made-up-type") == ""


@pytest.mark.asyncio
async def test_spawn_builtin_type_injects_baseline_prompt():
    captured: list = []
    result = await _spawn_one(
        _ctx(),
        SubagentJob(spec=explorer_spec("scout"), task="t"),
        invoke=_ok_invoke(capture=captured),
    )
    assert result.ok
    assert "READ-ONLY" in captured[0].standalone_system_prompt


@pytest.mark.asyncio
async def test_spawn_custom_prompt_replaces_baseline():
    captured: list = []
    spec = SubagentSpec(name="scout", type="explorer", system_prompt="Custom marching orders.")
    result = await _spawn_one(_ctx(), SubagentJob(spec=spec, task="t"), invoke=_ok_invoke(capture=captured))
    assert result.ok
    # Mirror of the allowed_tools preset fallback: an explicit body REPLACES
    # the type baseline (CC semantics: the definition body IS the prompt).
    assert "Custom marching orders." in captured[0].standalone_system_prompt
    assert "READ-ONLY MODE" not in captured[0].standalone_system_prompt


@pytest.mark.asyncio
async def test_spawn_unknown_type_has_no_baseline():
    captured: list = []
    spec = SubagentSpec(name="x", type="bespoke", system_prompt="", allowed_tools=("read_file",))
    result = await _spawn_one(_ctx(), SubagentJob(spec=spec, task="t"), invoke=_ok_invoke(capture=captured))
    assert result.ok
    assert captured[0].standalone_system_prompt == ""


# --- message assembly (fork) ------------------------------------------------


def test_build_messages_fork_none_ignores_context_override():
    msgs = _build_subagent_messages("task", fork="none", context_brief="BRIEF")
    assert msgs == [{"role": "user", "content": "task"}]


def test_build_messages_fork_all_prepends():
    msgs = _build_subagent_messages("task", fork="all", context_brief="HISTORY")
    assert msgs == [
        {"role": "user", "content": "HISTORY"},
        {"role": "user", "content": "task"},
    ]


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
async def test_spawn_writes_replayable_t0_sidechain(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    ctx = _ctx(
        parent_agent_id=parent_agent_id,
        tenant_id=tenant_id,
        trace_id="trace-subagent",
        parent_session_id="parent-session",
    )
    long_tool_result = "tool evidence " + ("完整证据" * 600) + " END_OF_SUBAGENT_TOOL"

    monkeypatch.setattr(
        "app.memory.t0.ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    async def invoke(request):
        await request.on_tool_call(
            {
                "status": "done",
                "name": "web_fetch",
                "args": {"url": "https://example.com/report"},
                "result": long_tool_result,
            }
        )
        return SimpleNamespace(content="subagent final answer", tokens_used=10)

    result = await _spawn_one(ctx, SubagentJob(spec=explorer_spec("scout"), task="investigate X"), invoke=invoke)

    assert result.ok
    session_dirs = list((tmp_path / str(parent_agent_id) / "memory" / "t0" / "sessions").iterdir())
    assert len(session_dirs) == 1
    events = replay_t0_session_events(agent_id=parent_agent_id, session_id=session_dirs[0].name, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [
        ("user_message", "user"),
        ("tool_result", "tool"),
        ("assistant_message", "assistant"),
        ("segment_boundary", "system"),
    ]
    assert events[0].content == "investigate X"
    assert "END_OF_SUBAGENT_TOOL" in events[1].content
    assert events[2].content == "subagent final answer"
    assert events[-1].content == "subagent_complete"
    assert events[0].metadata["subagent_name"] == "scout"
    assert events[0].metadata["parent_session_id"] == "parent-session"


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
async def test_unknown_type_without_explicit_tools_fails_closed():
    called = False

    async def invoke(request):
        nonlocal called
        called = True
        return SimpleNamespace(content="x", tokens_used=0)

    result = await _spawn_one(
        _ctx(),
        SubagentJob(spec=SubagentSpec(name="mystery", type="custom-missing-tools"), task="t"),
        invoke=invoke,
    )

    assert result.status == "failed"
    assert "no allowed tools" in (result.error or "")
    assert called is False


@pytest.mark.asyncio
async def test_spawn_truncates_output():
    result = await _spawn_one(
        _ctx(),
        SubagentJob(spec=explorer_spec("e"), task="t"),
        budget=SubagentBudget(max_output_chars=5),
        invoke=_ok_invoke(content="0123456789"),
    )
    assert result.content == "01234"


@pytest.mark.asyncio
async def test_spawn_uses_spec_model_override():
    captured: list = []
    parent_model = SimpleNamespace(model="parent")
    child_model = SimpleNamespace(model="child")

    async def resolve_model(model_name: str):
        assert model_name == "child"
        return child_model

    ctx = _ctx(model=parent_model, model_resolver=resolve_model)
    spec = SubagentSpec(name="e", type="explorer", model="child")

    await _spawn_one(ctx, SubagentJob(spec=spec, task="t"), invoke=_ok_invoke(capture=captured))

    assert captured[0].model is child_model


@pytest.mark.asyncio
async def test_spawn_captures_sources_under_budget():
    captured_request: list = []

    async def invoke(request):
        captured_request.append(request)
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "args": {"url": "https://example.com/a"},
                "result": "abcdef",
            }
        )
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "args": {"url": "https://example.com/b"},
                "result": "ignored by max_sources",
            }
        )
        return SimpleNamespace(content="digest", tokens_used=1)

    result = await _spawn_one(
        _ctx(),
        SubagentJob(spec=explorer_spec("e"), task="t"),
        budget=SubagentBudget(max_sources=1, max_source_chars=3),
        invoke=invoke,
    )

    assert captured_request[0].on_tool_call is not None
    assert result.sources == [{"url": "https://example.com/a", "tool_name": "web_fetch", "content": "abc"}]


@pytest.mark.asyncio
async def test_spawn_injects_memory_and_records_distilled_how(tmp_path, monkeypatch):
    from app.agents import subagent_memory as mem_mod
    from app.agents.subagent_memory import SubagentMemoryStore

    def allowed_decision(content, **kwargs):
        return SimpleNamespace(
            rejected=False,
            reason="",
            content=content,
            metadata={"entry_id": "e1", "sensitivity": "internal"},
        )

    monkeypatch.setattr(mem_mod, "prepare_memory_write", allowed_decision)
    memory_store = SubagentMemoryStore(tmp_path)
    memory_store.record_how("e", "Prefer official docs.", category="source_calibration")
    captured: list = []

    async def invoke(request):
        captured.append(request)
        return SimpleNamespace(content="new lesson", tokens_used=1)

    ctx = _ctx(
        memory_store=memory_store,
        memory_distiller=lambda run_log: [("pitfall", "Avoid thin snippets.")],
    )

    result = await _spawn_one(ctx, SubagentJob(spec=explorer_spec("e"), task="t"), invoke=invoke)

    assert result.ok
    assert "Subagent Memory" in captured[0].standalone_system_prompt
    assert "Prefer official docs." in captured[0].standalone_system_prompt
    assert "Avoid thin snippets." in memory_store.load("e")


@pytest.mark.asyncio
async def test_spawn_disable_tools_reaches_invocation_request():
    """RC11 semantics for leaf presets: a synthesis-style subagent must run
    with ZERO tools exposed — ``allowed_tools=()`` is not enough (it falls
    back to the type preset), so the spec carries an explicit switch that
    threads to ``AgentInvocationRequest.disable_tools``."""
    captured: list = []
    spec = SubagentSpec(name="synth", type="worker", disable_tools=True)

    result = await _spawn_one(_ctx(), SubagentJob(spec=spec, task="synthesize"), invoke=_ok_invoke(capture=captured))

    assert result.ok
    assert captured[0].disable_tools is True
    # Default stays off — existing subagents are untouched.
    captured.clear()
    await _spawn_one(
        _ctx(), SubagentJob(spec=SubagentSpec(name="e", type="explorer"), task="t"), invoke=_ok_invoke(capture=captured)
    )
    assert captured[0].disable_tools is False
