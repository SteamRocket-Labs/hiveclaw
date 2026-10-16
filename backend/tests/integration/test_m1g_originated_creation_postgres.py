"""Original-work creation uses the canonical HR ledger, never a second runtime."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.hr_creation import router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.tools.handlers.hr import _build_blueprint_preview_payload, preview_agent_blueprint
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def direct_origin(origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch):
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.tenant_setting import TenantSetting

    user, context = origin
    command_id, input_id, model_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=model_id, tenant_id=user.tenant_id, label="Approved direct model", provider="openai",
                        model="synthetic-no-network", api_key_encrypted="synthetic", base_url="http://127.0.0.1:1", enabled=True))
        await db.flush()
        db.add(TenantSetting(tenant_id=user.tenant_id, key="default_model_id", value={"model_id": str(model_id)}))
        db.add(CapabilityPolicy(tenant_id=user.tenant_id, capability="agent.employee.create", allowed=True,
                                requires_approval=False))
        db.add(CapabilityPolicy(tenant_id=user.tenant_id, capability="agent.message.send", allowed=True,
                                requires_approval=False))
        db.add(SessionCommand(id=command_id, tenant_id=user.tenant_id, principal_type="user", principal_id=user.id,
                              session_id=uuid.UUID(context.session_id), namespace="human_input", command_kind="start_turn",
                              idempotency_key=str(input_id), request_hash="a" * 64, target_hash="b" * 64,
                              request_json={"content": "Create a research employee"},
                              target_json={"runtime_metadata": {"source": "session_input_api"}},
                              status="applied", receipt_ref=f"input:{input_id}"))
        await db.flush()
        db.add(SessionTurnInput(id=input_id, tenant_id=user.tenant_id, session_id=uuid.UUID(context.session_id),
                                command_id=command_id, intent="start_turn", content_parts_json=[{"type": "text", "text": "Create a research employee"}],
                                content_hash="c" * 64, target_run_id=uuid.UUID(context.runtime_task_id),
                                queue_priority="now", queue_ordinal=1, status="bound"))
        run = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
        run.metadata_json = {"session_v2_input_id": str(input_id), "session_v2_command_id": str(command_id)}
        agent = await db.get(Agent, context.agent_id)
        agent.primary_model_id = model_id
        await db.commit()

    async def no_wakeup(**_kwargs):
        return None

    class SyntheticMemoryTransport:
        async def stream(self, **_kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(content=json.dumps({"summary": "Synthetic first-task session summary."}))

        async def close(self):
            pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr("app.services.runtime_task_service.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda *_args: SyntheticMemoryTransport())
    return user, context, model_id, input_id


def _direct_arguments(**overrides):
    return {"name": "Research colleague", "role_description": "Prepare referenced research drafts",
            "core_outputs": ["Research draft"], "boundaries": "Draft only; never send externally", **overrides}


async def _direct_call(context, **overrides):
    from app.tools.handlers.hr import create_employee_from_request

    return json.loads(await create_employee_from_request(ToolExecutionRequest(
        tool_name="create_employee_from_request", context=context, arguments=_direct_arguments(**overrides),
    )))


async def test_direct_request_queues_canonical_job_once_without_fake_confirmation(direct_origin, owner_sessionmaker):
    user, context, model_id, input_id = direct_origin
    first = await _direct_call(context, first_task="Draft a sourced comparison")
    assert first.get("draft_status") == "confirmed", first
    second = await _direct_call(context, name="Retry must not create another employee")
    assert second["blueprint_id"] == first["blueprint_id"]
    assert second["provisioning_task_id"] == first["provisioning_task_id"]
    assert first["confirmed_at"] is None and first["confirmed_by_user_id"] is None
    assert first["creation_authorization"]["mode"] == "policy_direct"
    assert first["creation_flow"]["mode"] == "policy_direct" and first["confirmation_requirements"]["required"] is False
    assert first["creation_authorization"]["input_id"] == str(input_id)
    assert first["blueprint"]["approved_model_id"] == str(model_id)
    assert first["blueprint"]["permission_scope"] == "self"
    assert first["blueprint"]["first_task_autostart"] is False
    assert first["blueprint"]["triggers"] == [] and first["blueprint"]["skill_names"] == []
    async with owner_sessionmaker() as db:
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 1


@pytest.mark.parametrize("change, expected", [
    ("relayed", "direct_human_input_required"), ("wrong_requester", "direct_human_input_required"),
    ("no_input", "direct_human_input_required"), ("company_deny", "capability_denied"),
    ("approval", "approval_required"), ("no_model", "approved_model_unavailable"),
])
async def test_direct_creation_keeps_authority_and_defaults_real(direct_origin, owner_sessionmaker, change, expected):
    from app.models.capability_policy import CapabilityPolicy
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.tenant_setting import TenantSetting

    user, context, _, input_id = direct_origin
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id)
        if change == "relayed":
            command.target_json = {"runtime_metadata": {"source": "hr_agent_handoff"}}
        elif change == "wrong_requester":
            command.principal_id = uuid.uuid4()
        elif change == "no_input":
            run = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
            run.metadata_json = {}
        elif change in {"company_deny", "approval"}:
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == user.tenant_id,
                                                                   CapabilityPolicy.capability == "agent.employee.create"))
            policy.allowed = change != "company_deny"
            policy.requires_approval = change == "approval"
        else:
            setting = await db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == user.tenant_id,
                                                                 TenantSetting.key == "default_model_id"))
            await db.delete(setting)
        await db.commit()
    result = await _direct_call(context)
    assert result.get("error") == expected, result
    async with owner_sessionmaker() as db:
        assert (await db.scalars(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id))).all() == []


@pytest.mark.parametrize("has_ingress", [True, False])
async def test_direct_creation_from_mapped_feishu_requires_its_durable_ingress(
    direct_origin, owner_sessionmaker, has_ingress,
):
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.session_v2 import SessionCommand, SessionTurnInput

    user, context, _, input_id = direct_origin
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, uuid.UUID(context.session_id))
        session.source_channel = "feishu"
        session.runtime_source = "channel_chat"
        row = await db.get(SessionTurnInput, input_id)
        command = await db.get(SessionCommand, row.command_id)
        command.target_json = {"runtime_metadata": {"source": "feishu", "channel_ingress_event_id": str(input_id)}}
        if has_ingress:
            db.add(ChannelIngressEvent(id=input_id, tenant_id=user.tenant_id, agent_id=context.agent_id,
                                       provider="feishu", installation_ref="synthetic-installation", provider_event_id=str(input_id),
                                       handler_key="synthetic-verified-feishu", payload_digest="d" * 64, payload_json={},
                                       status="processed", result_session_id=session.id,
                                       result_runtime_task_id=uuid.UUID(context.runtime_task_id)))
        await db.commit()
    result = await _direct_call(context)
    if has_ingress:
        assert result.get("draft_status") == "confirmed", result
        assert result["session_id"] == context.session_id
    else:
        assert result.get("error") == "direct_human_input_required", result


async def test_direct_worker_rechecks_company_policy_before_any_creation(direct_origin, owner_sessionmaker):
    from app.models.capability_policy import CapabilityPolicy
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    user, context, _, _ = direct_origin
    initial = await _direct_call(context)
    task_id = uuid.UUID(initial["provisioning_task_id"])
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == user.tenant_id,
                                                               CapabilityPolicy.capability == "agent.employee.create"))
        policy.allowed = False
        task = await db.get(RuntimeTask, task_id)
        task.status = "running"
        await db.commit()
    assert await execute_claimed_hr_provisioning(task_id) == "failed"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        assert draft.failure_code == "capability_denied" and draft.created_agent_id is None
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 1


async def test_first_task_recovery_cannot_replace_the_accepted_request(direct_origin, owner_sessionmaker, monkeypatch):
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    user, context, model_id, _ = direct_origin
    initial = await _direct_call(context, first_task="Draft a comparison")
    task_id = uuid.UUID(initial["provisioning_task_id"])
    async with owner_sessionmaker() as db:
        employee = Agent(tenant_id=user.tenant_id, creator_id=user.id, owner_user_id=user.id,
                         name="Provisioned fixture", primary_model_id=model_id, agent_type="native", agent_class="internal_tenant")
        db.add(employee)
        await db.flush()
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        draft.status = "completed"
        draft.created_agent_id = employee.id
        draft.preview_json = {**draft.preview_json, "creation_authorization": {
            **draft.preview_json["creation_authorization"], "first_task": "A different unauthorized task",
        }}
        task = await db.get(RuntimeTask, task_id)
        task.status = "running"
        await db.commit()

    async def forbidden(**_kwargs):
        pytest.fail("Changed first-task authorization must never reach delegation")

    monkeypatch.setattr("app.agents.orchestrator.delegate_async", forbidden)
    assert await execute_claimed_hr_provisioning(task_id) == "needs_reconciliation"
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        assert "policy_snapshot_mismatch" in task.metadata_json["authority_issues"]
        assert task.metadata_json["side_effect_risk"] == "journaled"


@pytest.mark.parametrize("first_task_mode", ["none", "requested", "denied", "unapproved", "question"])
async def test_direct_worker_pins_model_and_creates_native_employee_without_boot(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, first_task_mode,
):
    from app.models.llm import LLMModel
    from app.models.tenant_setting import TenantSetting
    from app.models.trigger import AgentTrigger
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning
    from app.services.agent_manager import agent_manager

    user, context, model_id, _ = direct_origin
    initial = await _direct_call(context, first_task="Draft a sourced comparison" if first_task_mode != "none" else "")
    assert initial.get("draft_status") == "confirmed", initial
    task_id = uuid.UUID(initial["provisioning_task_id"])
    other_model = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=other_model, tenant_id=user.tenant_id, label="Different default", provider="openai",
                        model="different-synthetic", api_key_encrypted="synthetic", base_url="http://127.0.0.1:1", enabled=True))
        await db.flush()
        setting = await db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == user.tenant_id,
                                                             TenantSetting.key == "default_model_id"))
        setting.value = {"model_id": str(other_model)}
        task = await db.get(RuntimeTask, task_id)
        task.status = "running"
        task.claimed_by = "direct-test-worker"
        task.claim_version = 1
        await db.commit()

    async def no_model(*_args, **_kwargs):
        return None, "synthetic_transport"

    async def refinement(**kwargs):
        return {"role_description": kwargs["role_description"], "boundaries": kwargs["boundaries"]}

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_model)
    monkeypatch.setattr("app.tools.handlers.hr._refine_soul_inputs", refinement)
    if first_task_mode in {"denied", "unapproved"}:
        from app.models.capability_policy import CapabilityPolicy
        from app.services.governance_capability_taxonomy import CAPABILITY_MAP

        async with owner_sessionmaker() as db:
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == user.tenant_id,
                                                                   CapabilityPolicy.capability == CAPABILITY_MAP["delegate_to_agent"]))
            if first_task_mode == "denied":
                policy.allowed = False
            else:
                await db.delete(policy)
            await db.commit()
        assert await execute_claimed_hr_provisioning(task_id) == "failed"
        async with _client(user, app_user_sessionmaker) as client:
            failed = await client.get(f"/api/agents/{context.agent_id}/hr-creation-drafts/{initial['blueprint_id']}")
            assert failed.json()["draft_status"] == "completed"
            assert failed.json()["provisioning"]["first_task"]["error"] == (
                "capability_denied" if first_task_mode == "denied" else "approval_required"
            )
            assert failed.json()["recovery"]["can_retry"] is True
        async with owner_sessionmaker() as db:
            policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == user.tenant_id,
                                                                   CapabilityPolicy.capability == CAPABILITY_MAP["delegate_to_agent"]))
            if policy is None:
                db.add(CapabilityPolicy(tenant_id=user.tenant_id, capability=CAPABILITY_MAP["delegate_to_agent"],
                                        allowed=True, requires_approval=False))
            else:
                policy.allowed = True
            creation_policy = await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == user.tenant_id,
                                                                            CapabilityPolicy.capability == "agent.employee.create"))
            creation_policy.allowed = False
            await db.commit()
        async with _client(user, app_user_sessionmaker) as client:
            retry = await client.post(f"/api/agents/{context.agent_id}/hr-creation-drafts/{initial['blueprint_id']}/retry")
            assert retry.status_code == 200, retry.text
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, task_id)
            task.status = "running"
            task.claimed_by = "direct-retry-worker"
            await db.commit()
    assert await execute_claimed_hr_provisioning(task_id) == "completed"
    assert await execute_claimed_hr_provisioning(task_id) == "completed"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(initial["blueprint_id"]))
        employee = await db.get(Agent, draft.created_agent_id)
        assert draft.confirmed_at is None and draft.confirmed_by_user_id is None
        assert employee.primary_model_id == model_id
        assert employee.owner_user_id == user.id and employee.agent_type == "native"
        assert (agent_manager._agent_dir(employee.id) / "soul.md").is_file()
        assert (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == employee.id,
                                                         AgentTrigger.name == "first_task_boot"))).all() == []
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 2
        if first_task_mode != "none":
            from app.models.runtime_notification_outbox import RuntimeNotificationOutbox

            first = draft.provisioning_json["first_task"]
            assert first["status"] == "dispatched", first
            child_id = uuid.UUID(first["runtime_task_id"])
            child = await db.get(RuntimeTask, child_id)
            assert child.prompt == "Draft a sourced comparison"
            assert child.parent_session_id == context.session_id and child.root_user_id == user.id
            assert child.child_agent_id == employee.id
            assert (await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == user.tenant_id,
                                                              RuntimeTask.task_type == "delegation"))).all() == [child]
            receipts = (await db.scalars(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.tenant_id == user.tenant_id,
                RuntimeNotificationOutbox.source_kind == "employee_creation"))).all()
            assert len(receipts) == (2 if first_task_mode in {"denied", "unapproved"} else 1)
            assert sum(row.terminal_status == "completed" for row in receipts) == 1
            assert all(row.parent_session_id == uuid.UUID(context.session_id) for row in receipts)
            # Crash after child commit, before saving its receipt: recover the same child, not another send.
            draft.provisioning_json = {key: value for key, value in draft.provisioning_json.items() if key != "first_task"}
            task = await db.get(RuntimeTask, task_id)
            task.status = "running"
            await db.commit()
    if first_task_mode != "none":
        assert await execute_claimed_hr_provisioning(task_id) == "completed"
        async with owner_sessionmaker() as db:
            children = (await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == user.tenant_id,
                                                                  RuntimeTask.task_type == "delegation"))).all()
            assert len(children) == 1 and children[0].id == child_id
        from app.agents.orchestrator import dispatch_persisted_async_delegation
        from app.runtime.invoker import AgentInvocationResult
        from app.models.activity_log import AgentActivityLog
        from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
        from app.models.runtime_result import RuntimeResultObject
        from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

        invocation_requests = []

        async def synthetic_invocation(request):
            invocation_requests.append(request)
            content = ("Need input: which companies should the comparison cover?" if first_task_mode == "question"
                       else "Synthetic transport result: comparison draft ready.")
            return AgentInvocationResult(content=content, response_complete_payload={
                "agent_id": str(request.agent_id), "session_id": request.session_context.session_id,
                "messages": request.messages, "source": "agent",
                "metadata": {"tenant_id": str(request.tenant_id), "final_response": content},
            })

        monkeypatch.setattr("app.agents.orchestrator.invoke_agent", synthetic_invocation)
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import run_claimed_runtime_task

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, user.tenant_id)
            claimed = await RuntimeTaskClaimService(db=db, worker_id="first-task-worker", task_types=("delegation",)).claim_available()
        assert [task.id for task in claimed] == [child_id]
        assert await run_claimed_runtime_task(
            dispatch_persisted_async_delegation(child_id.hex, wait_for_completion=True),
            task_id=child_id, claim_version=claimed[0].claim_version, worker_id="first-task-worker", lease_seconds=120,
        )
        assert len(invocation_requests) == 1
        assert invocation_requests[0].agent_id == employee.id and invocation_requests[0].user_id == user.id
        from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

        processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker,
                                                              data_root=agent_manager._agent_dir(employee.id).parent)
        boundaries = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=user.tenant_id, worker_id="first-task-terminal", canonical_validator=processor.validate,
            process_callback=processor, task_types=("delegation",),
        )
        assert boundaries["delivered"] == 1, boundaries
        async with owner_sessionmaker() as db:
            child = await db.get(RuntimeTask, child_id)
            assert child.status == "completed", child.result_summary
            results = (await db.scalars(select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.tenant_id == user.tenant_id,
                RuntimeNotificationOutbox.source_kind == "a2a_delegation"))).all()
            assert len(results) == 1 and results[0].parent_session_id == uuid.UUID(context.session_id)
            result_object = await db.get(RuntimeResultObject, results[0].result_object_id)
            assert invocation_requests and (
                b"which companies" if first_task_mode == "question" else b"comparison draft ready"
            ) in result_object.payload_bytes
            activity = await db.scalar(select(AgentActivityLog).where(AgentActivityLog.agent_id == context.agent_id,
                                                                     AgentActivityLog.action_type == "delegation_queued"))
            assert activity.owner_user_id == user.id and activity.root_session_id == uuid.UUID(context.session_id)
            source_run = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
            source_run.status = "completed"
            await db.commit()
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        claimed = await service.claim_batch(worker_id=f"first-task-{child_id}", limit=100)
        own = [item for item in claimed if item.tenant_id == user.tenant_id]
        pages = await service.prepare_integration_pages(worker_id=f"first-task-{child_id}", claimed=own)
        assert pages
        for page in pages:
            receipt = await service._deliver_page(page)
            assert receipt.get("ok") is True, receipt
        async with owner_sessionmaker() as db:
            returns = (await db.scalars(select(RuntimeTask).where(
                RuntimeTask.tenant_id == user.tenant_id, RuntimeTask.parent_session_id == context.session_id,
                RuntimeTask.id != uuid.UUID(context.runtime_task_id),
                RuntimeTask.metadata_json["integration_page_id"].astext.in_([str(page.id) for page in pages]),
            ))).all()
            assert len(returns) == 1
            assert returns[0].task_type == "web_chat_turn"
            assert returns[0].parent_session_id == context.session_id and returns[0].root_user_id == user.id
            assert "runtime-result://" in returns[0].prompt
    replay = await _direct_call(context)
    assert replay["created_agent_id"] == str(employee.id)
    if destination := os.environ.get("HIVE_TEST_WIRE_DIR"):
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / f"direct-native-soul-{first_task_mode}.md").write_bytes(
            (agent_manager._agent_dir(employee.id) / "soul.md").read_bytes()
        )
        (Path(destination) / f"direct-creation-{first_task_mode}.json").write_text(json.dumps({
            "initial": initial, "completed": replay,
            "blocked": failed.json() if first_task_mode in {"denied", "unapproved"} else None,
        }, indent=2))


@pytest.fixture
async def origin(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Origin company", slug=f"origin-{tenant_id.hex}"))
        user = User(id=user_id, tenant_id=tenant_id, username=f"origin-{user_id.hex}",
                    email=f"{user_id.hex}@origin.test", password_hash="synthetic", display_name="Origin owner", is_active=True)
        db.add(user)
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, creator_id=user_id, owner_user_id=user_id,
                     name="My Assistant", role_description="Assist the owner", status="idle",
                     agent_type="native", agent_class="internal_tenant"))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id,
                           source_channel="web", runtime_source="web_chat", actor_type="user",
                           session_kind="human_chat", visibility_scope="direct_user", listed_surface="chat"))
        db.add(RuntimeTask(id=run_id, tenant_id=tenant_id, task_type="web_chat_turn", status="running",
                           parent_agent_id=agent_id, child_agent_id=agent_id,
                           parent_session_id=str(session_id), child_session_id=str(session_id),
                           root_session_id=str(session_id), root_user_id=user_id,
                           prompt="Create a long-term research employee."))
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    context = ToolExecutionContext(agent_id=agent_id, user_id=user_id, tenant_id=str(tenant_id),
                                   session_id=str(session_id), runtime_task_id=str(run_id),
                                   workspace=tmp_path, emit_runtime_hooks=False)
    return user, context


def _arguments():
    return {"name": "Research colleague", "role_description": "Prepare referenced research drafts",
            "primary_users": ["Owner"], "core_outputs": ["Research draft"],
            "boundaries": "Draft only; never send externally", "focus_content": "Wait for the owner to assign research",
            "permission_scope": "self", "first_task_autostart": False}


def _client(user, sessionmaker):
    async def scoped_db():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, user.tenant_id)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = scoped_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://origin")


async def _draft(owner_sessionmaker, context):
    from app.services.hr_creation_service import upsert_hr_creation_draft

    async with owner_sessionmaker() as db:
        draft = await upsert_hr_creation_draft(
            db, tenant_id=uuid.UUID(context.tenant_id), hr_agent_id=context.agent_id,
            session_id=uuid.UUID(context.session_id), requested_by_user_id=context.user_id,
            preview_payload=_build_blueprint_preview_payload(_arguments()),
        )
        await db.commit()
        return draft.id


async def test_native_preview_remains_in_original_work_without_hr_session(
    origin, owner_sessionmaker,
):
    user, context = origin
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert payload.get("status") == "preview", payload
    assert payload["session_id"] == context.session_id
    assert payload["hr_agent_id"] == str(context.agent_id)
    assert payload["requested_by_user_id"] == str(user.id)
    assert payload["blueprint"]["first_task_autostart"] is False
    async with owner_sessionmaker() as db:
        assert len((await db.scalars(select(ChatSession).where(ChatSession.tenant_id == user.tenant_id))).all()) == 1
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 1


async def test_origin_confirmation_queues_once_and_recovery_discovers_source(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    wakeups = []

    async def wake(**kwargs):
        wakeups.append(kwargs["runtime_task_id"])

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", wake)
    base = f"/api/agents/{context.agent_id}/hr-creation-drafts"
    async with _client(user, app_user_sessionmaker) as client:
        first = await client.post(f"{base}/{draft_id}/confirm", json={"blueprint_version": 1})
        assert first.status_code == 200, first.text
        second = await client.post(f"{base}/{draft_id}/confirm", json={"blueprint_version": 1})
        assert second.status_code == 200, second.text
        assert first.json()["provisioning_task_id"] == second.json()["provisioning_task_id"]
        assert len(wakeups) == 1
        recovered = await client.get("/api/agents/hr-creation-drafts/mine")
        assert recovered.status_code == 200, recovered.text
        assert any(row["blueprint_id"] == str(draft_id) and row["session_id"] == context.session_id
                   for row in recovered.json())
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.confirmed_by_user_id == user.id
        assert draft.provisioning_task_id == wakeups[0]
    from app.tools.handlers.hr import _claim_canonical_hr_blueprint

    claimed = await _claim_canonical_hr_blueprint(ToolExecutionRequest(
        tool_name="create_digital_employee", arguments={"blueprint_id": str(draft_id)}, context=context,
    ))
    assert claimed[0] == draft_id
    assert claimed[2] == user.id
    assert str(claimed[3]) == context.session_id


async def test_read_access_keeps_draft_visible_but_cannot_confirm(
    origin, owner_sessionmaker, app_user_sessionmaker,
):
    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, context.agent_id)
        peer_id = uuid.uuid4()
        db.add(User(id=peer_id, tenant_id=user.tenant_id, username=f"peer-{peer_id.hex}",
                    email=f"{peer_id.hex}@origin.test", password_hash="synthetic", display_name="Peer"))
        await db.flush()
        agent.owner_user_id = peer_id
        db.add(AgentPermission(agent_id=context.agent_id, tenant_id=user.tenant_id,
                               scope_type="user", scope_id=user.id, access_level="read"))
        await db.commit()
    base = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"
    async with _client(user, app_user_sessionmaker) as client:
        visible = await client.get(base)
        assert visible.status_code == 200, visible.text
        denied = await client.post(base + "/confirm", json={"blueprint_version": 1})
        assert denied.status_code == 403, denied.text
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "awaiting_confirmation"
        assert draft.provisioning_task_id is None


async def test_native_preview_cannot_borrow_another_users_session(origin, owner_sessionmaker):
    _, context = origin
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, uuid.UUID(context.session_id))
        session.user_id = None
        await db.commit()
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert payload.get("status") == "error", payload
    async with owner_sessionmaker() as db:
        assert not (await db.scalars(select(HrCreationDraft).where(
            HrCreationDraft.tenant_id == uuid.UUID(context.tenant_id),
        ))).all()


async def test_platform_requester_uses_the_runtime_selected_company(origin, owner_sessionmaker):
    user, context = origin
    async with owner_sessionmaker() as db:
        principal = await db.get(User, user.id)
        principal.role = "platform_admin"
        principal.tenant_id = None
        await db.commit()
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert payload.get("status") == "preview", payload
    assert payload["session_id"] == context.session_id
    assert payload["requested_by_user_id"] == str(user.id)


async def test_provisioning_rechecks_source_access_before_acquiring_a_claim(origin, owner_sessionmaker, app_user_sessionmaker):
    from app.services.hr_creation_service import HrCreationConflict, confirm_hr_creation_draft_record
    from app.tools.handlers.hr import _claim_canonical_hr_blueprint

    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        confirm_hr_creation_draft_record(draft, confirming_user_id=user.id, blueprint_version=1)
        source = await db.get(Agent, context.agent_id)
        source.deactivated_at = datetime.now(timezone.utc)
        await db.commit()
    with pytest.raises(HrCreationConflict) as error:
        await _claim_canonical_hr_blueprint(ToolExecutionRequest(
            tool_name="create_digital_employee", arguments={"blueprint_id": str(draft_id)}, context=context,
        ))
    assert error.value.code == "source_execute_denied"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "confirmed"
        assert draft.claim_token is None
        assert draft.created_agent_id is None

    # A retired source must not hide recoverable drafts from other work.
    from dataclasses import replace
    other_agent_id, other_session_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=other_agent_id, tenant_id=user.tenant_id, creator_id=user.id, owner_user_id=user.id,
                     name="Other assistant", role_description="Other work", status="idle",
                     agent_type="native", agent_class="internal_tenant"))
        await db.flush()
        db.add(ChatSession(id=other_session_id, tenant_id=user.tenant_id, agent_id=other_agent_id, user_id=user.id))
        await db.commit()
    other_draft = await _draft(owner_sessionmaker, replace(context, agent_id=other_agent_id, session_id=str(other_session_id)))
    async with _client(user, app_user_sessionmaker) as client:
        mine = await client.get("/api/agents/hr-creation-drafts/mine")
        assert mine.status_code == 200, mine.text
        assert [row["blueprint_id"] for row in mine.json()] == [str(other_draft)]


@pytest.mark.parametrize("platform", [False, True])
async def test_original_work_drives_real_native_provisioning_and_reopens_the_receipt(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, platform,
):
    from app.models.llm import LLMModel
    from app.models.tenant_setting import TenantSetting
    from app.models.trigger import AgentTrigger
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning
    from app.services.agent_manager import agent_manager

    user, context = origin
    if platform:
        async with owner_sessionmaker() as db:
            principal = await db.get(User, user.id)
            principal.role = "platform_admin"
            principal.tenant_id = None
            await db.commit()
        user.role = "platform_admin"
    model_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=model_id, tenant_id=user.tenant_id, label="Approved synthetic model",
                        provider="openai", model="synthetic-no-network", api_key_encrypted="synthetic", enabled=True))
        await db.flush()
        db.add(TenantSetting(tenant_id=user.tenant_id, key="default_model_id", value={"model_id": str(model_id)}))
        await db.commit()

    async def no_refinement_model(*_args, **_kwargs):
        return None, "synthetic_transport"

    refinement_calls = []

    async def synthetic_refinement(**kwargs):
        refinement_calls.append(kwargs["name"])
        return {"role_description": kwargs["role_description"], "boundaries": kwargs["boundaries"]}

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement_model)
    monkeypatch.setattr("app.tools.handlers.hr._refine_soul_inputs", synthetic_refinement)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    preview = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert preview["status"] == "preview", preview
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{preview['blueprint_id']}"
    async with _client(user, app_user_sessionmaker) as client:
        response = await client.post(path + "/confirm", json={"blueprint_version": preview["blueprint_version"]})
        assert response.status_code == 200, response.text
        confirmed = response.json()
        task_id = uuid.UUID(confirmed["provisioning_task_id"])
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, task_id)
            task.status = "running"
            task.claimed_by = "synthetic-worker"
            task.claim_version = 1
            await db.commit()
        assert await execute_claimed_hr_provisioning(task_id) == "completed"
        assert await execute_claimed_hr_provisioning(task_id) == "completed"
        completed_response = await client.get(path)
        assert completed_response.status_code == 200, completed_response.text
        completed = completed_response.json()
        mine = await client.get("/api/agents/hr-creation-drafts/mine")
        assert mine.status_code == 200 and mine.json() == [], mine.text
    assert completed["draft_status"] == "completed"
    assert completed["session_id"] == context.session_id
    assert completed["hr_agent_id"] == str(context.agent_id)
    created_id = uuid.UUID(completed["created_agent_id"])
    async with owner_sessionmaker() as db:
        employee = await db.get(Agent, created_id)
        assert employee.owner_user_id == user.id
        assert employee.agent_type == "native"
        assert employee.primary_model_id == model_id
        assert (agent_manager._agent_dir(created_id) / "soul.md").is_file()
        boot = (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == created_id,
                                                           AgentTrigger.name == "first_task_boot"))).all()
        assert boot == []
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 2
    assert refinement_calls == ["Research colleague"]
    if destination := os.environ.get("HIVE_TEST_WIRE_DIR"):
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / ("originated-creation-platform.json" if platform else "originated-creation.json")).write_text(json.dumps({
            "scope": {"user_id": str(user.id), "tenant_id": str(user.tenant_id)},
            "preview": preview, "confirmed": confirmed, "completed": completed,
        }, indent=2))


def test_preview_is_discoverable_by_native_employees_but_creation_stays_worker_owned():
    from types import SimpleNamespace
    from app.services.agent_tools import _core_tools_for_agent

    agent = SimpleNamespace(agent_class="internal_tenant", name="My Assistant", agent_type="native")
    names = {tool["function"]["name"] for tool in _core_tools_for_agent(agent)}
    assert "preview_agent_blueprint" in names
    assert "create_digital_employee" not in names
