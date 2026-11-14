"""Real kernel failure sealing distinguishes refusal from an executed provider response."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from tests.integration import test_r23a_stop_ends_blocked_model_call as stops
from tests.services.test_r23d_api_error_delivery import COMPLETED_EFFECT

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = stops.t2_work
MCP_SERVERS = [{"name": "synthetic-effect", "url": "http://127.0.0.1:1/mcp"}]


@pytest.mark.parametrize(
    "stopped,has_output",
    [(True, False), (True, True), (False, True), (False, False)],
    ids=["stopped_refusal", "stopped_after_effect", "running_after_effect", "running_refusal"],
)
async def test_provider_error_uses_output_before_stop_or_fallback(
    t2_work,
    owner_sessionmaker,
    monkeypatch,
    stopped,
    has_output,
):
    import app.runtime.invoker as invoker
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services import web_chat_runtime
    from app.services.llm_client import OpenAIResponsesClient

    seed = t2_work
    ready, release = asyncio.Event(), asyncio.Event()
    requests, clients = [], []
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        primary = await db.get(LLMModel, agent.primary_model_id)
        primary.provider, primary.model = "openai-response", "synthetic-primary"
        primary.provider_options = {"mcp_servers": MCP_SERVERS}
        fallback = LLMModel(
            id=uuid4(),
            tenant_id=seed["tenant_id"],
            provider="openai-response",
            model="synthetic-fallback",
            label="Synthetic fallback",
            enabled=True,
            api_key_encrypted=primary.api_key_encrypted,
            max_input_tokens=256_000,
            provider_options={"mcp_servers": MCP_SERVERS},
        )
        db.add(fallback)
        await db.flush()
        agent.fallback_model_id = fallback.id
        await db.commit()

    async def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            ready.set()
            await release.wait()
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {"message": "Synthetic provider failure"},
                    "output": [COMPLETED_EFFECT] if has_output else [],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Synthetic final answer"}]}],
            },
        )

    def create_client(**kwargs):
        client = OpenAIResponsesClient(api_key="synthetic-key", model=kwargs["model"], base_url="http://127.0.0.1/v1")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    async def delayed_signal(**_kwargs):
        # Exercise the real durable-stop window before the control signal arrives.
        return None

    monkeypatch.setattr(invoker, "create_llm_client", create_client)
    monkeypatch.setattr(web_chat_runtime, "signal_web_chat_cancel", delayed_signal)
    worker = await stops._dispatch(owner_sessionmaker, seed["run_id"])
    try:
        await asyncio.wait_for(ready.wait(), timeout=20)
        async with owner_sessionmaker() as db:
            prepared_owner = await db.scalar(
                select(SessionModelResult.reconciliation_owner).where(SessionModelResult.run_id == seed["run_id"])
            )
            assert prepared_owner is not None
        if stopped:
            assert (await stops._stop(owner_sessionmaker, seed))["accepted"] is True
            async with owner_sessionmaker() as db:
                run = await db.get(RuntimeTask, seed["run_id"])
                assert run.status == "running" and run.metadata_json["cancel_state"] == "cancelling"
            assert not web_chat_runtime._CANCEL_EVENTS[seed["run_id"].hex].is_set()
        release.set()
        await asyncio.wait_for(asyncio.shield(worker), timeout=15)
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, seed["run_id"])
            rows = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
            print(
                f"R23D stopped={stopped} output={has_output} requests={len(requests)} "
                f"models={[item['model'] for item in requests]} run={run.status} "
                f"seals={[(row.state, row.seal_json.get('error_class'), row.seal_json.get('delivery_state')) for row in rows]}"
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.model_request_snapshot_json["wire_request"]["reasoning"]["mcp_servers"] == MCP_SERVERS
            assert requests[0]["mcp_servers"] == MCP_SERVERS
            if has_output:
                assert len(requests) == 1, "An effectful unknown delivery must never retry or call the fallback"
                assert row.state == run.status == "needs_reconciliation"
                assert row.reconciliation_owner == "session_model_round:ambiguous_failure"
                assert row.seal_json["delivery_state"] == "unknown" and row.seal_json["retry_safe"] is False
                assert row.seal_json["error_class"] == ("cancelled" if stopped else "unknown")
            elif stopped:
                assert len(requests) == 1
                assert row.state == "failed" and run.status == "killed"
                assert row.seal_json == {"delivery_state": "rejected", "error_class": "cancelled", "retry_safe": False}
                assert row.reconciliation_owner is None
                assert not run.metadata_json.get("session_v2_reconciliation")
                assert run.metadata_json["terminal_reason"] == "user_cancel"
            else:
                assert [item["model"] for item in requests] == ["synthetic-primary", "synthetic-fallback"]
                assert row.state == "round_committed" and run.status == "completed"
                # Successful fallback preparation retains its execution owner; it is not a reconciliation fence.
                assert row.reconciliation_owner == prepared_owner
                assert not run.metadata_json.get("session_v2_reconciliation")
    finally:
        release.set()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        for client in clients:
            await client.close()
