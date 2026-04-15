from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.runtime.session import SessionContext


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def test_runtime_invocation_contract_no_longer_exposes_manual_memory_context() -> None:
    from app.kernel.contracts import InvocationRequest
    from app.runtime.invoker import AgentInvocationRequest

    assert "memory_context" not in AgentInvocationRequest.__dataclass_fields__
    assert "memory_context" not in InvocationRequest.__dataclass_fields__


@pytest.mark.asyncio
async def test_build_system_prompt_uses_static_agent_context_only(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _build_system_prompt

    captured = {}

    async def fake_build_agent_context(*args, **kwargs):
        captured["kwargs"] = kwargs
        return "STATIC_AGENT_CONTEXT"

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=SessionContext(session_id="s-1", source="websocket"),
    )

    prompt = await _build_system_prompt(
        request,
        tenant_id=uuid4(),
        resolved_memory_context="MEMORY_SNAPSHOT",
        current_user_name="Rocky",
    )

    # Frozen prefix now includes only stable sections; memory snapshot stays dynamic.
    assert "STATIC_AGENT_CONTEXT" in prompt
    assert "MEMORY_SNAPSHOT" not in prompt
    assert "## System" in prompt
    assert captured["kwargs"]["include_runtime_metadata"] is False
    assert "include_memory_file" not in captured["kwargs"]
    assert "include_focus" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_resolve_retrieval_context_appends_runtime_hints(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _resolve_retrieval_context

    async def fake_build_memory_context(*args, **kwargs):
        return "MEMORY_RECALL"

    async def fake_build_agent_runtime_context(*args, **kwargs):
        return "RUNTIME_HINTS"

    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_build_memory_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", lambda *_args, **_kwargs: "KNOWLEDGE")
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_build_agent_runtime_context)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None),
        messages=[{"role": "user", "content": "最新进展是什么"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=SessionContext(session_id="s-2", source="websocket"),
    )

    result = await _resolve_retrieval_context(request, tenant_id=uuid4())

    assert result.startswith("## Runtime Context\nRUNTIME_HINTS")
    assert "## Relevant Memory Recall\nMEMORY_RECALL" in result
    assert "## Knowledge" in result
    assert "KNOWLEDGE" in result
    assert result.index("## Runtime Context") < result.index("## Relevant Memory Recall") < result.index("## Knowledge")


@pytest.mark.asyncio
async def test_invoke_agent_keeps_core_tools_when_skill_read_has_no_declared_expansion(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    tool_load_calls: list[bool] = []

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def fake_execute_tool(tool_name, args, agent_id=None, user_id=None):
        assert tool_name == "read_file"
        assert args == {"path": "skills/web-research/SKILL.md"}
        assert agent_id
        assert user_id
        return "SKILL_CONTENT"

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "read_file", "arguments": '{"path":"skills/web-research/SKILL.md"}'},
            }],
            reasoning_content="reasoning",
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="final answer",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 8},
        ),
    ])

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_get_agent_tools_for_llm)
    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "帮我做调研"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "final answer"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"


@pytest.mark.asyncio
async def test_resolve_tool_expansion_does_not_fallback_to_full_tools_when_workspace_fails(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _resolve_tool_expansion

    async def fake_ensure_workspace(_agent_id):
        raise RuntimeError("workspace unavailable")

    async def fake_get_agent_tools_for_llm(*_args, **_kwargs):
        return [{"type": "function", "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}}}]

    monkeypatch.setattr("app.runtime.invoker.ensure_workspace", fake_ensure_workspace)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_get_agent_tools_for_llm)

    result = await _resolve_tool_expansion(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None),
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        ),
        "load_skill",
        {"name": "web research"},
    )

    assert result is None


@pytest.mark.asyncio
async def test_invoke_agent_emits_response_complete_and_session_close_hooks(monkeypatch):
    from app.runtime.hooks import HookEvent
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    fake_client = _FakeClient([
        SimpleNamespace(
            content="final answer",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])
    emitted: list[tuple[HookEvent, dict]] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        return [{"type": "function", "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}}}]

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine.asyncio.ensure_future", lambda coro: asyncio.create_task(coro))
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_get_agent_tools_for_llm)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "帮我总结当前差距"}],
            agent_name="Planner",
            role_description="Planning agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="session-123",
            session_context=SessionContext(session_id="session-123", source="websocket"),
        )
    )
    await asyncio.sleep(0)

    event_names = [event for event, _ in emitted]
    assert HookEvent.SESSION_START in event_names
    assert HookEvent.RESPONSE_COMPLETE in event_names
    assert HookEvent.SESSION_CLOSE in event_names
    response_payload = next(payload for event, payload in emitted if event == HookEvent.RESPONSE_COMPLETE)
    close_payload = next(payload for event, payload in emitted if event == HookEvent.SESSION_CLOSE)
    assert response_payload["messages"][-1]["role"] == "user"
    assert close_payload["messages"][-1]["role"] == "assistant"
    assert close_payload["messages"][-1]["content"] == "final answer"


@pytest.mark.asyncio
async def test_invoke_agent_session_close_hook_preserves_session_interaction_metadata(monkeypatch):
    from app.runtime.hooks import HookEvent
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    fake_client = _FakeClient([
        SimpleNamespace(
            content="final answer",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])
    emitted: list[tuple[HookEvent, dict]] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine.asyncio.ensure_future", lambda coro: asyncio.create_task(coro))
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "直接回复另一个 agent"}],
            agent_name="Planner",
            role_description="Planning agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="session-agent-message",
            session_context=SessionContext(
                session_id="session-agent-message",
                source="agent",
                metadata={
                    "interaction_type": "agent_message",
                    "agent_message": True,
                    "agent_message_parent_agent_id": "parent-agent-id",
                },
            ),
        )
    )
    await asyncio.sleep(0)

    close_payload = next(payload for event, payload in emitted if event == HookEvent.SESSION_CLOSE)
    assert close_payload["source"] == "agent"
    assert close_payload["metadata"]["interaction_type"] == "agent_message"
    assert close_payload["metadata"]["agent_message"] is True
    assert close_payload["metadata"]["agent_message_parent_agent_id"] == "parent-agent-id"


@pytest.mark.asyncio
async def test_invoke_agent_emits_response_complete_only_once(monkeypatch):
    from app.runtime.hooks import HookEvent
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    fake_client = _FakeClient([
        SimpleNamespace(
            content="final answer",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])
    emitted: list[tuple[HookEvent, dict]] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine.asyncio.ensure_future", lambda coro: asyncio.create_task(coro))
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "帮我总结当前差距"}],
            agent_name="Planner",
            role_description="Planning agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="session-123",
            session_context=SessionContext(session_id="session-123", source="websocket"),
        )
    )
    await asyncio.sleep(0)

    assert [event for event, _ in emitted].count(HookEvent.RESPONSE_COMPLETE) == 1


def test_resolve_context_budget_initializes_missing_session_metadata():
    from app.runtime.invoker import AgentInvocationRequest, _resolve_context_budget

    request = AgentInvocationRequest(
        model=SimpleNamespace(max_input_tokens=128000),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        session_context=SessionContext(session_id="s-1", source="websocket"),
    )
    request.session_context.metadata = None  # type: ignore[assignment]

    budget = _resolve_context_budget(request)

    assert budget is not None
    assert isinstance(request.session_context.metadata, dict)
    assert request.session_context.metadata["context_window_tokens"] == 128000


@pytest.mark.asyncio
async def test_resolve_runtime_config_defaults_new_flags_to_false_when_missing(monkeypatch):
    from app.runtime.invoker import _resolve_runtime_config

    tenant_id = uuid4()
    agent_id = uuid4()
    fake_agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        max_tool_rounds=33,
        execution_mode="conversation",
    )

    class _FakeSession:
        def __init__(self) -> None:
            self._results = [
                _FakeScalarResult(fake_agent),
                _FakeScalarResult(None),
                _FakeScalarResult(None),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _stmt):
            return self._results.pop(0)

    async def fake_is_feature_enabled(*args, **kwargs):
        raise AssertionError("is_feature_enabled should not be called when the flag row is missing")

    monkeypatch.setattr("app.runtime.invoker.async_session", lambda: _FakeSession())
    monkeypatch.setattr("app.runtime.invoker.get_settings", lambda: SimpleNamespace(DEBUG=False))
    monkeypatch.setattr("app.runtime.invoker.is_feature_enabled", fake_is_feature_enabled)

    config = await _resolve_runtime_config(agent_id)

    assert config.tenant_id == tenant_id
    assert config.max_tool_rounds == 33
    assert config.runtime_continuity_enabled is False
    assert config.skill_candidate_loop_enabled is False


@pytest.mark.asyncio
async def test_invoke_agent_filters_excluded_tools_from_runtime_surface(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        return [
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "delegate_to_agent", "description": "", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "send_message_to_agent", "description": "", "parameters": {"type": "object"}}},
        ]

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_get_agent_tools_for_llm)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "完成这个子任务"}],
            agent_name="Worker",
            role_description="Focused worker",
            agent_id=uuid4(),
            user_id=uuid4(),
            excluded_tool_names=("delegate_to_agent", "send_message_to_agent"),
        )
    )

    tool_names = [tool["function"]["name"] for tool in fake_client.calls[0]["tools"]]
    assert tool_names == ["read_file"]


@pytest.mark.asyncio
async def test_invoke_agent_filters_allowed_tools_from_runtime_surface(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        return [
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "write_file", "description": "", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "search_memory", "description": "", "parameters": {"type": "object"}}},
        ]

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_get_agent_tools_for_llm)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "只做只读分析"}],
            agent_name="Worker",
            role_description="Focused worker",
            agent_id=uuid4(),
            user_id=uuid4(),
            allowed_tool_names=("read_file", "search_memory"),
        )
    )

    tool_names = [tool["function"]["name"] for tool in fake_client.calls[0]["tools"]]
    assert tool_names == ["read_file", "search_memory"]


@pytest.mark.asyncio
async def test_invoke_agent_composes_system_prompt_once(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    tenant_id = uuid4()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return "KB_CONTEXT"

    async def fake_resolve_runtime_config(_agent_id):
        return SimpleNamespace(tenant_id=tenant_id, max_tool_rounds=50, quota_message=None)

    async def fake_build_memory_snapshot(_agent_id, _tenant_id, session_id=None):
        del _agent_id, _tenant_id, session_id
        return "MEMORY_CONTEXT"

    async def fake_build_memory_context(*args, **kwargs):
        del args, kwargs
        return ""

    async def fake_build_agent_runtime_context(*args, **kwargs):
        del args, kwargs
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", fake_resolve_runtime_config)
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.build_memory_snapshot", fake_build_memory_snapshot)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_build_memory_context)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_build_agent_runtime_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "最近公司政策有什么变化"}],
            agent_name="Analyst",
            role_description="Policy analyst",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    system_prompt = fake_client.calls[0]["messages"][0].content
    # Frozen prefix includes agent context + stable sections; dynamic suffix adds memory + knowledge.
    assert "BASE_PROMPT" in system_prompt
    assert "## System" in system_prompt
    assert "__PROMPT_DYNAMIC_BOUNDARY__" in system_prompt
    frozen_prefix, dynamic_suffix = system_prompt.split("__PROMPT_DYNAMIC_BOUNDARY__", 1)
    assert "MEMORY_CONTEXT" not in frozen_prefix
    assert "## Your Memory System" in dynamic_suffix
    assert "MEMORY_CONTEXT" in dynamic_suffix
    assert "search_memory" in dynamic_suffix
    assert "recall" not in dynamic_suffix
    assert "KB_CONTEXT" in system_prompt


@pytest.mark.asyncio
async def test_invoke_agent_passes_cancel_and_fallback_to_kernel(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}
    cancel_event = asyncio.Event()
    fallback_model = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet",
        api_key="fallback",
        base_url=None,
        max_output_tokens=None,
    )

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai",
                model="gpt-4.1",
                api_key="key",
                base_url=None,
                max_output_tokens=None,
            ),
            fallback_model=fallback_model,
            cancel_event=cancel_event,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "ok"
    assert captured["request"].cancel_event is cancel_event
    assert captured["request"].fallback_model is fallback_model


@pytest.mark.asyncio
async def test_invoke_agent_passes_execution_mode_to_kernel(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai",
                model="gpt-4.1",
                api_key="key",
                base_url=None,
                max_output_tokens=None,
            ),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Coordinator",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            execution_mode="coordinator",
        )
    )

    assert result.content == "ok"
    assert captured["request"].execution_mode == "coordinator"


@pytest.mark.asyncio
async def test_invoke_agent_without_agent_id_uses_collected_initial_tools(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    monkeypatch.setattr(
        "app.runtime.invoker.get_combined_openai_tools",
        lambda: [{"type": "function", "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}}}],
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai",
                model="gpt-4.1",
                api_key="key",
                base_url=None,
                max_output_tokens=None,
            ),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=None,
            user_id=uuid4(),
        )
    )

    assert result.content == "ok"
    assert captured["request"].initial_tools == [
        {"type": "function", "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}}}
    ]


@pytest.mark.asyncio
async def test_invoke_agent_emits_compaction_events(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])
    runtime_events: list[dict] = []

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        on_compaction = kwargs.get("on_compaction")
        assert on_compaction is not None
        await on_compaction({
            "summary": "older context compressed",
            "original_message_count": len(messages),
            "kept_message_count": 2,
        })
        return [{"role": "system", "content": "[Previous conversation summary]\nolder context compressed"}]

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ],
            agent_name="Analyst",
            role_description="Policy analyst",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=runtime_events.append,
        )
    )

    assert result.content == "done"
    assert runtime_events == [{
        "type": "session_compact",
        "summary": "older context compressed",
        "original_message_count": 3,
        "kept_message_count": 2,
    }]


@pytest.mark.asyncio
async def test_invoke_agent_forwards_permission_events(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "write_file", "arguments": '{"path":"workspace/focus.md","content":"todo"}'},
            }],
            reasoning_content="reasoning",
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="request blocked",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 8},
        ),
    ])

    runtime_events: list[dict] = []

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_execute_tool(tool_name, args, agent_id=None, user_id=None, event_callback=None):
        assert tool_name == "write_file"
        assert event_callback is not None
        await event_callback({
            "type": "permission",
            "tool_name": "write_file",
            "status": "approval_required",
            "message": "This action requires approval.",
            "approval_id": "approval-123",
        })
        return "⏳ This action requires approval. An approval request has been sent. (Approval ID: approval-123)"

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [{"type": "function", "function": {"name": "write_file", "description": "", "parameters": {"type": "object"}}}])
    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "写入 focus.md"}],
            agent_name="Analyst",
            role_description="Policy analyst",
            agent_id=agent_id,
            user_id=user_id,
            on_event=runtime_events.append,
        )
    )

    assert result.content == "request blocked"
    assert runtime_events == [{
        "type": "permission",
        "tool_name": "write_file",
        "status": "approval_required",
        "message": "This action requires approval.",
        "approval_id": "approval-123",
    }]


@pytest.mark.asyncio
async def test_invoke_agent_loads_and_persists_runtime_memory(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])
    captured = {}

    async def fake_resolve_runtime_config(_agent_id):
        return SimpleNamespace(tenant_id=tenant_id, max_tool_rounds=50, quota_message=None)

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    async def fake_build_memory_snapshot(_agent_id, _tenant_id, session_id=None):
        captured["loaded"] = (_agent_id, _tenant_id, session_id)
        return "RUNTIME_MEMORY"

    async def fake_build_memory_context(*args, **kwargs):
        return ""

    async def fake_build_agent_runtime_context(*args, **kwargs):
        return ""

    async def fake_persist_runtime_memory(*, agent_id, session_id, tenant_id, messages):
        captured["persisted"] = {
            "agent_id": agent_id,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "messages": messages,
        }

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", fake_resolve_runtime_config)
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_build_agent_runtime_context)
    monkeypatch.setattr("app.runtime.invoker.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_build_memory_context)
    monkeypatch.setattr("app.runtime.invoker.build_memory_snapshot", fake_build_memory_snapshot)
    monkeypatch.setattr("app.runtime.invoker.persist_runtime_memory", fake_persist_runtime_memory)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            memory_messages=[{"role": "user", "content": "hello"}],
            memory_session_id="session-1",
            agent_name="Analyst",
            role_description="Policy analyst",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "done"
    assert captured["loaded"] == (agent_id, tenant_id, "session-1")
    _sys_prompt = fake_client.calls[0]["messages"][0].content
    assert "BASE_PROMPT" in _sys_prompt
    assert "RUNTIME_MEMORY" in _sys_prompt
    assert "MANUAL_MEMORY" not in _sys_prompt
    assert "## System" in _sys_prompt
    assert captured["persisted"] == {
        "agent_id": agent_id,
        "session_id": "session-1",
        "tenant_id": tenant_id,
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
    }


@pytest.mark.asyncio
async def test_invoke_agent_routes_simple_turn_to_cheap_fallback_model(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}
    session_context = SessionContext(session_id="s-route", source="websocket")
    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="primary-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        api_key="cheap-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=32000,
        supports_vision=False,
    )

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    async def fake_emit_hook(*args, **kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda *_args, **_kwargs: _FakeKernel())
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "帮我润色这句话，让语气更礼貌。"}],
            agent_name="Assistant",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=session_context,
        )
    )

    assert result.content == "ok"
    assert captured["request"].model is fallback_model
    assert captured["request"].fallback_model is primary_model
    assert captured["request"].supports_vision is False
    assert session_context.metadata["turn_route"] == {
        "selected_model": "gpt-4.1-mini",
        "fallback_model": "gpt-4.1",
        "reason": "simple_turn_cheap_model",
        "task_profile": "general",
        "complexity": "low",
        "config_source": "runtime_default",
    }


@pytest.mark.asyncio
async def test_invoke_agent_keeps_primary_model_for_task_execution(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}
    session_context = SessionContext(session_id="s-task", source="task")
    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="primary-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        api_key="cheap-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=32000,
        supports_vision=False,
    )

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    async def fake_emit_hook(*args, **kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda *_args, **_kwargs: _FakeKernel())
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "帮我润色这句话，让语气更礼貌。"}],
            agent_name="Assistant",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            execution_mode="task",
            session_context=session_context,
        )
    )

    assert result.content == "ok"
    assert captured["request"].model is primary_model
    assert captured["request"].fallback_model is fallback_model
    assert captured["request"].supports_vision is True
    assert session_context.metadata["turn_route"] == {
        "selected_model": "gpt-4.1",
        "fallback_model": "gpt-4.1-mini",
        "reason": "primary_model",
        "task_profile": "general",
        "complexity": "low",
        "config_source": "runtime_default",
    }


@pytest.mark.asyncio
async def test_invoke_agent_respects_explicit_disabled_smart_model_routing(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}
    session_context = SessionContext(session_id="s-routing-off", source="websocket")
    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="primary-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        api_key="cheap-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=32000,
        supports_vision=False,
    )

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    async def fake_emit_hook(*args, **kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda *_args, **_kwargs: _FakeKernel())
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "帮我润色这句话，让语气更礼貌。"}],
            agent_name="Assistant",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=session_context,
            smart_model_routing={"enabled": False},
        )
    )

    assert result.content == "ok"
    assert captured["request"].model is primary_model
    assert captured["request"].fallback_model is fallback_model
    assert session_context.metadata["turn_route"] == {
        "selected_model": "gpt-4.1",
        "fallback_model": "gpt-4.1-mini",
        "reason": "primary_model",
        "task_profile": "general",
        "complexity": "low",
        "config_source": "agent_config",
    }
