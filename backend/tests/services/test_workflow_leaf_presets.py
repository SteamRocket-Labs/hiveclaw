"""DR-1 red tests: the leaf preset registry — DR leaves get system-side
capability injection (tools/prompt/RC11) + deterministic pre/post processing
around the REAL spawn, while preset-less leaves spawn exactly as before."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents.subagent import SubagentHandle, SubagentResult, SubagentSpawnContext
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_launch import build_subagent_leaf_executor
from app.services.workflow_leaf_presets import (
    LeafPreset,
    register_leaf_preset,
    reset_leaf_presets,
    resolve_leaf_preset,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_leaf_presets()
    yield
    reset_leaf_presets()


def _ctx() -> SubagentSpawnContext:
    return SubagentSpawnContext(
        parent_agent_id=uuid.uuid4(),
        parent_user_id=uuid.uuid4(),
        model=SimpleNamespace(provider="anthropic", model="claude-x"),
        tenant_id=uuid.uuid4(),
    )


def _request(leaf_name: str = "deep_research_explorer") -> LeafRequest:
    return LeafRequest(
        run_id=str(uuid.uuid4()),
        step_id="explore",
        leaf=SimpleNamespace(name=leaf_name, type="explorer", max_tool_rounds=6),
        task="Explore topic X",
        tenant_id=str(uuid.uuid4()),
        leaf_id="item-0",
    )


def _fake_spawn(captured: list):
    async def spawn(ctx, spec, task, *, budget=None):
        captured.append({"spec": spec, "task": task})
        return SubagentHandle(
            name=spec.name,
            trace_id="tr-test",
            depth=1,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="found", tokens_used=7),
        )

    return spawn


async def test_preset_overrides_reach_spawn():
    register_leaf_preset(
        "deep_research_explorer",
        LeafPreset(
            allowed_tools=("web_search", "web_fetch"),
            excluded_tools=("write_file",),
            system_prompt="You are a source-ledger-backed research explorer.",
            disable_tools=False,
        ),
    )
    captured: list = []
    executor = build_subagent_leaf_executor(_ctx(), spawn=_fake_spawn(captured))

    outcome = await executor(_request())

    assert outcome.ok
    spec = captured[0]["spec"]
    assert spec.allowed_tools == ("web_search", "web_fetch")
    assert "write_file" in spec.excluded_tools
    assert "research explorer" in spec.system_prompt
    assert spec.disable_tools is False


async def test_disable_tools_preset_flows_into_spec():
    register_leaf_preset("deep_research_synthesizer", LeafPreset(disable_tools=True))
    captured: list = []
    executor = build_subagent_leaf_executor(_ctx(), spawn=_fake_spawn(captured))

    await executor(_request("deep_research_synthesizer"))

    assert captured[0]["spec"].disable_tools is True


async def test_leaf_without_preset_spawns_unchanged():
    """Office and any non-DR workflow must be byte-for-byte unaffected."""
    captured: list = []
    executor = build_subagent_leaf_executor(_ctx(), spawn=_fake_spawn(captured))

    outcome = await executor(_request("office-doc-parser"))

    assert outcome.ok and outcome.output == {"text": "found", "sources": []}
    spec = captured[0]["spec"]
    assert spec.allowed_tools == ()
    assert spec.excluded_tools == ()
    assert spec.system_prompt == ""
    assert spec.disable_tools is False
    assert resolve_leaf_preset("office-doc-parser") is None


async def test_pre_process_rewrites_task_and_post_process_transforms_outcome():
    async def pre(request, ctx):
        return f"[briefed] {request.task}"

    async def post(request, ctx, result, outcome):
        return LeafOutcome(
            ok=outcome.ok,
            output={**(outcome.output or {}), "post": "ran", "leaf_id": request.leaf_id},
            tokens_used=outcome.tokens_used,
        )

    register_leaf_preset("deep_research_explorer", LeafPreset(pre_process=pre, post_process=post))
    captured: list = []
    executor = build_subagent_leaf_executor(_ctx(), spawn=_fake_spawn(captured))

    outcome = await executor(_request())

    assert captured[0]["task"] == "[briefed] Explore topic X"
    assert outcome.output["post"] == "ran"
    assert outcome.output["leaf_id"] == "item-0"
