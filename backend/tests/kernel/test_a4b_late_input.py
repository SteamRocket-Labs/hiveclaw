"""The sealed continuation belongs to one provider round, within its budget."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.kernel.test_engine import _FakeClient


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "max_rounds", "expected_calls"), [
    ("current", 2, 2), ("stale", 2, 1), ("last", 1, 1), ("delegation_no_late", 2, 1),
    ("both_missing", 2, 1), ("both_empty", 2, 1),
])
async def test_sealed_late_input_keeps_the_real_answer_and_lane_behavior(case, max_rounds, expected_calls):
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.runtime.session import SessionContext

    client = _FakeClient([SimpleNamespace(content=content, tool_calls=[], reasoning_content=None,
        usage={"total_tokens": 7}) for content in ["Real first answer.", "Late input included."]])
    bound = []

    async def bind(index):
        bound.append(index)
        return [{"role": "user", "content": "Late note."}] if index == 2 else []

    async def prepare(**kwargs):
        if case in {"both_missing", "both_empty"}:
            return None if case == "both_missing" else ""
        return f"provider:{kwargs['round_index']}"

    async def commit(**kwargs):
        return {"provider_request_id": "provider:old" if case == "stale" else kwargs["provider_request_id"],
            "continuation": {"verdict": "continue" if kwargs["round_index"] == 1 and case != "delegation_no_late" else "complete"}}

    kernel = AgentKernel(KernelDependencies(
        resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=max_rounds),
        resolve_current_user_name=lambda *_a, **_k: "Owner", build_system_prompt=lambda *_a, **_k: "PROMPT",
        resolve_memory_context=lambda *_a, **_k: "", get_tools=lambda *_a, **_k: [],
        maybe_compress_messages=lambda messages, **_k: messages, create_client=lambda _m: client,
        execute_tool=lambda *_a, **_k: "unused", persist_memory=lambda **_k: None,
        record_token_usage=lambda *_a, **_k: None, get_max_tokens=lambda *_a, **_k: 2048,
        extract_usage_tokens=lambda usage: usage.get("total_tokens"), estimate_tokens_from_chars=lambda chars: chars // 4))
    result = await kernel.handle(InvocationRequest(
        model=SimpleNamespace(provider="openai", model="fake", api_key="synthetic", base_url=None),
        messages=[{"role": "user", "content": "Finish the work."}], agent_name="Engineer", role_description="Engineer",
        agent_id=uuid4(), user_id=uuid4(), session_context=SessionContext(source="delegation"),
        round_input_bind=bind, model_request_prepare=prepare, model_response_commit=commit))
    assert len(client.calls) == expected_calls and bound == list(range(1, expected_calls + 1))
    assert result.content == ("Late input included." if expected_calls == 2 else "Real first answer.")
    assert "round limit" not in result.content.lower()
