"""Real clients, kernel empty finalization, PostgreSQL sealing, and repeat entry."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tests.integration import test_r23a_stop_ends_blocked_model_call as stops
from tests.services.test_r23e_provider_output import (
    CLIENTS,
    EFFECT_ITEMS,
    SECONDARY_COMPATIBLE_EFFECT,
    success_body,
    transport_response,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = stops.t2_work
MCP_SERVERS = [{"name": "synthetic-effect", "url": "http://127.0.0.1:1/mcp"}]
PROVIDERS = {"responses": "openai-response", "gemini": "gemini", "anthropic": "anthropic", "openai": "openai"}

CASES = []
for protocol in ("responses", "anthropic", "gemini"):
    CASES.extend([
        pytest.param(protocol, success_body(protocol, EFFECT_ITEMS[protocol][:1]), True, False, id=f"{protocol}_effect"),
        pytest.param(protocol, success_body(protocol), False, False, id=f"{protocol}_empty"),
        pytest.param(
            protocol, success_body(protocol, EFFECT_ITEMS[protocol][:1], text=True), True, True,
            id=f"{protocol}_effect_with_text",
        ),
    ])
CASES.extend([
    pytest.param("openai", success_body("openai"), False, False, id="compatible_empty"),
    pytest.param("openai", SECONDARY_COMPATIBLE_EFFECT, True, False, id="compatible_secondary_effect"),
    pytest.param("responses", success_body("responses", [{"type": "mcp_list_tools"}]), False, False, id="discovery"),
    pytest.param("gemini", success_body("gemini", [{"thoughtSignature": "sig"}]), False, False, id="thought_signature"),
    pytest.param("gemini", success_body("gemini", EFFECT_ITEMS["gemini"][1:]), True, False, id="code_result"),
    pytest.param(
        "gemini", success_body("gemini", candidate_metadata={"groundingMetadata": {}}), True, False, id="grounding",
    ),
    pytest.param(
        "gemini", success_body("gemini", candidate_metadata={"urlContextMetadata": {}}), True, False, id="url_context",
    ),
])


@pytest.mark.parametrize("protocol,body,provider_output_seen,has_output", CASES)
@pytest.mark.parametrize("effectful_wire", [True, False], ids=["effectful_wire", "function_tools_only"])
async def test_successful_empty_response_seals_without_replaying_provider_execution(
    t2_work, owner_sessionmaker, monkeypatch, protocol, body, provider_output_seen, has_output, effectful_wire,
):
    import app.runtime.invoker as invoker
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.runtime_task_claim_service import build_runtime_task_claim_candidate_statement
    from app.services.session_model_round import (
        ModelRoundNeedsReconciliation,
        prepare_model_request,
        provider_request_has_effect,
    )

    seed = t2_work
    requests, clients, responses = [], [], []
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        primary = await db.get(LLMModel, agent.primary_model_id)
        primary.provider, primary.model = PROVIDERS[protocol], "synthetic-primary"
        primary.provider_options = {"mcp_servers": MCP_SERVERS} if effectful_wire else {}
        fallback = LLMModel(
            id=uuid4(), tenant_id=seed["tenant_id"], provider=primary.provider, model="synthetic-fallback",
            label="Synthetic fallback", enabled=True, api_key_encrypted=primary.api_key_encrypted,
            max_input_tokens=256_000, provider_options=primary.provider_options,
        )
        db.add(fallback)
        await db.flush()
        agent.fallback_model_id = fallback.id
        await db.commit()

    def respond(request):
        requests.append(json.loads(request.content))
        # A replay would return usable text, not fail because the fake ran out.
        payload = body if len(requests) == 1 else success_body(protocol, text=True)
        return transport_response(protocol, payload, stream=True)

    def create_client(**kwargs):
        client = CLIENTS[protocol](api_key="synthetic-key", model=kwargs["model"], base_url="http://127.0.0.1/native")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        real_stream = client.stream

        async def observed_stream(**payload):
            response = await real_stream(**payload)
            responses.append(response)
            return response

        client.stream = observed_stream
        clients.append(client)
        return client

    monkeypatch.setattr(invoker, "create_llm_client", create_client)
    worker = await stops._dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=30)
        assert len(requests) == len(responses) == 1, "No repeat generation or fallback after provider execution"
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
            assert len(rows) == 1
            row = rows[0]
            initial_request_id = row.provider_request_id
            snapshot = row.model_request_snapshot_json
            assert provider_request_has_effect(snapshot["wire_request"]) is effectful_wire
            if effectful_wire:
                assert requests[0]["mcp_servers"] == MCP_SERVERS
            if has_output:
                assert row.state == "round_committed" and run.status == "completed"
                assert responses[0].content == "Synthetic answer"
            else:
                reconciles = provider_output_seen and effectful_wire
                assert row.state == ("needs_reconciliation" if reconciles else "failed")
                assert run.status == ("needs_reconciliation" if reconciles else "failed")
                assert row.seal_json == {
                    "delivery_state": "response_received", "error_class": "provider_empty_response",
                    "retry_safe": not provider_output_seen,
                }
                assert bool(run.metadata_json.get("session_v2_reconciliation")) is reconciles
                assert "provider_output_seen" not in row.seal_json
                prepare_args = dict(
                    tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
                    run_id=seed["run_id"], turn_id=row.turn_id, round_index=1,
                    messages=snapshot["messages"], tools=snapshot["tools"],
                    provider=snapshot["provider"], model=snapshot["model"], wire_request=snapshot["wire_request"],
                )
                if provider_output_seen:
                    # The exact prepare boundary that formerly opened attempt 2 must now reject it.
                    with pytest.raises(ModelRoundNeedsReconciliation):
                        await prepare_model_request(db, **prepare_args)
                else:
                    # R23e unchanged cases: emptiness/discovery/signatures retain a fresh retry lane.
                    retry_request_id = await prepare_model_request(db, **prepare_args)
                    assert retry_request_id != initial_request_id
                await db.rollback()
            assert responses[0].provider_output_seen is provider_output_seen
        # R23e: use the real claim predicate, not a direct call that bypasses admission
        # and re-enters an already-terminal worker. Preparation probes above roll back.
        async with owner_sessionmaker() as db:
            candidate = build_runtime_task_claim_candidate_statement().where(RuntimeTask.id == seed["run_id"])
            assert await db.scalar(candidate) is None, "The worker must not retry this terminated/held run"
            run = await db.get(RuntimeTask, seed["run_id"])
            assert run.status == ("completed" if has_output else (
                "needs_reconciliation" if provider_output_seen and effectful_wire else "failed"
            ))
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            assert row.provider_request_id == initial_request_id
        # Invoker may construct fallback clients eagerly; only transport requests count as generation.
        assert len(requests) == 1, "No second provider request or fallback generation"
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for client in clients:
            await client.close()
