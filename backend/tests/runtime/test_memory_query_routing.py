from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


@pytest.mark.asyncio
async def test_resolve_memory_context_uses_snapshot_only_before_prefix_exists(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    snapshot_calls: list[str] = []

    async def fake_build_memory_snapshot(agent_id, tenant_id, session_id=None):
        snapshot_calls.append(str(session_id))
        return "SNAPSHOT"

    monkeypatch.setattr(invoker, "build_memory_snapshot", fake_build_memory_snapshot)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="s-1"),
    )

    result = await invoker._resolve_memory_context(request, uuid4())
    assert "SNAPSHOT" in result
    assert '<context_block kind="memory_snapshot" source="memory_provider:snapshot">' in result
    assert snapshot_calls == ["s-1"]
    assert request.session_context.metadata["context_artifacts"][0]["kind"] == "memory_snapshot"

    # After prefix is cached, memory context should STILL be loaded (not skipped)
    # This ensures the engine's hash-based cache invalidation works correctly.
    request.session_context.prompt_prefix = "CACHED_PREFIX"
    result = await invoker._resolve_memory_context(request, uuid4())
    assert "SNAPSHOT" in result  # Memory always loaded, even with cached prefix
    assert len(snapshot_calls) == 2  # Called twice — once per invocation


@pytest.mark.asyncio
async def test_resolve_retrieval_context_routes_last_user_query(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    calls: list[tuple[str, str | None]] = []

    async def fake_build_memory_context(agent_id, tenant_id, *, session_id=None, query=""):
        del agent_id, tenant_id
        calls.append(("memory", query))
        assert session_id == "s-2"
        return "MEMORY_RECALL"

    async def fake_fetch_relevant_knowledge(query, tenant_id):
        del tenant_id
        calls.append(("knowledge", query))
        return "KNOWLEDGE_RECALL"

    async def fake_build_agent_runtime_context(agent_id, *, current_user_name=None):
        del agent_id, current_user_name
        calls.append(("runtime", None))
        return "RUNTIME_HINTS"

    monkeypatch.setattr(invoker, "build_memory_context", fake_build_memory_context)
    monkeypatch.setattr(invoker, "fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", fake_build_agent_runtime_context)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "latest question"},
        ],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="s-2"),
    )

    result = await invoker._resolve_retrieval_context(request, uuid4())
    assert "RUNTIME_HINTS" in result
    assert "MEMORY_RECALL" in result
    assert "KNOWLEDGE_RECALL" in result
    assert '<context_block kind="agent_runtime_context" source="runtime_context:agent">' in result
    assert '<context_block kind="memory_recall" source="memory_provider:recall">' in result
    assert '<context_block kind="knowledge_relevant" source="knowledge_provider:relevant">' in result
    assert calls == [
        ("runtime", None),
        ("memory", "latest question"),
        ("knowledge", "latest question"),
    ]
    assert [item["kind"] for item in request.session_context.metadata["context_artifacts"]] == [
        "agent_runtime_context",
        "memory_recall",
        "knowledge_relevant",
    ]


@pytest.mark.asyncio
async def test_resolve_retrieval_context_passes_current_user_to_memory(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    user_id = uuid4()
    captured = {}

    async def fake_resolve_current_user_name(_user_id):
        assert _user_id == user_id
        return "Bob"

    async def fake_build_memory_context(
        agent_id,
        tenant_id,
        *,
        session_id=None,
        query="",
        current_user_id=None,
        current_user_name=None,
    ):
        del agent_id, tenant_id, session_id, query
        captured["current_user_id"] = current_user_id
        captured["current_user_name"] = current_user_name
        return ""

    async def fake_fetch_relevant_knowledge(*_args, **_kwargs):
        return ""

    async def fake_build_agent_runtime_context(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(invoker, "_resolve_current_user_name", fake_resolve_current_user_name)
    monkeypatch.setattr(invoker, "build_memory_context", fake_build_memory_context)
    monkeypatch.setattr(invoker, "fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", fake_build_agent_runtime_context)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=user_id,
        session_context=SessionContext(session_id="s-3"),
    )

    await invoker._resolve_retrieval_context(request, uuid4())

    assert captured == {
        "current_user_id": user_id,
        "current_user_name": "Bob",
    }
