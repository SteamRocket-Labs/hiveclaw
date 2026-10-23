"""Native quota gates cover full output and each cap-escalation generation."""

import json
from uuid import uuid4

import httpx
import pytest

from app.services.llm_client import LLMError, LLMMessage, create_llm_client, get_provider_output_ceiling
from app.services.runtime_budget_llm import RuntimeBudgetedLLMClient
from app.services.runtime_budget_service import RuntimeBudgetDenied, RuntimeBudgetRunCreate, RuntimeBudgetService
from tests.integration import conftest as integration
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(scope="module")
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture(scope="module")
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


def _response(content, *, finish="stop", output=20, stream=False):
    usage = {"prompt_tokens": 100, "completion_tokens": output, "total_tokens": 100 + output}
    if stream:
        chunks = [{"choices": [{"delta": {"content": content}}]},
            {"choices": [{"delta": {}, "finish_reason": finish}], "usage": usage}]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage, "model": "synthetic-budget-model"})


@pytest.mark.parametrize("method", ["complete", "stream"])
async def test_full_selected_output_is_reserved_before_the_real_transport(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, method,
):
    from app.runtime.provider_prompt_ledger import build_provider_prompt_ledger

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    messages = [LLMMessage(role="user", content="Keep the selected model, reasoning and complete source")]
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
    input_tokens = build_provider_prompt_ledger(messages=messages, tools=tools)["projected_input_tokens"]
    selected_output, calls = 128_000, []

    async def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return _response("COMPLETE_NATIVE_ANSWER", stream=payload["stream"])

    real = create_llm_client("openai", "synthetic-key", "synthetic-budget-model", base_url="http://127.0.0.1:1/v1")
    raw = real._inner
    raw._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    try:
        for fits in (False, True):
            run = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="test",
                root_run_key=str(uuid4()), max_tokens=input_tokens + selected_output - (0 if fits else 1),
                max_provider_calls=2, fail_mode="fail_closed"))
            client = RuntimeBudgetedLLMClient(real, budget_run_id=run.id, runtime_task_id=seed["run_id"],
                default_llm_call_token_reservation=0, service=service)
            if not fits:
                with pytest.raises(RuntimeBudgetDenied):
                    await getattr(client, method)(messages, tools=tools, max_tokens=selected_output, reasoning_effort="high")
                assert calls == []
                continue
            result = await getattr(client, method)(messages, tools=tools, max_tokens=selected_output, reasoning_effort="high")
            assert result.content == "COMPLETE_NATIVE_ANSWER"
            assert len(calls) == 1 and calls[0]["model"] == "synthetic-budget-model"
            assert calls[0].get("max_tokens", calls[0].get("max_completion_tokens")) == selected_output
            assert calls[0]["tools"] == tools and calls[0]["reasoning_effort"] == "high"
            events = await service.list_events(tenant_id=seed["tenant_id"], budget_run_id=run.id)
            reservation = next(event for event in events if event.event_type == "reservation")
            assert reservation.amounts_json["tokens"] == input_tokens + selected_output
            assert reservation.metadata_json["reserved_output_tokens"] == selected_output
    finally:
        await real.close()
    assert raw._client is None or raw._client.is_closed


@pytest.mark.parametrize("outcome", ["complete", "token_denied", "calls_denied", "second_unknown"])
async def test_cap_escalation_reserves_and_settles_each_actual_generation(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, outcome,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="test",
        root_run_key=str(uuid4()), max_tokens=20_000 if outcome == "token_denied" else 200_000,
        max_provider_calls=1 if outcome == "calls_denied" else 3, fail_mode="fail_closed"))
    calls = []

    async def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return _response("PARTIAL_NATIVE_ANSWER", finish="length", output=8192)
        if outcome == "second_unknown":
            raise httpx.ReadTimeout("Synthetic second generation acknowledgement lost", request=request)
        return _response("COMPLETE_NATIVE_ANSWER", output=200)

    real = create_llm_client("openai", "synthetic-key", "synthetic-budget-model", base_url="http://127.0.0.1:1/v1")
    raw = real._inner
    raw._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = RuntimeBudgetedLLMClient(real, budget_run_id=run.id, runtime_task_id=seed["run_id"],
        default_llm_call_token_reservation=0, service=service)
    try:
        if outcome in {"token_denied", "calls_denied"}:
            with pytest.raises(RuntimeBudgetDenied):
                await client.complete([LLMMessage(role="user", content="Continue at the full native output ceiling")], max_tokens=8192)
            assert len(calls) == 1
        elif outcome == "second_unknown":
            with pytest.raises(LLMError) as exc:
                await client.complete([LLMMessage(role="user", content="Continue at the full native output ceiling")], max_tokens=8192)
            assert exc.value.delivery_state == "unknown" and len(calls) == 2
        else:
            response = await client.complete([LLMMessage(role="user", content="Continue at the full native output ceiling")], max_tokens=8192)
            assert response.content == "COMPLETE_NATIVE_ANSWER" and len(calls) == 2
        caps = [call.get("max_tokens", call.get("max_completion_tokens")) for call in calls]
        assert caps == ([8192] if len(calls) == 1 else [8192, get_provider_output_ceiling("openai")])
        current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=run.id)
        events = await service.list_events(tenant_id=seed["tenant_id"], budget_run_id=run.id)
        reservations = [event for event in events if event.event_type == "reservation"]
        settlements = [event for event in events if event.event_type == "settlement"]
        assert len(reservations) == len(calls)
        assert len({event.reservation_key for event in reservations}) == len(calls)
        assert current.used_tokens == (8592 if outcome == "complete" else 8292)
        assert current.used_provider_calls == (2 if outcome == "complete" else 1)
        if outcome == "second_unknown":
            assert len(settlements) == 1 and current.reserved_tokens >= get_provider_output_ceiling("openai")
            assert current.reserved_provider_calls == 1
        else:
            assert len(settlements) == len(calls) and current.reserved_tokens == 0
            assert current.reserved_provider_calls == 0
    finally:
        await client.close()
    assert raw._client is None or raw._client.is_closed


@pytest.mark.parametrize("method", ["complete", "stream"])
async def test_rate_limit_rejection_retries_share_one_generation_reservation(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, method,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="test",
        root_run_key=str(uuid4()), max_tokens=200_000, max_provider_calls=2, fail_mode="fail_closed"))
    calls = []

    async def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "Synthetic rate-limit rejection"}, headers={"retry-after": "0"})
        return _response("ONE_ACCEPTED_GENERATION", stream=payload["stream"])

    real = create_llm_client("openai", "synthetic-key", "synthetic-budget-model", base_url="http://127.0.0.1:1/v1")
    raw = real._inner
    raw._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = RuntimeBudgetedLLMClient(real, budget_run_id=run.id, runtime_task_id=seed["run_id"],
        default_llm_call_token_reservation=0, service=service)
    try:
        result = await getattr(client, method)([LLMMessage(role="user", content="Keep the native retry policy")], max_tokens=128_000)
        assert result.content == "ONE_ACCEPTED_GENERATION" and len(calls) == 2
        assert calls[0] == calls[1]
        current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=run.id)
        events = await service.list_events(tenant_id=seed["tenant_id"], budget_run_id=run.id)
        assert len([event for event in events if event.event_type == "reservation"]) == 1
        assert len([event for event in events if event.event_type == "settlement"]) == 1
        assert current.used_tokens == 120 and current.used_provider_calls == 1
        assert current.reserved_tokens == 0 and current.reserved_provider_calls == 0
    finally:
        await client.close()
