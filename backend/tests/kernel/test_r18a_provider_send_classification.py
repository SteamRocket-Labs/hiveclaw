"""R18a Part 2: an effect-free unknown delivery fails plainly.

E2E run 15 round 30: llm.stream hit httpx.ReadTimeout after 133 s
(LLMError delivery_state="unknown"), the run was fenced as
needs_reconciliation, and the member never got an answer because only a
platform admin can close that state. When the durable round ledger settles
the failed request as effect-free (function tools only, no mcp_servers), the
kernel must return the classified provider failure instead of raising.
"""

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
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        return None


def _kernel(client: _FakeClient, model_request_fail):
    from app.kernel import AgentKernel, KernelDependencies, RuntimeConfig

    return AgentKernel(KernelDependencies(
        resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
        resolve_current_user_name=lambda *_a, **_k: "Member",
        build_system_prompt=lambda *_a, **_k: "PROMPT",
        resolve_memory_context=lambda *_a, **_k: "",
        get_tools=lambda *_a, **_k: [],
        maybe_compress_messages=lambda messages, **_k: messages,
        create_client=lambda _m: client,
        execute_tool=lambda *_a, **_k: "",
        persist_memory=lambda **_k: None,
        record_token_usage=lambda *_a, **_k: None,
        get_max_tokens=lambda *_a, **_k: 2048,
        extract_usage_tokens=lambda usage: usage.get("total_tokens"),
        estimate_tokens_from_chars=lambda n: n // 4,
    ))


def _request(model_request_fail) -> "object":
    from app.kernel import InvocationRequest

    return InvocationRequest(
        model=SimpleNamespace(provider="openai", model="local", api_key="local", base_url=None),
        messages=[{"role": "user", "content": "继续处理"}],
        agent_name="Agent", role_description="", agent_id=uuid4(), user_id=uuid4(),
        model_request_prepare=lambda **_k: "provider-request:r18a-1",
        model_request_fail=model_request_fail,
    )


@pytest.mark.asyncio
async def test_ledger_settled_failed_unknown_delivery_returns_provider_failure():
    """The round ledger's effect-free "failed" stops the reconciliation raise."""

    from app.kernel.contracts import TerminalReason
    from app.services.llm_client import LLMError

    failures: list[dict] = []

    async def fail_settled_failed(**payload) -> str:
        failures.append(payload)
        return "failed"

    client = _FakeClient([LLMError("Provider stream delivery is unknown: ReadTimeout: timed out")])
    kernel = _kernel(client, fail_settled_failed)
    result = await kernel.handle(_request(fail_settled_failed))

    assert failures and failures[0]["delivery_state"] == "unknown" and failures[0]["retry_safe"] is False
    assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    assert result.failure_code == "timeout"
    assert result.failure_delivery_state == "unknown"


@pytest.mark.asyncio
async def test_ledger_without_a_decision_still_fences_the_run():
    """No settlement the ledger could make (None) keeps the operator fence."""

    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.services.llm_client import LLMError

    async def fail_unresolved(**_payload):
        return None

    client = _FakeClient([LLMError("Provider stream delivery is unknown: ReadTimeout: timed out")])
    kernel = _kernel(client, fail_unresolved)
    with pytest.raises(ProviderRequestNeedsReconciliation):
        await kernel.handle(_request(fail_unresolved))


@pytest.mark.asyncio
async def test_ledger_settled_needs_reconciliation_still_fences_the_run():
    """A provider-side effect (the ledger says so) keeps the operator fence."""

    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.services.llm_client import LLMError

    async def fail_effectful(**_payload) -> str:
        return "needs_reconciliation"

    client = _FakeClient([LLMError("Provider stream delivery is unknown: ReadTimeout: timed out")])
    kernel = _kernel(client, fail_effectful)
    with pytest.raises(ProviderRequestNeedsReconciliation):
        await kernel.handle(_request(fail_effectful))
