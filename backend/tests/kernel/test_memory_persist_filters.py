"""T-G1 — M2 pin: runtime reminders never leak into memory persistence.

docs/runtime-guidance-cc-alignment.md §3 M2: ``_build_persisted_memory_messages``
only skips ``api_messages[0]``, so under the old per-round append the stacked
role="system" reminders flowed straight into persist → polluting the T0/T2
distillation input. Transient injection is the root fix: reminders take part in
the per-round LLM request only and never enter ``api_messages``, so the persist
path is clean by construction. These tests pin that behaviour from the persist
sink's point of view — and pin the inverse: real conversation content (user,
assistant, tool results) must keep flowing to persist unfiltered.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import PlanModeState, SessionContext


class _ToolLoopClient:
    def __init__(self, tool_rounds: int, *, distinct_args: bool = False) -> None:
        self._remaining = tool_rounds
        self._distinct_args = distinct_args
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if self._remaining > 0:
            self._remaining -= 1
            return SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{self._remaining}",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": f'{{"path": "x-{self._remaining}"}}' if self._distinct_args else '{"path": "x"}',
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 10},
            )
        return SimpleNamespace(content="final answer", tool_calls=[], reasoning_content=None, usage={"total_tokens": 5})

    async def close(self) -> None:
        return None


def _kernel(client, persist_sink: list):
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    def _persist(**kwargs):
        persist_sink.append(kwargs)

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=6),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            resolve_retrieval_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": "read", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: client,
            execute_tool=lambda *_a, **_k: "file content",
            persist_memory=_persist,
            record_token_usage=lambda *a, **k: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )


def _model():
    return SimpleNamespace(
        provider="openai",
        model="gpt-test",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
        supports_vision=False,
    )


def _persisted_texts(persist_sink: list) -> list[str]:
    texts: list[str] = []
    for call in persist_sink:
        for message in call["messages"]:
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if isinstance(content, str):
                texts.append(content)
    return texts


@pytest.mark.asyncio
async def test_persisted_messages_contain_no_plan_reminder_text():
    from app.kernel.contracts import InvocationRequest

    persist_sink: list = []
    client = _ToolLoopClient(tool_rounds=3)
    kernel = _kernel(client, persist_sink)
    sc = SessionContext()
    sc.plan_mode = PlanModeState(active=True)

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "plan the rollout"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=sc,
        )
    )

    assert persist_sink, "persist_memory was never called on the normal completion path"
    texts = _persisted_texts(persist_sink)
    assert not any("Plan Mode is active" in t for t in texts), "FULL plan reminder leaked into persist"
    assert not any("Plan Mode is still active" in t for t in texts), "SPARSE plan reminder leaked into persist"


@pytest.mark.asyncio
async def test_persisted_messages_contain_no_ledger_or_pressure_reminder_text():
    from app.kernel.contracts import InvocationRequest

    persist_sink: list = []
    # 14 tool rounds: enough for the ledger reminder (idle 10) AND the
    # round-pressure thresholds of max_tool_rounds=15 (80% → 12, final-2 → 13).
    client = _ToolLoopClient(tool_rounds=14, distinct_args=True)

    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    def _persist(**kwargs):
        persist_sink.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=15),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            resolve_retrieval_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": "read", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: client,
            execute_tool=lambda *_a, **_k: "file content",
            persist_memory=_persist,
            record_token_usage=lambda *a, **k: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )
    sc = SessionContext()
    sc.metadata = {"work_ledger_enabled": True}

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "do the long task"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=sc,
        )
    )

    assert persist_sink
    texts = _persisted_texts(persist_sink)
    assert not any("gentle reminder" in t for t in texts), "ledger reminder leaked into persist"
    assert not any("Current Work Ledger snapshot" in t for t in texts), "ledger snapshot leaked into persist"
    assert not any("tool rounds used" in t for t in texts), "round-pressure warning leaked into persist"


@pytest.mark.asyncio
async def test_persisted_messages_keep_real_conversation():
    """The inverse pin: filtering must not eat genuine conversation —
    user message, tool results, and the final assistant answer all persist."""
    from app.kernel.contracts import InvocationRequest

    persist_sink: list = []
    client = _ToolLoopClient(tool_rounds=2)
    kernel = _kernel(client, persist_sink)

    await kernel.handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "summarize the quarterly file"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(),
        )
    )

    assert persist_sink
    texts = _persisted_texts(persist_sink)
    assert any("summarize the quarterly file" in t for t in texts), "user message missing from persist"
    assert any("file content" in t for t in texts), "tool result missing from persist"
    assert any("final answer" in t for t in texts), "final assistant content missing from persist"
