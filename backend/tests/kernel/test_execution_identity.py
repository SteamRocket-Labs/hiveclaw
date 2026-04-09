from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def stream(self, **kwargs):
        if not self._responses:
            raise AssertionError("No fake response prepared")
        return self._responses.pop(0)

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_agent_kernel_sets_execution_identity_for_tool_resolution():
    from app.core.execution_context import clear_execution_identity
    from app.kernel.contracts import ExecutionIdentityRef, InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    captured = {}

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=2, quota_message=None)

    async def resolve_current_user_name(_user_id):
        return "Rocky"

    async def build_system_prompt(*args, **kwargs):
        return "PROMPT"

    async def resolve_memory_context(*args, **kwargs):
        return ""

    async def get_tools(_agent_id, _core_only):
        return [{"type": "function", "function": {"name": "write_file", "description": "", "parameters": {"type": "object"}}}]

    async def maybe_compress_messages(messages, **kwargs):
        return messages

    async def execute_tool(_tool_name, _args, _request, _emit_event):
        from app.core.execution_context import get_execution_identity

        captured["identity"] = get_execution_identity()
        return "BLOCKED"

    async def persist_memory(**kwargs):
        return None

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=resolve_current_user_name,
            build_system_prompt=build_system_prompt,
            resolve_memory_context=resolve_memory_context,
            get_tools=get_tools,
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda model: _FakeClient([
                SimpleNamespace(
                    content="",
                    tool_calls=[{
                        "id": "call_1",
                        "function": {"name": "write_file", "arguments": '{"path":"focus.md","content":"x"}'},
                    }],
                    reasoning_content=None,
                    usage={"total_tokens": 2},
                ),
                SimpleNamespace(
                    content="done",
                    tool_calls=[],
                    reasoning_content=None,
                    usage={"total_tokens": 1},
                ),
            ]),
            execute_tool=execute_tool,
            persist_memory=persist_memory,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    clear_execution_identity()
    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            execution_identity=ExecutionIdentityRef(
                identity_type="delegated_user",
                identity_id=uuid4(),
                label="Rocky via web",
            ),
        )
    )

    assert result.content == "done"
    assert captured["identity"] is not None
    assert captured["identity"].identity_type == "delegated_user"
    assert captured["identity"].label == "Rocky via web"


@pytest.mark.asyncio
async def test_runtime_invoker_captures_current_execution_identity(monkeypatch):
    from app.core.execution_context import ExecutionIdentity, set_execution_identity
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())

    identity_id = uuid4()
    set_execution_identity(ExecutionIdentity("delegated_user", identity_id, "Rocky via web"))

    result = await invoke_agent(
        AgentInvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "ok"
    assert captured["request"].execution_identity is not None
    assert captured["request"].execution_identity.identity_type == "delegated_user"
    assert captured["request"].execution_identity.identity_id == identity_id
    assert captured["request"].execution_identity.label == "Rocky via web"
