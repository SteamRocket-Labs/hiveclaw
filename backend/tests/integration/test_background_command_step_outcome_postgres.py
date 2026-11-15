"""Background workers preserve real command outcomes through member replay."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401
from tests.integration.test_web_chat_worker_restart_end_to_end_recovery import (
    _patch_environment_seams,
    _patch_runtime_settings,
)
from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient,
    _patch_invoker_seams,
    _patch_runtime_seams,
    _seed_run,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def background_command_runtime(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, request,
    drain_terminal_boundary_for_task,
):
    scoped_factories = request.getfixturevalue("scoped_runtime_factories")
    import app.runtime.invoker as invoker
    from app.api import chat_sessions, plans, tasks, triggers
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.services import agent_context, agent_manager, agent_tools, runtime_task_worker, tenant_resolver
    from app.services import conversation_summarizer, fast_reflection_learning_brain, llm_client, session_memory
    from app.services.code_execution import local_provider, service
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.subprocess_sandbox import AgentSandboxCommand

    outbound = []

    async def no_transport(_transport, http_request):
        outbound.append(str(http_request.url))
        raise AssertionError("Unexpected external transport")

    async def summary(*_args, **_kwargs):
        return "Controlled local summary."

    async def memory(*_args, **_kwargs):
        return json.dumps({"session_title": "Command outcome", "current_state": "Controlled fixture"})

    class UnavailableAuxiliaryClient:
        async def complete(self, *_args, **_kwargs):
            raise RuntimeError("Auxiliary intelligence unavailable in controlled fixture")

        async def close(self):
            pass

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_transport)
    monkeypatch.setattr(conversation_summarizer, "_llm_summarize", summary)
    monkeypatch.setattr(session_memory, "_run_session_memory_llm", memory)
    for module in (llm_client, fast_reflection_learning_brain):
        monkeypatch.setattr(module, "create_llm_client_from_config", lambda *a, **k: UnavailableAuxiliaryClient())
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Prepare this work for a background command.")

    @asynccontextmanager
    async def scoped():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    real_resolve = tenant_resolver.resolve_tenant_for_agent
    _patch_runtime_seams(monkeypatch, seed, scoped)
    _patch_runtime_settings(monkeypatch, seed)
    _patch_environment_seams(monkeypatch, app_user_sessionmaker)
    scoped_factories()
    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", real_resolve)
    monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(runtime_task_worker, "tenant_scoped_session", tenant_scoped_session)
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with scoped() as db:
        agent = await db.get(Agent, seed["agent_id"])
        user = await db.get(User, seed["user_id"])
        user.role = "member"
        assert agent.agent_type == "native"
        await agent_manager.AgentManager().initialize_agent_files(db, agent, original_author_id=user.id)
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.command.execute",
            allowed=True, requires_approval=False))
        await db.commit()
    llm = ScriptedLLMClient([])
    _patch_invoker_seams(monkeypatch, llm, seed, scoped)
    monkeypatch.setattr(invoker, "execute_tool", agent_tools.execute_tool)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *a, **k: [{
        "type": "function", "function": {"name": "run_command", "parameters": {
            "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
        }},
    }])
    # The suite already runs inside its network-denying OS sandbox. Avoid a
    # nested sandbox while retaining the real local provider, process and timeout.
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setattr(local_provider, "build_sandboxed_agent_command",
        lambda command, **_kwargs: AgentSandboxCommand(command=list(command)))
    command_results = []

    async def execute_command(*args, **kwargs):
        result = await local_provider.execute_local_sandboxed_command(*args, **kwargs)
        command_results.append(result)
        return result

    monkeypatch.setattr(service, "execute_local_sandboxed_command", execute_command)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr(tasks, "notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    app = FastAPI()
    for router in (chat_sessions.router, plans.router, tasks.router, triggers.router):
        app.include_router(router, prefix="/api")

    async def database():
        async with scoped() as db:
            yield db
            await db.commit()

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user

    async def claim(kind):
        async with scoped() as db:
            claims = await RuntimeTaskClaimService(db=db, worker_id="command-outcome-worker",
                task_types=[kind], lease_seconds=60).claim_available(batch_size=1)
            assert len(claims) == 1
            return claims[0].id

    async def boundary(run_id, *, direct=False):
        lane = (runtime_task_worker.drain_direct_terminal_boundary_outbox_once if direct
            else runtime_task_worker.drain_web_terminal_boundary_outbox_once)
        result = await drain_terminal_boundary_for_task(lane, task_id=run_id,
            worker_id="command-outcome-boundary", session_factory=app_user_sessionmaker)
        assert result["target_delivered"] == 1, result

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://command-outcome.test") as client:
        yield SimpleNamespace(seed=seed, scoped=scoped, llm=llm, client=client,
            endpoint=f"/api/agents/{seed['agent_id']}", claim=claim, boundary=boundary,
            command_results=command_results)
    from app.runtime import hooks_setup

    own = [task for key, task in list(hooks_setup._T2_BACKGROUND_TASKS.items())
        if key.startswith(str(seed["agent_id"]) + ":")]
    if own:
        await asyncio.wait_for(asyncio.gather(*own), 10)
    assert outbound == []


def _script_command(state, *, timed_out=False):
    command = "exec sleep 10" if timed_out else "printf 'Q3A_COMMAND_STDOUT\\n'; exit 23"
    state.llm.script.extend([
        {"tool_calls": [{"id": "background-command", "type": "function", "function": {
            "name": "run_command", "arguments": json.dumps({"command": command, "timeout": 1}),
        }}], "finish_reason": "tool_calls"},
        {"content": "The command outcome has been recorded.", "finish_reason": "stop"},
    ])


async def _assert_command_replay(state, owner_sessionmaker, tmp_path, *, run_id, session_id, timed_out):
    from app.models.session_v2 import SessionEventOutbox, SessionToolInvocation

    assert len(state.command_results) == 1
    result = state.command_results[0]
    assert result.timed_out is timed_out
    if not timed_out:
        assert result.exit_code == 23 and result.stdout == "Q3A_COMMAND_STDOUT\n"
    else:
        assert result.error == "❌ Command timed out after 1s"
    expected = {"exit_code": None if timed_out else 23, "timed_out": timed_out}
    async with owner_sessionmaker() as db:
        invocations = list(await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == run_id, SessionToolInvocation.tool_name == "run_command")))
        assert len(invocations) == 1
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocations[0].id,
            ChatTranscriptEvent.item_kind.in_(["tool_call", "tool_result"]),
            ChatTranscriptEvent.lifecycle == "completed")))
        assert {event.item_kind for event in events} == {"tool_call", "tool_result"}
        assert len(events) == 2
        for event in events:
            assert event.metadata_json["v2_payload"]["code_execution_result"] == expected
            outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == event.id))
            assert outbox.envelope_json["payload"]["code_execution_result"] == expected
    served = await state.client.get(state.endpoint + f"/sessions/{session_id}/transcript",
        params={"schema_version": 2, "limit": 1000})
    assert served.status_code == 200, served.text
    for event in events:
        replay = next(item for item in served.json() if item["event_id"] == str(event.id))
        assert replay["scope"]["run_id"] == str(run_id)
        assert replay["payload"]["code_execution_result"] == expected
    # Use the page's actual event consumer and row projection with the member
    # response above, rather than reconstructing an event or parsing stdout.
    fixture = tmp_path / "command-member-events.json"
    fixture.write_text(json.dumps({"events": served.json(), "expected": expected, "run_id": str(run_id)}))
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    report_file = tmp_path / "command-row-vitest.json"
    process = await asyncio.create_subprocess_exec(str(frontend / "node_modules/.bin/vitest"), "run",
        "src/pages/agent-detail/CommandOutcome.q3a.test.tsx", "--reporter=json", f"--outputFile={report_file}",
        cwd=frontend, env={**os.environ, "Q3A_COMMAND_EVENTS": str(fixture),
            "NODE_OPTIONS": "--no-experimental-webstorage"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), 60)
    (tmp_path / "command-row-vitest.log").write_bytes(output)
    assert process.returncode == 0, output.decode()
    report = json.loads(report_file.read_text())
    assert report["numTotalTests"] == report["numPassedTests"] == 1, report
    assert report["numPendingTests"] == report["numFailedTests"] == 0, report


@pytest.mark.parametrize("timed_out", [False, True], ids=["nonzero", "timeout"])
async def test_business_worker_keeps_command_outcome_in_transcript_and_member_row(
    background_command_runtime, owner_sessionmaker, tmp_path, timed_out,
):
    from app.models.plan_request import AgentPlanRequest
    from app.models.task import Task
    from app.schemas.schemas import TaskCreate
    from app.services.plan_authorization_lease import canonical_action_artifact, issue_plan_authorization_leases
    from app.services.plan_mode_core import compute_plan_hash
    from app.services.runtime_task_worker import _execute_claimed_business_task

    state = background_command_runtime
    seed = state.seed
    body = TaskCreate(request_id=str(uuid4()), title="Record command outcome",
        description="Run one command and report its outcome.",
        confirmed_plan_session_id=str(seed["session_id"])).model_dump(mode="json")
    plan_json = {
        "schema": "hive_plan.v1", "intent_type": "in_session_execution", "title": body["title"],
        "objective": body["description"], "steps": [{"order": 1, "description": body["description"]}],
        "success_criteria": ["Report the command outcome."], "stop_conditions": ["The owner cancels."],
        "handoff": {"target": "continue_current_session"}, "authorization_scopes": [{
            "action_kind": "start_long_task", "target_ref": "task:new",
            "arguments": canonical_action_artifact(body), "summary": body["description"],
        }],
    }
    async with state.scoped() as db:
        plan = AgentPlanRequest(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            session_id=str(seed["session_id"]), requested_by_user_id=seed["user_id"],
            confirmed_by_user_id=seed["user_id"], confirmed_at=datetime.now(timezone.utc),
            source="web_chat", intent_type="in_session_execution", original_request=body["description"],
            status="confirmed", plan_version=1, plan_hash=compute_plan_hash(plan_json), plan_json=plan_json)
        db.add(plan)
        await db.flush()
        leases = await issue_plan_authorization_leases(db=db, plan=plan, confirming_user_id=seed["user_id"])
        assert len(leases) == 1
        body.update(confirmed_plan_id=str(plan.id), confirmed_plan_version=1, confirmed_plan_hash=plan.plan_hash)
        await db.commit()
    created = await state.client.post(state.endpoint + "/tasks/", json=body)
    assert created.status_code == 201, created.text
    run_id = await state.claim("business_task")
    assert str(run_id) == created.json()["active_runtime_task_id"]
    _script_command(state, timed_out=timed_out)
    await asyncio.wait_for(_execute_claimed_business_task(run_id), 30)
    assert len(state.llm.requests) == 2 and state.llm.script == []
    async with state.scoped() as db:
        run = await db.get(RuntimeTask, run_id)
        task = await db.get(Task, UUID(created.json()["id"]))
        assert run.status == "completed", run.metadata_json
        assert task.status == "done" and task.active_runtime_task_id == run_id
        session_id = run.child_session_id
    await _assert_command_replay(state, owner_sessionmaker, tmp_path,
        run_id=run_id, session_id=session_id, timed_out=timed_out)


async def test_native_trigger_keeps_nonzero_command_in_transcript_and_member_row(
    background_command_runtime, owner_sessionmaker, tmp_path,
):
    from app.models.chat_session import ChatSession
    from app.services import runtime_task_worker, trigger_daemon, web_chat_runtime

    state = background_command_runtime
    seed = state.seed
    state.llm.script.append({"content": "This work is ready for its scheduled follow-up.", "finish_reason": "stop"})
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(seed["run_id"]), 30)
    await state.boundary(seed["run_id"])
    recovered = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="command-outcome-prior", session_factory=state.scoped, tenant_id=seed["tenant_id"])
    assert recovered["dispatched"] == 1, recovered
    created = await state.client.post(state.endpoint + "/triggers", json={
        "name": "command_outcome", "type": "interval", "is_enabled": False,
        "config": {"minutes": 15, "toolset": ["run_command"]},
        "reason": "Run one command and report its outcome in the scheduled work.",
    })
    assert created.status_code == 201, created.text
    trigger_id = created.json()["id"]
    review = await state.client.post(state.endpoint + "/plan-recommendations", json={
        "original_request": "Enable this follow-up without plan mode.", "session_id": str(seed["session_id"]),
        "source": "agent_aware", "action_kind": "enable_autonomous_wake", "tool_name": "trigger_rest",
    })
    assert review.status_code == 201, review.text
    declined = await state.client.post(state.endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
    assert declined.status_code == 200, declined.text
    enabled = await state.client.patch(state.endpoint + "/triggers/" + trigger_id, json={
        "is_enabled": True, "plan_mode_decision": "declined", "plan_recommendation_id": declined.json()["id"],
    })
    assert enabled.status_code == 200, enabled.text
    fired = await trigger_daemon.fire_trigger_once_now(seed["agent_id"], trigger_id)
    assert fired["fired"], fired
    wrapper_id = await state.claim("trigger")
    assert wrapper_id == UUID(fired["runtime_task_id"])
    assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(wrapper_id))
    await state.boundary(wrapper_id, direct=True)
    dispatched = await runtime_task_worker.recover_session_input_dispatches_once(
        worker_id="command-outcome-input",
        session_factory=state.scoped, tenant_id=seed["tenant_id"])
    assert dispatched["dispatched"] == 1, dispatched
    run_id = await state.claim("web_chat_turn")
    _script_command(state)
    await asyncio.wait_for(web_chat_runtime.execute_web_chat_run(run_id), 30)
    assert len(state.llm.requests) == 3 and state.llm.script == []
    async with state.scoped() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == "completed", run.metadata_json
        assert UUID(run.metadata_json["trigger_runtime_task_id"]) == wrapper_id
        assert run.metadata_json["trigger_ids"] == [trigger_id]
        wrapper = await db.get(RuntimeTask, wrapper_id)
        assert wrapper.status == "completed" and wrapper.metadata_json["delivery"] == "reflection"
        session_id = UUID(run.child_session_id)
        reflection = await db.get(ChatSession, session_id)
        assert session_id != seed["session_id"]
        assert reflection.session_kind == "trigger_run" and reflection.source_channel == "trigger"
        assert reflection.runtime_task_id == wrapper_id and reflection.user_id == seed["user_id"]
    await _assert_command_replay(state, owner_sessionmaker, tmp_path,
        run_id=run_id, session_id=session_id, timed_out=False)
