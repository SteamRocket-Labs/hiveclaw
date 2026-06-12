from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services import llm_client
from app.services.llm_client import LLMClient, LLMMessage, LLMResponse


class _FakeClient(LLMClient):
    def __init__(self) -> None:
        super().__init__(api_key="k", model="fake-model")
        self.closed = False

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content="ok", usage={"input_tokens": 3, "output_tokens": 7}, model="fake-model")

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        on_chunk=None,
        on_thinking=None,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(content="ok", usage={"total_tokens": 11}, model="fake-model")

    def _get_headers(self) -> dict[str, str]:
        return {}

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_config_factory_meters_autonomous_complete_and_stream(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    records: list[dict[str, Any]] = []

    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: _FakeClient())

    async def fake_record_autonomous_llm_token_usage(**kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(
        llm_client,
        "record_autonomous_llm_token_usage",
        fake_record_autonomous_llm_token_usage,
        raising=False,
    )

    client = llm_client.create_llm_client_from_config(
        {
            "provider": "openai",
            "api_key": "k",
            "model": "fake-model",
            "_usage_source": "dream",
            "_usage_agent_id": str(agent_id),
            "_usage_tenant_id": str(tenant_id),
            "_usage_metadata": {"phase": "consolidation"},
        }
    )

    await client.complete([LLMMessage(role="user", content="hi")])
    await client.stream([LLMMessage(role="user", content="hi")])
    await client.close()

    assert [item["source"] for item in records] == ["dream", "dream"]
    assert [item["usage"] for item in records] == [
        {"input_tokens": 3, "output_tokens": 7},
        {"total_tokens": 11},
    ]
    assert all(item["agent_id"] == agent_id for item in records)
    assert all(item["tenant_id"] == tenant_id for item in records)
    assert all(item["provider"] == "openai" for item in records)
    assert all(item["model"] == "fake-model" for item in records)
    assert all(item["metadata"] == {"phase": "consolidation"} for item in records)


@pytest.mark.asyncio
async def test_config_factory_does_not_meter_without_usage_context(monkeypatch) -> None:
    records: list[dict[str, Any]] = []

    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: _FakeClient())

    async def fake_record_autonomous_llm_token_usage(**kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(
        llm_client,
        "record_autonomous_llm_token_usage",
        fake_record_autonomous_llm_token_usage,
        raising=False,
    )

    client = llm_client.create_llm_client_from_config(
        {"provider": "openai", "api_key": "k", "model": "fake-model"}
    )

    await client.complete([LLMMessage(role="user", content="hi")])

    assert records == []


@pytest.mark.asyncio
async def test_chat_complete_uses_metered_factory_when_usage_source_is_set(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    records: list[dict[str, Any]] = []

    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: _FakeClient())

    async def fake_record_autonomous_llm_token_usage(**kwargs: Any) -> None:
        records.append(kwargs)

    monkeypatch.setattr(
        llm_client,
        "record_autonomous_llm_token_usage",
        fake_record_autonomous_llm_token_usage,
        raising=False,
    )

    response = await llm_client.chat_complete(
        provider="openai",
        api_key="k",
        model="fake-model",
        messages=[{"role": "user", "content": "hi"}],
        usage_source="subagent_generator",
        usage_agent_id=agent_id,
    )

    assert response["usage"] == {"input_tokens": 3, "output_tokens": 7}
    assert records[0]["source"] == "subagent_generator"
    assert records[0]["agent_id"] == agent_id
