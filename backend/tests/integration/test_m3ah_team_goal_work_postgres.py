"""A reviewed shared goal starts once in the current user's private work."""

import copy
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from app.database import pin_rls_tenant_context
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionTurnInput
from app.models.task import Task
from tests.integration.test_m1g84_task_handover_postgres import handover_case  # noqa: F401
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_m3ag_native_trigger_questions_postgres import scheduled_question_runtime  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def goal_case(handover_case, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.services import runtime_task_worker, web_chat_runtime

    c = handover_case

    @asynccontextmanager
    async def scoped(*_args, **_kwargs):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, c.tenant)
            yield db

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr(web_chat_runtime, "tenant_scoped_session", scoped)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    shared = await c.client.post(f"/api/agents/{c.agent}/tasks/{c.task.id}/team-sharing", json={
        "request_id": str(uuid.uuid4()), "business_team_id": str(c.team), "responsible_user_id": str(c.owner.id),
        "expected_updated_at": c.task.updated_at.isoformat(), "reason": "Review and share only this goal"})
    assert shared.status_code == 200, shared.text
    listed = await c.client.get(f"/api/business-teams/{c.team}/tasks")
    c.context = listed.json()["tasks"][0]["work_context"]
    c.input_id = uuid.uuid4()
    c.url = f"/api/agents/{c.agent}/sessions/runs"
    c.body = {"title": "My shared goal work", "content": "Prepare a reviewed draft. Do not repeat old execution.",
        "input_id": str(c.input_id), "idempotency_key": f"session-input:{c.input_id}", "parts": [c.context]}
    return c


async def _start(c):
    response = await c.client.post(c.url, json=c.body)
    assert response.status_code == 201, response.text
    c.work = response.json()
    return c.work


async def _bound(c, app_user_sessionmaker):
    from app.runtime.hooks import HookResult
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_model_round import bind_round_inputs
    from tests.services.test_session_input_control_v2 import _authority

    async def allow(**_kwargs):
        return HookResult()

    session_id = uuid.UUID(c.work["session"]["id"])
    run_id = uuid.UUID(c.work["run"]["run_id"])
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, c.tenant)
        c.turn_id = (await db.get(RuntimeTask, run_id)).metadata_json["turn_id"]
        authority = await _authority(db, user_id=c.owner.id, agent_id=c.agent, session_id=session_id)
        await run_user_prompt_admission(db, authority=authority, input_id=c.input_id, worker_id="shared-goal", hook_executor=allow)
        await queue_admitted_human_input(db, authority=authority, input_id=c.input_id)
        bound = await bind_round_inputs(db, tenant_id=c.tenant, agent_id=c.agent, session_id=session_id,
            run_id=run_id, turn_id=c.turn_id, round_index=1)
        await db.commit()
    return session_id, run_id, bound


@pytest.mark.parametrize("key_only", [False, True])
async def test_reviewed_goal_and_exact_first_input_retry_preserve_private_ownership(goal_case, owner_sessionmaker, key_only):
    c = goal_case
    if key_only:
        c.body.pop("input_id")
        c.input_id = uuid.uuid5(c.owner.id, f"first-input-key:{c.agent}:{c.body['idempotency_key']}")
    async with owner_sessionmaker() as db:
        (await db.get(Task, c.task.id)).description = "PRIVATE_TASK_CANARY: edited after disclosure"
        await db.commit()
    first = await _start(c)
    second = await c.client.post(c.url, json=c.body)
    assert second.status_code == 201, second.text
    assert second.json()["session"]["id"] == first["session"]["id"]
    assert second.json()["run"]["run_id"] == first["run"]["run_id"]
    assert (await c.client.post(c.url, json={**c.body, "content": "Changed instruction under the same identity"})).status_code == 409
    async with owner_sessionmaker() as db:
        row = await db.get(SessionTurnInput, c.input_id)
        work = await db.get(ChatSession, uuid.UUID(first["session"]["id"]))
        assert work.user_id == c.owner.id and work.id != c.session and work.audience_kind in {None, "private"}
        assert row.content_parts_json[1] == c.context
        assert "PRIVATE_TASK_CANARY" not in str(row.content_parts_json)
        assert await db.scalar(select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == str(work.id))) == 1
        original = await db.get(RuntimeTask, c.runtime)
        assert original.root_user_id == c.owner.id and original.status == "pending"


async def test_concurrent_first_input_retries_create_one_private_work(goal_case, owner_sessionmaker, monkeypatch):
    import asyncio
    from app.services import session_input_admission

    c = goal_case
    entered, release = asyncio.Event(), asyncio.Event()
    real_hook = session_input_admission.emit_hook

    async def hold_hook(event, **kwargs):
        if event.value == "user_prompt_submit":
            entered.set()
            await release.wait()
        return await real_hook(event, **kwargs)

    monkeypatch.setattr(session_input_admission, "emit_hook", hold_hook)
    first = asyncio.create_task(c.client.post(c.url, json=c.body))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        right = await asyncio.wait_for(c.client.post(c.url, json=c.body), 10)
    finally:
        release.set()
        left = await asyncio.wait_for(first, 10)
    assert left.status_code == 201 and right.status_code == 202, (left.text, right.text)
    assert left.json()["session"]["id"] == right.json()["session"]["id"]
    canonical_run = uuid.uuid5(c.input_id, "session-v2-runtime-run")
    for response in (left, right):
        if response.status_code == 201:
            assert uuid.UUID(response.json()["run"]["run_id"]) == canonical_run
        else:
            pending = response.json()["run"]
            assert pending["schema"] == "hive.human_input_receipt" and pending["schema_version"] == 2
            assert pending["input_id"] == str(c.input_id) and pending["run"] is None
    recovered = await c.client.post(c.url, json=c.body)
    assert recovered.status_code == 201 and uuid.UUID(recovered.json()["run"]["run_id"]) == canonical_run, recovered.text
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.agent_id == c.agent)) == 2
        assert await db.scalar(select(func.count()).select_from(SessionTurnInput).where(SessionTurnInput.id == c.input_id)) == 1
        assert await db.scalar(select(func.count()).select_from(RuntimeTask).where(RuntimeTask.parent_session_id == left.json()["session"]["id"])) == 1


@pytest.mark.parametrize("fault", ["body", "source", "other_agent", "unshare", "membership", "handover", "republish"])
async def test_selection_rejects_stale_or_unauthorized_goal_before_new_work(goal_case, owner_sessionmaker, fault):
    from app.models.organization_identity import BusinessTeamMembership
    from app.models.audit import AuditLog

    c = goal_case
    body = copy.deepcopy(c.body)
    async with owner_sessionmaker() as db:
        task = await db.get(Task, c.task.id)
        if fault == "body":
            body["parts"][0]["text"] += " caller-forged private details"
        elif fault == "source":
            body["parts"][0]["team_task_source"]["disclosure_id"] = str(uuid.uuid4())
        elif fault == "other_agent":
            body["parts"][0]["team_task_source"]["agent_id"] = str(uuid.uuid4())
        elif fault == "unshare":
            task.business_team_id, task.audience_kind = None, "private"
        elif fault == "membership":
            member = await db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.owner.id))
            member.status = "left"
        elif fault == "handover":
            task.responsible_user_id = c.successor.id
        elif fault == "republish":
            old = await db.get(AuditLog, uuid.UUID(c.context["team_task_source"]["disclosure_id"]))
            db.add(AuditLog(id=uuid.uuid4(), tenant_id=c.tenant, user_id=c.owner.id, agent_id=c.agent,
                action="task:team_share", details={**old.details, "shared_task": {"title": "New reviewed title"}}))
        await db.commit()
    rejected = await c.client.post(c.url, json=body)
    assert rejected.status_code == 403, rejected.text
    async with owner_sessionmaker() as db:
        assert await db.get(SessionTurnInput, c.input_id) is None
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.agent_id == c.agent)) == 1


@pytest.mark.parametrize("fault", [None, "unshare", "membership", "missing_task"])
async def test_bound_goal_has_exact_model_sources_and_live_consumption_authority(goal_case, app_user_sessionmaker, owner_sessionmaker, fault):
    from app.kernel.contracts import ExecutionAuthorityUnavailable
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.models.organization_identity import BusinessTeamMembership
    from app.services.session_model_round import prepare_model_request

    c = goal_case
    await _start(c)
    session_id, run_id, bound = await _bound(c, app_user_sessionmaker)
    messages = _mid_run_items_to_user_messages(bound)
    assert "Explicit shared goal" in str(messages) and "PRIVATE_TASK_CANARY" not in str(messages)
    if fault:
        async with owner_sessionmaker() as db:
            if fault == "unshare":
                task = await db.get(Task, c.task.id)
                task.business_team_id, task.audience_kind = None, "private"
            elif fault == "membership":
                member = await db.scalar(select(BusinessTeamMembership).where(BusinessTeamMembership.user_id == c.owner.id))
                member.status = "left"
            else:
                await db.delete(await db.get(Task, c.task.id))
            await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, c.tenant)
        call = prepare_model_request(db, tenant_id=c.tenant, agent_id=c.agent, session_id=session_id,
            run_id=run_id, turn_id=c.turn_id, round_index=1, messages=messages, tools=[],
            provider="openai", model="synthetic-no-provider")
        if fault:
            with pytest.raises(ExecutionAuthorityUnavailable):
                await call
            placeholders = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)))
            assert all(row.model_request_snapshot_json.get("phase") == "awaiting_exact_snapshot" for row in placeholders)
        else:
            request_id = await call
            result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
            refs = result.model_request_snapshot_json["input_bindings"]["dependencies"]
            assert {ref["source_kind"] for ref in refs} == {"session_command", "team_task_input"}
            assert result.model_request_snapshot_json["input_bindings"]["dependency_coverage"]["complete"]


@pytest.mark.parametrize("revoked", [False, True])
async def test_actual_tool_consumer_rechecks_goal_after_the_model_round(goal_case, app_user_sessionmaker, owner_sessionmaker, monkeypatch, revoked):
    from app.kernel.engine import _mid_run_items_to_user_messages
    from app.services import agent_tools
    from app.services.session_model_round import prepare_model_request, commit_model_response
    from app.services.session_tool_runtime import prepare_tool_invocation

    c = goal_case
    await _start(c)
    session_id, run_id, bound = await _bound(c, app_user_sessionmaker)
    arguments = {"questions": [{"question": "Which delivery audience?", "header": "Audience",
        "options": [{"label": "Team"}, {"label": "Personal"}]}]}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, c.tenant)
        provider = await prepare_model_request(db, tenant_id=c.tenant, agent_id=c.agent, session_id=session_id,
            run_id=run_id, turn_id=c.turn_id, round_index=1, messages=_mid_run_items_to_user_messages(bound),
            tools=[{"type": "function", "function": {"name": "ask_user_question"}}], provider="openai", model="synthetic-no-provider")
        await commit_model_response(db, tenant_id=c.tenant, agent_id=c.agent, session_id=session_id,
            run_id=run_id, turn_id=c.turn_id, round_index=1, provider_request_id=provider,
            response={"content": None, "tool_calls": [{"id": "team-goal-question", "type": "function", "function": {
                "name": "ask_user_question", "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls", "usage": {}})
        await prepare_tool_invocation(db, tenant_id=c.tenant, agent_id=c.agent, session_id=session_id,
            run_id=run_id, provider_request_id=provider, provider_tool_use_id="team-goal-question",
            tool_name="ask_user_question", arguments=arguments)
        await db.commit()
    if revoked:
        async with owner_sessionmaker() as db:
            task = await db.get(Task, c.task.id)
            task.audience_kind, task.business_team_id = "private", None
            await db.commit()
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    trace = {}
    result = await agent_tools._get_tool_runtime_service().execute("ask_user_question", arguments,
        agent_id=c.agent, user_id=c.owner.id, session_id=str(session_id), runtime_task_id=str(run_id),
        turn_id=c.turn_id, tool_call_id="team-goal-question", emit_runtime_hooks=False, trace_metadata_sink=trace)
    if revoked:
        assert "team_task_" in str(result) and "denied" in str(result), (result, trace)
        assert "awaiting_user_clarification" not in str(result)
    else:
        assert "awaiting_user_clarification" in str(result), (result, trace)


async def test_real_native_worker_delivers_the_shared_goal_in_the_callers_own_work(scheduled_question_runtime, owner_sessionmaker):  # noqa: F811
    import asyncio
    from httpx import ASGITransport, AsyncClient
    from app.api import business_teams, tasks
    from app.models.organization_identity import BusinessTeam, BusinessTeamMembership
    from app.models.task import TaskLog
    from app.services import web_chat_runtime

    c = scheduled_question_runtime
    seed = c.seed
    c.app.include_router(business_teams.router, prefix="/api")
    c.app.include_router(tasks.router, prefix="/api")
    team_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(BusinessTeam(id=team_id, tenant_id=seed["tenant_id"], name="Shared delivery", created_by_user_id=seed["user_id"], status="active"))
        await db.flush()
        db.add(BusinessTeamMembership(tenant_id=seed["tenant_id"], business_team_id=team_id, user_id=seed["user_id"], role="lead", status="active"))
        task = Task(id=task_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], created_by=seed["user_id"],
            title="Reviewed delivery goal", description="Prepare the shared delivery comparison", root_session_id=seed["session_id"],
            status="needs_reconciliation", active_runtime_task_id=c.scheduled_id, authority_state="owned", request_id="shared-native-work", request_hash="fixture")
        db.add(task)
        await db.commit()
        await db.refresh(task)
    async with AsyncClient(transport=ASGITransport(app=c.app), base_url="http://shared-team-work.test") as client:
        shared = await client.post(c.endpoint + f"/tasks/{task_id}/team-sharing", json={"request_id": str(uuid.uuid4()),
            "business_team_id": str(team_id), "responsible_user_id": str(seed["user_id"]),
            "expected_updated_at": task.updated_at.isoformat(), "reason": "Explicitly review and share only this delivery goal"})
        assert shared.status_code == 200, shared.text
        context = shared.json()["task"]["work_context"]
        async with owner_sessionmaker() as db:
            (await db.get(Task, task_id)).description = "PRIVATE_TASK_TRANSFER_CANARY"
            db.add(TaskLog(task_id=task_id, tenant_id=seed["tenant_id"], content="PRIVATE_TASK_TRANSFER_CANARY"))
            await db.commit()
        input_id = uuid.uuid4()
        created = await client.post(c.endpoint + "/sessions/runs", json={"content": "Prepare a draft from the selected shared goal.",
            "parts": [context], "input_id": str(input_id), "idempotency_key": f"session-input:{input_id}"})
        assert created.status_code == 201, created.text
        own = created.json()
    run_id, session_id = uuid.UUID(own["run"]["run_id"]), uuid.UUID(own["session"]["id"])
    c.llm.script.append({"content": "Controlled shared delivery draft", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(run_id), 20)
    await c.boundary(run_id)
    assert "Prepare the shared delivery comparison" in json.dumps(c.llm.requests[-1]["messages"])
    assert "PRIVATE_TASK_TRANSFER_CANARY" not in json.dumps(c.llm.requests[-1])
    async with owner_sessionmaker() as db:
        own_task = await db.get(RuntimeTask, run_id)
        original = await db.get(Task, task_id)
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == run_id))
        assert own_task.status == "completed" and own_task.root_user_id == seed["user_id"]
        assert own_task.parent_session_id == str(session_id) and session_id != seed["session_id"]
        assert own_task.metadata_json.get("trigger_origin") is None
        assert original.status == "needs_reconciliation" and original.active_runtime_task_id == c.scheduled_id
        assert "team_task_input" in {ref["source_kind"] for ref in result.model_request_snapshot_json["input_bindings"]["dependencies"]}
