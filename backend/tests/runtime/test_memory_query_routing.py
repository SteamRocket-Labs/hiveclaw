from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


@pytest.mark.asyncio
async def test_resolve_memory_context_uses_single_query_scoped_retrieval(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    memory_calls: list[tuple[str | None, str]] = []

    async def fake_build_memory_context(agent_id, tenant_id, *, session_id=None, query="", **kwargs):
        del agent_id, tenant_id, kwargs
        memory_calls.append((str(session_id), query))
        return "QUERY_SCOPED_MEMORY" if query else "SNAPSHOT_MEMORY"

    monkeypatch.setattr(invoker, "build_memory_context", fake_build_memory_context)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="s-1"),
    )

    result = await invoker._resolve_memory_context(request, uuid4())
    assert "QUERY_SCOPED_MEMORY" in result
    assert "SNAPSHOT_MEMORY" not in result
    assert '<context_block kind="memory_context" source="memory_provider:context">' in result
    assert memory_calls == [("s-1", "latest question")]
    assert request.session_context.metadata["context_artifacts"][0]["kind"] == "memory_context"

    # After prefix is cached, memory context should STILL be loaded (not skipped)
    # This ensures the engine's hash-based cache invalidation works correctly.
    request.session_context.prompt_prefix = "CACHED_PREFIX"
    result = await invoker._resolve_memory_context(request, uuid4())
    assert "QUERY_SCOPED_MEMORY" in result  # Memory always loaded, even with cached prefix
    assert memory_calls == [("s-1", "latest question"), ("s-1", "latest question")]


@pytest.mark.asyncio
async def test_resolve_retrieval_context_routes_last_user_query(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    calls: list[tuple[str, str]] = []

    async def fake_fetch_relevant_knowledge(query, tenant_id):
        del tenant_id
        calls.append(("knowledge", query))
        return "KNOWLEDGE_RECALL"

    async def fail_build_memory_context(*_args, **_kwargs):
        raise AssertionError("memory retrieval must not run in _resolve_retrieval_context")

    async def fail_build_agent_runtime_context(*_args, **_kwargs):
        raise AssertionError("runtime metadata must not run in _resolve_retrieval_context")

    monkeypatch.setattr(invoker, "build_memory_context", fail_build_memory_context)
    monkeypatch.setattr(invoker, "fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", fail_build_agent_runtime_context)

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
    assert "KNOWLEDGE_RECALL" in result
    assert '<context_block kind="knowledge_relevant" source="knowledge_provider:relevant">' in result
    assert calls == [("knowledge", "latest question")]
    assert [item["kind"] for item in request.session_context.metadata["context_artifacts"]] == ["knowledge_relevant"]


@pytest.mark.asyncio
async def test_resolve_retrieval_context_passes_agent_and_user_identity_to_knowledge(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    captured = {}

    async def fake_fetch_relevant_knowledge(query, tenant_id_arg, *, agent_id=None, current_user_id=None, **kwargs):
        captured.update(
            {
                "query": query,
                "tenant_id": tenant_id_arg,
                "agent_id": agent_id,
                "current_user_id": current_user_id,
                "kwargs": kwargs,
            }
        )
        return ""

    monkeypatch.setattr(invoker, "fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=agent_id,
        user_id=user_id,
        session_context=SessionContext(session_id="s-knowledge-principal"),
    )

    await invoker._resolve_retrieval_context(request, tenant_id)

    assert captured["query"] == "latest question"
    assert captured["tenant_id"] == tenant_id
    assert captured["agent_id"] == agent_id
    assert captured["current_user_id"] == user_id


@pytest.mark.asyncio
async def test_resolve_retrieval_context_registers_connector_source_items(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.services.connector_acl import CONNECTOR_SOURCE_ITEMS_METADATA_KEY

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    hidden_source = "feishu://doc/retrieved-hidden"

    async def fake_fetch_relevant_knowledge(query, tenant_id_arg, *, source_collector=None, **kwargs):
        del query, tenant_id_arg, kwargs
        source_collector.append(
            {
                "source": hidden_source,
                "content": "hidden result",
                "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
            }
        )
        return ""

    monkeypatch.setattr(invoker, "fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=agent_id,
        user_id=user_id,
        session_context=SessionContext(session_id="s-knowledge-acl"),
    )

    result = await invoker._resolve_retrieval_context(request, tenant_id)

    assert result == ""
    assert request.session_context.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["source"] == hidden_source


@pytest.mark.asyncio
async def test_resolve_runtime_metadata_context_routes_runtime_hints(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    calls: list[tuple[str, str | None]] = []

    async def fake_build_agent_runtime_context(agent_id, *, current_user_name=None, budget_profile=None):
        del agent_id, budget_profile
        calls.append(("runtime", current_user_name))
        return "RUNTIME_HINTS"

    async def fake_resolve_current_user_name(_user_id):
        return "Rocky"

    monkeypatch.setattr(invoker, "build_agent_runtime_context", fake_build_agent_runtime_context)
    monkeypatch.setattr(invoker, "_resolve_current_user_name", fake_resolve_current_user_name)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=SessionContext(session_id="s-runtime"),
    )

    result = await invoker._resolve_runtime_metadata_context(request, uuid4())

    assert "RUNTIME_HINTS" in result
    assert '<context_block kind="agent_runtime_context" source="runtime_context:agent">' in result
    assert calls == [("runtime", "Rocky")]
    assert request.session_context.metadata["context_artifacts"][0]["kind"] == "agent_runtime_context"


@pytest.mark.asyncio
async def test_resolve_retrieval_context_excludes_memory_and_runtime_metadata(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    async def fail_build_memory_context(*_args, **_kwargs):
        raise AssertionError("memory context belongs to _resolve_memory_context, not retrieval context")

    async def fail_build_agent_runtime_context(*_args, **_kwargs):
        raise AssertionError("runtime metadata belongs to _resolve_runtime_metadata_context, not retrieval context")

    async def fake_fetch_relevant_knowledge(query, tenant_id, **kwargs):
        del tenant_id, kwargs
        assert query == "latest question"
        return "KNOWLEDGE_RECALL"

    monkeypatch.setattr(invoker, "build_memory_context", fail_build_memory_context)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", fail_build_agent_runtime_context)
    monkeypatch.setattr(invoker, "fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="s-knowledge"),
    )

    result = await invoker._resolve_retrieval_context(request, uuid4())

    assert "KNOWLEDGE_RECALL" in result
    assert "memory_recall" not in result
    assert "agent_runtime_context" not in result
    assert '<context_block kind="knowledge_relevant" source="knowledge_provider:relevant">' in result
    assert request.session_context.metadata["context_artifacts"][0]["kind"] == "knowledge_relevant"


@pytest.mark.asyncio
async def test_resolve_memory_context_passes_current_user_to_memory(monkeypatch):
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

    monkeypatch.setattr(invoker, "_resolve_current_user_name", fake_resolve_current_user_name)
    monkeypatch.setattr(invoker, "build_memory_context", fake_build_memory_context)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=user_id,
        session_context=SessionContext(session_id="s-3"),
    )

    await invoker._resolve_memory_context(request, uuid4())

    assert captured == {
        "current_user_id": user_id,
        "current_user_name": "Bob",
    }


@pytest.mark.asyncio
async def test_resolve_memory_context_injects_skill_evolution_digest(monkeypatch, tmp_path):
    """Gap3: the agent's own skill evolution is surfaced into the dynamic memory
    context so skills are a first-class evolution axis beside memory."""
    import json

    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    agent_id = uuid4()
    skills_dir = tmp_path / str(agent_id) / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / ".usage.json").write_text(
        json.dumps(
            {
                "deploy-checklist": {
                    "created_by": "agent",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "last_used_at": "2026-05-01T00:00:00+00:00",
                    "use_count": 7,
                    "view_count": 0,
                    "state": "active",
                    "pinned": False,
                    "archived_at": None,
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(invoker, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "hi"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=agent_id,
        session_context=SessionContext(session_id="s-skill"),
    )

    # tenant_id=None → memory snapshot skipped; only the skill digest is injected.
    result = await invoker._resolve_memory_context(request, None)
    assert "## Your Skill Assets" in result
    assert "deploy-checklist (7×)" in result
    assert '<context_block kind="skill_evolution_digest" source="skill_curator:digest">' in result
