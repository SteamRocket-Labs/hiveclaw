from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_agent_kernel_uses_model_temperature_override_when_present():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    fake_client = _FakeClient(
        SimpleNamespace(content="ok", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3})
    )
    model = SimpleNamespace(
        provider="openai",
        model="gpt-5.4",
        api_key="secret",
        base_url="https://api.openai.com/v1",
        max_output_tokens=1024,
        max_input_tokens=128000,
        temperature=1.2,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3)

    async def resolve_current_user_name(_user_id):
        return "Rocky"

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        assert request.agent_name == "Temp Agent"
        assert tenant_id is not None
        assert memory_context == ""
        assert current_user_name == "Rocky"
        return "SYSTEM"

    async def resolve_memory_context(request, tenant_id):
        assert request.agent_name == "Temp Agent"
        assert tenant_id is not None
        return ""

    async def get_tools(_agent_id, _core_only):
        return []

    async def maybe_compress_messages(messages, **kwargs):
        return messages

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=resolve_current_user_name,
            build_system_prompt=build_system_prompt,
            resolve_memory_context=resolve_memory_context,
            get_tools=get_tools,
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *args, **kwargs: None,
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Temp Agent",
            role_description="temperature test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "ok"
    assert fake_client.calls
    assert fake_client.calls[0]["temperature"] == 1.2
