"""Autonomous factory usage survives cap recovery and reaches tenant counters."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.tenant import Tenant
from app.models.token_usage_event import TokenUsageEvent
from app.models.user import User
from app.services.llm_client import (
    LLMError, LLMMessage, create_llm_client_from_config, get_provider_output_ceiling, with_llm_usage_context,
)
from tests.integration import conftest as integration
from tests.integration.test_provider_output_budget import _response
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(scope="module")
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture(scope="module")
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


@pytest.mark.parametrize("outcome", ["complete", "second_unknown", "missing_usage", "stream"])
async def test_autonomous_factory_records_each_reported_generation(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, monkeypatch, outcome,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    other = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    calls, before_retry, chunks = [], [], []

    async def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return _response("PARTIAL_ANSWER", finish="length", output=8192, stream=payload["stream"])
        async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
            before_retry.extend((await db.scalars(select(TokenUsageEvent.tokens))).all())
        if outcome == "second_unknown":
            raise httpx.ReadTimeout("Synthetic second generation acknowledgement lost", request=request)
        response = _response("COMPLETE_ANSWER", output=200)
        if outcome == "missing_usage":
            body = json.loads(response.content)
            body.pop("usage")
            return httpx.Response(200, json=body)
        return response

    client = create_llm_client_from_config(with_llm_usage_context(
        {"provider": "openai", "model": "synthetic-budget-model", "api_key": "synthetic-key",
            "base_url": "http://127.0.0.1:1/v1", "max_input_tokens": 128_000},
        source="skill_candidate_selector", agent_id=seed["agent_id"], tenant_id=seed["tenant_id"],
        user_id=seed["user_id"], metadata={"candidate_count": 3},
    ))
    raw = client
    while hasattr(raw, "_inner"):
        raw = raw._inner
    raw._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))

    async def on_chunk(value):
        chunks.append(value)

    try:
        messages = [LLMMessage(role="user", content="Review the complete authorized candidate pool")]
        if outcome == "second_unknown":
            with pytest.raises(LLMError) as exc:
                await client.complete(messages, max_tokens=8192, reasoning_effort="high")
            assert exc.value.delivery_state == "unknown"
        else:
            kwargs = {"on_chunk": on_chunk} if outcome == "stream" else {}
            response = await getattr(client, "stream" if outcome == "stream" else "complete")(
                messages, max_tokens=8192, reasoning_effort="high", **kwargs,
            )
            assert response.content == ("PARTIAL_ANSWER" if outcome == "stream" else "COMPLETE_ANSWER")
            if outcome == "complete":
                assert response.usage == {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
            elif outcome == "missing_usage":
                assert not response.usage
            else:
                assert "".join(chunks) == "PARTIAL_ANSWER" and response.finish_reason == "length"
    finally:
        await client.close()
    assert raw._client is None or raw._client.is_closed

    assert len(calls) == (1 if outcome == "stream" else 2)
    assert all(call["model"] == "synthetic-budget-model" and call["reasoning_effort"] == "high" for call in calls)
    assert [call.get("max_tokens", call.get("max_completion_tokens")) for call in calls] == (
        [8192] if outcome == "stream" else [8192, get_provider_output_ceiling("openai")]
    )
    if outcome != "stream":
        assert before_retry == [8292]
    expected = [300, 8292] if outcome == "complete" else [8292]
    async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
        events = (await db.scalars(select(TokenUsageEvent))).all()
        assert sorted(event.tokens for event in events) == expected
        assert all(event.source == "skill_candidate_selector" and event.provider == "openai" for event in events)
        assert all(event.agent_id == seed["agent_id"] and event.user_id == seed["user_id"] for event in events)
        assert all(event.tenant_id == seed["tenant_id"] and event.details == {"candidate_count": 3} for event in events)
        for model, key in ((Tenant, "tenant_id"), (User, "user_id"), (Agent, "agent_id")):
            row = await db.get(model, seed[key])
            assert (row.tokens_used_today, row.tokens_used_month, row.tokens_used_total) == (sum(expected),) * 3
    async with tenant_scoped_session(other["tenant_id"], session_factory=app_user_sessionmaker) as db:
        assert (await db.scalars(select(TokenUsageEvent))).all() == []
        for model, key in ((Tenant, "tenant_id"), (User, "user_id"), (Agent, "agent_id")):
            assert (await db.get(model, other[key])).tokens_used_total == 0
