"""Read current ledger witnesses without reloading each bound fact separately."""

import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, select

from tests.integration import test_unverified_t2_recovery_postgres as support
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
t2_work = support.t2_work


async def test_ledger_history_batches_current_witnesses_and_rechecks_changes(
    t2_work, owner_sessionmaker, app_user_sessionmaker, app_user_engine, monkeypatch
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services import work_ledger_tool_sources as sources
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run

    s = t2_work
    operations = [
        ("track_todo", {"action": "add", "title": "Verify the authorized report"}),
        ("track_todo", {"action": "update", "title": "Verify the authorized report", "status": "in_progress"}),
        ("read_ledger", {}),
    ]
    client = ScriptedLLMClient(
        [
            {
                "tool_calls": [
                    {
                        "id": f"ledger-{index}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
            for index, (name, arguments) in enumerate(operations)
        ]
        + [{"content": "The work remains in progress.", "finish_reason": "stop"}]
    )
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    async with owner_sessionmaker() as db:
        db.add(
            CapabilityPolicy(
                tenant_id=s["tenant_id"], capability="agent.task.track", allowed=True, requires_approval=False
            )
        )
        (await db.get(RuntimeTask, s["run_id"])).claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await db.commit()
    fence = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    assert len(client.requests) == 4

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        calls = list(
            await db.scalars(
                select(SessionToolInvocation)
                .where(SessionToolInvocation.run_id == s["run_id"])
                .order_by(SessionToolInvocation.round_id)
            )
        )
        assert len(calls) == 3 and all(call.effect_state == "effect_committed" for call in calls)
        identity = uuid5(calls[-1].id, "work-ledger-tool-result")
        row = await db.get(InvocationSpan, identity)
        original = copy.deepcopy(row.metadata_json)
        proof = original["ledger_result"]
        assert proof["ledger_complete"] is True and proof["ledger_inputs"]
        arguments = dict(
            tenant_id=s["tenant_id"],
            agent_id=s["agent_id"],
            actor_id=s["user_id"],
            session_id=s["session_id"],
            scope=proof["scope"],
            identity=str(identity),
        )
        queries = []

        def observe(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                queries.append(statement)

        event.listen(app_user_engine.sync_engine, "before_cursor_execute", observe)
        try:
            retained, complete = await sources._snapshot_sources(db, **arguments)
        finally:
            event.remove(app_user_engine.sync_engine, "before_cursor_execute", observe)
        assert complete and {ref["source_id"] for ref in retained} == {str(call.id) for call in calls}
        query_count = len(queries)
        for key in ("tenant_id", "agent_id", "actor_id", "session_id"):
            assert await sources._snapshot_sources(db, **{**arguments, key: uuid4()}) == ([], False)
        for key, value in [
            ("invocation_id", "malformed"),
            ("invocation_id", str(uuid4())),
            ("model_result_id", str(uuid4())),
            ("args_hash", "0" * 64),
            ("authority_hash", "0" * 64),
            ("model_request_hash", "0" * 64),
            ("content_hash", "0" * 64),
            ("actor_id", str(uuid4())),
            ("audience", {"kind": "forged"}),
            ("schema", "forged"),
            ("unbound", ["missing"]),
            ("result_inputs", {}),
            ("ledger_inputs", [str(identity)]),
            ("ledger_complete", False),
            ("scope", {}),
        ]:
            async with owner_sessionmaker() as writer:
                changed = await writer.get(InvocationSpan, identity)
                changed.metadata_json = {"ledger_result": {**proof, key: value}}
                await writer.commit()
            assert await sources._snapshot_sources(db, **arguments) == ([], False), key
        async with owner_sessionmaker() as writer:
            (await writer.get(InvocationSpan, identity)).metadata_json = original
            (await writer.get(ChatSession, s["session_id"])).audience_kind = "group"
            await writer.commit()
        assert await sources._snapshot_sources(db, **arguments) == ([], False)
        async with owner_sessionmaker() as writer:
            (await writer.get(ChatSession, s["session_id"])).audience_kind = "private"
            await writer.commit()
        assert await sources._snapshot_sources(db, **arguments) == (retained, True)
        assert len({UUID(ref["source_id"]) for ref in retained}) == 3
        assert query_count == 2 * len(calls), queries
