"""DR-1 red tests: the leaf preset registry — DR leaves get system-side
capability injection (tools/prompt/RC11) + deterministic pre/post processing
around the REAL spawn, while preset-less leaves spawn exactly as before."""

from __future__ import annotations

import uuid
import asyncio
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


def _request(leaf_name: str = "source_explorer") -> LeafRequest:
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
        captured.append({"ctx": ctx, "spec": spec, "task": task})
        return SubagentHandle(
            name=spec.name,
            trace_id="tr-test",
            depth=1,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="found", tokens_used=7),
        )

    return spawn


_WORKFLOW_LEAF_RECOVERY_NAMESPACE = uuid.UUID("6f8d4e61-4f22-5d85-9d3e-bc7396fbe2ea")


def _expected_recovery_session_id(*, run_id: str, step_id: str, leaf_id: str | None) -> str:
    run_uuid = uuid.UUID(str(run_id))
    leaf_key = leaf_id or "singleton"
    identity = uuid.uuid5(
        _WORKFLOW_LEAF_RECOVERY_NAMESPACE,
        f"{run_uuid.hex}:{step_id}:{leaf_key}",
    )
    return f"workflow-leaf-{identity.hex}"


async def test_preset_overrides_reach_spawn():
    register_leaf_preset(
        "source_explorer",
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
    register_leaf_preset("source_synthesizer", LeafPreset(disable_tools=True))
    captured: list = []
    executor = build_subagent_leaf_executor(_ctx(), spawn=_fake_spawn(captured))

    await executor(_request("source_synthesizer"))

    assert captured[0]["spec"].disable_tools is True


async def test_leaf_without_preset_spawns_unchanged():
    """Office and any workflow without a preset must be byte-for-byte unaffected."""
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

    register_leaf_preset("source_explorer", LeafPreset(pre_process=pre, post_process=post))
    captured: list = []
    executor = build_subagent_leaf_executor(_ctx(), spawn=_fake_spawn(captured))

    outcome = await executor(_request())

    assert captured[0]["task"] == "[briefed] Explore topic X"
    assert outcome.output["post"] == "ran"
    assert outcome.output["leaf_id"] == "item-0"


async def test_leaf_executor_binds_restart_stable_workflow_recovery_identity_without_subagent_run_id():
    parent_ctx = _ctx()
    request = _request()
    captured: list = []
    executor = build_subagent_leaf_executor(parent_ctx, spawn=_fake_spawn(captured))

    await executor(request)

    child_ctx = captured[0]["ctx"]
    expected_session_id = _expected_recovery_session_id(
        run_id=request.run_id,
        step_id=request.step_id,
        leaf_id=request.leaf_id,
    )
    assert child_ctx is not parent_ctx
    assert child_ctx.child_session_id == expected_session_id
    assert child_ctx.trace_id == f"workflow:{uuid.UUID(request.run_id).hex}:{request.step_id}:{request.leaf_id}"
    assert child_ctx.subagent_run_id is None
    assert child_ctx.recovery_metadata == {
        "runtime_task_id": uuid.UUID(request.run_id).hex,
        "tenant_id": request.tenant_id,
        "workflow_run_id": uuid.UUID(request.run_id).hex,
        "workflow_step_id": request.step_id,
        "workflow_leaf_id": request.leaf_id,
        "recovery_authority_type": "workflow_leaf",
    }
    assert parent_ctx.child_session_id is None
    assert parent_ctx.recovery_metadata == {}


async def test_fanout_leaf_executor_uses_isolated_recovery_context_per_concurrent_leaf():
    parent_ctx = _ctx()
    run_id = str(uuid.uuid4())
    captured: list = []

    async def spawn(ctx, spec, task, *, budget=None):
        await asyncio.sleep(0)
        captured.append(ctx)
        return SubagentHandle(
            name=spec.name,
            trace_id=ctx.trace_id or "",
            depth=1,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="ok"),
        )

    executor = build_subagent_leaf_executor(parent_ctx, spawn=spawn)
    requests = [
        LeafRequest(
            run_id=run_id,
            step_id="fanout",
            leaf=SimpleNamespace(name="worker", type="worker", max_tool_rounds=3),
            task=f"item {index}",
            tenant_id=str(parent_ctx.tenant_id),
            leaf_id=f"item-{index}",
        )
        for index in range(2)
    ]

    await asyncio.gather(*(executor(request) for request in requests))

    assert len({ctx.child_session_id for ctx in captured}) == 2
    assert {ctx.recovery_metadata["workflow_leaf_id"] for ctx in captured} == {"item-0", "item-1"}
    assert all(ctx is not parent_ctx for ctx in captured)
    assert parent_ctx.recovery_metadata == {}
