"""A claimed private Web run must persist a refusal after its authority changes."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.security_audit import ResourcePermission
from app.models.tenant import Tenant
from app.models.user import User
from app.runtime import invoker
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure, execute
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

AUTHORITY_SUMMARY = "Execution stopped before the next effect; restore authority or revise the work."
TENANT_MESSAGE = (
    "[Error] Cannot invoke agent — tenant resolution failed: Agent {agent_id} "
    "is not executable: tenant is inactive. Please retry or contact admin."
)
EXPECTED_FAILURES = {
    # The pre-batch runtime still invoked the model after an execute deny.
    "execute_revoked": (AUTHORITY_SUMMARY, "turn_abort", "agent_execute_denied", ""),
    "agent_deactivated": (AUTHORITY_SUMMARY, "turn_abort", "runtime_agent_unavailable", ""),
    "tenant_inactive": ("tenant_resolution_error", "tenant_resolution_error", None, TENANT_MESSAGE),
    "credential_unavailable": ("Web chat run failed: ContextDependencyUnavailable", "turn_abort", None, ""),
}


@pytest.mark.parametrize("refusal", EXPECTED_FAILURES)
async def test_private_turn_start_refusal(member_request, owner_sessionmaker, monkeypatch, tmp_path, refusal):  # noqa: F811
    from app.core.events import close_redis
    from app.services import credential_boundary_loader

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    if refusal == "execute_revoked":
        async with owner_sessionmaker() as db:
            agent = await db.get(Agent, seed.agent_id)
            agent.owner_user_id = await db.scalar(
                select(User.id).where(User.tenant_id == seed.tenant_id, User.id != seed.user_id)
            )
            db.add_all([
                ResourcePermission(
                    tenant_id=seed.tenant_id, principal_type="user", principal_id=seed.user_id,
                    resource_type="agent", resource_id=agent.id, actions=[action], effect="allow",
                )
                for action in ("read", "execute")
            ])
            await db.commit()

    url = f"/api/agents/{seed.agent_id}/sessions"
    response = await client.post(f"{url}/runs", json={"content": "Hello", "input_id": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    session_id = response.json()["session"]["id"]
    run_id = uuid.UUID(response.json()["run"]["run_id"])

    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, run_id)).status == "pending"
        if refusal == "execute_revoked":
            grants = await db.scalars(select(ResourcePermission).where(
                ResourcePermission.resource_id == seed.agent_id,
                ResourcePermission.principal_id == seed.user_id,
            ))
            execute_grant, = [grant for grant in grants if grant.actions == ["execute"]]
            execute_grant.effect = "deny"
        elif refusal == "agent_deactivated":
            agent = await db.get(Agent, seed.agent_id)
            agent.status = "stopped"
            agent.deactivated_at = datetime.now(timezone.utc)
        elif refusal == "tenant_inactive":
            (await db.get(Tenant, seed.tenant_id)).is_active = False
        await db.commit()

    if refusal == "credential_unavailable":
        async def unavailable(db, **_scope):
            await db.execute(text("SELECT 1 / 0"))

        monkeypatch.setattr(credential_boundary_loader, "load_requester_exact_secret_boundary", unavailable)
    provider = ScriptedLLMClient([{"content": "Unexpected reply", "stream_text": "Unexpected reply"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
    try:
        await claim(seed, run_id, owner_sessionmaker)
        await execute(run_id)
    finally:
        await close_redis()

    transcript_url = f"{url}/{session_id}/transcript"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        actual = {
            "status": task.status,
            "summary": task.result_summary,
            "persistence_error": (task.metadata_json or {}).get("persistence_error"),
            "provider_calls": len(provider.requests),
        }
        # Restore history access only after recording the finished run.
        if refusal == "agent_deactivated":
            refused_replay = await client.get(transcript_url, params={"schema_version": 2})
            assert refused_replay.status_code == 410 and refused_replay.json() == {"detail": "已停用"}
            actual["history_refusal"] = refused_replay.json()["detail"]
            agent = await db.get(Agent, seed.agent_id)
            agent.status = "idle"
            agent.deactivated_at = None
        elif refusal == "tenant_inactive":
            (await db.get(Tenant, seed.tenant_id)).is_active = True
        await db.commit()

    replay = await client.get(transcript_url, params={"schema_version": 2})
    assert replay.status_code == 200, (actual, replay.text)
    actual["transcript"] = [
        (
            event["kind"], event["payload"].get("terminal_reason"), event["payload"].get("failure_code"),
            event["payload"].get("message"),
        )
        for event in replay.json()
        if event.get("run_id") == str(run_id)
        and event.get("kind") in {"runtime_failure.recorded", "assistant_final.completed", "run.completed"}
    ]
    print("A2FP1_REFUSAL", refusal, json.dumps(actual, ensure_ascii=False), flush=True)
    summary, terminal_reason, failure_code, message = EXPECTED_FAILURES[refusal]
    expected = {
        "status": "failed",
        "summary": summary,
        "persistence_error": None,
        "provider_calls": 0,
        "transcript": [("runtime_failure.recorded", terminal_reason, failure_code, message.format(agent_id=seed.agent_id))],
    }
    if refusal == "agent_deactivated":
        expected["history_refusal"] = "已停用"
    assert actual == expected
