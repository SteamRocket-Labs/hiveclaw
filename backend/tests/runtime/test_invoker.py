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


@pytest.fixture(autouse=True)
def _allow_invocation_quota(monkeypatch):
    async def allow(_user_id, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", allow, raising=False)

    async def no_native_memory(*_args, **_kwargs):
        return ""

    # These unit tests isolate the kernel/transport behavior with synthetic
    # agent UUIDs. Memory-specific tests override this fixture explicitly.
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", no_native_memory)


@pytest.mark.asyncio
async def test_invoke_agent_enforces_user_token_quota_before_kernel(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.services.quota_guard import QuotaExceeded

    user_id = uuid4()
    kernel_called = False

    async def fake_check_user_token_quota(checked_user_id):
        assert checked_user_id == user_id
        raise QuotaExceeded("Daily token limit reached (10/10).", quota_type="tokens_daily")

    class FakeKernel:
        async def handle(self, _request):
            nonlocal kernel_called
            kernel_called = True
            raise AssertionError("kernel must not run after quota denial")

    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", fake_check_user_token_quota, raising=False)
    monkeypatch.setattr("app.runtime.invoker._resolve_kernel_for_request", lambda _request: FakeKernel())

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=user_id,
        )
    )

    assert kernel_called is False
    assert result.tokens_used == 0
    assert "Daily token limit reached" in result.content


@pytest.mark.asyncio
async def test_invoke_agent_passes_agent_to_token_quota_before_kernel(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.services.quota_guard import QuotaExceeded

    user_id = uuid4()
    subject_agent_id = uuid4()
    kernel_called = False

    async def fake_check_user_token_quota(checked_user_id, *, agent_id=None, tenant_id=None):
        assert checked_user_id == user_id
        assert agent_id == subject_agent_id
        assert tenant_id is None
        raise QuotaExceeded("Agent daily token limit reached (50/50).", quota_type="agent_tokens_daily")

    class FakeKernel:
        async def handle(self, _request):
            nonlocal kernel_called
            kernel_called = True
            raise AssertionError("kernel must not run after quota denial")

    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", fake_check_user_token_quota, raising=False)
    monkeypatch.setattr("app.runtime.invoker._resolve_kernel_for_request", lambda _request: FakeKernel())

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=subject_agent_id,
            user_id=user_id,
        )
    )

    assert kernel_called is False
    assert result.terminal_reason.value == "quota_denied"
    assert "Agent daily token limit" in result.content


@pytest.mark.asyncio
async def test_invoke_agent_does_not_prefetch_or_inject_personal_kb_before_kernel(monkeypatch):
    from app.kernel import InvocationResult
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    session = SessionContext(
        session_id="session-kb",
        source="web_chat",
        metadata={"request_id": "request-kb", "tenant_id": str(uuid4()), "owner_user_id": str(uuid4())},
    )
    seen_suffixes: list[str] = []
    prefetch_calls = 0

    async def fake_record_knowledge_activation(_request):
        nonlocal prefetch_calls
        prefetch_calls += 1
        return "## Personal Knowledge Hint\n- Retrieval notes [kb://person/owner/documents/doc#segment=seg]"

    class FakeKernel:
        async def handle(self, request):
            seen_suffixes.append(request.system_prompt_suffix)
            return InvocationResult(content="done")

    # Keep raising=False so the contract remains valid after the retired
    # prefetch helper is removed from the module: adding the legacy name back
    # must not make invoke_agent consume it.
    monkeypatch.setattr(
        "app.runtime.invoker._record_knowledge_activation_for_request",
        fake_record_knowledge_activation,
        raising=False,
    )
    monkeypatch.setattr("app.runtime.invoker._resolve_kernel_for_request", lambda _request: FakeKernel())

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "查一下我的 source refs notes"}],
            agent_name="Agent",
            role_description="Runtime engineer",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=session,
        )
    )

    assert result.content == "done"
    assert len(seen_suffixes) == 1
    assert prefetch_calls == 0
    assert "## Personal Knowledge Hint" not in seen_suffixes[0]
    assert "Retrieval notes" not in seen_suffixes[0]
    assert "kb://person/" not in seen_suffixes[0]


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
    assert captured["kwargs"]["include_memory_file"] is False
    assert captured["kwargs"]["include_runtime_metadata"] is False


@pytest.mark.asyncio
async def test_resolve_retrieval_context_does_not_prefetch_knowledge(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _resolve_retrieval_context

    async def fake_build_memory_context(*args, **kwargs):
        raise AssertionError("memory context belongs to _resolve_memory_context")

    async def fake_build_agent_runtime_context(*args, **kwargs):
        raise AssertionError("runtime metadata belongs to _resolve_runtime_metadata_context")

    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_build_memory_context)
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

    assert result == ""
    assert "context_artifacts" not in request.session_context.metadata


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

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"skills/web-research/SKILL.md"}'},
                    }
                ],
                reasoning_content="reasoning",
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="final answer",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 8},
            ),
        ]
    )

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
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
async def test_invoke_agent_applies_model_temperature_and_reasoning_kwargs(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(
        provider="qwen",
        model="qwen3-max",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
        temperature=0.2,
        reasoning_mode="enabled",
        reasoning_effort=None,
        reasoning_budget_tokens=4096,
        preserve_reasoning=True,
        text_verbosity=None,
        provider_options=None,
    )
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            )
        ]
    )

    async def fake_empty_context(*args, **kwargs):
        return ""

    async def fake_resolve_runtime_config(_agent_id):
        from app.kernel import RuntimeConfig

        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", fake_resolve_runtime_config)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", lambda messages, **kwargs: messages)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Qwen Agent",
            role_description="Tests reasoning config",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "done"
    assert fake_client.calls[0]["temperature"] == 0.2
    assert fake_client.calls[0]["enable_thinking"] is True
    assert fake_client.calls[0]["thinking_budget"] == 4096
    assert fake_client.calls[0]["preserve_thinking"] is True


@pytest.mark.asyncio
async def test_invoke_agent_forwards_delegation_token_to_tool_governance(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    user_id = uuid4()
    expected_token = object()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"workspace/notes.md"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )

    async def fake_execute_tool(
        tool_name,
        args,
        agent_id=None,
        user_id=None,
        event_callback=None,
        delegation_token=None,
    ):
        assert tool_name == "read_file"
        assert args == {"path": "workspace/notes.md"}
        assert delegation_token is expected_token
        return "notes contents"

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_empty_context(*args, **kwargs):
        return ""

    async def fake_resolve_runtime_config(_agent_id):
        from app.kernel import RuntimeConfig

        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", fake_resolve_runtime_config)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", lambda messages, **kwargs: messages)
    monkeypatch.setattr(
        "app.runtime.invoker.get_agent_tools_for_llm",
        lambda *args, **kwargs: [
            {
                "type": "function",
                "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}},
            }
        ],
    )
    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "read focus"}],
            agent_name="Reader",
            role_description="Reads files",
            agent_id=agent_id,
            user_id=user_id,
            delegation_token=expected_token,
        )
    )

    assert result.content == "done"


@pytest.mark.asyncio
async def test_custom_tool_executor_receives_delegation_token(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _execute_tool_with_request

    expected_token = object()
    seen: dict[str, object] = {}

    async def custom_executor(tool_name, args, *, delegation_token=None, event_callback=None):
        seen["tool_name"] = tool_name
        seen["args"] = args
        seen["delegation_token"] = delegation_token
        seen["event_callback"] = event_callback
        return "custom-ok"

    async def emit_event(_payload):
        return None

    request = AgentInvocationRequest(
        model=object(),
        messages=[{"role": "user", "content": "x"}],
        agent_name="Agent",
        role_description="role",
        tool_executor=custom_executor,
        delegation_token=expected_token,
    )

    result = await _execute_tool_with_request("read_file", {"path": "workspace/notes.md"}, request, emit_event)

    assert result == "custom-ok"
    assert seen["tool_name"] == "read_file"
    assert seen["args"] == {"path": "workspace/notes.md"}
    assert seen["delegation_token"] is expected_token
    assert seen["event_callback"] is emit_event


@pytest.mark.asyncio
async def test_custom_tool_executor_disables_inner_runtime_hooks():
    from app.runtime.invoker import AgentInvocationRequest, _execute_tool_with_request

    seen: dict[str, object] = {}

    async def custom_executor(tool_name, args, *, emit_runtime_hooks=True):
        seen["tool_name"] = tool_name
        seen["args"] = args
        seen["emit_runtime_hooks"] = emit_runtime_hooks
        return "custom-ok"

    async def emit_event(_payload):
        return None

    request = AgentInvocationRequest(
        model=object(),
        messages=[{"role": "user", "content": "x"}],
        agent_name="Agent",
        role_description="role",
        tool_executor=custom_executor,
    )

    result = await _execute_tool_with_request("read_file", {"path": "workspace/notes.md"}, request, emit_event)

    assert result == "custom-ok"
    assert seen["tool_name"] == "read_file"
    assert seen["args"] == {"path": "workspace/notes.md"}
    assert seen["emit_runtime_hooks"] is False


@pytest.mark.asyncio
async def test_execute_tool_receives_interactive_available_for_web_chat_session(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _execute_tool_with_request
    from app.runtime.session import SessionContext

    seen: dict[str, object] = {}

    async def fake_execute_tool(
        tool_name,
        args,
        *,
        agent_id,
        user_id,
        event_callback=None,
        delegation_token=None,
        session_id=None,
        plan_mode_interactive_available=False,
    ):
        seen["tool_name"] = tool_name
        seen["args"] = args
        seen["agent_id"] = agent_id
        seen["user_id"] = user_id
        seen["event_callback"] = event_callback
        seen["delegation_token"] = delegation_token
        seen["session_id"] = session_id
        seen["plan_mode_interactive_available"] = plan_mode_interactive_available
        return "tool-ok"

    async def emit_event(_payload):
        return None

    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    agent_id = uuid4()
    user_id = uuid4()
    request = AgentInvocationRequest(
        model=object(),
        messages=[{"role": "user", "content": "schedule daily brief"}],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=user_id,
        session_context=SessionContext(session_id="session-1", source="web_chat", channel="web"),
    )

    result = await _execute_tool_with_request("set_trigger", {"name": "Daily brief"}, request, emit_event)

    assert result == "tool-ok"
    assert seen["session_id"] == "session-1"
    assert seen["plan_mode_interactive_available"] is True


@pytest.mark.asyncio
async def test_execute_tool_receives_session_frame_metadata(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _execute_tool_with_request
    from app.runtime.session import SessionContext

    seen: dict[str, object] = {}

    async def fake_execute_tool(
        tool_name,
        args,
        *,
        agent_id,
        user_id,
        turn_id=None,
        runtime_task_id=None,
        budget_run_id=None,
        round_state=None,
        t0_refs=(),
        **_kwargs,
    ):
        seen.update(
            {
                "tool_name": tool_name,
                "args": args,
                "agent_id": agent_id,
                "user_id": user_id,
                "turn_id": turn_id,
                "runtime_task_id": runtime_task_id,
                "budget_run_id": budget_run_id,
                "round_state": round_state,
                "t0_refs": t0_refs,
            }
        )
        return "tool-ok"

    async def emit_event(_payload):
        return None

    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    agent_id = uuid4()
    user_id = uuid4()
    session = SessionContext(
        session_id="session-1",
        metadata={
            "turn_id": "turn-1",
            "runtime_task_id": "runtime-1",
            "budget_run_id": str(uuid4()),
            "round_state": {"round": 2},
            "t0_refs": ["t0://sessions/session-1/segments/seg-1/events.jsonl#9"],
        },
    )
    request = AgentInvocationRequest(
        model=object(),
        messages=[{"role": "user", "content": "write"}],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=user_id,
        session_context=session,
    )

    result = await _execute_tool_with_request(
        "write_file", {"path": "workspace/a.md", "content": "x"}, request, emit_event
    )

    assert result == "tool-ok"
    assert seen["turn_id"] == "turn-1"
    assert seen["runtime_task_id"] == "runtime-1"
    assert seen["budget_run_id"] == session.metadata["budget_run_id"]
    assert seen["round_state"] == {"round": 2}
    assert seen["t0_refs"] == ("t0://sessions/session-1/segments/seg-1/events.jsonl#9",)


@pytest.mark.asyncio
async def test_execute_tool_receives_execution_shape_in_round_state(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _execute_tool_with_request
    from app.runtime.session import SessionContext

    seen: dict[str, object] = {}

    async def fake_execute_tool(
        tool_name,
        args,
        *,
        agent_id,
        user_id,
        round_state=None,
        **_kwargs,
    ):
        seen.update(
            {
                "tool_name": tool_name,
                "args": args,
                "agent_id": agent_id,
                "user_id": user_id,
                "round_state": round_state,
            }
        )
        return "tool-ok"

    async def emit_event(_payload):
        return None

    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    session = SessionContext(
        session_id="session-1",
        metadata={"turn_route": {"execution_shape": "one_off_parallel"}},
    )
    request = AgentInvocationRequest(
        model=object(),
        messages=[{"role": "user", "content": "parallelize research"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=session,
    )

    result = await _execute_tool_with_request("spawn_subagent", {"task": "work"}, request, emit_event)

    assert result == "tool-ok"
    assert seen["round_state"] == {"execution_shape": "one_off_parallel"}


@pytest.mark.asyncio
async def test_execute_tool_receives_plan_execution_contract_in_round_state(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _execute_tool_with_request
    from app.runtime.session import PlanModeState, SessionContext

    seen: dict[str, object] = {}

    async def fake_execute_tool(
        tool_name,
        args,
        *,
        agent_id,
        user_id,
        round_state=None,
        **_kwargs,
    ):
        seen.update(
            {
                "tool_name": tool_name,
                "args": args,
                "agent_id": agent_id,
                "user_id": user_id,
                "round_state": round_state,
            }
        )
        return "tool-ok"

    async def emit_event(_payload):
        return None

    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    agent_id = uuid4()
    user_id = uuid4()
    contract = {"type": "agent_team", "name": "ABS research team"}
    session = SessionContext(
        session_id="session-1",
        metadata={"round_state": {"round": 2}},
        plan_mode=PlanModeState(active=False, execution_contract=contract),
    )
    request = AgentInvocationRequest(
        model=object(),
        messages=[{"role": "user", "content": "execute approved plan"}],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=user_id,
        session_context=session,
    )

    result = await _execute_tool_with_request("spawn_subagent", {"prompt": "work"}, request, emit_event)

    assert result == "tool-ok"
    assert seen["round_state"] == {"round": 2, "execution_contract": contract}


@pytest.mark.asyncio
async def test_resolve_tool_expansion_ignores_load_skill_for_schema_loading(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, _resolve_tool_expansion

    async def fake_get_agent_tools_for_llm(*_args, **_kwargs):
        return [
            {
                "type": "function",
                "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}},
            }
        ]

    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_get_agent_tools_for_llm)

    result = await _resolve_tool_expansion(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
            ),
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
async def test_tool_search_records_discovered_tools_and_returns_deferred_schema(monkeypatch):
    import app.runtime.invoker as invoker
    from app.runtime.invoker import AgentInvocationRequest, _resolve_tool_expansion
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    session = SessionContext()
    requested_names_seen: list[list[str] | None] = []

    async def fake_get_agent_tools_for_llm(agent_id_arg, *, core_only=False, requested_names=None):
        assert agent_id_arg == agent_id
        assert core_only is False
        requested_names_seen.append(list(requested_names or []))
        return [
            {
                "type": "function",
                "function": {"name": "firecrawl_fetch", "description": "", "parameters": {"type": "object"}},
            }
        ]

    async def fake_deferred_tool_names(*_args, **_kwargs):
        return ["firecrawl_fetch"]

    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", fake_get_agent_tools_for_llm)
    monkeypatch.setattr(invoker, "_deferred_tool_names_for_query", fake_deferred_tool_names)

    result = await _resolve_tool_expansion(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
            ),
            messages=[{"role": "user", "content": "search web"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=uuid4(),
            session_context=session,
        ),
        "tool_search",
        {"query": "firecrawl_fetch"},
    )

    assert result is not None
    assert requested_names_seen == [["firecrawl_fetch"]]
    assert [tool["function"]["name"] for tool in result.tools] == ["firecrawl_fetch"]
    assert session.discovered_tools == ["firecrawl_fetch"]
    assert session.metadata["discovered_tools"] == ["firecrawl_fetch"]
    assert result.event_payload["type"] == "deferred_tools_delta"
    assert result.event_payload["discovered_tools"] == ["firecrawl_fetch"]
    manifest = result.event_payload["tool_search_manifest"]
    assert manifest["loaded_tool_schemas"] == [{"name": "firecrawl_fetch", "callable": True}]
    assert manifest["skill_candidates"] == []
    assert manifest["subagent_candidates"] == []
    assert manifest["mcp_candidates"] == []
    assert session.metadata["tool_search_manifests"][-1] == manifest


@pytest.mark.asyncio
async def test_tool_search_records_compact_requested_tool_alias(monkeypatch):
    import app.runtime.invoker as invoker
    from app.runtime.invoker import AgentInvocationRequest, _resolve_tool_expansion
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    session = SessionContext()
    requested_names_seen: list[list[str] | None] = []

    async def fake_get_agent_tools_for_llm(agent_id_arg, *, core_only=False, requested_names=None):
        assert agent_id_arg == agent_id
        assert core_only is False
        requested_names_seen.append(list(requested_names or []))
        return [
            {
                "type": "function",
                "function": {"name": "firecrawl_fetch", "description": "", "parameters": {"type": "object"}},
            }
        ]

    async def fake_deferred_tool_names(*_args, **_kwargs):
        return ["firecrawl_fetch"]

    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", fake_get_agent_tools_for_llm)
    monkeypatch.setattr(invoker, "_deferred_tool_names_for_query", fake_deferred_tool_names)

    result = await _resolve_tool_expansion(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None
            ),
            messages=[{"role": "user", "content": "use firecrawlfetch"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=uuid4(),
            session_context=session,
        ),
        "tool_search",
        {"query": "firecrawlfetch"},
    )

    assert result is not None
    assert requested_names_seen == [["firecrawl_fetch"]]
    assert session.discovered_tools == ["firecrawl_fetch"]


@pytest.mark.asyncio
async def test_load_skill_and_skill_file_reads_do_not_expand_tool_schemas(monkeypatch, tmp_path):
    import app.runtime.invoker as invoker
    from app.runtime.invoker import AgentInvocationRequest, _resolve_tool_expansion

    skill_dir = tmp_path / "skills" / "web-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: web research
description: web research guide
tools:
  - web_search
packs:
  - web_pack
---
# Web Research

Use tool_search to discover web tools, then call the matching tool.
""",
        encoding="utf-8",
    )
    agent_id = uuid4()

    async def fake_get_agent_tools_for_llm(*_args, **_kwargs):
        return [
            {
                "type": "function",
                "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}},
            }
        ]

    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", fake_get_agent_tools_for_llm)

    request = AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None, max_output_tokens=None),
        messages=[{"role": "user", "content": "load skill"}],
        agent_name="Researcher",
        role_description="Research agent",
        agent_id=agent_id,
        user_id=uuid4(),
    )

    assert await _resolve_tool_expansion(request, "load_skill", {"name": "web research"}) is None
    assert await _resolve_tool_expansion(request, "read_file", {"path": "skills/web-research/SKILL.md"}) is None


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
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="final answer",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )
    emitted: list[tuple[HookEvent, dict]] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        return [
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}}}
        ]

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine.asyncio.ensure_future", lambda coro: asyncio.create_task(coro))
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
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
    assert HookEvent.TURN_STOP in event_names
    response_payload = next(payload for event, payload in emitted if event == HookEvent.RESPONSE_COMPLETE)
    close_payload = next(payload for event, payload in emitted if event == HookEvent.TURN_STOP)
    assert response_payload["messages"][-1]["role"] == "user"
    assert response_payload["metadata"]["skill_candidate_loop_enabled"] is False
    assert close_payload["messages"][-1]["role"] == "assistant"
    assert close_payload["messages"][-1]["content"] == "final answer"
    assert close_payload["metadata"]["checkpoint_kind"] == "user_turn_stop"


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
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="final answer",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )
    emitted: list[tuple[HookEvent, dict]] = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine.asyncio.ensure_future", lambda coro: asyncio.create_task(coro))
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "app.runtime.invoker.build_skill_catalog_section_for_agent",
        lambda *args, **kwargs: "## Skills\n- load_skill",
    )
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


@pytest.mark.real_runtime_config
@pytest.mark.asyncio
async def test_resolve_runtime_config_defaults_skill_candidate_loop_to_true_when_missing(monkeypatch):
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
                _FakeScalarResult(None),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, _stmt):
            # enter_rls_bypass issues SET LOCAL app.current_tenant_id before the
            # real query — the GUC statement must not consume a business result.
            if "app.current_tenant_id" in str(_stmt):
                return _FakeScalarResult(None)
            return self._results.pop(0)

    async def fake_is_feature_enabled(*args, **kwargs):
        raise AssertionError("is_feature_enabled should not be called when the flag row is missing")

    async def fake_resolve_tenant_for_agent(_agent_id, **_kwargs):
        return tenant_id

    monkeypatch.setattr("app.runtime.invoker.resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr("app.runtime.invoker.tenant_scoped_session", lambda *a, **k: _FakeSession())
    monkeypatch.setattr("app.runtime.invoker.get_settings", lambda: SimpleNamespace(DEBUG=False))
    monkeypatch.setattr("app.runtime.invoker.is_feature_enabled", fake_is_feature_enabled)

    config = await _resolve_runtime_config(agent_id)

    assert config.tenant_id == tenant_id
    assert config.max_tool_rounds == 33
    assert config.turn_token_budget is not None
    assert config.turn_token_budget > 0
    assert config.runtime_continuity_enabled is False
    assert config.skill_candidate_loop_enabled is True


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

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        return [
            {
                "type": "function",
                "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "delegate_to_agent", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "send_message_to_agent", "description": "", "parameters": {"type": "object"}},
            },
        ]

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
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

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_get_agent_tools_for_llm(_agent_id, core_only=False, requested_names=None):
        return [
            {
                "type": "function",
                "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "write_file", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "search_memory", "description": "", "parameters": {"type": "object"}},
            },
        ]

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
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

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_empty_context(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "app.runtime.invoker.build_skill_catalog_section_for_agent",
        lambda *args, **kwargs: "## Skills\n- load_skill",
    )
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
            memory_context="MEMORY_CONTEXT",
        )
    )

    assert result.content == "done"
    system_prompt = fake_client.calls[0]["messages"][0].content
    # Frozen prefix stays in the first system message; dynamic suffix is a transient
    # tail notice so provider prompt-cache prefixes are not invalidated every turn.
    assert "BASE_PROMPT" in system_prompt
    assert "## System" in system_prompt
    assert "__PROMPT_DYNAMIC_BOUNDARY__" not in system_prompt
    assert "MEMORY_CONTEXT" not in system_prompt
    dynamic_notice = fake_client.calls[0]["messages"][-1]
    assert dynamic_notice.role == "user"
    assert "[System Notice]" in dynamic_notice.content
    assert "## Your Memory System" in dynamic_notice.content
    assert "MEMORY_CONTEXT" in dynamic_notice.content
    assert "search_memory" in dynamic_notice.content
    assert "memory_provider:recall" not in dynamic_notice.content
    assert 'kind="memory_recall"' not in dynamic_notice.content
    assert "KB_CONTEXT" not in dynamic_notice.content


@pytest.mark.asyncio
async def test_invoke_agent_writes_prompt_assembly_manifest_from_actual_prompt(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
    )
    session_context = SessionContext(session_id="session-manifest", source="web", channel="web")
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_runtime_context(*args, **kwargs):
        return "RUNTIME_CONTEXT"

    async def fake_empty_context(*args, **kwargs):
        return ""

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.build_agent_runtime_context", fake_runtime_context)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_empty_context)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "app.runtime.invoker.build_skill_catalog_section_for_agent",
        lambda *args, **kwargs: "## Skills\n- load_skill",
    )
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "检查上下文组装"}],
            agent_name="Analyst",
            role_description="Policy analyst",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_context="MEMORY_CONTEXT",
            session_context=session_context,
        )
    )

    assert result.content == "done"
    manifest = session_context.metadata["runtime_assembly_state"]["prompt_assembly_manifest"]
    assert "prompt_assembly_manifest" not in session_context.metadata
    system_prompt = fake_client.calls[0]["messages"][0].content
    dynamic_notice = fake_client.calls[0]["messages"][-1].content

    assert manifest["schema"] == "hive.ccplus.prompt_assembly_manifest.v1"
    assert manifest["source_of_truth"] == "runtime_prompt_assembly"
    assert manifest["session_id"] == "session-manifest"
    assert manifest["actual_system_prompt_chars"] == len(system_prompt)
    assert manifest["actual_dynamic_notice_chars"] == len(dynamic_notice)
    assert manifest["context_budget"]["model_window"] == 128000
    assert manifest["loaded_skills"] == ["load_skill"]
    assert "runtime_metadata_context" in manifest["dynamic_sections"]
    assert "skill_catalog" in manifest["dynamic_sections"]
    assert "knowledge_context" not in manifest["dynamic_sections"]
    ledger = manifest["context_usage_ledger"]
    categories = {item["name"]: item for item in ledger["categories"]}
    assert ledger["schema"] == "hive.ccplus.context_usage_ledger.v1"
    assert categories["messages"]["tokens"] > 0
    assert categories["free_space"]["tokens"] >= 0
    dynamic_section_ledger = manifest["dynamic_context_section_ledger"]
    assert dynamic_section_ledger["schema"] == "hive.ccplus.dynamic_context_section_ledger.v1"
    section_decisions = {item["candidate_id"]: item for item in dynamic_section_ledger["sections"]}
    assert section_decisions["dynamic:runtime:runtime_metadata"]["selected"] is True
    assert section_decisions["dynamic:skill:skill_catalog"]["budget_key"] == "skill_catalog_budget_chars"
    assert (
        session_context.metadata["runtime_assembly_state"]["dynamic_context_section_ledger"] == dynamic_section_ledger
    )
    assert "dynamic_context_section_ledger" not in session_context.metadata
    artifacts = session_context.metadata["context_artifacts"]
    artifact_kinds = {item["kind"] for item in artifacts}
    assert {"frozen_prefix", "skill_catalog"} <= artifact_kinds
    assert any(item.get("candidate_id") == "ctx:skill:skill_catalog" for item in artifacts)


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
            invocation_scope="coordinator",
        )
    )

    assert result.content == "ok"
    assert captured["request"].invocation_scope == "coordinator"


@pytest.mark.asyncio
async def test_invoke_agent_records_skill_runtime_usage_and_preserves_tool_callback(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    session_context = SessionContext(session_id="task-session-1", source="task", channel="task")
    forwarded_events: list[dict] = []
    telemetry_calls: list[dict] = []

    async def record_forwarded_event(data: dict) -> None:
        forwarded_events.append(data)

    class _FakeKernel:
        async def handle(self, request):
            await request.on_tool_call({"name": "load_skill", "args": {"name": "Incident Response"}, "status": "done"})
            await request.on_tool_call(
                {"name": "read_file", "args": {"path": "workspace/runbook.md"}, "status": "done"}
            )
            return SimpleNamespace(
                content="[OUTCOME:action_taken] Updated the runbook.",
                tokens_used=7,
                final_tools=None,
                parts=[],
            )

    def fake_record_skill_runtime_usage_for_invocation(**kwargs):
        telemetry_calls.append(kwargs)
        return {"decision": "candidate"}

    async def fake_emit_hook(*args, **kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr(
        "app.runtime.invoker.record_skill_runtime_usage_for_invocation",
        fake_record_skill_runtime_usage_for_invocation,
        raising=False,
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "use the incident skill"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=uuid4(),
            session_context=session_context,
            on_tool_call=record_forwarded_event,
        )
    )

    assert result.content == "[OUTCOME:action_taken] Updated the runbook."
    assert [event["name"] for event in forwarded_events] == ["load_skill", "read_file"]
    assert len(telemetry_calls) == 1
    assert telemetry_calls[0]["agent_id"] == agent_id
    assert telemetry_calls[0]["session_context"] is session_context
    assert telemetry_calls[0]["terminal_status"] == "completed"
    assert telemetry_calls[0]["assistant_text"] == "[OUTCOME:action_taken] Updated the runbook."
    assert telemetry_calls[0]["tool_events"] == [
        {"name": "load_skill", "args": {"name": "Incident Response"}, "status": "done"},
        {"name": "read_file", "args": {"path": "workspace/runbook.md"}, "status": "done"},
    ]


@pytest.mark.asyncio
async def test_invoke_agent_records_failed_skill_runtime_usage_when_kernel_raises(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    agent_id = uuid4()
    session_context = SessionContext(session_id="task-session-failure", source="task", channel="task")
    telemetry_calls: list[dict] = []

    class _FakeKernel:
        async def handle(self, request):
            assert request.on_tool_call is not None
            await request.on_tool_call({"name": "load_skill", "args": {"name": "Incident Response"}, "status": "done"})
            raise RuntimeError("provider exploded")

    def fake_record_skill_runtime_usage_for_invocation(**kwargs):
        telemetry_calls.append(kwargs)
        return {"decision": "candidate"}

    async def fake_emit_hook(*args, **kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr(
        "app.runtime.invoker.record_skill_runtime_usage_for_invocation",
        fake_record_skill_runtime_usage_for_invocation,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        await invoke_agent(
            AgentInvocationRequest(
                model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
                messages=[{"role": "user", "content": "use the incident skill"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=agent_id,
                user_id=uuid4(),
                session_context=session_context,
            )
        )

    assert len(telemetry_calls) == 1
    assert telemetry_calls[0]["agent_id"] == agent_id
    assert telemetry_calls[0]["session_context"] is session_context
    assert telemetry_calls[0]["terminal_status"] == "failed"
    assert "provider exploded" in telemetry_calls[0]["assistant_text"]
    assert telemetry_calls[0]["tool_events"] == [
        {"name": "load_skill", "args": {"name": "Incident Response"}, "status": "done"}
    ]


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
        lambda: [
            {
                "type": "function",
                "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}},
            }
        ],
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

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )
    runtime_events: list[dict] = []

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        on_compaction = kwargs.get("on_compaction")
        assert on_compaction is not None
        await on_compaction(
            {
                "summary": "older context compressed",
                "original_message_count": len(messages),
                "kept_message_count": 2,
            }
        )
        return [{"role": "system", "content": "[Previous conversation summary]\nolder context compressed"}]

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
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
    compaction_events = [event for event in runtime_events if event.get("type") == "session_compact"]
    assert compaction_events == [
        {
            "type": "session_compact",
            "summary": "older context compressed",
            "original_message_count": 3,
            "kept_message_count": 2,
        }
    ]


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

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"workspace/todo.md","content":"todo"}',
                        },
                    }
                ],
                reasoning_content="reasoning",
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="request blocked",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 8},
            ),
        ]
    )

    runtime_events: list[dict] = []

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        return messages

    async def fake_execute_tool(tool_name, args, agent_id=None, user_id=None, event_callback=None):
        assert tool_name == "write_file"
        assert event_callback is not None
        await event_callback(
            {
                "type": "permission",
                "tool_name": "write_file",
                "status": "approval_required",
                "message": "This action requires approval.",
                "approval_id": "approval-123",
            }
        )
        return "⏳ This action requires approval. An approval request has been sent. (Approval ID: approval-123)"

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr(
        "app.runtime.invoker.get_agent_tools_for_llm",
        lambda *args, **kwargs: [
            {
                "type": "function",
                "function": {"name": "write_file", "description": "", "parameters": {"type": "object"}},
            }
        ],
    )
    monkeypatch.setattr("app.runtime.invoker.execute_tool", fake_execute_tool)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *args, **kwargs: 2048)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "写入 workspace/notes.md"}],
            agent_name="Analyst",
            role_description="Policy analyst",
            agent_id=agent_id,
            user_id=user_id,
            on_event=runtime_events.append,
        )
    )

    assert result.content == "request blocked"
    permission_events = [event for event in runtime_events if event.get("type") == "permission"]
    assert permission_events == [
        {
            "type": "permission",
            "tool_name": "write_file",
            "status": "approval_required",
            "message": "This action requires approval.",
            "approval_id": "approval-123",
        }
    ]


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

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )
    captured = {}

    async def fake_resolve_runtime_config(_agent_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            max_tool_rounds=50,
            quota_message=None,
            skill_candidate_loop_enabled=True,
        )

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_build_memory_context(_agent_id, _tenant_id, *, session_id=None, query="", **_kwargs):
        captured["loaded"] = (_agent_id, _tenant_id, session_id, query)
        return "RUNTIME_MEMORY"

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
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", fake_build_memory_context)
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
            memory_context="MANUAL_MEMORY",
        )
    )

    assert result.content == "done"
    assert captured["loaded"] == (agent_id, tenant_id, "session-1", "hello")
    _sys_prompt = fake_client.calls[0]["messages"][0].content
    assert "BASE_PROMPT" in _sys_prompt
    assert "RUNTIME_MEMORY" not in _sys_prompt
    assert any(
        "RUNTIME_MEMORY" in (getattr(message, "content", "") or "") for message in fake_client.calls[0]["messages"]
    )
    assert "MANUAL_MEMORY" not in _sys_prompt
    assert any(
        "MANUAL_MEMORY" in (getattr(message, "content", "") or "") for message in fake_client.calls[0]["messages"]
    )
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
async def test_invoke_agent_keeps_primary_for_simple_turn_without_explicit_smart_routing(monkeypatch):
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
    assert captured["request"].model is primary_model
    assert captured["request"].fallback_model is fallback_model
    assert captured["request"].supports_vision is True
    route = session_context.metadata["turn_route"]
    assert {
        key: route[key]
        for key in (
            "selected_model",
            "fallback_model",
            "reason",
            "task_profile",
            "complexity",
            "execution_shape",
            "config_source",
        )
    } == {
        "selected_model": "gpt-4.1",
        "fallback_model": "gpt-4.1-mini",
        "reason": "primary_model",
        "task_profile": "model_owned",
        "complexity": "model_owned",
        "execution_shape": "direct",
        "config_source": "runtime_default",
    }


@pytest.mark.asyncio
async def test_invoke_agent_does_not_downgrade_model_from_smart_routing_keywords(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}
    session_context = SessionContext(session_id="s-route-enabled", source="websocket")
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
            smart_model_routing={"enabled": True},
        )
    )

    assert result.content == "ok"
    assert captured["request"].model is primary_model
    assert captured["request"].fallback_model is fallback_model
    assert captured["request"].supports_vision is True
    route = session_context.metadata["turn_route"]
    assert {
        key: route[key]
        for key in (
            "selected_model",
            "fallback_model",
            "reason",
            "task_profile",
            "complexity",
            "execution_shape",
            "config_source",
        )
    } == {
        "selected_model": "gpt-4.1",
        "fallback_model": "gpt-4.1-mini",
        "reason": "primary_model",
        "task_profile": "model_owned",
        "complexity": "model_owned",
        "execution_shape": "direct",
        "config_source": "agent_config",
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
            invocation_scope="task",
            session_context=session_context,
        )
    )

    assert result.content == "ok"
    assert captured["request"].model is primary_model
    assert captured["request"].fallback_model is fallback_model
    assert captured["request"].supports_vision is True
    route = session_context.metadata["turn_route"]
    assert {
        key: route[key]
        for key in (
            "selected_model",
            "fallback_model",
            "reason",
            "task_profile",
            "complexity",
            "execution_shape",
            "config_source",
        )
    } == {
        "selected_model": "gpt-4.1",
        "fallback_model": "gpt-4.1-mini",
        "reason": "primary_model",
        "task_profile": "model_owned",
        "complexity": "model_owned",
        "execution_shape": "direct",
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
    route = session_context.metadata["turn_route"]
    assert {
        key: route[key]
        for key in (
            "selected_model",
            "fallback_model",
            "reason",
            "task_profile",
            "complexity",
            "execution_shape",
            "config_source",
        )
    } == {
        "selected_model": "gpt-4.1",
        "fallback_model": "gpt-4.1-mini",
        "reason": "primary_model",
        "task_profile": "model_owned",
        "complexity": "model_owned",
        "execution_shape": "direct",
        "config_source": "agent_config",
    }


# ── P0-1b: tenant_resolution_error sentinel on invoker fallback paths ─────
# Three fallback paths previously returned RuntimeConfig(tenant_id=None)
# silently, letting governance skip capability checks. Now they set the
# tenant_resolution_error sentinel so kernel.engine aborts before any tool
# runs (governance fail-closed in P0-1a is the second line of defence).


class _FakeAsyncSessionCM:
    """Minimal async context manager mimicking async_session() for unit tests."""

    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeDB:
    def __init__(self, agent_value, raise_on_execute: Exception | None = None) -> None:
        self._agent_value = agent_value
        self._raise = raise_on_execute

    async def execute(self, _stmt):
        if self._raise is not None:
            raise self._raise
        return _FakeScalarResult(self._agent_value)


@pytest.mark.real_runtime_config
@pytest.mark.asyncio
async def test_resolve_runtime_config_no_agent_id_sets_tenant_error():
    from app.runtime.invoker import _resolve_runtime_config

    cfg = await _resolve_runtime_config(None)

    assert cfg.tenant_id is None
    assert cfg.tenant_resolution_error is not None
    assert "agent_id" in cfg.tenant_resolution_error.lower() or "no agent" in cfg.tenant_resolution_error.lower()


@pytest.mark.real_runtime_config
@pytest.mark.asyncio
async def test_resolve_runtime_config_agent_not_found_sets_tenant_error(monkeypatch):
    from app.runtime import invoker

    missing_id = uuid4()
    monkeypatch.setattr(invoker, "async_session", lambda: _FakeAsyncSessionCM(_FakeDB(agent_value=None)))

    cfg = await invoker._resolve_runtime_config(missing_id)

    assert cfg.tenant_id is None
    assert cfg.tenant_resolution_error is not None
    assert str(missing_id) in cfg.tenant_resolution_error
    assert "not found" in cfg.tenant_resolution_error


@pytest.mark.real_runtime_config
@pytest.mark.asyncio
async def test_resolve_runtime_config_db_exception_sets_tenant_error(monkeypatch):
    from app.runtime import invoker

    failing_id = uuid4()
    monkeypatch.setattr(
        invoker,
        "async_session",
        lambda: _FakeAsyncSessionCM(_FakeDB(agent_value=None, raise_on_execute=RuntimeError("DB down"))),
    )

    cfg = await invoker._resolve_runtime_config(failing_id)

    assert cfg.tenant_id is None
    assert cfg.tenant_resolution_error is not None
    assert "DB down" in cfg.tenant_resolution_error or "failed" in cfg.tenant_resolution_error.lower()


@pytest.mark.real_runtime_config
@pytest.mark.asyncio
async def test_resolve_runtime_config_success_does_not_set_tenant_error(monkeypatch):
    """Sanity: existing successful path must not set the new sentinel."""
    from app.runtime import invoker

    fake_agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        max_tool_rounds=42,
        execution_mode="standard",
    )

    async def _fake_flag(*_args, **_kwargs):
        return False

    async def _fake_resolve_tenant_for_agent(_agent_id, **_kwargs):
        return fake_agent.tenant_id

    monkeypatch.setattr(invoker, "resolve_tenant_for_agent", _fake_resolve_tenant_for_agent)
    monkeypatch.setattr(
        invoker,
        "tenant_scoped_session",
        lambda *a, **k: _FakeAsyncSessionCM(_FakeDB(agent_value=fake_agent)),
    )
    monkeypatch.setattr(invoker, "is_feature_enabled", _fake_flag)

    cfg = await invoker._resolve_runtime_config(fake_agent.id)

    assert cfg.tenant_id == fake_agent.tenant_id
    assert cfg.max_tool_rounds == 42
    assert cfg.tenant_resolution_error is None


@pytest.mark.real_runtime_config
@pytest.mark.asyncio
async def test_invoke_agent_aborts_when_tenant_resolution_fails(monkeypatch):
    """End-to-end: invoke_agent with an unresolvable agent_id must abort
    before any tool runs. Verifies kernel.engine reads the sentinel and
    returns an error result, never reaching create_llm_client."""
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    # Make _resolve_runtime_config hit the "agent not found" fallback by
    # stubbing async_session to return None for any agent lookup.
    from app.runtime import invoker as invoker_mod

    monkeypatch.setattr(
        invoker_mod,
        "async_session",
        lambda: _FakeAsyncSessionCM(_FakeDB(agent_value=None)),
    )

    # If kernel ever reaches LLM client creation, fail loudly — we expect early abort.
    def _exploding_client(**_kwargs):
        raise AssertionError(
            "create_llm_client must NOT be called when tenant resolution fails — kernel should abort first"
        )

    monkeypatch.setattr("app.runtime.invoker.create_llm_client", _exploding_client)

    bad_agent_id = uuid4()
    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(
                provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None
            ),
            messages=[{"role": "user", "content": "hi"}],
            agent_name="Ghost",
            role_description="non-existent agent",
            agent_id=bad_agent_id,
            user_id=uuid4(),
        )
    )

    # Kernel returned an error result with the tenant_resolution_error message.
    assert result.content.startswith("[Error]")
    assert "tenant resolution failed" in result.content.lower()
    assert str(bad_agent_id) in result.content


@pytest.mark.asyncio
async def test_disable_tools_yields_empty_tool_surface(monkeypatch):
    """RC11: a disable_tools request must expose ZERO tools to the LLM.

    Pure-text reasoning passes must expose ZERO tools to the LLM. Previously
    they set core_tools_only=True, which still leaked write_file even though
    the prompt declared "Tools are disabled". With low max_tool_rounds that
    extra write_file call could blow the round budget; disable_tools makes the
    surface genuinely empty so the model must answer in text.
    """
    from app.runtime.invoker import AgentInvocationRequest, get_agent_kernel

    async def fake_tools(agent_id, core_only=False, requested_names=None):
        return [
            {"function": {"name": "write_file"}},
            {"function": {"name": "read_file"}},
        ]

    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_tools)

    disabled_kernel = get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
            messages=[{"role": "user", "content": "hi"}],
            agent_name="Researcher",
            role_description="internal reasoning",
            disable_tools=True,
        )
    )
    assert await disabled_kernel._deps.get_tools(uuid4(), True) == []

    # Regression guard: a normal request must still receive its full tool surface.
    enabled_kernel = get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
            messages=[{"role": "user", "content": "hi"}],
            agent_name="Researcher",
            role_description="internal reasoning",
        )
    )
    enabled_tools = await enabled_kernel._deps.get_tools(uuid4(), True)
    assert [t["function"]["name"] for t in enabled_tools] == ["write_file", "read_file"]


@pytest.mark.asyncio
async def test_invoke_agent_uses_request_max_output_tokens(monkeypatch):
    """Task1: a request may raise the per-call output-token ceiling so the Deep
    Research synthesis emits a full-length report instead of being truncated at
    the model's chat default. The kernel must feed request.max_output_tokens into
    get_max_tokens (which clamps to the hard limit) rather than only reading the
    model's configured ceiling."""
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **kwargs):
        return messages

    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    # Mirror get_max_tokens' real contract: the requested ceiling (3rd arg) wins
    # when present. A pass-through stub proves the request value reaches the call.
    monkeypatch.setattr(
        "app.runtime.invoker.get_max_tokens",
        lambda provider, model, max_output_tokens=None: max_output_tokens or 2048,
    )

    await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "write a long report"}],
            agent_name="Synthesizer",
            role_description="writer",
            agent_id=uuid4(),
            user_id=uuid4(),
            max_output_tokens=32768,
        )
    )

    assert fake_client.calls[0]["max_tokens"] == 32768


@pytest.mark.asyncio
async def test_invoke_agent_keeps_model_reasoning_alive_when_memory_is_unavailable(monkeypatch):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.services.memory_service import MemoryContextResult

    tenant_id = uuid4()
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="continued with visible evidence",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            )
        ]
    )

    async def runtime_config(_agent_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            tenant_resolution_error=None,
            quota_message=None,
            max_tool_rounds=50,
            execution_mode=None,
            primary_model=None,
            skill_candidate_loop_enabled=True,
        )

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

    async def fake_build_agent_context(*_args, **_kwargs):
        return "BASE_PROMPT"

    async def fake_compress(messages, **_kwargs):
        return messages

    async def fake_tools(*_args, **_kwargs):
        return []

    monkeypatch.setattr("app.runtime.invoker._resolve_runtime_config", runtime_config)
    monkeypatch.setattr("app.runtime.invoker.build_memory_context", unavailable)
    monkeypatch.setattr("app.runtime.invoker.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.invoker.maybe_compress_messages", fake_compress)
    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_tools)
    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **_kwargs: fake_client)
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.runtime.invoker.get_max_tokens", lambda *_args, **_kwargs: 2048)
    monkeypatch.setattr("app.runtime.invoker.persist_invocation_span", ignore_span)

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(session_id=str(uuid4()), metadata={"tenant_id": str(tenant_id)}),
        )
    )

    assert result.content == "continued with visible evidence"
    prompt_text = "\n".join(str(message.content) for message in fake_client.calls[0]["messages"])
    assert "Memory runtime degraded" in prompt_text
    assert "external effects are frozen" in prompt_text


# ── 切口② Work Ledger enable decision on the general path ─────────────────────


def _route_request(query: str) -> "object":
    from app.runtime.invoker import AgentInvocationRequest

    return AgentInvocationRequest(
        model=SimpleNamespace(model="gpt-4.1", provider="openai", max_input_tokens=128000),
        messages=[{"role": "user", "content": query}],
        agent_name="Agent",
        role_description="desc",
        session_context=SessionContext(session_id="s-1", source="web", channel="web"),
    )


def test_turn_route_keeps_optional_work_ledger_reminder_available_for_simple_qa():
    from app.runtime.invoker import _resolve_effective_turn_route

    request = _route_request("hi there")
    _resolve_effective_turn_route(request, routing_config=None)

    assert request.session_context.metadata["work_ledger_enabled"] is True


def test_turn_route_does_not_semantically_gate_work_ledger_by_prompt_complexity():
    from app.runtime.invoker import _resolve_effective_turn_route

    request = _route_request(
        "Please refactor the auth middleware in backend/app/auth/middleware.py, "
        "fix the failing pytest, and update the migration"
    )
    _resolve_effective_turn_route(request, routing_config=None)

    assert request.session_context.metadata["work_ledger_enabled"] is True


def test_skill_catalog_ranking_inputs_include_prompt_session_active_and_path_triggers() -> None:
    from app.runtime.invoker import AgentInvocationRequest, _skill_catalog_ranking_inputs

    session = SessionContext(session_id="session-skill-ranking")
    session.active_skills = ["incident"]
    session.metadata["task_profile"] = {"domain": "backend", "objective": "stabilize API boundary"}
    session.metadata["conditional_skill_activations"] = [
        {"skill_name": "python", "matched_path": "projects/api/app.py"},
        {"skill_name": "", "matched_path": "ignored"},
    ]
    request = AgentInvocationRequest(
        model=SimpleNamespace(name="test-model"),
        messages=[{"role": "user", "content": "Fix the typed Python handler"}],
        agent_name="Engineer",
        role_description="Runtime engineer",
        session_context=session,
    )

    inputs = _skill_catalog_ranking_inputs(request)

    assert "Fix the typed Python handler" in inputs["scenario_text"]
    assert "stabilize API boundary" in inputs["scenario_text"]
    assert inputs["active_skill_names"] == ("incident",)
    assert inputs["path_triggered_skill_names"] == ("python",)


@pytest.mark.asyncio
async def test_kernel_get_tools_reinjects_discovered_tool_schemas(monkeypatch):
    """R3 (closure plan §7): deferred tools made callable by tool_search in an
    earlier turn must have their schemas re-injected on a fresh invocation, so a
    discovered capability survives compaction / recovery — not just the in-run
    full_toolset accumulator. _kernel_get_tools merges session.discovered_tools
    into requested_names."""
    from app.runtime.invoker import AgentInvocationRequest, get_agent_kernel
    from app.runtime.session import SessionContext

    captured: dict = {}

    async def fake_tools(agent_id, core_only=False, requested_names=None):
        captured["requested_names"] = requested_names
        return [{"function": {"name": "write_file"}}]

    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_tools)

    kernel = get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
            messages=[{"role": "user", "content": "hi"}],
            agent_name="Researcher",
            role_description="r",
            session_context=SessionContext(source="web", discovered_tools=["web_search"]),
        )
    )
    await kernel._deps.get_tools(uuid4(), True)
    assert captured["requested_names"] is not None
    assert "web_search" in captured["requested_names"]


@pytest.mark.asyncio
async def test_kernel_get_tools_without_discovered_tools_keeps_none_requested(monkeypatch):
    """Regression guard: with no discovered tools and no channel tools, the
    requested_names stays None (full core surface), not an empty-list narrowing."""
    from app.runtime.invoker import AgentInvocationRequest, get_agent_kernel
    from app.runtime.session import SessionContext

    captured: dict = {}

    async def fake_tools(agent_id, core_only=False, requested_names=None):
        captured["requested_names"] = requested_names
        return [{"function": {"name": "write_file"}}]

    monkeypatch.setattr("app.runtime.invoker.get_agent_tools_for_llm", fake_tools)

    kernel = get_agent_kernel(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
            messages=[{"role": "user", "content": "hi"}],
            agent_name="Researcher",
            role_description="r",
            session_context=SessionContext(source="web"),
        )
    )
    await kernel._deps.get_tools(uuid4(), True)
    assert captured["requested_names"] is None
