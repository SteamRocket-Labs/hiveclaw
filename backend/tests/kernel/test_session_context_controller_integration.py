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
async def test_kernel_preserves_unrecoverable_tool_result_before_next_model_request() -> None:
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.runtime.session import SessionContext

    model = SimpleNamespace(
        provider="openai",
        model="gpt-test",
        api_key="key",
        base_url=None,
        max_input_tokens=256_000,
        max_output_tokens=None,
    )
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_large",
                        "function": {"name": "run_command", "arguments": '{"cmd":"cat big.log"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
        ]
    )
    events: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=4,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Example Owner",
            build_system_prompt=lambda *_args, **_kwargs: "SYSTEM",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {"name": "run_command", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "X" * 120,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: max(chars // 4, 1),
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "run it"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(
                session_id="s-context-controller",
                source="web",
                channel="web",
                metadata={
                    "context_policy": {
                        "tool_result_inline_limit": 40,
                        "round_tool_result_budget": 80,
                    }
                },
            ),
            on_event=lambda event: events.append(event),
        )
    )

    assert result.content == "done"
    assert len(fake_client.calls) == 2
    second_messages = fake_client.calls[1]["messages"]
    tool_messages = [msg for msg in second_messages if msg.role == "tool"]
    assert tool_messages
    assert tool_messages[0].content == "X" * 120
    assert not any(event.get("event_type") == "tool_result_budget_pass" for event in events)


@pytest.mark.asyncio
async def test_kernel_reports_context_skipped_with_cumulative_usage_anchor() -> None:
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.runtime.session import SessionContext

    model = SimpleNamespace(
        provider="openai",
        model="gpt-test",
        api_key="key",
        base_url=None,
        max_input_tokens=256_000,
        max_output_tokens=None,
    )
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="ok",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
            )
        ]
    )
    events: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=2,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Example Owner",
            build_system_prompt=lambda *_args, **_kwargs: "SYSTEM",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: max(chars // 4, 1),
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "short"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(
                session_id="s-anchor",
                source="web",
                channel="web",
                metadata={"usage_anchor_tokens": 1_200_000},
            ),
            on_event=lambda event: events.append(event),
        )
    )

    assert result.content == "ok"
    skipped = [event for event in events if event.get("event_type") == "compaction_skipped"]
    assert skipped
    assert skipped[0]["reason"] == "below_autocompact_threshold"
    assert skipped[0]["cumulative_run_tokens"] == 1_200_000
