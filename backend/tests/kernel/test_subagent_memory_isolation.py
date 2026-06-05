"""Subagent runs must not leak into the HOST agent's memory pipeline.

A spawned subagent runs under the parent's agent_id, but it is a clean
specialist (standalone prompt, CC semantics): its INTERNAL transcript is not
the parent's behavior. The conclusion already reaches the parent's T2 through
the parent's own main-session RESPONSE_COMPLETE (the spawn tool result), so
extracting/persisting the subagent session as well double-counts the work and
buries the conclusion in tool noise. Mirror of the heartbeat exclusion.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.kernel.contracts import InvocationRequest
from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
from app.runtime.session import SessionContext


class _OneShotClient:
    """LLM client double (mirrors test_engine._FakeClient): one final message, no tools."""

    async def stream(self, **kwargs):
        return SimpleNamespace(
            content="final answer",
            tool_calls=[],
            reasoning_content="",
            usage={"total_tokens": 5},
        )

    async def close(self) -> None:
        return None


def _build_kernel(persisted: list) -> AgentKernel:
    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid.uuid4(), max_tool_rounds=5)

    async def resolve_current_user_name(_user_id):
        return "Rocky"

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        return "PROMPT"

    async def resolve_memory_context(request, tenant_id):
        return ""

    async def get_tools(_agent_id, core_only):
        return []

    async def maybe_compress_messages(messages, **kwargs):
        return messages

    async def execute_tool(tool_name, args, request, emit_event):
        raise AssertionError("no tools in this test")

    async def persist_memory(**kwargs):
        persisted.append(kwargs)

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=resolve_current_user_name,
            build_system_prompt=build_system_prompt,
            resolve_memory_context=resolve_memory_context,
            get_tools=get_tools,
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda model: _OneShotClient(),
            execute_tool=execute_tool,
            persist_memory=persist_memory,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )


def _request(source: str) -> InvocationRequest:
    return InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-x", max_input_tokens=100_000),
        messages=[{"role": "user", "content": "task"}],
        agent_name="HR · scout",
        role_description="explorer subagent",
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_context=SessionContext(source=source, channel="internal"),
    )


async def _run_and_capture_hooks(monkeypatch, source: str) -> tuple[list, list]:
    persisted: list = []
    hook_events: list = []

    async def fake_emit_hook(event, **kwargs):
        hook_events.append((getattr(event, "value", event), kwargs.get("source")))

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    kernel = _build_kernel(persisted)
    result = await kernel.handle(_request(source))
    assert result.content == "final answer"
    # RESPONSE_COMPLETE is fired via ensure_future — let scheduled tasks run.
    await asyncio.sleep(0)
    return persisted, hook_events


@pytest.mark.asyncio
async def test_subagent_source_skips_host_memory_pipeline(monkeypatch):
    persisted, hook_events = await _run_and_capture_hooks(monkeypatch, "subagent")
    assert persisted == [], "subagent transcript must not persist into the host memory session"
    assert all(name != "response_complete" for name, _src in hook_events), (
        "RESPONSE_COMPLETE must not fire for subagent runs (no T2 extraction of internals)"
    )


@pytest.mark.asyncio
async def test_regular_source_still_runs_memory_pipeline(monkeypatch):
    """Control case: the default path persists + extracts as before."""
    persisted, hook_events = await _run_and_capture_hooks(monkeypatch, "web")
    assert persisted, "regular sessions must keep persisting memory"
    assert any(name == "response_complete" and src == "web" for name, src in hook_events)
