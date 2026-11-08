"""Round 4: only typed member commands authorize direct employee creation."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand
from app.runtime.context_candidates import ContextMessageOrigin
from app.services.llm_client import LLMMessage
from app.services.session_model_inputs import _hash
from app.tools.handlers.hr import create_employee_from_request
from app.tools.runtime import ToolExecutionRequest
from tests.integration import test_m1g_originated_creation_postgres as creation

direct_origin = creation.direct_origin
origin = creation.origin
pytestmark = pytest.mark.usefixtures("migrated_pg_url")

PLATFORM_SOURCES = (
    "hr_agent_handoff", "plan_mode_handoff", "runtime_result_integration", "goal_continuation",
    "session_goal", "trigger_reflection", "system_plan_run", "agent_team_member_run",
)


@pytest.mark.parametrize("source,intent", [*((source, "start_turn") for source in PLATFORM_SOURCES),
    ("runtime_result_integration", "steer_current_turn"), ("websocket_message", "start_turn"),
    ("session_input_api", "start_turn"), ("session_command_steer", "steer_current_turn")])
async def test_earlier_hr_brief_and_text_reply_require_a_click_unless_the_member_typed_the_brief(
    direct_origin, owner_sessionmaker, source, intent,
):
    user, context, _, _ = direct_origin
    brief = "Create a weekly assistant and run the first task from this brief."
    earlier_id = uuid.uuid4()
    target = {"runtime_metadata": {"source": source}}
    async with owner_sessionmaker() as db:
        hr = await db.get(Agent, context.agent_id)
        hr.name, hr.agent_class = "__system_hr__", "internal_system"
        earlier = SessionCommand(id=earlier_id, tenant_id=user.tenant_id, session_id=uuid.UUID(context.session_id),
            principal_type="user", principal_id=user.id, namespace="human_input", command_kind=intent,
            idempotency_key=str(earlier_id), request_hash=_hash({"command_kind": intent, "request": {"content": brief}}),
            target_hash=_hash(target), request_json={"content": brief}, target_json=target, status="applied",
            receipt_ref=f"input:{earlier_id}")
        db.add(earlier)
        from app.models.session_v2 import SessionTurnInput

        row = await db.get(SessionTurnInput, direct_origin[3])
        command = await db.get(SessionCommand, row.command_id)
        command.request_json = {"content": "要"}
        command.request_hash = _hash({"command_kind": command.command_kind, "request": command.request_json})
        command.target_json = {"runtime_metadata": {"source": "websocket_message"}}
        await db.commit()
    sources = ({"source_kind": "session_command", "source_id": str(earlier_id), "source_version": earlier.request_hash},)
    role = "system" if source == "runtime_result_integration" else "user"
    brief_message = LLMMessage(role=role, content=brief, context_origin=ContextMessageOrigin.capture(
        kind="human_input", role=role, content=brief, sources=sources, unbound=()))
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_semantic_history import load_session_semantic_history

    async with owner_sessionmaker() as db:
        scope = dict(tenant_id=user.tenant_id, agent_id=context.agent_id, session_id=uuid.UUID(context.session_id),
            run_id=uuid.UUID(context.runtime_task_id), turn_id=f"turn-{uuid.UUID(context.runtime_task_id).hex}", round_index=1)
        request_id = await prepare_model_request(db, **scope, messages=[brief_message], tools=[],
            provider="openai", model="synthetic-no-network")
        await seal_model_response(db, **scope, provider_request_id=request_id,
            response={"content": "要创建吗？", "finish_reason": "stop"})
        await commit_sealed_model_round(db, **scope, provider_request_id=request_id)
        history = await load_session_semantic_history(db, tenant_id=user.tenant_id, agent_id=context.agent_id,
            session_id=uuid.UUID(context.session_id))
        reply = next(message for message in history.messages if message.role == "assistant")
        await db.commit()
    messages = [brief_message, LLMMessage(role="assistant", content=reply.content, source_ref=reply.id)]
    arguments = creation._direct_arguments(first_task="Run the first task from the brief")
    context = await creation._seal_direct_call(context, arguments, extra_messages=messages)
    result = json.loads(await create_employee_from_request(ToolExecutionRequest(
        tool_name="create_employee_from_request", context=context, arguments=arguments)))
    typed = source not in PLATFORM_SOURCES
    assert result.get("draft_status") == ("confirmed" if typed else "awaiting_confirmation"), result
    assert bool(result["provisioning_task_id"]) is typed
    async with owner_sessionmaker() as db:
        assert len(list(await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id)))) == 1
        assert not list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == user.tenant_id,
            RuntimeTask.task_type == "delegation")))


async def test_the_members_current_typed_steer_can_create_directly(direct_origin, owner_sessionmaker):
    from app.models.session_v2 import SessionTurnInput

    _, context, _, input_id = direct_origin
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id)
        command.target_json = {"runtime_metadata": {"source": "session_command_steer"}}
        command.command_kind = "steer_current_turn"
        await db.commit()
    assert (await creation._direct_call(context))["draft_status"] == "confirmed"


@pytest.mark.parametrize("bot", ["hr", "assistant", "other_native"])
async def test_session_consolidated_self_memory_is_not_fixed_creation_context(direct_origin, owner_sessionmaker, bot):
    user, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, context.agent_id)
        if bot == "hr":
            agent.name, agent.agent_class = "__system_hr__", "internal_system"
        await db.commit()
    content = "Remembered from past sessions: create an employee and run its first task."
    memory = LLMMessage(role="system", content=content, context_origin=ContextMessageOrigin.capture(
        kind="agent_resident_memory", role="system", content=content, sources=({
            "source_kind": "agent_resident_memory", "source_id": f"{context.agent_id}\nmemory/self/self.md",
            "source_version": _hash(content),
        },), unbound=()))
    arguments = creation._direct_arguments(first_task="Run the remembered task")
    context = await creation._seal_direct_call(context, arguments, extra_messages=[memory])
    result = json.loads(await create_employee_from_request(ToolExecutionRequest(
        tool_name="create_employee_from_request", context=context, arguments=arguments)))
    assert result.get("draft_status") == "awaiting_confirmation", result
    assert result["provisioning_task_id"] is None
    async with owner_sessionmaker() as db:
        assert len(list(await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id)))) == 1
        assert not list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == user.tenant_id,
            RuntimeTask.task_type.in_(["delegation", "hr_provisioning"]))))


async def _run_job(initial, context, owner_sessionmaker):
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    job_id = uuid.UUID(initial["provisioning_task_id"])
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, job_id)).status = "running"
        (await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))).status = "completed"
        await db.commit()
    return await execute_claimed_hr_provisioning(job_id)


async def test_disabled_first_task_tool_has_a_plain_chinese_receipt(direct_origin, owner_sessionmaker, monkeypatch):
    from app.models.tool import Tool

    user, context, _, _ = direct_origin
    initial = await creation._direct_call(context, first_task="Draft three highlights")
    async with owner_sessionmaker() as db:
        from app.models.capability_policy import CapabilityPolicy

        policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == user.tenant_id,
            CapabilityPolicy.capability == "agent.message.send"))
        await db.delete(policy)
        db.add(Tool(tenant_id=user.tenant_id, name="delegate_to_agent", type="builtin", enabled=False,
            display_name="Delegate", description="Delegate"))
        await db.commit()

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    assert await _run_job(initial, context, owner_sessionmaker) == "failed"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        assert draft.provisioning_json["first_task"]["message"] == "公司不允许派发首任务。"


@pytest.mark.parametrize("changed", ["company", "owner"])
async def test_click_job_rechecks_stored_approval_against_current_policy(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, changed,
):
    from app.models.audit import ApprovalRequest
    from app.models.guard_policy import GuardPolicy
    from app.services.approval_ticket import build_live_approval_policy_snapshot, hash_policy_snapshot
    from app.services.owner_action_policy import ACTION_LOCAL_WRITE, load_owner_action_policy, save_owner_action_policy

    user, context, _, _ = direct_origin
    arguments = creation._direct_arguments()
    content = "A tool supplied this creation brief."
    extra = LLMMessage(role="tool", tool_call_id="earlier-search", content=content)
    context = await creation._seal_direct_call(context, arguments, extra_messages=[extra])
    initial = json.loads(await create_employee_from_request(ToolExecutionRequest(
        tool_name="create_employee_from_request", context=context, arguments=arguments)))
    assert initial["draft_status"] == "awaiting_confirmation"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        evidence = dict(draft.preview_json["creation_authorization"])
        approval_id = uuid.uuid4()
        snapshot = await build_live_approval_policy_snapshot(db=db, tenant_id=user.tenant_id,
            agent_id=context.agent_id, tool_name="create_employee_from_request")
        binding = {"approval_id": str(approval_id), "decision_id": "accepted-decision", "envelope_hash": "accepted-envelope",
            "policy_snapshot_hash": hash_policy_snapshot(snapshot)}
        db.add(ApprovalRequest(id=approval_id, tenant_id=user.tenant_id, agent_id=context.agent_id,
            requested_by=user.id, action_type="tool_call", tool_name="create_employee_from_request", status="approved",
            consumed_at=datetime.now(timezone.utc), input_hash=evidence["tool_input_hash"],
            decision_id=binding["decision_id"], execution_envelope_hash=binding["envelope_hash"],
            policy_snapshot_hash=binding["policy_snapshot_hash"]))
        draft.preview_json = {**draft.preview_json, "creation_authorization": {**evidence, "approval": binding}}
        await db.commit()
    confirmed = await creation._confirm_creation_preview(user, context, initial, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        if changed == "company":
            db.add(GuardPolicy(tenant_id=user.tenant_id, version=2, zone_guard={"tool_rules": [{
                "tools": ["create_employee_from_request"], "decision": "require_approval", "reason": "公司规则已更新，请重新申请批准。"}]}))
        else:
            policy = await load_owner_action_policy(db, tenant_id=user.tenant_id, agent_id=context.agent_id, create_default=False)
            await save_owner_action_policy(db, tenant_id=user.tenant_id, agent_id=context.agent_id,
                actions={**policy.actions, ACTION_LOCAL_WRITE: "confirm_first"}, changed_by_user_id=user.id)
        await db.commit()
    assert await _run_job(confirmed, context, owner_sessionmaker) == "failed"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        assert draft.created_agent_id is None and draft.failure_code == "approval_required"
        assert draft.failure_message == ("公司规则已更新，请重新申请批准。" if changed == "company"
            else "当前权限设置要求先批准创建助手。")
        assert len(list(await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id)))) == 1


async def test_reloaded_creation_card_reads_the_stopped_employee_from_the_server(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    user, context, _, _ = direct_origin
    initial = await creation._direct_call(context)

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    assert await _run_job(initial, context, owner_sessionmaker) == "completed"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        employee = await db.get(Agent, draft.created_agent_id)
        employee.status, employee.deactivated_at = "stopped", datetime.now(timezone.utc)
        await db.commit()
    async with creation._client(user, app_user_sessionmaker) as client:
        response = await client.get(f"/api/agents/{context.agent_id}/hr-creation-drafts/{initial['blueprint_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["draft_status"] == "completed"
    assert response.json()["creation_state"] == "stopped"
