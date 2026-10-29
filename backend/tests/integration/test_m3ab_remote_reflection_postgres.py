"""Independent reflections use canonical queued work, real SDK execution and recovery."""

import asyncio
from copy import deepcopy
import json
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionInputAdmission, SessionTurnInput
from app.services.runtime_task_claim_service import RuntimeTaskClaimService
from tests.integration import test_m3t_remote_chat_postgres as original
from tests.integration.test_m3q_remote_runtime_postgres import selection
from tests.integration.test_m3u_remote_sdk_context_postgres import sdk_receiver

app_user_engine = original.app_user_engine
app_user_sessionmaker = original.app_user_sessionmaker
machine_api = original.machine_api
migrated_pg_url = original.migrated_pg_url
owner_engine = original.owner_engine
owner_sessionmaker = original.owner_sessionmaker
pg_container = original.pg_container
remote_work = original.remote_work
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "budget_stopped",
        "trigger_revoked",
        "origin_tamper",
        "control",
        "unknown",
        "input_required",
        "confirmed_plan",
    ],
)
async def test_independent_reflection_uses_remote_without_native_fallback_and_keeps_work_authority(
    remote_work, app_user_sessionmaker, monkeypatch, tmp_path, fault
):
    from app.api import chat_sessions, plans, triggers, websocket
    from app.models.agent import Agent
    from app.services import runtime_task_worker, trigger_daemon
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.session_input_dispatch import recover_admitted_session_inputs_once

    seed, api, original_session_id = remote_work
    for router in (chat_sessions.router, plans.router, triggers.router):
        api._transport.app.include_router(router)
    await original.prepare_employee(seed, app_user_sessionmaker, monkeypatch, tmp_path)

    async def no_native(*args, **kwargs):
        raise AssertionError("Independent selected-remote work must not invoke a native model")

    monkeypatch.setattr(websocket, "call_llm", no_native)
    async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
        agent = await db.get(Agent, seed["agent"])
        agent.primary_model_id = agent.fallback_model_id = None
        await db.commit()
    base = f"/agents/{seed['agent']}"
    async with sdk_receiver(tmp_path / "receiver", True, input_required=fault == "input_required") as receiver:
        path = await original.original.configure(seed, api, receiver)
        assert (await api.post(path + "/runtime", json=selection(seed))).status_code == 200
        if fault == "confirmed_plan":
            from app.models.plan_request import AgentPlanRequest
            from app.services.plan_mode_core import compute_plan_hash
            from app.services.plan_mode_service import PlanModeService
            from app.services.plan_mode_handoff import register_scheduled_trigger_handoff

            service = PlanModeService()
            register_scheduled_trigger_handoff(service)
            monkeypatch.setattr(plans, "get_plan_mode_service", lambda: service)
            plan_id = uuid4()
            plan_body = (
                "## Approved remote work\nCompare the complete approved plan with current evidence.\n"
                + "Preserve this exact approved instruction.\n" * 200
                + "DECISIVE_APPROVED_PLAN_TAIL"
            )
            plan_json = {
                "schema": "hive_plan.v1",
                "title": "REMOTE_REFLECTION_ASSIGNMENT: approved routine",
                "objective": "Compare the complete approved plan with current evidence.",
                "plan_markdown": plan_body,
                "steps": [{"order": 1, "description": "Read the approved plan and report current evidence."}],
                "success_criteria": ["The report includes the complete approved instructions."],
                "stop_conditions": ["Stop when authority is unavailable."],
                "wake_policy": {"type": "interval", "config": {"minutes": 30}},
                "handoff": {"target": "scheduled_trigger"},
            }
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                db.add(
                    AgentPlanRequest(
                        id=plan_id,
                        tenant_id=seed["tenant"],
                        agent_id=seed["agent"],
                        requested_by_user_id=seed["owner"],
                        session_id=str(original_session_id),
                        source="web_chat",
                        intent_type="autonomous_wake",
                        original_request="Execute the complete approved plan.",
                        status="awaiting_confirmation",
                        plan_version=1,
                        plan_hash=compute_plan_hash(plan_json),
                        plan_json=plan_json,
                    )
                )
                await db.commit()
            confirmed = await api.post(base + f"/plans/{plan_id}/confirm-and-handoff", json={"plan_version": 1})
            assert confirmed.status_code == 200 and confirmed.json()["status"] == "confirmed", confirmed.text
            assert confirmed.json()["handoff_status"] == "completed", confirmed.text
            trigger_id = confirmed.json()["handoff_payload"]["created_trigger_id"]
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                from app.models.trigger import AgentTrigger
                from app.models.audit import ApprovalRequest

                trigger = await db.get(AgentTrigger, UUID(trigger_id))
                assert trigger.config["created_by"] == str(seed["owner"])
                assert trigger.config["root_session_id"] == str(original_session_id)
                assert trigger.config["authority_state"] == "owned"
                lease = await db.get(ApprovalRequest, UUID(trigger.config["plan_authorization"]["lease_id"]))
                assert lease.requested_by == seed["owner"] and lease.consumed_at is not None
        else:
            saved = await api.post(
                base + "/triggers",
                json={
                    "name": "remote_independent",
                    "type": "interval",
                    "config": {"minutes": 30, **({"toolset": ["read_file"]} if fault == "control" else {})},
                    "reason": "REMOTE_REFLECTION_ASSIGNMENT: compare the current evidence in this original work.",
                    "is_enabled": False,
                },
            )
            assert saved.status_code == 201, saved.text
            recommendation = await api.post(
                base + "/plan-recommendations",
                json={
                    "original_request": "Enable this routine without a separate plan.",
                    "session_id": str(original_session_id),
                    "source": "agent_aware",
                    "action_kind": "enable_autonomous_wake",
                    "tool_name": "trigger_rest",
                },
            )
            assert recommendation.status_code == 201, recommendation.text
            declined = await api.post(base + "/plan-recommendations/" + recommendation.json()["id"] + "/decline")
            assert declined.status_code == 200, declined.text
            enabled = await api.patch(
                base + "/triggers/" + saved.json()["id"],
                json={
                    "is_enabled": True,
                    "plan_mode_decision": "declined",
                    "plan_recommendation_id": declined.json()["id"],
                },
            )
            assert enabled.status_code == 200, enabled.text
            trigger_id = saved.json()["id"]
        fired = await trigger_daemon.fire_trigger_once_now(seed["agent"], trigger_id)
        if fault == "control":
            assert fired["fired"] is False and fired["reason"] == "remote_a2a_trigger_control_unavailable", fired
            assert not (receiver.directory / "consumed-0.json").exists()
            return
        assert fired["fired"], fired
        wrapper_id = UUID(fired["runtime_task_id"])
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            claims = await RuntimeTaskClaimService(
                db=db, worker_id="remote-trigger", task_types=("trigger",)
            ).claim_available()
            claim = next(task for task in claims if task.id == wrapper_id)
        assert runtime_task_worker._dispatch_claimed_task(claim)
        _kind, process = runtime_task_worker._DISPATCHED_TASKS[claim.id.hex]
        await asyncio.wait_for(process, 30)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            wrapper = await db.get(RuntimeTask, wrapper_id)
            assert wrapper.status == "completed" and wrapper.metadata_json["delivery"] == "reflection"
            session_id = UUID(wrapper.child_session_id)
            assert session_id != original_session_id
            input_id = trigger_daemon._trigger_child_run_id(
                wrapper_id, effect="same_session", discriminator=str(session_id)
            )
            row = await db.get(SessionTurnInput, input_id)
            assert row is not None
            admission = await db.scalar(select(SessionInputAdmission).where(SessionInputAdmission.input_id == input_id))
            admission_id = admission.id
            budget_id = wrapper.budget_run_id
            assert budget_id is not None
        from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

        processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=tmp_path)
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="remote-trigger-boundary",
            tenant_id=seed["tenant"],
            task_types=("trigger",),
            canonical_validator=processor.validate,
            process_callback=processor,
        )
        assert boundary["delivered"] == 1, boundary
        await asyncio.sleep(5.1)
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            result = await recover_admitted_session_inputs_once(
                db, worker_id="remote-trigger-input", tenant_id=seed["tenant"], admission_ids=(admission_id,)
            )
            admission = await db.get(SessionInputAdmission, admission_id, populate_existing=True)
            assert result["dispatched"] == 1, (result, admission.dispatch_last_error)
            row = await db.get(SessionTurnInput, input_id, populate_existing=True)
            run_id = row.target_run_id
            task = await db.get(RuntimeTask, run_id)
            assert task.root_user_id == seed["owner"] and task.parent_session_id == str(session_id)
            assert task.budget_run_id == budget_id and task.root_runtime_task_id == wrapper_id
            claims = await RuntimeTaskClaimService(
                db=db, worker_id="remote-scheduled-leaf", task_types=("web_chat_turn",)
            ).claim_available()
            claim = next(item for item in claims if item.id == run_id)
            # Invalidate after the durable claim, so the actual worker must
            # recheck current authority before the remote effect.
            if fault == "budget_stopped":
                from app.models.runtime_budget import RuntimeBudgetRun

                (await db.get(RuntimeBudgetRun, budget_id)).status = "stopped"
            elif fault == "trigger_revoked":
                from app.models.trigger import AgentTrigger

                trigger = await db.get(AgentTrigger, UUID(trigger_id))
                trigger.config = {**trigger.config, "authority_state": "quarantined"}
            elif fault == "origin_tamper":
                metadata = deepcopy(task.metadata_json)
                metadata["remote_a2a"]["trigger_origin"]["wrapper_id"] = str(uuid4())
                task.metadata_json = metadata
            await db.commit()
        if fault == "unknown":
            from app.services.remote_a2a_transport import RemoteA2AEndpoint, RemoteA2AError

            rpc = RemoteA2AEndpoint.rpc

            async def lost_reply(endpoint, *args, **kwargs):
                await rpc(endpoint, *args, **kwargs)
                raise RemoteA2AError("remote_a2a_response_lost", execution_status="unknown")

            monkeypatch.setattr(RemoteA2AEndpoint, "rpc", lost_reply)
        assert runtime_task_worker._dispatch_claimed_task(claim)
        _kind, process = runtime_task_worker._DISPATCHED_TASKS[claim.id.hex]
        await asyncio.wait_for(process, 30)
        if fault in {"budget_stopped", "trigger_revoked", "origin_tamper", "unknown"}:
            assert (receiver.directory / "consumed-0.json").exists() == (fault == "unknown")
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                task = await db.get(RuntimeTask, run_id)
                assert task.status == "needs_reconciliation", task.metadata_json
                assert task.metadata_json["remote_a2a"]["operations"][-1]["phase"] == (
                    "unknown" if fault == "unknown" else "not_started"
                )
            view = await api.get(path + f"/tasks/{run_id}")
            assert view.status_code == 200 and "abandon" in view.json()["recovery_actions"], view.text
            abandoned = await api.post(
                path + f"/tasks/{run_id}/operations",
                json={
                    "expected_user_id": str(seed["owner"]),
                    "expected_tenant_id": str(seed["tenant"]),
                    "expected_version": 1,
                    "operation_id": str(uuid4()),
                    "action": "abandon",
                    "params": {},
                },
            )
            assert abandoned.status_code == 200 and abandoned.json()["local_abandoned"], abandoned.text
            assert not (receiver.directory / "consumed-1.json").exists()
            await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, str(run_id))
            return
        consumed = json.loads((receiver.directory / "consumed-0.json").read_text())
        context = consumed["context"]
        assert context["principal"]["purpose"] == "autonomous_agent" and context["principal"]["autonomous"] is True
        assert context["budget_run_id"] == str(budget_id)
        assert "REMOTE_REFLECTION_ASSIGNMENT" in consumed["sdk_input"]
        if fault == "confirmed_plan":
            assert plan_body in consumed["sdk_input"]
            assert "Compare the complete approved plan with current evidence." in consumed["sdk_input"]
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                plan = await db.get(AgentPlanRequest, plan_id)
                assert plan.status == "confirmed" and plan.confirmed_by_user_id == seed["owner"]
        if fault == "input_required":
            from app.models.chat_session import ChatSession
            from app.models.chat_transcript_event import ChatTranscriptEvent

            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                assert (await db.get(RuntimeTask, run_id)).status == "suspended"
                session = await db.get(ChatSession, session_id)
                question = await db.scalar(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.run_id == run_id,
                        ChatTranscriptEvent.metadata_json["source"].as_string() == "remote_a2a_chat",
                    )
                )
                assert question is not None
                assert (
                    (question.listed_surface, question.visibility_scope)
                    == (session.listed_surface, session.visibility_scope)
                    == ("task_updates", "agent_owner")
                )
            answer = await api.post(
                base + f"/sessions/{session_id}/runs",
                json={
                    "content": "REMOTE_REFLECTION_ANSWER: the requested detail.",
                    "input_id": str(uuid4()),
                    "idempotency_key": "reflection-answer",
                },
            )
            assert answer.status_code == 202 and UUID(answer.json()["target_run_id"]) == run_id, answer.text
            await original.execute_chat(seed, str(run_id), app_user_sessionmaker)
            second = json.loads((receiver.directory / "consumed-1.json").read_text())
            assert "REMOTE_REFLECTION_ANSWER" in second["sdk_input"]
            assert "Please provide the requested detail" in second["sdk_input"]
            assert second["context"]["principal"]["autonomous"] is True
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            task = await db.get(RuntimeTask, run_id)
            assert task.status == "completed" and task.budget_run_id == budget_id
        if fault is None:
            from app.models.invocation_span import InvocationSpan
            from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
            from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch
            from app.services.web_terminal_boundary_processor import build_web_terminal_boundary_binding

            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                snapshot = await db.get(InvocationSpan, uuid5(run_id, "remote-terminal:completed"))
                original_proof = deepcopy(snapshot.metadata_json)
                snapshot.metadata_json = {**original_proof, "sha256": "0" * 64}
                await db.flush()
                boundary = await db.scalar(
                    select(RuntimeTerminalBoundaryOutbox).where(RuntimeTerminalBoundaryOutbox.runtime_task_id == run_id)
                )
                with pytest.raises(TerminalBoundaryCanonicalMismatch):
                    await build_web_terminal_boundary_binding(
                        db,
                        tenant_id=seed["tenant"],
                        runtime_task_id=run_id,
                        agent_id=seed["agent"],
                        session_id=session_id,
                        event_kind=boundary.event_kind,
                        terminal_status=boundary.terminal_status,
                        authority_ref=boundary.authority_ref,
                        authority_id=boundary.authority_id,
                    )
                snapshot.metadata_json = original_proof
                await db.commit()
        await original.settle_boundary(seed, app_user_sessionmaker, tmp_path, str(run_id))
        returned = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="remote-trigger-return"
        )
        assert returned["delivered"] >= 1, returned
        if fault == "input_required":
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                events = list(
                    await db.scalars(
                        select(ChatTranscriptEvent).where(
                            ChatTranscriptEvent.session_id == session_id,
                            ChatTranscriptEvent.event_type.in_(("assistant_message", "remote_a2a_artifacts")),
                        )
                    )
                )
                assert any(event.event_type == "remote_a2a_artifacts" for event in events)
                assert all(
                    (event.listed_surface, event.visibility_scope) == ("task_updates", "agent_owner")
                    for event in events
                )
        report = (receiver.directory / ("report-1.txt" if fault == "input_required" else "report-0.txt")).read_text()
        transcript = await api.get(base + f"/sessions/{session_id}/transcript")
        assert transcript.status_code == 200 and any(item.get("content") == report for item in transcript.json())
