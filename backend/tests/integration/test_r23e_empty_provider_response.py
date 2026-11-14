"""Three ledger outcomes; protocol projection is covered by the client matrix."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tests.integration import test_r23a_stop_ends_blocked_model_call as stops
from tests.services.test_r23e_provider_output import MCP_CALL, success_body, transport_response

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = stops.t2_work
MCP_SERVERS = [{"name": "synthetic-effect", "url": "http://127.0.0.1:1/mcp"}]


@pytest.mark.parametrize("provider_output_seen,effectful_wire,state", [
    (True, True, "needs_reconciliation"), (True, False, "failed"), (False, True, "failed"),
], ids=["executed_effectful", "executed_function_only", "genuinely_empty"])
async def test_successful_empty_response_seals_without_replaying_provider_execution(
    t2_work, owner_sessionmaker, monkeypatch, provider_output_seen, effectful_wire, state,
):
    import app.runtime.invoker as invoker
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.llm_client import OpenAIResponsesClient
    from app.services.runtime_task_claim_service import build_runtime_task_claim_candidate_statement
    from app.services.session_model_round import ModelRoundNeedsReconciliation, prepare_model_request, provider_request_has_effect

    seed = t2_work
    requests, clients = [], []
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        primary = await db.get(LLMModel, agent.primary_model_id)
        primary.provider, primary.model = "openai-response", "synthetic-primary"
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
        body = success_body("responses", [MCP_CALL] if provider_output_seen else [])
        return transport_response("responses", body)

    def create_client(**kwargs):
        client = OpenAIResponsesClient(api_key="synthetic", model=kwargs["model"], base_url="http://127.0.0.1/native")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(invoker, "create_llm_client", create_client)
    worker = await stops._dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(asyncio.shield(worker), timeout=30)
        assert len(requests) == 1, "No repeat generation or fallback after provider execution"
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
            assert len(rows) == 1
            row = rows[0]
            initial_request_id, initial_version = row.provider_request_id, row.version
            snapshot = row.model_request_snapshot_json
            assert provider_request_has_effect(snapshot["wire_request"]) is effectful_wire
            if effectful_wire:
                assert requests[0]["mcp_servers"] == MCP_SERVERS
            assert row.state == run.status == state
            assert row.seal_json == {
                "delivery_state": "response_received", "error_class": "provider_empty_response",
                "retry_safe": not provider_output_seen,
            }
            assert bool(run.metadata_json.get("session_v2_reconciliation")) is (state == "needs_reconciliation")
            assert "provider_output_seen" not in row.seal_json
            prepare_args = dict(
                tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], session_id=seed["session_id"],
                run_id=seed["run_id"], turn_id=row.turn_id, round_index=1,
                messages=snapshot["messages"], tools=snapshot["tools"],
                provider=snapshot["provider"], model=snapshot["model"], wire_request=snapshot["wire_request"],
            )
            if provider_output_seen:
                with pytest.raises(ModelRoundNeedsReconciliation):
                    await prepare_model_request(db, **prepare_args)
            else:
                retry_request_id = await prepare_model_request(db, **prepare_args)
                assert retry_request_id != initial_request_id
                assert row.state == "prepared" and row.version == initial_version + 1, "Exactly one fresh attempt"
            await db.rollback()
        async with owner_sessionmaker() as db:
            candidate = build_runtime_task_claim_candidate_statement().where(RuntimeTask.id == seed["run_id"])
            assert await db.scalar(candidate) is None, "The worker must not retry this terminated/held run"
            assert (await db.get(RuntimeTask, seed["run_id"])).status == state
            row = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
            assert row.provider_request_id == initial_request_id
        # Eager fallback construction is not another generation; only count transport requests.
        assert len(requests) == 1, "No second provider request or fallback generation"
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for client in clients:
            await client.close()
