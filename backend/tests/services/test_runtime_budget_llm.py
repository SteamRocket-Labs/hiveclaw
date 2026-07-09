from __future__ import annotations

import uuid

import pytest

from app.services.llm_client import LLMClient, LLMMessage, LLMResponse
from app.services.runtime_budget_llm import (
    RuntimeBudgetedLLMClient,
    estimate_llm_prompt_tokens,
    runtime_budget_usage_amounts,
)
from app.services.runtime_budget_service import RuntimeBudgetDenied


class _FakeLLMClient(LLMClient):
    def __init__(self, *, response: LLMResponse | None = None):
        super().__init__(api_key="k", model="test-model")
        self.response = response or LLMResponse(content="ok", usage={"input_tokens": 100, "output_tokens": 20})
        self.complete_calls = 0
        self.stream_calls = 0

    async def complete(self, messages, tools=None, temperature=0.7, max_tokens=None, **kwargs):
        self.complete_calls += 1
        return self.response

    async def stream(self, messages, tools=None, temperature=0.7, max_tokens=None, on_chunk=None, on_thinking=None, **kwargs):
        self.stream_calls += 1
        return self.response

    def _get_headers(self):
        return {}


class _CapturingBudgetService:
    def __init__(self, *, deny: bool = False):
        self.deny = deny
        self.reservations = []
        self.settlements = []

    async def reserve(self, reservation):
        self.reservations.append(reservation)
        if self.deny:
            raise RuntimeBudgetDenied("budget exhausted", budget_run_id=reservation.budget_run_id)

    async def settle(self, settlement):
        self.settlements.append(settlement)


@pytest.mark.asyncio
async def test_runtime_budgeted_llm_client_reserves_and_settles_provider_usage():
    budget_run_id = uuid.uuid4()
    runtime_task_id = uuid.uuid4()
    service = _CapturingBudgetService()
    inner = _FakeLLMClient(response=LLMResponse(content="done", usage={"input_tokens": 100, "output_tokens": 20}))
    client = RuntimeBudgetedLLMClient(
        inner,
        budget_run_id=budget_run_id,
        runtime_task_id=runtime_task_id,
        service=service,
    )

    response = await client.complete([LLMMessage(role="user", content="hello")])

    assert response.content == "done"
    assert inner.complete_calls == 1
    assert service.reservations[0].budget_run_id == budget_run_id
    assert service.reservations[0].runtime_task_id == runtime_task_id
    assert service.reservations[0].provider_calls == 1
    assert service.reservations[0].tokens >= 50_000
    assert service.settlements[0].actual_tokens == 120
    assert service.settlements[0].actual_cache_miss_tokens == 100
    assert service.settlements[0].actual_provider_calls == 1
    assert service.settlements[0].reason == "provider_call_completed"


@pytest.mark.asyncio
async def test_runtime_budgeted_llm_client_denial_blocks_provider_call():
    service = _CapturingBudgetService(deny=True)
    inner = _FakeLLMClient()
    client = RuntimeBudgetedLLMClient(
        inner,
        budget_run_id=uuid.uuid4(),
        runtime_task_id=uuid.uuid4(),
        service=service,
    )

    with pytest.raises(RuntimeBudgetDenied):
        await client.stream([LLMMessage(role="user", content="hello")])

    assert inner.stream_calls == 0
    assert service.settlements == []


def test_runtime_budget_usage_amounts_treats_missing_cache_metrics_as_cache_miss_risk():
    total, cache_miss, observed = runtime_budget_usage_amounts(
        {"prompt_tokens": 80, "completion_tokens": 10},
        prompt_estimate=50,
    )

    assert total == 90
    assert cache_miss == 80
    assert observed is False


def test_estimate_llm_prompt_tokens_includes_tool_schema_tokens():
    messages = [LLMMessage(role="user", content="hi")]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "Large schema. " * 120,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Query text."},
                    },
                },
            },
        }
    ]

    message_only = estimate_llm_prompt_tokens(messages)
    with_tools = estimate_llm_prompt_tokens(messages, tools=tools)

    assert with_tools > message_only


@pytest.mark.asyncio
async def test_runtime_budgeted_llm_client_reservation_accounts_tool_schema_tokens():
    service = _CapturingBudgetService()
    inner = _FakeLLMClient(response=LLMResponse(content="done", usage={"input_tokens": 20, "output_tokens": 5}))
    client = RuntimeBudgetedLLMClient(
        inner,
        budget_run_id=uuid.uuid4(),
        runtime_task_id=uuid.uuid4(),
        default_llm_call_token_reservation=0,
        service=service,
    )
    messages = [LLMMessage(role="user", content="hi")]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "Large schema. " * 120,
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]

    await client.stream(messages, tools=tools)

    reservation = service.reservations[0]
    metadata = reservation.metadata
    assert metadata["prompt_estimate_tokens"] > estimate_llm_prompt_tokens(messages)
    assert metadata["tool_schema_estimate_tokens"] > 0
    assert metadata["projected_input_tokens"] == metadata["prompt_estimate_tokens"]
    assert reservation.tokens == metadata["projected_input_tokens"]
    assert reservation.cache_miss_tokens == metadata["projected_uncached_input_tokens"]
