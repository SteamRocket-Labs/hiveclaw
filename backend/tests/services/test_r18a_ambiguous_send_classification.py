"""R18a Part 2: an effect-free unknown provider delivery is a plain failure.

E2E run 15: the 30th model round's stream timed out (ReadTimeout,
delivery_state="unknown"); the run was fenced needs_reconciliation and only a
platform admin could ever close it. The durable round ledger already knows the
exact wire request; when it carried no provider-side effect (function tools
only, no mcp_servers), failing plainly is truthful and the member keeps a
usable session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

_FUNCTION_TOOLS = [{"type": "function", "function": {"name": "search_company_kb", "parameters": {}}}]


async def _prepare_round(db, common, wire_request):
    from app.services.session_model_round import bind_round_inputs, prepare_model_request

    messages = await bind_round_inputs(db, **common)
    request_id = await prepare_model_request(db, **common, messages=messages, tools=[],
        provider="openai", model="local", wire_request={"messages": messages, **wire_request})
    await db.commit()
    return request_id


async def _run_task(db, run_id):
    return await db.get(RuntimeTask, run_id)


@pytest.mark.parametrize("wire_request,expected_state,expected_run_status", [
    # Function tools only: whatever the provider did with the request, it
    # executed nothing on our behalf — a plain failure.
    ({"tools": _FUNCTION_TOOLS}, "failed", "running"),
    # Provider-side effects the platform cannot prove absent stay fenced.
    ({"tools": [{"type": "web_search_2025"}]}, "needs_reconciliation", "needs_reconciliation"),
    ({"mcp_servers": [{"name": "local"}]}, "needs_reconciliation", "needs_reconciliation"),
])
async def test_unknown_delivery_settlement_follows_the_wire_effect(
    owner_sessionmaker, wire_request, expected_state, expected_run_status,
):
    from app.services.session_model_round import fail_model_request
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    common = dict(tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, run_id=run_id,
        turn_id=f"turn-{run_id.hex}", round_index=1)
    async with owner_sessionmaker() as db:
        # A live worker holds the claim, so the loaded-task fence check passes.
        run = await db.get(RuntimeTask, run_id)
        run.claimed_by = "r18a-worker"
        run.claim_version = 0
        run.claim_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()
    async with owner_sessionmaker() as db:
        request_id = await _prepare_round(db, common, wire_request)

    async with owner_sessionmaker() as db:
        token = set_runtime_task_fence(task_id=run_id, claim_version=0, worker_id="r18a-worker")
        try:
            state = await fail_model_request(db, **common, provider_request_id=request_id,
                error_class="timeout", delivery_state="unknown", retry_safe=False)
            await db.commit()
        finally:
            reset_runtime_task_fence(token)

    assert state == expected_state
    async with owner_sessionmaker() as db:
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        run = await _run_task(db, run_id)
        assert result.state == expected_state
        assert result.seal_json == {"delivery_state": "unknown", "error_class": "timeout", "retry_safe": False}
        assert result.reconciliation_owner == (
            "session_model_round:ambiguous_failure" if expected_state == "needs_reconciliation" else None
        )
        # Effect-free: the run is left to the ordinary failure finalizer (the
        # kernel returns the classified provider error), so no reconciliation
        # hold is written on the task and the member's session stays usable.
        assert run.status == expected_run_status
        assert (("session_v2_reconciliation" in (run.metadata_json or {}))
            == (expected_state == "needs_reconciliation"))
        if expected_state == "failed":
            assert run.completed_at is None
        else:
            assert run.completed_at is not None
