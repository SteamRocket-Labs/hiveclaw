from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _SwitchableClient:
    def __init__(self, responses=None, error: Exception | None = None, delay: float = 0.0) -> None:
        self._responses = list(responses or [])
        self._error = error
        self._delay = delay
        self.calls: list[dict] = []
        self.closed = False

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("No fake response prepared")
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_agent_kernel_retries_once_with_fallback_model():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_utils import LLMError

    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="primary",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )
    fallback_model = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet",
        api_key="fallback",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )

    primary_client = _SwitchableClient(error=LLMError("primary failed"))
    fallback_client = _SwitchableClient(
        responses=[
            SimpleNamespace(
                content="fallback success",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 12},
            )
        ]
    )
    create_calls: list[str] = []

    def create_client(model):
        create_calls.append(model.model)
        if model is primary_model:
            return primary_client
        if model is fallback_model:
            return fallback_client
        raise AssertionError(f"Unexpected model {model}")

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=2),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=create_client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "fallback success"
    assert create_calls == ["gpt-4.1", "claude-sonnet"]
    assert primary_client.closed is True
    assert fallback_client.closed is True


@pytest.mark.asyncio
async def test_agent_kernel_returns_stopped_result_when_cancel_event_fires():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    cancel_event = asyncio.Event()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )
    client = _SwitchableClient(delay=0.2)
    chunk_sink: list[str] = []
    thinking_sink: list[str] = []

    async def delayed_cancel_chunk(text: str) -> None:
        chunk_sink.append(text)

    async def delayed_cancel_thinking(text: str) -> None:
        thinking_sink.append(text)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=2),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    task = asyncio.create_task(
        kernel.handle(
            InvocationRequest(
                model=model,
                messages=[{"role": "user", "content": "stop this"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
                cancel_event=cancel_event,
                on_chunk=delayed_cancel_chunk,
                on_thinking=delayed_cancel_thinking,
            )
        )
    )

    await asyncio.sleep(0.05)
    cancel_event.set()
    result = await task

    assert result.content == "*[Generation stopped]*"
    assert result.parts[-1] == {"type": "text", "text": "*[Generation stopped]*"}
    assert client.closed is True
