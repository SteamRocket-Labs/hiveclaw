"""Standalone system prompt (CC subagent semantics: replace, not layer).

A spawned subagent's definition body / type baseline IS its entire system
prompt — the invoker must not assemble the HOST agent's identity (soul,
memory snapshot, retrieval, navigation) around it. Style mirrors
test_memory_query_routing.py: call the invoker callbacks directly with a
constructed AgentInvocationRequest.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


def _standalone_request(standalone_system_prompt: str = "You are a search specialist. READ-ONLY."):
    from app.runtime.invoker import AgentInvocationRequest

    return AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "investigate X"}],
        agent_name="HR · scout",
        role_description="explorer subagent",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="s-sub", source="subagent"),
        standalone_system_prompt=standalone_system_prompt,
    )


@pytest.mark.asyncio
async def test_build_system_prompt_standalone_replaces_host_identity(monkeypatch):
    from app.runtime import invoker

    def _boom(*args, **kwargs):  # the host context builder must never run
        raise AssertionError("build_agent_context must not be called for standalone prompts")

    monkeypatch.setattr(invoker, "build_agent_context", _boom)

    prompt = await invoker._build_system_prompt(_standalone_request(), uuid4(), "")
    assert prompt == "You are a search specialist. READ-ONLY."


@pytest.mark.asyncio
async def test_resolve_memory_context_standalone_skips_host_memory(monkeypatch):
    from app.runtime import invoker

    async def _boom(*args, **kwargs):
        raise AssertionError("host memory snapshot must not be built for standalone prompts")

    monkeypatch.setattr(invoker, "build_memory_snapshot", _boom)

    assert await invoker._resolve_memory_context(_standalone_request(), uuid4()) == ""


@pytest.mark.asyncio
async def test_resolve_retrieval_context_standalone_is_empty():
    from app.runtime import invoker

    assert await invoker._resolve_retrieval_context(_standalone_request(), uuid4()) == ""


@pytest.mark.asyncio
async def test_resolve_memory_navigation_context_standalone_is_empty():
    from app.runtime import invoker

    assert await invoker._resolve_memory_navigation_context(_standalone_request(), uuid4()) == ""


@pytest.mark.asyncio
async def test_non_standalone_request_still_builds_host_prompt(monkeypatch):
    """Control case: the default path is untouched (host identity assembled)."""

    from app.runtime import invoker

    calls: list = []

    async def fake_build_agent_context(**kwargs):
        calls.append(kwargs)
        return "HOST CONTEXT"

    monkeypatch.setattr(invoker, "build_agent_context", fake_build_agent_context)
    monkeypatch.setattr(invoker, "build_frozen_prompt_prefix", lambda agent_context: agent_context)

    request = _standalone_request("")
    prompt = await invoker._build_system_prompt(request, uuid4(), "", current_user_name="rocky")
    assert prompt == "HOST CONTEXT"
    assert len(calls) == 1
