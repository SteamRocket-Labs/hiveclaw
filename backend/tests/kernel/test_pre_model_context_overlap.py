"""Independent pre-model context can load while semantic memory awaits its provider."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.contracts import ContextDependencyUnavailable, InvocationRequest, RuntimeConfig
from app.kernel.engine import AgentKernel, KernelDependencies


@pytest.mark.parametrize("memory_unavailable", [False, True])
async def test_memory_provider_overlaps_optional_context_and_joins_before_prompt(memory_unavailable):
    events = []

    async def memory(*_args):
        events.append("aux_provider:start")
        await asyncio.sleep(0.03)
        events.append("aux_provider:end")
        if memory_unavailable:
            raise ContextDependencyUnavailable(
                dependency="memory", code="fixture_unavailable", user_message="Memory unavailable"
            )
        return "Authorized memory"

    async def metadata(*_args):
        events.append("metadata:start")
        await asyncio.sleep(0.01)
        events.append("metadata:end")
        return "Runtime metadata"

    async def prompt(_request, _tenant, memory_context, _name):
        events.append("prompt")
        assert memory_context == "Authorized memory"
        return "System instructions"

    class Client:
        async def stream(self, **_kwargs):
            events.append("main_provider")
            return SimpleNamespace(content="Done", tool_calls=[], reasoning_content=None, usage={})

        async def close(self):
            pass

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
            resolve_current_user_name=lambda _: "Owner",
            build_system_prompt=prompt,
            resolve_memory_context=memory,
            resolve_runtime_metadata_context=metadata,
            get_tools=lambda *a, **kw: [],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _: Client(),
            execute_tool=lambda *a, **kw: "OK",
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a: 1024,
            extract_usage_tokens=lambda usage: None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )
    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="fake", model="fake", max_output_tokens=1024),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Fixture",
            role_description="Local fake",
        )
    )
    assert events.index("aux_provider:start") < events.index("metadata:start") < events.index("aux_provider:end")
    assert "metadata:end" in events
    if memory_unavailable:
        assert result.content == "Memory unavailable"
        assert "main_provider" not in events and "prompt" not in events
    else:
        assert result.content == "Done"
        assert events.index("aux_provider:end") < events.index("prompt") < events.index("main_provider")
