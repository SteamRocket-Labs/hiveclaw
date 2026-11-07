"""D1 uses the live human turn without requiring the old preview evidence bundle."""

from dataclasses import replace
import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent, AgentPermission
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.chat_session import ChatSession
from tests.integration import test_m1g_originated_creation_postgres as creation

_direct_call = creation._direct_call
direct_origin = creation.direct_origin
origin = creation.origin

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("conversation", ["assistant", "another_bot"])
async def test_d1_current_message_creates_and_dispatches_once_without_preview_receipts(
    direct_origin, owner_sessionmaker, monkeypatch, conversation,
):
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    user, context, model_id, input_id = direct_origin
    if conversation == "another_bot":
        from app.models.user import User

        async with owner_sessionmaker() as db:
            owner = User(id=uuid.uuid4(), tenant_id=user.tenant_id, username=f"peer-{uuid.uuid4().hex}",
                         email=f"{uuid.uuid4().hex}@d1.test", display_name="Colleague",
                         password_hash="synthetic", is_active=True)
            db.add(owner)
            await db.flush()
            agent = await db.get(Agent, context.agent_id)
            agent.owner_user_id = owner.id
            agent.creator_id = owner.id
            db.add(AgentPermission(agent_id=agent.id, tenant_id=user.tenant_id,
                                   scope_type="user", scope_id=user.id, access_level="use"))
            await db.commit()
    context = replace(context, tool_call_id="first-create-call")
    first = await _direct_call(context, first_task="List three weekly highlights")
    assert first.get("draft_status") == "confirmed", first
    retry = await _direct_call(replace(context, tool_call_id="retried-create-call"))
    assert retry["blueprint_id"] == first["blueprint_id"]
    assert retry["provisioning_task_id"] == first["provisioning_task_id"]

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    async with owner_sessionmaker() as db:
        job = await db.get(RuntimeTask, uuid.UUID(first["provisioning_task_id"]))
        job.status = "running"
        source = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
        source.status = "completed"  # Creation never waits for its first task.
        await db.commit()
    assert await execute_claimed_hr_provisioning(job.id) == "completed"
    assert await execute_claimed_hr_provisioning(job.id) == "completed"
    replay = await _direct_call(replace(context, tool_call_id="network-replay"))
    assert replay["blueprint_id"] == first["blueprint_id"]
    async with owner_sessionmaker() as db:
        drafts = list(await db.scalars(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id)))
        assert len(drafts) == 1
        draft = drafts[0]
        employee = await db.get(Agent, draft.created_agent_id)
        assert employee.owner_user_id == user.id and employee.creator_id == user.id
        assert employee.primary_model_id == model_id and draft.blueprint_json["permission_scope"] == "self"
        child = await db.get(RuntimeTask, uuid.uuid5(draft.id, "explicit-first-task.v1"))
        assert child.child_agent_id == employee.id and child.root_user_id == user.id
        assert child.prompt == "List three weekly highlights"
        assert child.child_session_id != context.session_id
        session = await db.get(ChatSession, uuid.UUID(child.child_session_id))
        assert session.agent_id == employee.id and session.user_id == user.id
        assert session.runtime_task_id == child.id
        assert session.title == "List three weekly highlights"
        assert draft.creation_idempotency_key == f"direct-employee:{input_id}"
        children = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == user.tenant_id, RuntimeTask.task_type == "delegation")))
        assert children == [child]
        employees = list(await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id)))
        assert len(employees) == 2


async def test_d1_no_company_policy_needs_no_extra_confirmation(direct_origin, owner_sessionmaker):
    from app.models.capability_policy import CapabilityPolicy
    from app.services.capability_gate import check_capability

    user, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(
            CapabilityPolicy.tenant_id == user.tenant_id, CapabilityPolicy.capability == "agent.employee.create"))
        await db.delete(policy)
        await db.commit()
        decision = await check_capability(db, user.tenant_id, context.agent_id, "create_employee_from_request")
        assert decision.allowed and not decision.denied
    assert (await _direct_call(context)).get("draft_status") == "confirmed"


@pytest.mark.parametrize("task_type", ["delegation", "trigger"])
async def test_d1_background_cannot_borrow_a_human_input(direct_origin, owner_sessionmaker, task_type):
    _, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
        run.task_type = task_type
        await db.commit()
    result = await _direct_call(context)
    assert result.get("error") == "direct_human_input_required", result


@pytest.mark.parametrize("source", ["hr_agent_handoff", "file", "tool_result", "trigger"])
async def test_d1_nonhuman_sources_cannot_authorize_creation(direct_origin, owner_sessionmaker, source):
    from app.models.session_v2 import SessionCommand, SessionTurnInput

    _, context, _, input_id = direct_origin
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id)
        command.target_json = {"runtime_metadata": {"source": source}}
        await db.commit()
    result = await _direct_call(context)
    assert result.get("error") == "direct_human_input_required", result


async def test_d1_guard_deny_returns_its_company_reason(direct_origin, owner_sessionmaker):
    from app.models.guard_policy import GuardPolicy

    user, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        db.add(GuardPolicy(tenant_id=user.tenant_id, zone_guard={"tool_rules": [{
            "tools": ["create_employee_from_request"], "decision": "deny", "reason": "公司暂时关闭新建数字员工。",
        }]}))
        await db.commit()
    result = await _direct_call(context)
    assert result["error"] == "capability_denied"
    assert result["message"] == "公司暂时关闭新建数字员工。"


@pytest.mark.parametrize('policy_kind', ['capability', 'guard'])
async def test_d1_company_approval_resumes_creation_in_place(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, policy_kind,
):
    from app.models.audit import ApprovalRequest
    from app.models.capability_policy import CapabilityPolicy
    from app.models.guard_policy import GuardPolicy
    from app.models.user import User
    from app.services import agent_tools
    from app.services.approval_service import approval_service
    from app.services.approval_execution_runtime import execute_claimed_approval_execution
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from tests.integration.test_m1g_originated_creation_postgres import _direct_arguments
    import json

    user, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        if policy_kind == 'capability':
            policy = await db.scalar(select(CapabilityPolicy).where(
                CapabilityPolicy.tenant_id == user.tenant_id, CapabilityPolicy.capability == 'agent.employee.create'))
            policy.requires_approval = True
        else:
            db.add(GuardPolicy(tenant_id=user.tenant_id, zone_guard={'tool_rules': [{
                'tools': ['create_employee_from_request'], 'decision': 'require_approval', 'reason': '新建数字员工需要公司批准。',
            }]}))
        admin = User(id=uuid.uuid4(), tenant_id=user.tenant_id, username=f'admin-{uuid.uuid4().hex}',
                     email=f'{uuid.uuid4().hex}@d1.test', display_name='Reviewer', password_hash='synthetic',
                     role='org_admin', is_active=True)
        db.add(admin)
        await db.commit()
    monkeypatch.setattr('app.tools.resolver.async_session', app_user_sessionmaker)
    monkeypatch.setattr('app.tools.governance_resolver.async_session', app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, '_TOOL_RUNTIME_SERVICE', None)
    context = await creation._seal_direct_call(context, _direct_arguments())
    blocked = await agent_tools._get_tool_runtime_service().execute(
        'create_employee_from_request', _direct_arguments(), agent_id=context.agent_id, user_id=user.id,
        session_id=context.session_id, runtime_task_id=context.runtime_task_id,
        tool_call_id=context.tool_call_id, emit_runtime_hooks=False)
    assert json.loads(str(blocked))['status'] == 'approval_required', str(blocked)
    async with owner_sessionmaker() as db:
        assert list(await db.scalars(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id))) == []
        approval = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.agent_id == context.agent_id))
        assert approval.requested_by == user.id and approval.consumed_at is None
        result = await approval_service.resolve_approval(db, approval.id, admin, 'approve')
        job_id = result.execution_task_id
        await db.commit()
    async with app_user_sessionmaker() as db:
        from app.database import pin_rls_tenant_context

        await pin_rls_tenant_context(db, user.tenant_id)
        claims = await RuntimeTaskClaimService(db=db, worker_id='d1-company-approval',
            task_types=('approval_execution',)).claim_available()
    assert [row.id for row in claims] == [job_id]
    assert await run_claimed_runtime_task(execute_claimed_approval_execution(job_id), task_id=job_id,
        claim_version=claims[0].claim_version, worker_id='d1-company-approval', lease_seconds=180) == 'succeeded'
    async with owner_sessionmaker() as db:
        draft = await db.scalar(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id))
        assert draft.session_id == uuid.UUID(context.session_id) and draft.requested_by_user_id == user.id
        assert draft.status == 'confirmed' and draft.provisioning_task_id is not None
