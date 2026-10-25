"""A running first task remains source-bound in its original work's context."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.agents import orchestrator
from app.database import pin_rls_tenant_context
from app.models.invocation_span import InvocationSpan
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
from app.services.session_model_inputs import _hash
from tests.integration import test_m1g49_delegation_instruction_origin_postgres as native_cases


@pytest.mark.usefixtures("migrated_pg_url")
async def test_original_work_consumes_running_first_task_context_before_its_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, migrated_pg_url
):
    import app.runtime.invoker as invoker
    from app.services.runtime_root_ledger import register_runtime_root_item
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    original_delegate = orchestrator._delegate
    observed = []

    async def inspect_running_context(request):
        tenant_id, agent_id, user_id = request.tenant_id, request.parent_agent_id, request.owner_id
        session_id, child_id = UUID(str(request.parent_session_id)), UUID(str(request.runtime_task_id))
        run_id, input_id = uuid4(), uuid4()
        turn_id, worker = f"turn-{run_id.hex}", "running-context-worker"
        prompt = "Check the same first task's current status without dispatching it again."
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            child = await db.get(RuntimeTask, child_id)
            assert child.status == "running" and child.attempt_count == 1
            # The creation fixture has finished issuing its one first task.
            # Admit a real follow-up turn in the same work while that task runs.
            prior = list(
                await db.scalars(
                    select(RuntimeTask).where(
                        RuntimeTask.tenant_id == tenant_id,
                        RuntimeTask.parent_agent_id == agent_id,
                        RuntimeTask.parent_session_id == str(session_id),
                        RuntimeTask.task_type == "web_chat_turn",
                        RuntimeTask.status == "running",
                    )
                )
            )
            assert len(prior) <= 1
            for task in prior:
                task.status = "completed"
            await db.flush()
            db.add(
                RuntimeTask(
                    id=run_id,
                    task_type="web_chat_turn",
                    status="running",
                    tenant_id=tenant_id,
                    parent_agent_id=agent_id,
                    child_agent_id=agent_id,
                    parent_session_id=str(session_id),
                    child_session_id=str(session_id),
                    root_user_id=user_id,
                    root_session_id=str(session_id),
                    root_runtime_task_id=run_id,
                    prompt=prompt,
                    claimed_by=worker,
                    claim_version=1,
                    attempt_count=1,
                    claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                    metadata_json={
                        "turn_id": turn_id,
                        "user_id": str(user_id),
                        "runtime_budget": {
                            "schema": "hive.runtime_budget_binding.v1",
                            "status": "active",
                            "interactive": True,
                        },
                    },
                )
            )
            await db.flush()
            await register_runtime_root_item(
                db,
                tenant_id=tenant_id,
                root_runtime_task_id=run_id,
                source_agent_id=agent_id,
                intent_key=f"web-chat-turn:{run_id.hex}",
                work_type="web_chat_turn",
                target_ref=str(session_id),
                runtime_task_id=run_id,
                root_user_id=user_id,
                root_session_id=str(session_id),
                state="running",
            )
            await db.commit()
            user = await db.get(User, user_id)
            authority = await resolve_session_mutation_authority(
                db,
                user=user,
                agent_id=agent_id,
                session_id=session_id,
                action="mutate_session_input",
            )
            await accept_human_input(
                db,
                authority=authority,
                intent={
                    "kind": "steer_current_turn",
                    "input_id": str(input_id),
                    "idempotency_key": f"input:{input_id}",
                    "session_id": str(session_id),
                    "content_parts": [{"type": "text", "text": prompt}],
                    "expected_turn_id": turn_id,
                    "expected_run_id": str(run_id),
                    "terminal_fallback": "queue_next_turn",
                },
            )
            await run_user_prompt_admission(db, authority=authority, input_id=input_id, worker_id=worker)
            await queue_admitted_human_input(db, authority=authority, input_id=input_id)
            await db.commit()

        client = ScriptedLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "id": "inspect-running-child",
                            "type": "function",
                            "function": {
                                "name": "check_async_task",
                                "arguments": json.dumps({"task_id": str(child_id)}),
                            },
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
                {
                    "content": "The same first task is still running; keep following its result.",
                    "finish_reason": "stop",
                },
            ]
        )
        prior_factory = invoker.create_llm_client
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        token = set_runtime_task_fence(task_id=run_id, worker_id=worker, claim_version=1)
        try:
            await execute_web_chat_run(run_id)
        finally:
            reset_runtime_task_fence(token)
            monkeypatch.setattr(invoker, "create_llm_client", prior_factory)
        (tmp_path / "running-context-parent-requests.json").write_text(
            json.dumps(client.requests, default=str, indent=2)
        )
        assert len(client.requests) == 2
        startup = json.dumps(client.requests[0]["messages"])
        assert str(child_id) in startup and "[running]" in startup
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            models = list(
                await db.scalars(
                    select(SessionModelResult).where(
                        SessionModelResult.tenant_id == tenant_id,
                        SessionModelResult.run_id == run_id,
                    )
                )
            )
            bindings = [model.model_request_snapshot_json["input_bindings"] for model in models]
            (tmp_path / "running-context-source-coverage.json").write_text(json.dumps(bindings, default=str, indent=2))
            assert len(models) == 2 and all(binding["dependency_coverage"]["complete"] for binding in bindings)
            assert (await db.get(RuntimeTask, child_id)).status == "running"
            assert (await db.get(RuntimeTask, child_id)).attempt_count == 1
            rows = list(
                await db.scalars(
                    select(InvocationSpan).where(
                        InvocationSpan.tenant_id == tenant_id,
                        InvocationSpan.runtime_task_id == run_id,
                        InvocationSpan.span_type == "runtime_team_context",
                    )
                )
            )
            assert rows
            user = await db.get(User, user_id)
            refs = []
            for row in rows:
                snapshot = deepcopy(row.metadata_json)
                ref = {
                    "source_kind": "runtime_team_context",
                    "source_id": f"{agent_id}\n{session_id}\n{row.id}",
                    "source_version": _hash(snapshot["observation"]),
                }
                await _validate_input_dependencies(db, tenant_id, [ref], user=user)
                control = await db.begin_nested()
                row.metadata_json = {"observation": {**snapshot["observation"], "content": "Forged running state"}}
                await db.flush()
                with pytest.raises(ContentInputReceiptError):
                    await _validate_input_dependencies(db, tenant_id, [ref], user=user)
                await control.rollback()
                await _validate_input_dependencies(db, tenant_id, [ref], user=user)
                refs.append(ref)
            observed.append((tenant_id, user_id, child_id, refs))

    async def observe_delegate(request):
        if request.interaction_type == "delegation" and request.runtime_task_id:
            assert not observed
            await inspect_running_context(request)
        return await original_delegate(request)

    monkeypatch.setattr(orchestrator, "_delegate", observe_delegate)
    await native_cases.test_governed_delegation_preserves_its_parent_instruction_origin(
        owner_sessionmaker=owner_sessionmaker,
        app_user_sessionmaker=app_user_sessionmaker,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        input_case="first_task_child_readback",
        migrated_pg_url=migrated_pg_url,
    )
    assert len(observed) == 1
    tenant_id, user_id, child_id, refs = observed[0]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assert (await db.get(RuntimeTask, child_id)).status == "completed"
        assert (await db.get(RuntimeTask, child_id)).attempt_count == 1
        # Later completion/publication cannot invalidate the exact earlier
        # native observation; its source grants must still validate now.
        await _validate_input_dependencies(db, tenant_id, refs, user=await db.get(User, user_id))
