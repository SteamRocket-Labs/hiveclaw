"""Independent native checks retain real work and do not self-notify on no action."""

import asyncio
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_m3ag_native_trigger_questions_postgres import scheduled_question_runtime, submit_answer  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _confirm_native_plan(state, monkeypatch):
    import app.runtime.invoker as invoker
    from app.api.plans import get_plan_mode_service
    from app.services.plan_mode_registry import register_plan_mode_handoffs
    from app.models.plan_request import AgentPlanRequest
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services import runtime_task_worker, web_chat_runtime

    body = "## Stock check\nRead the current stock record, compare the agreed minimum, and retain evidence.\nPLAN_NATIVE_TAIL"
    args = {"title": "Planned stock check", "objective": "Check the agreed stock minimum.",
        "steps": ["Read the stock record and compare it with the agreed minimum."],
        "success_criteria": ["Current file evidence is retained."], "stop_conditions": ["Stop when authority is unavailable."],
        "intent_type": "autonomous_wake", "handoff_target": "scheduled_trigger",
        "wake_policy": {"type": "interval", "config": {"minutes": 30, "model_id": str(state.pinned_id), "toolset": ["read_file"]}}}
    service = get_plan_mode_service()
    monkeypatch.setattr(service, "_handoff_handlers", dict(service._handoff_handlers))
    register_plan_mode_handoffs(service)  # The same registration as application startup.
    invoke = web_chat_runtime.invoke_agent
    scripted = False
    async with state.scoped() as db:
        employee = await db.get(Agent, state.seed["agent_id"])
        planning_model = await db.get(LLMModel, employee.primary_model_id)
        planning_model.max_input_tokens = 131072  # Explicit capacity of this controlled provider fixture.
        await db.commit()

    async def plan_invocation(request):
        nonlocal scripted
        assert request.session_context.plan_mode.active
        path = request.session_context.plan_mode.plan_file_path
        assert path
        if not scripted:
            scripted = True
            state.llm.script.extend([
                {"tool_calls": [{"id": "write-business-plan", "type": "function", "function": {
                    "name": "write_file", "arguments": json.dumps({"path": path, "content": body})}}], "finish_reason": "tool_calls"},
                {"tool_calls": [{"id": "submit-business-plan", "type": "function", "function": {
                    "name": "exit_plan_mode", "arguments": json.dumps(args)}}], "finish_reason": "tool_calls"},
            ])
        return await invoke(request)

    with monkeypatch.context() as patch:
        patch.setattr(web_chat_runtime, "invoke_agent", plan_invocation)
        patch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **k: [{"type": "function", "function": {
            "name": name, "parameters": {"type": "object"}}} for name in ("write_file", "exit_plan_mode")])
        async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://native-check.test") as client:
            started = await client.post(state.endpoint + f"/sessions/{state.seed['session_id']}/runs",
                json={"content": "Plan an authorized recurring stock check with quiet nonactionable results.",
                    "plan_mode_requested": True, "input_id": str(uuid.uuid4()), "idempotency_key": "plan-native-check"})
            assert started.status_code == 201, started.text
        await asyncio.sleep(5.1)
        await runtime_task_worker.recover_session_input_dispatches_once(worker_id="stock-plan-input", session_factory=state.scoped)
        plan_run = await state.claim("web_chat_turn")
        await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(plan_run), 20)
        for _ in range(2):  # The two declared tools may each require an exact human decision.
            async with state.scoped() as db:
                task = await db.get(RuntimeTask, plan_run)
                if task.status == "completed":
                    break
                assert task.status == "suspended" and task.metadata_json["interactive_pause"] == "awaiting_session_permission"
                permission = (await db.scalars(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == plan_run, SessionToolInvocation.permission_state == "waiting"))).one()
                permission_id = permission.permission_item_id
                submitting_plan = permission.tool_name == "exit_plan_mode"
            async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://native-check.test") as client:
                granted = await client.post(state.endpoint + f"/sessions/{state.seed['session_id']}/permissions/{permission_id}/resolve",
                    json={"action": "allow_once"})
                assert granted.status_code == 200, granted.text
            before_resume = len(state.llm.requests)
            assert await state.claim("web_chat_turn") == plan_run
            await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(plan_run), 20)
            if submitting_plan:
                assert len(state.llm.requests) == before_resume, "The approved plan card must settle without another model call"
        async with state.scoped() as db:
            assert (await db.get(RuntimeTask, plan_run)).status == "completed"
        await state.boundary(plan_run)
    async with state.scoped() as db:
        plan = (await db.scalars(select(AgentPlanRequest).where(AgentPlanRequest.agent_id == state.seed["agent_id"],
            AgentPlanRequest.status == "awaiting_confirmation"))).one()
        assert plan.plan_json["plan_markdown"] == body
        plan_id, version = plan.id, plan.plan_version
    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://native-check.test") as client:
        confirmed = await client.post(state.endpoint + f"/plans/{plan_id}/confirm-and-handoff", json={"plan_version": version})
        assert confirmed.status_code == 200 and confirmed.json()["handoff_status"] == "completed", confirmed.text
        return confirmed.json()["handoff_payload"]["created_trigger_id"]


@pytest.mark.parametrize("fault", [None, "revoked", "confirmed_plan", "confirmed_plan_revoked"])
async def test_independent_native_checks_are_canonical_quiet_and_repeatable(scheduled_question_runtime, monkeypatch, fault):  # noqa: F811
    import app.runtime.invoker as invoker
    from app.api.autonomy import router
    from app.api.files import router as files_router
    from app.tools import workspace as tool_workspace
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome, SessionTurnInput
    from app.services import runtime_task_worker, trigger_daemon, web_chat_runtime

    state = scheduled_question_runtime
    state.app.include_router(router, prefix="/api")
    state.app.include_router(files_router, prefix="/api")
    monkeypatch.setattr(tool_workspace, "WORKSPACE_ROOT", state.seed["data_root"])
    await submit_answer(state, state.question.item_id, "Audience: Team")
    successor = await state.claim("web_chat_turn")
    state.llm.script.append({"content": "The original audience decision is recorded.", "finish_reason": "stop"})
    await web_chat_runtime.execute_web_chat_run(successor)
    await state.boundary(successor)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **k: [{"type": "function", "function": {
        "name": "read_file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}])
    if fault and fault.startswith("confirmed_plan"):
        trigger_id = await _confirm_native_plan(state, monkeypatch)
    else:
        trigger_id = await _enable_native_check(state)
    async with state.scoped() as db:
        original_events = await db.scalar(select(func.count()).select_from(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == state.seed["session_id"]))
    runs, budgets, content_hash = set(), set(), None
    for number, stock in enumerate((80, 90)):
        marker = "STOCK_RECORD_" + str(stock)
        async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://native-check.test") as client:
            saved = await client.put(state.endpoint + "/files/content", params={"path": "workspace/stock.json"},
                json={"content": json.dumps({"marker": marker, "stock": stock, "minimum": 20}),
                    "expected_current_hash": content_hash})
            assert saved.status_code == 200, saved.text
            content_hash = saved.json()["content_hash"]
        before = len(state.llm.requests)
        state.llm.script.extend([
            {"tool_calls": [{"id": f"stock-read-{number}", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "workspace/stock.json"})}}], "finish_reason": "tool_calls"},
            {"content": f"Checked {marker}. Stock is above the agreed minimum; no action is needed.", "finish_reason": "stop"},
        ])
        fired = await trigger_daemon.fire_trigger_once_now(state.seed["agent_id"], trigger_id)
        assert fired["fired"], fired
        wrapper_id = await state.claim("trigger")
        assert wrapper_id == uuid.UUID(fired["runtime_task_id"])
        assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
        assert len(state.llm.requests) == before, "Independent work must be admitted durably before any model call"
        async with state.scoped() as db:
            wrapper = await db.get(RuntimeTask, wrapper_id)
            assert wrapper.status == "completed" and wrapper.metadata_json["delivery"] == "reflection"
            reflection_id = uuid.UUID(wrapper.child_session_id)
            session = await db.get(ChatSession, reflection_id)
            assert session.user_id == state.seed["user_id"] and session.listed_surface == "task_updates"
            input_id = trigger_daemon._trigger_child_run_id(wrapper_id, effect="same_session", discriminator=str(reflection_id))
            assert await db.get(SessionTurnInput, input_id) is not None
            budgets.add(wrapper.budget_run_id)
        await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
        direct = await runtime_task_worker.drain_direct_terminal_boundary_outbox_once(worker_id="stock-check-wrapper", session_factory=state.scoped)
        assert direct["delivered"] == 1, direct
        await asyncio.sleep(5.1)
        dispatch = await runtime_task_worker.recover_session_input_dispatches_once(worker_id="stock-check-input", session_factory=state.scoped)
        assert dispatch["dispatched"] == 1, dispatch
        run_id = await state.claim("web_chat_turn")
        runs.add(run_id)
        if fault == "revoked":
            from app.services.trigger_resource_authority import quarantine_transferred_trigger_authority

            async with state.scoped() as db:
                await quarantine_transferred_trigger_authority(db, agent_id=state.seed["agent_id"],
                    previous_owner_id=state.seed["user_id"])
                await db.commit()
        if fault == "confirmed_plan_revoked":
            from app.models.audit import ApprovalRequest
            from app.models.trigger import AgentTrigger

            async with state.scoped() as db:
                trigger = await db.get(AgentTrigger, uuid.UUID(trigger_id))
                lease = await db.get(ApprovalRequest, uuid.UUID(trigger.config["plan_authorization"]["lease_id"]))
                # Trusted-state fault injection after admission, before the actual effect.
                lease.status = "rejected"
                await db.commit()
        await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(run_id), 20)
        if fault in {"revoked", "confirmed_plan_revoked"}:
            assert len(state.llm.requests) == before
            async with state.scoped() as db:
                denied = await db.get(RuntimeTask, run_id)
                assert denied.status == "failed" and denied.metadata_json["error_code"] == (
                    "trigger_plan_authority_revoked" if fault == "confirmed_plan_revoked" else "trigger_authority_revoked")
                assert denied.metadata_json["blocked_effect"]["effect_state"] == "not_started"
            await state.boundary(run_id)
            return
        assert len(state.llm.requests) == before + 2
        assert marker in json.dumps(state.llm.requests[-1]["messages"])
        if fault == "confirmed_plan":
            assert "PLAN_NATIVE_TAIL" in json.dumps(state.llm.requests[-1]["messages"])
        assert state.receipts[-1]["model_id"] == str(state.pinned_id) and state.receipts[-1]["fallback"] is False
        async with state.scoped() as db:
            task = await db.get(RuntimeTask, run_id)
            assert task.status == "completed" and task.budget_run_id in budgets and task.root_runtime_task_id == wrapper_id
            assert await db.scalar(select(SessionRunOutcome.state).where(SessionRunOutcome.run_id == run_id)) == "terminal_committed"
            assert await db.scalar(select(func.count()).select_from(SessionModelResult).where(SessionModelResult.run_id == run_id)) == 2
            surfaces = set(await db.scalars(select(ChatTranscriptEvent.listed_surface).where(
                ChatTranscriptEvent.session_id == reflection_id)))
            assert surfaces == {"task_updates"}
            assert await db.scalar(select(func.count()).select_from(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == state.seed["session_id"])) == original_events
        await state.boundary(run_id)
        async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://native-check.test") as client:
            view = await client.get(state.endpoint + "/autonomy/overview")
            assert view.status_code == 200, view.text
            entry = next(row for row in view.json()["triggers"] if row["id"] == trigger_id)
            assert entry["last_attempt"]["work_execution"]["status"] == "completed"
            assert entry["last_attempt"]["work_execution"]["session_id"] == str(reflection_id)
    assert len(runs) == len(budgets) == 2


async def _enable_native_check(state):
    async with AsyncClient(transport=ASGITransport(app=state.app), base_url="http://native-check.test") as client:
        created = await client.post(state.endpoint + "/triggers", json={"name": "stock_check", "type": "interval",
            "config": {"minutes": 30, "model_id": str(state.pinned_id), "toolset": ["read_file"],
                "context_from": ["session:" + str(state.seed["session_id"])]},
            "reason": "Read workspace/stock.json. Check stock against the agreed minimum. Keep nonactionable checks quiet in work history.",
            "is_enabled": False})
        assert created.status_code == 201, created.text
        trigger_id = created.json()["id"]
        review = await client.post(state.endpoint + "/plan-recommendations", json={
            "original_request": "Enable the stock check within its current scope and budget.",
            "session_id": str(state.seed["session_id"]), "source": "agent_aware",
            "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest"})
        assert review.status_code == 201, review.text
        declined = await client.post(state.endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
        assert declined.status_code == 200, declined.text
        enabled = await client.patch(state.endpoint + "/triggers/" + trigger_id, json={"is_enabled": True,
            "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"]})
        assert enabled.status_code == 200, enabled.text
    return trigger_id
