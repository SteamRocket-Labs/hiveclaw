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


@pytest.mark.asyncio
async def test_stop_hook_blocking_result_forces_continuation() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.runtime.hooks import HookContext, HookEvent, HookResult, hook_registry

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(content="draft", tool_calls=[], reasoning_content=None, usage={"total_tokens": 4}),
            SimpleNamespace(content="verified final", tool_calls=[], reasoning_content=None, usage={"total_tokens": 5}),
        ]
    )
    stop_calls: list[str] = []

    def stop_hook(ctx: HookContext) -> HookResult | None:
        stop_calls.append(ctx.last_assistant_message or "")
        if len(stop_calls) == 1:
            return HookResult(block=True, reason="verify the final answer before stopping")
        return None

    hook_registry.clear()
    hook_registry.register(HookEvent.STOP, stop_hook)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Example Owner",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    try:
        result = await kernel.handle(
            InvocationRequest(
                model=model,
                messages=[{"role": "user", "content": "answer carefully"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
                memory_session_id="session-1",
            )
        )
    finally:
        hook_registry.clear()

    assert result.content == "verified final"
    assert stop_calls == ["draft", "verified final"]
    assert len(fake_client.calls) == 2
    second_round_messages = fake_client.calls[1]["messages"]
    assert any(
        getattr(message, "role", None) == "user" and "verify the final answer" in (message.content or "")
        for message in second_round_messages
    )
