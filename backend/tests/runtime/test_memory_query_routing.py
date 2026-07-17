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
async def test_default_retrieval_context_does_not_prefetch_knowledge(monkeypatch):
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest

    async def fail_build_memory_context(*_args, **_kwargs):
        raise AssertionError("memory retrieval belongs to the Memory runtime")

    async def fail_build_agent_runtime_context(*_args, **_kwargs):
        raise AssertionError("runtime metadata is not knowledge retrieval")

    monkeypatch.setattr(invoker, "build_memory_context", fail_build_memory_context)
    monkeypatch.setattr(invoker, "build_agent_runtime_context", fail_build_agent_runtime_context)
    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=SessionContext(session_id="s-no-kb-prefetch"),
    )

    assert await invoker._resolve_retrieval_context(request, uuid4()) == ""
    assert "context_artifacts" not in request.session_context.metadata


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
    usage_dir = tmp_path / str(agent_id) / "evolution"
    usage_dir.mkdir(parents=True)
    (usage_dir / "skill_usage.json").write_text(
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


@pytest.mark.asyncio
async def test_resolve_memory_context_emits_durable_degraded_fact_and_model_marker(monkeypatch) -> None:
    from app.memory.metrics import reset_all, snapshot
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.services.memory_service import MemoryContextResult

    events: list[dict] = []
    spans: list[dict] = []
    reset_all()

    async def degraded(*_args, **_kwargs):
        return MemoryContextResult(
            content="RESIDENT_IDENTITY",
            status="degraded",
            code="semantic_retrieval_unavailable",
            user_message="Some long-term memory is temporarily unavailable.",
            retryable=True,
            attempts=2,
        )

    async def capture_span(**kwargs):
        spans.append(kwargs)

    monkeypatch.setattr(invoker, "build_memory_context", degraded)
    monkeypatch.setattr(invoker, "persist_invocation_span", capture_span)
    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        on_event=events.append,
        session_context=SessionContext(
            session_id=str(uuid4()),
            metadata={"tenant_id": str(uuid4()), "runtime_task_id": str(uuid4()), "trace_id": "trace-memory"},
        ),
    )

    context = await invoker._resolve_memory_context(request, uuid4())

    assert "RESIDENT_IDENTITY" in context
    assert "do not assume complete recall" in context
    assert events[0]["event_type"] == "memory_context_degraded"
    assert events[0]["status"] == "degraded"
    assert events[0]["retryable"] is True
    assert spans[0]["span_type"] == "memory"
    assert spans[0]["status"] == "degraded"
    assert request.session_context.metadata["memory_context_status"]["code"] == "semantic_retrieval_unavailable"
    assert snapshot()["memory_context_status_total"]["degraded:semantic_retrieval_unavailable"] == 1


@pytest.mark.asyncio
async def test_resolve_memory_context_passes_durable_turn_and_surfaces_budget_pressure(monkeypatch) -> None:
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.services.memory_service import MemoryContextResult

    captured: dict[str, object] = {}

    async def budget_exhausted(
        _agent_id,
        _tenant_id,
        *,
        session_id=None,
        query="",
        turn_id=None,
        return_result=False,
        **_kwargs,
    ):
        captured.update(
            session_id=session_id,
            query=query,
            turn_id=turn_id,
            return_result=return_result,
        )
        return MemoryContextResult(
            content="RESIDENT_INDEX",
            status="degraded",
            code="memory_auto_surface_budget_exhausted",
            user_message=(
                "Session automatic memory surfacing budget is exhausted; conversation can continue and "
                "search_memory/load_memory remain available."
            ),
            retryable=False,
            auto_surfaced_bytes=0,
            auto_surface_total_bytes=60 * 1024,
            auto_surface_remaining_bytes=0,
        )

    async def ignore_span(**_kwargs):
        return None

    monkeypatch.setattr(invoker, "build_memory_context", budget_exhausted)
    monkeypatch.setattr(invoker, "persist_invocation_span", ignore_span)
    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        session_context=SessionContext(
            session_id="session-budget",
            metadata={"turn_id": "turn-durable-17", "trace_id": "trace-budget"},
        ),
    )

    context = await invoker._resolve_memory_context(request, uuid4())

    assert captured == {
        "session_id": "session-budget",
        "query": "continue",
        "turn_id": "turn-durable-17",
        "return_result": True,
    }
    assert "RESIDENT_INDEX" in context
    assert "search_memory/load_memory remain available" in context
    status = request.session_context.metadata["memory_context_status"]
    assert status["auto_surface_total_bytes"] == 60 * 1024
    assert status["auto_surface_remaining_bytes"] == 0
    assert status["external_effects_available"] is True


def test_turn_identity_never_collapses_every_turn_to_the_session_identity() -> None:
    from app.runtime.invoker import AgentInvocationRequest, _ensure_turn_metadata

    first = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "first"}],
        agent_name="Agent",
        role_description="desc",
        memory_session_id="session-shared",
    )
    second = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "second"}],
        agent_name="Agent",
        role_description="desc",
        memory_session_id="session-shared",
    )

    first_turn = _ensure_turn_metadata(first)["turn_id"]
    second_turn = _ensure_turn_metadata(second)["turn_id"]

    assert first_turn != second_turn
    assert first_turn != "turn-session-shared"
    assert second_turn != "turn-session-shared"


@pytest.mark.asyncio
async def test_resolve_memory_context_keeps_conversation_alive_when_resident_context_is_unavailable(
    monkeypatch,
) -> None:
    from app.runtime import invoker
    from app.runtime.invoker import AgentInvocationRequest
    from app.services.memory_service import MemoryContextResult

    events: list[dict] = []

    async def unavailable(*_args, **_kwargs):
        return MemoryContextResult(
            content="",
            status="unavailable",
            code="resident_profile_unavailable",
            user_message="Required agent memory is temporarily unavailable.",
            retryable=True,
            conversation_available=True,
            authority_context_available=False,
            durable_write_available=False,
            external_effects_available=False,
        )

    async def ignore_span(**_kwargs):
        return None

    monkeypatch.setattr(invoker, "build_memory_context", unavailable)
    monkeypatch.setattr(invoker, "persist_invocation_span", ignore_span)
    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "latest question"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        on_event=events.append,
        session_context=SessionContext(
            session_id=str(uuid4()),
            metadata={"tenant_id": str(uuid4()), "trace_id": "trace-memory"},
        ),
    )

    context = await invoker._resolve_memory_context(request, uuid4())

    assert "Memory runtime degraded" in context
    assert "external effects are frozen" in context
    assert events[0]["event_type"] == "memory_context_unavailable"
    status = request.session_context.metadata["memory_context_status"]
    assert status["conversation_available"] is True
    assert status["authority_context_available"] is False
    assert status["durable_write_available"] is False
    assert status["external_effects_available"] is False
