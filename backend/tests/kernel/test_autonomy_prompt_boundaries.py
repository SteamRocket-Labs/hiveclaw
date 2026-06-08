from __future__ import annotations

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


def _model() -> SimpleNamespace:
    return SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)


@pytest.mark.asyncio
async def test_tool_round_warnings_preserve_objective_wake_policy_boundary() -> None:
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {"id": "call_1", "function": {"name": "noop_tool", "arguments": "{}"}},
                ],
                reasoning_content=None,
                usage={"total_tokens": 1},
            ),
            SimpleNamespace(
                content="",
                tool_calls=[
                    {"id": "call_2", "function": {"name": "noop_tool", "arguments": "{}"}},
                ],
                reasoning_content=None,
                usage={"total_tokens": 1},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 1},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {"type": "function", "function": {"name": "noop_tool", "description": "", "parameters": {"type": "object"}}}
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "ok",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "run a multi-round task"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(session_id="s-1", source="task"),
            max_tool_rounds=3,
        )
    )

    system_reminders = [
        msg.content
        for call in fake_client.calls
        for msg in call["messages"]
        if msg.role == "system" and isinstance(msg.content, str) and ("tool rounds" in msg.content or "Only 2" in msg.content)
    ]
    joined = "\n".join(system_reminders)

    assert "Objective Ledger is the source of truth" in joined
    assert "Trigger is wake policy" in joined
    assert "focus.md" not in joined
    assert "set_trigger" not in joined


def test_system_prompt_context_threshold_matches_kernel_threshold() -> None:
    """The percentage in the system prompt must match the kernel constants
    so the agent's mental model of when compaction fires stays accurate.

    P1-W2-3: tightened from 0.90 to 0.75; the 60% pressure threshold for
    aggressive microcompact is also surfaced.
    """
    from app.kernel.engine import (
        _MICROCOMPACT_PRESSURE_THRESHOLD,
        _MIDLOOP_COMPACT_THRESHOLD,
    )
    from app.runtime.prompt_sections import build_system_section

    section = build_system_section()

    assert _MIDLOOP_COMPACT_THRESHOLD == 0.75
    assert _MICROCOMPACT_PRESSURE_THRESHOLD == 0.60
    assert "~75%" in section
    assert "~60%" in section
    assert "~90%" not in section


@pytest.mark.asyncio
async def test_runtime_config_execution_mode_reaches_prompt_builder_and_tool_filter() -> None:
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    captured_modes: list[str | None] = []
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 1},
            ),
        ]
    )

    def build_system_prompt(request, *_args, **_kwargs):
        captured_modes.append(request.invocation_scope)
        return f"MODE={request.invocation_scope}"

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                execution_mode="coordinator",
            ),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=build_system_prompt,
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {"type": "function", "function": {"name": "delegate_to_agent", "description": "", "parameters": {}}},
                {"type": "function", "function": {"name": "web_search", "description": "", "parameters": {}}},
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "ok",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "coordinate this"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(session_id="s-3", source="chat"),
        )
    )

    assert captured_modes == ["coordinator"]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "delegate_to_agent"
    assert len(fake_client.calls[0]["tools"]) == 1
    assert "MODE=coordinator" in fake_client.calls[0]["messages"][0].content
