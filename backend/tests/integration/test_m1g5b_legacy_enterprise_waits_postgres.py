"""Recover the old enterprise-as-local-permission journal without executing its ticket."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.audit import ApprovalRequest
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.guard_policy import GuardPolicy
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.models.user import User
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize(
    "ticket_state", ["pending", "succeeded", "needs_reconciliation", "mixed_local", "uncertain_sibling", "bad_binding"]
)
async def test_worker_recovers_original_round_without_replaying_enterprise_execution(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    ticket_state,
):
    from app.config import get_settings
    from app.core.execution_context import ExecutionIdentity
    from app.services import agent_tools, session_approval
    from app.services.runtime_task_worker import expire_session_permission_requests_once
    from app.services.session_model_round import prepare_model_request, seal_model_response, commit_sealed_model_round
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation
    from app.services.workspace_resource_authority import register_workspace_path

    tenant_id, user_id, agent_id, session_id, run_id = await _seed_session(owner_sessionmaker, active_run=True)
    for module in ["app.database", "app.tools.resolver", "app.tools.governance_resolver"]:
        monkeypatch.setattr(f"{module}.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    calls = []

    async def no_wakeup(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    workspace = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    async with owner_sessionmaker() as db:
        await register_workspace_path(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            path="workspace/requested.md",
            owner_user_id=user_id,
            root_session_id=session_id,
            source="synthetic-test-fixture",
        )
        db.add(
            GuardPolicy(
                tenant_id=tenant_id,
                zone_guard={"tool_rules": [{"tools": ["read_file"], "decision": "require_approval"}]},
            )
        )
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        from app.api import files
        from app.models.workspace_resource import WorkspaceResourceManifest

        saved = await files.write_file(
            agent_id,
            "workspace/requested.md",
            files.FileWrite(content="Original scoped resource."),
            current_user=await db.get(User, user_id),
            db=db,
        )
        assert saved["status"] == "ok" and saved["content_revision"] == 1
        assert (
            await db.scalar(
                select(WorkspaceResourceManifest.root_session_id).where(
                    WorkspaceResourceManifest.tenant_id == tenant_id,
                    WorkspaceResourceManifest.agent_id == agent_id,
                    WorkspaceResourceManifest.path == "workspace/requested.md",
                )
            )
            == session_id
        )
        args, call_id, turn_id = {"path": "workspace/requested.md"}, f"legacy-{run_id.hex}", f"turn-{run_id.hex}"
        provider_calls = [
            {"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": json.dumps(args)}}
        ]
        sibling_args = {"path": "workspace/another.md", "content": "A separate draft"}
        if ticket_state in {"mixed_local", "uncertain_sibling"}:
            provider_calls.append(
                {
                    "id": call_id + "-sibling",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps(sibling_args)},
                }
            )
        wire = {
            "messages": [{"role": "user", "content": "Read the scoped resource"}],
            "tools": [{"type": "function", "function": {"name": c["function"]["name"]}} for c in provider_calls],
            "max_tokens": 8192,
        }
        request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            continuation_index=0,
            messages=wire["messages"],
            tools=wire["tools"],
            provider="openai",
            model="synthetic-no-network",
            wire_request=wire,
            attempt_owner="legacy-enterprise-test",
        )
        await seal_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={
                "content": None,
                "tool_calls": provider_calls,
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )
        await commit_sealed_model_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
        )
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            provider_tool_use_id=call_id,
            tool_name="read_file",
            arguments=args,
        )
        await db.commit()
        trace = {}
        result = await agent_tools._get_tool_runtime_service().execute(
            "read_file",
            args,
            agent_id=agent_id,
            user_id=user_id,
            session_id=str(session_id),
            runtime_task_id=str(run_id),
            turn_id=turn_id,
            execution_identity=ExecutionIdentity("delegated_user", user_id, "Original requester"),
            tool_call_id=call_id,
            emit_runtime_hooks=False,
            trace_metadata_sink=trace,
        )
        payload = json.loads(str(result))
        assert payload["status"] == "approval_required"
        approval_id = uuid.UUID(payload["approval_id"])

        async def old_classifier(*_args, **_kwargs):
            return None

        # Reproduce the exact pre-G5a branch and its durable journal, with a real ticket.
        with monkeypatch.context() as old:
            old.setattr(session_approval, "invocation_enterprise_approval", old_classifier)
            await complete_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation.id,
                provider_result_content=str(result),
                execution_evidence=_execution_evidence(trace),
                effective_arguments=trace["effective_arguments"],
            )
        if ticket_state in {"mixed_local", "uncertain_sibling"}:
            sibling = await prepare_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                provider_request_id=request_id,
                provider_tool_use_id=call_id + "-sibling",
                tool_name="write_file",
                arguments=sibling_args,
            )
            if ticket_state == "mixed_local":
                from app.services.approval_ticket import hash_tool_input

                await complete_tool_invocation(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    invocation_id=sibling.id,
                    provider_result_content='{"status":"session_permission_required"}',
                    execution_evidence={
                        "schema": "hive.tool_execution_evidence.v1",
                        "tool_decision": {
                            "outcome": "require_approval",
                            "input_hash": hash_tool_input("write_file", sibling_args),
                            "approval_id": str(uuid.uuid4()),
                            "decision_id": "separate-local-permission",
                        },
                    },
                    effective_arguments=sibling_args,
                )
            else:
                from app.services.session_tool_runtime import mark_tool_effect_started

                await mark_tool_effect_started(
                    db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, invocation_id=sibling.id
                )
        task = await db.get(RuntimeTask, run_id)
        task.status = "suspended"
        task.metadata_json = {
            **task.metadata_json,
            "interactive_pause": {"schema": "hive.tool_permission_wait.v1", "invocation_ids": [str(invocation.id)]},
        }
        await db.commit()
        approval = await db.get(ApprovalRequest, approval_id)
        if ticket_state == "succeeded":
            from app.services.approval_service import approval_service

            await approval_service.resolve_approval(db, approval_id, await db.get(User, user_id), "approve")
        elif ticket_state == "needs_reconciliation":
            approval.status = "approved"
            approval.execution_status = ticket_state
            approval.consumed_at = datetime.now(timezone.utc)
            approval.execution_receipt = {"side_effect_state": "unknown", "automatic_replay": False}
        elif ticket_state == "bad_binding":
            approval.execution_envelope = {**approval.execution_envelope, "session_id": str(uuid.uuid4())}
            invocation.permission_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db.commit()
    if ticket_state == "succeeded":
        from app.services.approval_execution_runtime import execute_claimed_approval_execution
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import run_claimed_runtime_task

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            claimed = await RuntimeTaskClaimService(
                db=db, worker_id="legacy-approved", task_types=("approval_execution",)
            ).claim_available()
        assert len(claimed) == 1
        assert (
            await run_claimed_runtime_task(
                execute_claimed_approval_execution(claimed[0].id),
                task_id=claimed[0].id,
                claim_version=claimed[0].claim_version,
                worker_id="legacy-approved",
                lease_seconds=180,
            )
            == "succeeded"
        )
    async with owner_sessionmaker() as db:
        before = await db.get(ApprovalRequest, approval_id)
        consumed_at, receipt = before.consumed_at, dict(before.execution_receipt or {})
    assert (
        await expire_session_permission_requests_once(worker_id="legacy-recover", session_factory=app_user_sessionmaker)
        == 0
    )
    assert (
        await expire_session_permission_requests_once(worker_id="legacy-recover", session_factory=app_user_sessionmaker)
        == 0
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        recovered = await db.get(SessionToolInvocation, invocation.id)
        task = await db.get(RuntimeTask, run_id)
        approval = await db.get(ApprovalRequest, approval_id)
        if ticket_state == "bad_binding":
            assert recovered.result_event_id is None and recovered.permission_state == "waiting"
            assert task.status == "suspended" and approval.execution_status == "pending"
            assert approval.consumed_at is None
            assert len((await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))).all()) == 1
            return
        assert recovered.result_event_id is not None
        assert recovered.permission_state == "cancelled" and recovered.effect_state == "prepared_not_started"
        expected_ticket_state = (
            ticket_state if ticket_state in {"pending", "succeeded", "needs_reconciliation"} else "pending"
        )
        assert approval.execution_status == expected_ticket_state and approval.consumed_at == consumed_at
        assert dict(approval.execution_receipt or {}) == receipt
        assert len((await db.scalars(select(ApprovalRequest).where(ApprovalRequest.tenant_id == tenant_id))).all()) == 1
        assert len(
            (
                await db.scalars(
                    select(RuntimeTask).where(
                        RuntimeTask.tenant_id == tenant_id, RuntimeTask.task_type == "approval_execution"
                    )
                )
            ).all()
        ) == (1 if ticket_state == "succeeded" else 0)
        if ticket_state == "uncertain_sibling":
            uncertain = await db.get(SessionToolInvocation, sibling.id)
            assert task.status == "needs_reconciliation" and uncertain.effect_state == "effect_started"
            assert uncertain.result_event_id is None
            assert task.metadata_json["session_permission_reconciliation"]["invocation_id"] == str(uncertain.id)
            return
        if ticket_state == "mixed_local":
            local = await db.get(SessionToolInvocation, sibling.id)
            assert task.status == "suspended" and local.permission_state == "waiting" and local.result_event_id is None
            from app.services.session_permission_runtime import resolve_session_tool_permission
            from app.services.session_v2_persistence import resolve_session_mutation_authority

            authority = await resolve_session_mutation_authority(
                db,
                user=await db.get(User, user_id),
                agent_id=agent_id,
                session_id=session_id,
                action="respond_tool_permission",
            )
            await resolve_session_tool_permission(
                db, authority=authority, permission_request_id=local.permission_item_id, decision="deny"
            )
            await db.refresh(task)
        assert task.status == "resumable" and task.root_user_id == user_id and task.prompt == "active"
        from app.services.web_chat_runtime import _session_permission_resume_history

        messages, round_index, _usage = await _session_permission_resume_history(db, task)
        assert round_index == 1 and len(messages) == (3 if ticket_state == "mixed_local" else 2)
        handoff = json.loads(messages[1]["content"])
        assert handoff["approval_id"] == str(approval_id) and handoff["execution_status"] == expected_ticket_state
        events = (
            await db.scalars(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == session_id, ChatTranscriptEvent.item_kind == "tool_result"
                )
            )
        ).all()
        assert len(events) == (2 if ticket_state == "mixed_local" else 1)
