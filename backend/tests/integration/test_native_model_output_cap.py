"""Real Responses parsing and kernel continuation retain capped output and usage."""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.kernel.engine import _continue_after_output_cap
from app.services.llm_client import LLMMessage, create_llm_client
from app.services.runtime_budget_llm import RuntimeBudgetedLLMClient
from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService
from tests.integration import conftest as integration
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(scope="module")
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture(scope="module")
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


@pytest.mark.parametrize("provider,model,mode", [
    ("openai", "gpt-5.5-2026-04-23", "complete"),
    ("openai-response", "gpt-5.4-2026-03-05", "complete"),
    ("openai", "gpt-5.5-2026-04-23", "continue"),
    ("openai", "gpt-5.5-2026-04-23", "already_full"),
])
async def test_native_cap_response_retains_usage_and_recovers_within_the_model_limit(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, provider, model, mode,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    service = RuntimeBudgetService(session_factory=app_user_sessionmaker)
    run = await service.create_run(RuntimeBudgetRunCreate(tenant_id=seed["tenant_id"], root_run_kind="test",
        root_run_key=str(uuid4()), max_tokens=300_000, max_provider_calls=3, fail_mode="fail_closed"))
    requests, chunks, prepared = [], [], []

    async def transport(request):
        payload = json.loads(request.content)
        assert request.url.path.endswith("/responses")
        requests.append(payload)
        if payload["max_output_tokens"] > 128_000:
            return httpx.Response(400, json={"error": "Synthetic native output limit exceeded"})
        initial = len(requests) == 1
        usage = {"input_tokens": 100, "output_tokens": 8192 if initial else 200}
        return httpx.Response(200, json={"model": model,
            "status": "incomplete" if initial else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if initial else None,
            "output": [{"type": "message", "content": [{"type": "output_text",
                "text": "PARTIAL_NATIVE_ANSWER" if initial else "COMPLETE_NATIVE_ANSWER"}]}],
            "usage": usage})

    real = create_llm_client(provider, "synthetic-key", model, base_url="http://127.0.0.1:1/v1")
    raw = real._inner
    raw._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    client = RuntimeBudgetedLLMClient(real, budget_run_id=run.id, runtime_task_id=seed["run_id"],
        default_llm_call_token_reservation=0, service=service)
    messages = [LLMMessage(role="user", content="Keep the entire authorized report and reasoning")]
    reasoning = {"reasoning": {"effort": "high"}}

    async def on_chunk(value):
        chunks.append(value)

    async def prepare(**kwargs):
        prepared.append(kwargs)
        return "synthetic-native-continuation"

    try:
        if mode == "continue":
            first = await client.stream(messages, max_tokens=8192, on_chunk=on_chunk, **reasoning)
            assert first.finish_reason == "length" and first.content == "PARTIAL_NATIVE_ANSWER"
            response, receipts = await _continue_after_output_cap(client=client, response=first,
                stream_messages=messages, cancel_event=None, active_model=SimpleNamespace(provider=provider, model=model),
                on_chunk=on_chunk, on_thinking=None, reasoning_kwargs=reasoning, round_index=1,
                model_request_prepare=prepare, provider=provider, model=model)
            assert response.content == "PARTIAL_NATIVE_ANSWERCOMPLETE_NATIVE_ANSWER"
            assert "".join(chunks) == response.content
            assert len(receipts) == 1 and receipts[0]["wire_request"]["max_tokens"] == 128_000
            assert len(prepared) == 1 and prepared[0]["wire_request"]["max_tokens"] == 128_000
        else:
            response = await client.complete(messages, max_tokens=128_000 if mode == "already_full" else 8192, **reasoning)
            assert response.content == ("PARTIAL_NATIVE_ANSWER" if mode == "already_full" else "COMPLETE_NATIVE_ANSWER")
            assert response.finish_reason == ("length" if mode == "already_full" else "stop")
        assert [request["max_output_tokens"] for request in requests] == (
            [128_000] if mode == "already_full" else [8192, 128_000]
        )
        assert all(request["model"] == model and request["reasoning"] == {"effort": "high"} for request in requests)
        current = await service.get_run(tenant_id=seed["tenant_id"], budget_run_id=run.id)
        events = await service.list_events(tenant_id=seed["tenant_id"], budget_run_id=run.id)
        assert current.used_tokens == (8292 if mode == "already_full" else 8592)
        assert current.used_provider_calls == len(requests) and current.reserved_tokens == 0
        assert len([event for event in events if event.event_type == "settlement"]) == len(requests)
    finally:
        await client.close()
    assert raw._client is None or raw._client.is_closed
