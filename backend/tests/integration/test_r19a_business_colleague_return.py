"""A real business worker stops locally; colleague delivery targets its task session."""

import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from tests.integration.test_business_task_text_sources import (
    test_business_task_text_rechecks_its_actual_original_source as _business_case,
)
from tests.integration.test_m4v_upload_intents_postgres import original_api, provider, delivered, s3_server
from tests.integration.test_failed_delegation_continuation import _call
from tests.integration.test_r19a_consumed_result import auxiliary_models
from tests.integration.test_r11c_colleague_lifecycle_postgres import _execute as _execute_child
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient


@pytest.mark.parametrize("provider", ["filesystem"], indirect=True)
@pytest.mark.parametrize("terminal_read", [False, True])
async def test_business_colleague_return_uses_task_session_and_nested_guard_code(
    original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
    auxiliary_models, terminal_read,
):
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.kernel.loop_guard import LoopGuard
    from app.services import runtime_task_worker
    from app.services.agent_manager import AgentManager
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    state = {}
    native_business = runtime_task_worker._execute_claimed_business_task

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)

    async def complete_child():
        native_client = invoker.create_llm_client
        child_client = ScriptedLLMClient([{"tool_calls": [_call("get_current_time", {}, "budget-stop")],
            "finish_reason": "tool_calls", "usage": {"total_tokens": 1_000_001}}] if terminal_read
            else [{"content": "The colleague completed the requested work."}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
        try:
            await _execute_child(state["child_id"], owner_sessionmaker, worker="r19a-r4-business-child", delegation=True)
        finally:
            monkeypatch.setattr(invoker, "create_llm_client", native_client)
        # The native autonomous-parent identity is currently refused before
        # the child provider call (execution_identity_agent_mismatch). This
        # is still a real terminal completion and must return its failure to
        # the task session. Do not bypass that separate authority check.
        assert len(child_client.requests) == 0
        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, state["child_id"])
            assert child.status == "failed" and child.completed_at is not None
            assert child.metadata_json["terminal_reason"] == "delegation_instruction_source_access_denied"
        processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker,
            data_root=get_settings().AGENT_DATA_DIR)
        result = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=state["tenant_id"], worker_id="r19a-r4-business-boundary",
            canonical_validator=processor.validate, process_callback=processor, task_types=("delegation",))
        assert result["delivered"] == 1, result

    async def business_with_colleague(run_id):
        # Only supply the scripted provider and a governed colleague. The real
        # worker, executor, kernel, business finalizer and notification all run.
        client = invoker.create_llm_client()
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, run_id)
            agent = await db.get(Agent, run.parent_agent_id)
            colleague = Agent(id=uuid4(), tenant_id=run.tenant_id, name="Business colleague",
                creator_id=run.root_user_id, owner_user_id=run.root_user_id, primary_model_id=agent.primary_model_id)
            db.add(colleague)
            await db.flush()
            await AgentManager().initialize_agent_files(db, colleague, original_author_id=run.root_user_id)
            for capability in ("agent.message.send", "agent.async_task.read", "agent.context.read"):
                db.add(CapabilityPolicy(tenant_id=run.tenant_id, capability=capability,
                    allowed=True, requires_approval=False))
            state.update(tenant_id=run.tenant_id, run_id=run.id)
            await db.commit()
        native_stream = client.stream

        async def scripted_business(**kwargs):
            if not client.requests:
                from app.models.plan_request import AgentPlanRequest
                from app.services.plan_authorization_lease import canonical_action_artifact, issue_plan_authorization_leases
                from app.services.plan_mode_core import compute_plan_hash, intent_type_for_action

                async with owner_sessionmaker() as db:
                    run = await db.get(RuntimeTask, run_id)
                    artifact = {"target_agent_id": str(colleague.id), "message": "Do the requested work.",
                        "tool_profile": "worker_safe", "target_artifact_path": None, "target_artifacts": [],
                        "edit_mode": "create_or_update"}
                    intent = intent_type_for_action("start_delegation")
                    plan_json = {"schema": "hive_plan.v1", "intent_type": intent,
                        "title": "Delegate business work", "objective": artifact["message"],
                        "steps": [{"order": 1, "description": artifact["message"]}],
                        "success_criteria": ["Use the colleague result."], "stop_conditions": ["Owner cancels."],
                        "handoff": {"target": "continue_current_session"}, "authorization_scopes": [{
                            "action_kind": "start_delegation", "target_ref": f"agent:{colleague.id}:delegation",
                            "arguments": canonical_action_artifact(artifact), "summary": artifact["message"]}]}
                    plan = AgentPlanRequest(tenant_id=run.tenant_id, agent_id=run.parent_agent_id,
                        session_id=run.child_session_id, requested_by_user_id=run.root_user_id,
                        confirmed_by_user_id=run.root_user_id, confirmed_at=datetime.now(timezone.utc),
                        source="web_chat", intent_type=intent, original_request=artifact["message"],
                        status="confirmed", plan_version=1, plan_hash=compute_plan_hash(plan_json), plan_json=plan_json)
                    db.add(plan)
                    await db.flush()
                    assert len(await issue_plan_authorization_leases(db=db, plan=plan,
                        confirming_user_id=run.root_user_id)) == 1
                    arguments = {"agent_name": "Business colleague", "message": artifact["message"],
                        "tool_profile": artifact["tool_profile"], "edit_mode": artifact["edit_mode"],
                        "confirmed_plan_id": str(plan.id), "confirmed_plan_version": 1,
                        "confirmed_plan_hash": plan.plan_hash, "confirmed_plan_session_id": run.child_session_id}
                    await db.commit()
                client.script = [{"tool_calls": [_call("delegate_to_agent", arguments, "business-delegate")],
                    "finish_reason": "tool_calls"}]
            else:
                if "child_id" not in state:
                    async with owner_sessionmaker() as db:
                        run = await db.get(RuntimeTask, run_id)
                        child = await db.scalar(select(RuntimeTask).where(RuntimeTask.task_type == "delegation",
                            RuntimeTask.parent_session_id == str(UUID(run.child_session_id))))
                        assert child is not None
                        state.update(child_id=child.id, session_id=UUID(run.child_session_id))
                    if terminal_read:
                        await complete_child()
                client.script = [{"tool_calls": [_call("check_async_task", {"task_id": str(state["child_id"])},
                    f"business-read-{len(client.requests)}")], "finish_reason": "tool_calls"}]
            return await native_stream(**kwargs)

        monkeypatch.setattr(client, "stream", scripted_business)
        await native_business(run_id)
        assert len(client.requests) == 1 + LoopGuard().identical_tool_threshold

    monkeypatch.setattr(runtime_task_worker, "_execute_claimed_business_task", business_with_colleague)

    async def inspect_return(**context):
        async with owner_sessionmaker() as db:
            run = await db.get(RuntimeTask, state["run_id"])
            code = "identical_terminal_task_read" if terminal_read else "identical_running_task_read"
            assert run.task_type == "business_task" and run.status == "failed"
            assert run.metadata_json["outcome"]["error_code"] == code
            task = await db.get(Task, UUID(run.metadata_json["business_task_id"]))
            assert task.status == "failed"
            session = await db.get(ChatSession, state["session_id"])
            assert session.session_kind == "user_task"
            before = list(await db.scalars(select(RuntimeTask.id).where(
                RuntimeTask.parent_session_id == str(session.id))))
            assert before == [state["child_id"]]
        if not terminal_read:
            await complete_child()
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id="r19a-r4-business-return", limit=100)
        pages = await service.prepare_integration_pages(worker_id="r19a-r4-business-return",
            claimed=[item for item in claimed if item.source_run_id == str(state["child_id"])])
        assert len(pages) == 1
        receipt = await service._deliver_page(pages[0])
        assert receipt["ok"] is True
        await service._mark_page_delivered(page=pages[0], worker_id="r19a-r4-business-return", receipt=receipt)
        async with owner_sessionmaker() as db:
            runs = list(await db.scalars(select(RuntimeTask).where(
                RuntimeTask.parent_session_id == str(state["session_id"]))))
            continued = [row for row in runs if row.id not in before]
            assert len(continued) == (0 if terminal_read else 1)
            if continued:
                assert continued[0].task_type == "web_chat_turn"
                assert continued[0].metadata_json["integration_page_id"] == str(pages[0].id)
            print("R19A_R4_BUSINESS_RETURN", json.dumps({"terminal_read": terminal_read,
                "before_count": len(before), "after_count": len(runs),
                "continuation_types": [row.task_type for row in continued], "business_task_status": task.status}))
        # The ruling measures successor rows in the task session. Its later
        # dispatch still obeys the existing next-input hold; do not bypass it.

    await _business_case(original_api, provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, withdrawn=False, unknown_effect=False, delivery_channel="web", permission_handler=inspect_return)
