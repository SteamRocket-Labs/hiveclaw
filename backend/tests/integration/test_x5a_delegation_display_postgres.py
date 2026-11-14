"""Run20 delegation display facts through real dispatch, reads and stop events."""

import asyncio
import json
import os
from pathlib import Path
import uuid

import pytest
from sqlalchemy import select

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.services.session_event_contract import serialize_session_event
from tests.integration import test_m1g_originated_creation_postgres as creation

origin = creation.origin
direct_origin = creation.direct_origin
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_x5a_first_task_dispatch_records_one_child_id_started_signal(
    direct_origin, owner_sessionmaker, monkeypatch,
):
    from app.models.hr_creation import HrCreationDraft
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    user, context, _, _ = direct_origin
    result = await creation._direct_call(context, first_task="Introduce your work in one sentence")

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, uuid.UUID(result["provisioning_task_id"]))
        task.status = "running"
        source = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
        source.status = "completed"
        await db.commit()
    assert await execute_claimed_hr_provisioning(task.id) == "completed"
    assert await execute_claimed_hr_provisioning(task.id) == "completed"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(result["blueprint_id"]))
        child_id = uuid.uuid5(draft.id, "explicit-first-task.v1")
        events = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == user.tenant_id,
            ChatTranscriptEvent.session_id == uuid.UUID(context.session_id),
            ChatTranscriptEvent.event_type == "runtime_action_started")))
        assert len(events) == 1
        projected = serialize_session_event(events[0], audience="user")
        metadata = projected["payload"]["metadata"]
        assert metadata["runtime_task_id"] == str(child_id), "The child, not its completed parent, owns the work"
        assert metadata["action_kind"] == "a2a_delegation" and metadata["status"] == "running"
        assert metadata["child_session_id"] == str(uuid.uuid5(child_id, "session"))


@pytest.mark.parametrize("initially_dispatched", [False, True])
async def test_x5a_first_task_real_tool_recovery_and_replay_have_one_start_and_finish_parent(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, initially_dispatched,
):
    import sys
    import app.database as database
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_session import ChatSession
    from app.models.user import User
    from app.models.hr_creation import HrCreationDraft
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools, web_chat_runtime
    from app.services.agent_manager import AgentManager
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning
    from app.services.secrets_provider import get_secrets_provider
    from tests.integration.test_r11c_colleague_lifecycle_postgres import PausedClient, _execute, _session_api, _tool
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    user, context, model_id, _ = direct_origin
    root = tmp_path / "agent-data"
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(root))
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", root)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", root)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", root)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", root)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    # Route every imported production session factory to the isolated app role.
    factory = web_chat_runtime._async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        model = await db.get(LLMModel, model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-only")
        parent = await db.get(Agent, context.agent_id)
        await AgentManager().initialize_agent_files(db, parent, personality="Assist the owner.", original_author_id=user.id)
        await db.commit()
    message = "Introduce your work in one sentence"
    result = await creation._direct_call(context, first_task=message)

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    async with owner_sessionmaker() as db:
        policy = await db.scalar(select(CapabilityPolicy).where(
            CapabilityPolicy.tenant_id == user.tenant_id, CapabilityPolicy.capability == "agent.message.send"))
        policy.allowed = initially_dispatched
        task = await db.get(RuntimeTask, uuid.UUID(result["provisioning_task_id"]))
        task.status = "running"
        source = await db.get(RuntimeTask, uuid.UUID(context.runtime_task_id))
        source.status = "completed"
        await db.commit()
    assert await execute_claimed_hr_provisioning(task.id) == ("completed" if initially_dispatched else "failed")
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(result["blueprint_id"]))
        assert draft.created_agent_id is not None
        assert draft.provisioning_json["first_task"]["status"] == ("dispatched" if initially_dispatched else "blocked")
        employee = await db.get(Agent, draft.created_agent_id)
        policy = await db.scalar(select(CapabilityPolicy).where(
            CapabilityPolicy.tenant_id == user.tenant_id, CapabilityPolicy.capability == "agent.message.send"))
        policy.allowed = True
        await db.commit()
    arguments = {"first_task_draft_id": str(draft.id), "agent_name": employee.name,
        "target_agent_id": str(employee.id), "message": message}
    seed = {"agent_id": context.agent_id, "session_id": uuid.UUID(context.session_id),
        "tenant_id": user.tenant_id, "user_id": user.id}
    # Both calls go through the real model/tool loop and completed-tool callback;
    # the second call is recovery after dispatch, not an early provisioning return.
    for index in range(2):
        call_id = f"first-task-recovery-{index}"
        tool_call = _tool("delegate_to_agent", arguments)
        tool_call["tool_calls"][0]["id"] = call_id
        client = ScriptedLLMClient([tool_call, {"content": "同事正在执行首个任务。"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        async with owner_sessionmaker() as db:
            session = await db.get(ChatSession, seed["session_id"])
            parent = await db.get(Agent, context.agent_id)
            actor = await db.get(User, user.id)
            queued = await web_chat_runtime.start_web_chat_run(db=db, agent=parent, user=actor, session=session,
                content="Resume the recorded first task without changing it.")
            await db.commit()
        run_id = uuid.UUID(queued["run_id"])
        await asyncio.wait_for(_execute(run_id, owner_sessionmaker, worker=f"x5a-recovery-{index}"), 30)
        assert len(client.requests) == 2
        tool_messages = [m for m in client.requests[1]["messages"] if m["role"] == "tool" and m["tool_call_id"] == call_id]
        assert len(tool_messages) == 1
        page = json.loads(tool_messages[0]["content"])
        assert page["ok"] is True and page["operation"] == "delegate"
        assert page["first_task_draft_id"] == str(draft.id)
        assert page["runtime_task_id"] == str(uuid.uuid5(draft.id, "explicit-first-task.v1"))
    child_id = uuid.uuid5(draft.id, "explicit-first-task.v1")
    async with owner_sessionmaker() as db:
        starts = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == seed["session_id"], ChatTranscriptEvent.event_type == "runtime_action_started")))
        assert len(starts) == 1
        assert starts[0].metadata_json["runtime_task_id"] == str(child_id)
        assert len(list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == user.tenant_id,
            RuntimeTask.task_type == "delegation")))) == 1
    child_client = PausedClient([{"content": "我负责整理研究草稿。"}], pause_at=0)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    child_execution = asyncio.create_task(_execute(child_id, owner_sessionmaker, worker="x5a-first-task-child", delegation=True))
    try:
        await asyncio.wait_for(child_client.started.wait(), 20)
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, child_id)).status == "running"
        running_events = await _session_api(app_user_sessionmaker, seed)
        assert sum(e.get("legacy_event_type") == "runtime_action_started" for e in running_events) == 1
    finally:
        child_client.release.set()
        await asyncio.wait_for(child_execution, 30)
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=root)
    boundaries = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=user.tenant_id, worker_id="x5a-first-task-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert boundaries["delivered"] == 1, boundaries
    async with owner_sessionmaker() as db:
        assert (await db.get(RuntimeTask, child_id)).status == "completed"
    events = await _session_api(app_user_sessionmaker, seed)
    fixture = tmp_path / "first-task-events.json"
    fixture.write_text(json.dumps(events))
    running_fixture = tmp_path / "first-task-running-events.json"
    running_fixture.write_text(json.dumps(running_events))
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    process = await asyncio.create_subprocess_exec(str(frontend / "node_modules/.bin/vitest"), "run",
        "src/pages/agent-detail/RunDisclosureBlock.x5a.test.tsx", "-t", "X5a-(r2-1|r3-5) integration",
        cwd=frontend, env={**os.environ, "X5A_FIRST_TASK_EVENTS": str(fixture),
            "X5A_FIRST_TASK_RUNNING_EVENTS": str(running_fixture), "NODE_OPTIONS": "--no-experimental-webstorage"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await asyncio.wait_for(process.communicate(), 60)
    assert process.returncode == 0, output.decode()


@pytest.mark.parametrize("task_type,side", [
    ("subagent", "child"), ("a2a_delegation", "child"), ("delegation", "parent"),
    ("web_chat_turn", "invalid"), ("delegation", "neither"),
])
async def test_x5a_terminal_start_binds_either_side_and_tolerates_non_uuid_sessions(
    direct_origin, owner_sessionmaker, task_type, side,
):
    from datetime import datetime
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    user, context, _, _ = direct_origin
    run_id, session_id = uuid.UUID(context.runtime_task_id), uuid.UUID(context.session_id)
    start = datetime.fromisoformat("2026-11-14T04:35:22.261668+00:00")
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        task.task_type, task.started_at, task.status = task_type, start, "killed"
        if task_type == "subagent":
            # Production subagents use their parent agent on the child session.
            task.child_agent_id = None
        other = "legacy-non-uuid-session" if side == "invalid" else str(uuid.uuid4())
        task.parent_session_id = str(session_id) if side == "parent" else other
        task.child_session_id = str(session_id) if side in {"child", "invalid"} else other
        draft = SessionEventDraft(item_id=run_id, item_kind="run", lifecycle="cancelled",
            scope={"level": "run", "session_id": str(session_id), "thread_id": str(session_id),
                "turn_id": f"turn-{run_id.hex}", "run_id": str(run_id)},
            actor={"type": "runtime"}, payload={"terminal_source": "tool_card_finalizer"})
        if side == "neither":
            from sqlalchemy.exc import IntegrityError

            # The existing database authority boundary rejects the event too.
            with pytest.raises(IntegrityError, match="session_event_run_authority_mismatch"):
                await append_session_events(db, tenant_id=user.tenant_id, agent_id=context.agent_id,
                    session_id=session_id, drafts=[draft])
            return
        rows = await append_session_events(db, tenant_id=user.tenant_id, agent_id=context.agent_id,
            session_id=session_id, drafts=[draft])
        await db.commit()
        assert serialize_session_event(rows[0], audience="user")["payload"]["started_at"] == start.isoformat()


async def test_x5a_hire_result_read_carries_its_actual_kind(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.tools.handlers.context_resources import read_runtime_result
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from tests.integration.test_x1_creation_result_verification_postgres import _seed_creation, _enqueue, _stored_result

    seed = await _seed_creation(owner_sessionmaker)
    await _enqueue(owner_sessionmaker, seed, legacy=False)
    result_id, sha = await _stored_result(owner_sessionmaker, seed)
    async with owner_sessionmaker() as db:
        from app.models.hr_creation import HrCreationDraft

        task = await db.get(RuntimeTask, seed["task_id"])
        draft = await db.get(HrCreationDraft, seed["draft_id"])
        source_run_id = draft.preview_json["creation_authorization"]["source_run_id"]
    monkeypatch.setattr("app.tools.handlers.context_resources.async_session", app_user_sessionmaker)
    page = json.loads(await read_runtime_result(ToolExecutionRequest(tool_name="read_runtime_result",
        arguments={"result_ref": f"runtime-result://{result_id}/{sha}"}, context=ToolExecutionContext(
            agent_id=task.parent_agent_id, tenant_id=str(task.tenant_id), user_id=task.root_user_id,
            session_id=task.parent_session_id, runtime_task_id=source_run_id, workspace=tmp_path))))
    assert page["status"] == "ok", page
    assert page["source_kind"] == "employee_creation"
    # Native run20 seq118 has outcome=success and no args on the result.
    from app.services.session_event_contract import _member_tool_summary
    summary = _member_tool_summary({"tool_name": "read_runtime_result", "outcome": "success",
        "invocation_id": str(uuid.uuid4()), "content": json.dumps(page)}, item_kind="tool_result")
    assert summary == {"kind": "runtime_result", "state": "done"}


@pytest.mark.parametrize("lifecycle", ["completed", "failed", "cancelled", "needs_reconciliation"])
async def test_x5a_terminal_events_carry_the_server_start_without_a_queued_item(
    direct_origin, owner_sessionmaker, lifecycle,
):
    from datetime import datetime
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events

    user, context, _, _ = direct_origin
    start = datetime.fromisoformat("2026-11-14T04:35:22.261668+00:00")
    cancelling = datetime.fromisoformat("2026-11-14T04:38:21.510771+00:00")
    end = datetime.fromisoformat("2026-11-14T04:38:22.807647+00:00")
    run_id, session_id = uuid.UUID(context.runtime_task_id), uuid.UUID(context.session_id)
    scope = {"level": "run", "session_id": str(session_id), "thread_id": str(session_id),
        "turn_id": f"turn-{run_id.hex}", "run_id": str(run_id)}
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        task.status, task.started_at, task.completed_at = "killed" if lifecycle == "cancelled" else lifecycle, start, end
        rows = await append_session_events(db, tenant_id=user.tenant_id, agent_id=context.agent_id,
            session_id=session_id, drafts=[
                SessionEventDraft(item_id=run_id, item_kind="run", lifecycle="cancelling", scope=scope,
                    actor={"type": "runtime"}, payload={"control_id": "stop"}, occurred_at=cancelling),
                SessionEventDraft(item_id=run_id, item_kind="run", lifecycle=lifecycle, scope=scope,
                    actor={"type": "runtime"}, payload={"terminal_source": "tool_card_finalizer"}, occurred_at=end),
            ])
        await db.commit()
        terminal = serialize_session_event(rows[-1], audience="user")
        assert terminal["payload"]["started_at"] == start.isoformat()
        assert terminal["occurred_at"] == end.isoformat()
        assert terminal["payload"]["terminal_source"] == "tool_card_finalizer"


async def test_x5a_actual_child_stop_events_render_after_the_task_without_reopening(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from tests.integration.test_r11c_colleague_lifecycle_postgres import _setup, _execute, _child, _cancel, _session_api

    seed, parent, child_client = await _setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent", paused=True)
    parent_task = asyncio.create_task(_execute(seed["run_id"], owner_sessionmaker, worker="x5a-parent"))
    child_task = None
    try:
        await asyncio.wait_for(parent.started.wait(), 20)
        child = await _child(owner_sessionmaker, seed)
        assert child is not None
        child_task = asyncio.create_task(_execute(child.id, owner_sessionmaker, worker="x5a-child", delegation=True))
        await asyncio.wait_for(child_client.started.wait(), 20)
        async with owner_sessionmaker() as db:
            await _cancel(db, seed["agent_id"], seed["session_id"], seed["run_id"], seed["user_id"])
        await asyncio.wait_for(child_client.cancelled.wait(), 5)
        parent.release.set()
        await asyncio.wait_for(asyncio.gather(parent_task, child_task, return_exceptions=True), 20)
        events = await _session_api(app_user_sessionmaker,
            {**seed, "agent_id": child.child_agent_id, "session_id": uuid.UUID(child.child_session_id)})
        assert any(e.get("item_kind") == "run" and e["lifecycle"] == "cancelled" for e in events)
        assert any(e.get("item_kind") == "human_input" for e in events)
        fixture = tmp_path / "child-stop.json"
        fixture.write_text(json.dumps(events))
        frontend = Path(__file__).resolve().parents[3] / "frontend"
        process = await asyncio.create_subprocess_exec(str(frontend / "node_modules/.bin/vitest"), "run",
            "src/pages/agent-detail/RunDisclosureBlock.x5a.test.tsx", "-t", "X5a-5 integration",
            cwd=frontend, env={**os.environ, "X5A_CHILD_EVENTS": str(fixture), "NODE_OPTIONS": "--no-experimental-webstorage"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, output.decode()
    finally:
        parent.release.set()
        child_client.release.set()
        for task in (parent_task, child_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (parent_task, child_task) if task is not None), return_exceptions=True)
