"""First-task seed publication must observe the current producing model."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import pytest
from sqlalchemy import select, text

from tests.integration import test_unverified_t2_recovery_postgres as support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = support.t2_work


@pytest.mark.parametrize("mutation", ["reconciliation", "snapshot"])
async def test_creation_seed_rechecks_retained_producing_model(
    t2_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, mutation
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.employee_creation_sources import _owned_context_source
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_model_inputs import tool_result_source_hash
    from app.services.session_semantic_history import _event_payload
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = t2_work
    client = ScriptedLLMClient(
        [
            {
                "tool_calls": [
                    {
                        "id": "inspect-current-work",
                        "type": "function",
                        "function": {"name": "task_list", "arguments": json.dumps({})},
                    }
                ],
                "finish_reason": "tool_calls",
            },
            {"content": "The current work list was inspected.", "finish_reason": "stop"},
        ]
    )
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    async with owner_sessionmaker() as writer:
        writer.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"], capability="agent.task.read", allowed=True, requires_approval=False
            )
        )
        run = await writer.get(RuntimeTask, seed["run_id"])
        run.claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await writer.commit()
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 2

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        assert (
            await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one() == (False, False)
        invocation = await db.scalar(
            select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"])
        )
        assert invocation.effect_state == "effect_committed" and invocation.result_event_id is not None
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        retained = await db.get(SessionModelResult, event.result_id)
        assert retained is not None
        arguments = dict(
            dependency={
                "source_kind": "session_tool_invocation",
                "source_id": str(invocation.id),
                "source_version": tool_result_source_hash(_event_payload(event)),
            },
            agent_id=seed["agent_id"],
            tenant_id=seed["tenant_id"],
            session_id=seed["session_id"],
            user_id=seed["user_id"],
        )
        assert await _owned_context_source(db, **arguments) is True
        original_state = retained.state
        original_snapshot = deepcopy(retained.model_request_snapshot_json)
        await db.commit()
        async with owner_sessionmaker() as writer:
            current = await writer.get(SessionModelResult, retained.id)
            if mutation == "reconciliation":
                current.state = "needs_reconciliation"
            else:
                changed = deepcopy(current.model_request_snapshot_json)
                changed["input_bindings"]["dependency_coverage"]["complete"] = False
                current.model_request_snapshot_json = changed
            await writer.commit()
        try:
            async with app_user_sessionmaker() as fresh:
                await pin_rls_tenant_context(fresh, seed["tenant_id"])
                current_allowed = await _owned_context_source(fresh, **arguments)
                assert current_allowed is False
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert await _owned_context_source(db, **arguments) is current_allowed
        finally:
            async with owner_sessionmaker() as writer:
                current = await writer.get(SessionModelResult, retained.id)
                current.state = original_state
                current.model_request_snapshot_json = original_snapshot
                await writer.commit()
        assert await _owned_context_source(db, **arguments) is True
        assert retained.state == original_state
        assert retained.model_request_snapshot_json == original_snapshot
