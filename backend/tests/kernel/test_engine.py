from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


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


def test_build_restoration_context_prefers_newest_recent_files(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    files = []
    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        path = tmp_path / name
        path.write_text(f"content for {name}", encoding="utf-8")
        files.append(str(path))

    session = SessionContext()
    session.recent_files = files

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    e_idx = restored.index("### Recent File: e.txt")
    d_idx = restored.index("### Recent File: d.txt")
    c_idx = restored.index("### Recent File: c.txt")
    assert e_idx < d_idx < c_idx


def test_build_persisted_memory_messages_includes_runtime_events():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _build_persisted_memory_messages
    from app.runtime.session import SessionContext

    session = SessionContext()
    session.track_tool_outcome("web_search", "Found deployment guide and rollback notes")
    session.track_file_write("workspace/deploy-plan.md")
    session.track_external_ref("https://docs.example.com/deploy")
    session.track_pending_item("Verify rollback checklist before production deploy")

    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "继续部署任务"}],
        agent_name="Ops Agent",
        role_description="deployment helper",
        session_context=session,
    )

    persisted = _build_persisted_memory_messages(request, "部署步骤已整理")
    contents = [msg.get("content", "") for msg in persisted if isinstance(msg.get("content"), str)]

    assert any("Runtime event: tool outcome web_search" in content for content in contents)
    assert any("Runtime event: wrote file workspace/deploy-plan.md" in content for content in contents)
    assert any("Runtime event: external reference https://docs.example.com/deploy" in content for content in contents)
    assert any("Runtime event: pending work Verify rollback checklist before production deploy" in content for content in contents)


@pytest.mark.asyncio
async def test_agent_kernel_handles_tool_round_and_collects_parts():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

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
    persisted_payloads: list[dict] = []

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message=None)

    async def resolve_current_user_name(_user_id):
        return "Rocky"

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        assert tenant_id is not None
        assert current_user_name == "Rocky"
        assert memory_context == ""
        return f"PROMPT::{request.agent_name}"

    async def resolve_memory_context(request, tenant_id):
        assert tenant_id is not None
        return ""

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def maybe_compress_messages(messages, **kwargs):
        return messages

    async def execute_tool(tool_name, args, request, emit_event):
        del request, emit_event
        assert tool_name == "read_file"
        assert args == {"path": "skills/web-research/SKILL.md"}
        return "SKILL_CONTENT"

    async def persist_memory(**kwargs):
        persisted_payloads.append(kwargs)

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
            reasoning_content="final reasoning",
            usage={"total_tokens": 8},
        ),
    ])

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=resolve_current_user_name,
            build_system_prompt=build_system_prompt,
            resolve_memory_context=resolve_memory_context,
            get_tools=get_tools,
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda model: fake_client,
            execute_tool=execute_tool,
            persist_memory=persist_memory,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "帮我做调研"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "final answer"
    assert tool_load_calls == [True, False]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "expanded_tool"
    assert result.parts == [
        {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "skills/web-research/SKILL.md"},
            "status": "done",
            "result": "SKILL_CONTENT",
            "reasoning": "reasoning",
        },
        {"type": "reasoning", "text": "final reasoning"},
        {"type": "text", "text": "final answer"},
    ]
    assert persisted_payloads


@pytest.mark.asyncio
async def test_runtime_invoker_delegates_to_agent_kernel(monkeypatch):
    from app.kernel.contracts import InvocationResult
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return InvocationResult(
                content="kernel-result",
                tokens_used=7,
                final_tools=[{"type": "function", "function": {"name": "x"}}],
                parts=[{"type": "text", "text": "kernel-result"}],
            )

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert captured["request"].agent_name == "Agent"
    assert captured["request"].messages == [{"role": "user", "content": "hello"}]
    assert result.content == "kernel-result"
    assert result.tokens_used == 7
    assert result.final_tools == [{"type": "function", "function": {"name": "x"}}]
    assert result.parts == [{"type": "text", "text": "kernel-result"}]


@pytest.mark.asyncio
async def test_agent_kernel_expands_tools_after_load_skill():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    tool_load_calls: list[bool] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=4, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "load_skill", "arguments": '{"name":"web research"}'},
            }],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
    ])

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "SKILL",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True, False]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "expanded_tool"


@pytest.mark.asyncio
async def test_agent_kernel_prefers_declared_tool_subset_after_load_skill():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig, ToolExpansionResult

    tool_load_calls: list[bool] = []
    expansion_calls: list[tuple[str, dict[str, str]]] = []
    emitted_events: list[dict] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=4, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def resolve_tool_expansion(_request, tool_name, args):
        expansion_calls.append((tool_name, args))
        return ToolExpansionResult(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            active_packs=[{
                "name": "web_pack",
                "summary": "网页搜索与抓取能力",
                "tools": ["web_search"],
                "source": "system",
            }],
            event_payload={
                "type": "pack_activation",
                "packs": [{
                    "name": "web_pack",
                    "summary": "网页搜索与抓取能力",
                    "tools": ["web_search"],
                    "source": "system",
                }],
                "message": "Activated web_pack",
                "status": "info",
            },
        )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "load_skill", "arguments": '{"name":"web research"}'},
            }],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
    ])

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            resolve_tool_expansion=resolve_tool_expansion,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "SKILL",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=lambda event: emitted_events.append(event),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert expansion_calls == [("load_skill", {"name": "web research"})]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "web_search"
    assert emitted_events[0]["type"] == "pack_activation"
    assert emitted_events[0]["packs"][0]["name"] == "web_pack"


@pytest.mark.asyncio
async def test_agent_kernel_does_not_auto_expand_without_skill_or_mcp_trigger():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    tool_load_calls: list[bool] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_1",
                "function": {"name": "list_files", "arguments": '{"path":"skills"}'},
            }],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_2",
                "function": {"name": "list_files", "arguments": '{"path":"workspace"}'},
            }],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
        SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "call_3",
                "function": {"name": "list_files", "arguments": '{"path":"memory"}'},
            }],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 2},
        ),
    ])

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "OK",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "keep using the default toolkit"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[2]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[3]["tools"][0]["function"]["name"] == "core_tool"


@pytest.mark.asyncio
async def test_midloop_compaction_triggers_after_interval():
    """Mid-loop compaction fires every _MIDLOOP_COMPACT_CHECK_INTERVAL rounds
    and compresses when maybe_compress_messages returns fewer messages."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    compress_calls: list[int] = []
    compaction_events: list[dict] = []

    async def maybe_compress_messages(messages, **kwargs):
        compress_calls.append(len(messages))
        # Simulate compression: if >4 messages, summarise the old ones into 1
        if len(messages) > 4:
            on_compaction = kwargs.get("on_compaction")
            if on_compaction:
                result = on_compaction({
                    "summary": "compressed",
                    "original_message_count": len(messages),
                    "kept_message_count": 3,
                })
                if result is not None:
                    await result
            summary = {"role": "system", "content": "[Summary of previous conversation]"}
            return [summary] + messages[-2:]
        return messages

    # 4 tool-call rounds then a final text response
    responses = []
    for i in range(4):
        responses.append(SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"call_{i}",
                "function": {"name": "list_files", "arguments": f'{{"path":"dir{i}"}}'},
            }],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ))
    responses.append(SimpleNamespace(
        content="all done",
        tool_calls=[],
        reasoning_content=None,
        usage={"total_tokens": 3},
    ))

    fake_client = _FakeClient(responses)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(), max_tool_rounds=10, quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}}}
            ],
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_a, **_kw: "files: a.txt, b.txt",
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "list everything"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=lambda ev: compaction_events.append(ev) if ev.get("type") == "session_compact" else None,
        )
    )

    assert result.content == "all done"

    # Pre-loop compression is call 1 (1 user message → no compression needed)
    # Mid-loop compression fires at round 3 (interval=3)
    # At that point api_messages[1:] has: user + 3×(assistant+tool) = 7 messages → compressed
    assert len(compress_calls) >= 2, f"Expected at least 2 compress calls, got {compress_calls}"

    # The mid-loop call should have received >4 messages and compressed
    midloop_call_msg_count = compress_calls[1]
    assert midloop_call_msg_count > 4, f"Mid-loop should see >4 messages, got {midloop_call_msg_count}"

    # Compaction event should have been emitted
    assert len(compaction_events) >= 1, "Expected at least one session_compact event"
    assert compaction_events[0]["type"] == "session_compact"


def test_maybe_evict_tool_result_truncates_large_output():
    from app.kernel.engine import _maybe_evict_tool_result

    # Small result — returned unchanged
    small = "hello world"
    assert _maybe_evict_tool_result("run_code", "call_1", small) == small

    # Exempt tool — never evicted even if large
    large = "x" * 60000
    assert _maybe_evict_tool_result("read_file", "call_2", large) == large
    assert _maybe_evict_tool_result("list_files", "call_3", large) == large
    assert _maybe_evict_tool_result("web_search", "call_exempt", large) == large

    # Non-exempt large result — truncated (no eviction_dir)
    evicted = _maybe_evict_tool_result("run_code", "call_4", large)
    assert len(evicted) < len(large)
    assert "truncated" in evicted
    assert "60000 chars" in evicted
    assert "call_4" in evicted
    # Preview should be present (first 2000 chars)
    assert evicted.startswith("x" * 2000)


def test_maybe_evict_writes_file_when_eviction_dir_provided(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    large = "RESULT_DATA\n" * 5000  # ~60000 chars
    eviction_dir = tmp_path / "tool_results"

    evicted = _maybe_evict_tool_result("run_code", "call_99", large, eviction_dir=eviction_dir)

    # File should exist with full content
    written_file = eviction_dir / "call_99.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == large

    # Inline content should have file reference
    assert "workspace/tool_results/call_99.txt" in evicted
    assert "read_file" in evicted
    assert len(evicted) < len(large)


@pytest.mark.asyncio
async def test_large_tool_result_evicted_in_kernel_loop():
    """Kernel evicts large tool results inline during the LLM loop."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None,
    )

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{"id": "c1", "function": {"name": "run_code", "arguments": '{"code":"test"}'}}],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 3},
        ),
    ])

    large_result = "RESULT_LINE\n" * 5000  # ~60000 chars (exceeds 50K eviction threshold)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(), max_tool_rounds=5, quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "run_code", "description": "", "parameters": {"type": "object"}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: large_result,
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "search"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"

    # The second LLM call should have received the evicted (truncated) tool result
    second_call_messages = fake_client.calls[1]["messages"]
    tool_msg = [m for m in second_call_messages if m.role == "tool"][0]
    assert len(tool_msg.content) < len(large_result)
    assert "truncated" in tool_msg.content


@pytest.mark.asyncio
async def test_persist_memory_called_on_max_rounds_exceeded():
    """persist_memory must be called even when max tool rounds exhausted."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None,
    )
    persist_calls: list[dict] = []

    # 3 rounds of tool calls, max_tool_rounds=2 → will exceed
    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[{"id": f"c{i}", "function": {"name": "list_files", "arguments": '{}'}}],
            reasoning_content=None,
            usage={"total_tokens": 3},
        )
        for i in range(3)
    ])

    async def persist_memory(**kwargs):
        persist_calls.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(), max_tool_rounds=2, quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: "ok",
            persist_memory=persist_memory,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "keep going"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-max",
        )
    )

    assert "Too many tool call rounds" in result.content
    assert len(persist_calls) == 1, f"Expected 1 persist call, got {len(persist_calls)}"
    assert persist_calls[0]["session_id"] == "sess-max"


@pytest.mark.asyncio
async def test_persist_memory_called_on_llm_error():
    """persist_memory must be called when LLM returns an error (after fallback exhausted)."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_utils import LLMError

    model = SimpleNamespace(
        provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None,
    )
    persist_calls: list[dict] = []

    class _ErrorClient:
        async def stream(self, **kwargs):
            raise LLMError("rate limit exceeded")
        async def close(self):
            pass

    async def persist_memory(**kwargs):
        persist_calls.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(), max_tool_rounds=5, quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: _ErrorClient(),
            execute_tool=lambda *_a, **_kw: "ok",
            persist_memory=persist_memory,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-err",
        )
    )

    assert "LLM Error" in result.content
    assert len(persist_calls) == 1
    assert persist_calls[0]["session_id"] == "sess-err"


class TestPromptTooLongDetection:
    """_is_prompt_too_long detects PTL errors from various providers."""

    def test_openai_context_length_exceeded(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("HTTP 400: context_length_exceeded - This model's maximum context length is 128000 tokens")
        assert _is_prompt_too_long(exc) is True

    def test_anthropic_too_long(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("Anthropic stream error (invalid_request_error): prompt is too long: 210000 tokens > 200000 maximum")
        assert _is_prompt_too_long(exc) is True

    def test_gemini_request_too_large(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("HTTP 400: request too large for model")
        assert _is_prompt_too_long(exc) is True

    def test_input_too_long(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("input too long for this model")
        assert _is_prompt_too_long(exc) is True

    def test_unrelated_error_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("HTTP 500: Internal server error")
        assert _is_prompt_too_long(exc) is False

    def test_rate_limit_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("Rate limited after 3 attempts: 429 Too Many Requests")
        assert _is_prompt_too_long(exc) is False

    def test_connection_error_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long
        exc = Exception("Connection failed after 3 attempts: timeout")
        assert _is_prompt_too_long(exc) is False
