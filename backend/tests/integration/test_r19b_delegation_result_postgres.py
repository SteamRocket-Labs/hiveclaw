"""Read an inline-CSV colleague result through its completion continuation."""

import asyncio
import json
import sys
from collections import Counter
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.llm import LLMModel
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user import User
from tests.integration.test_failed_delegation_continuation import _call, _execute
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

CSV = "product,sales\n" + "".join(f"Product{i},{i * 10}\n" for i in range(1, 21))
POINTS = "Twenty sales rows; total sales 2100; Product20 leads with 200."
ANSWER = "The colleague checked all 20 rows: sales total 2100, led by Product20."


def _round(name, arguments, identity):
    return {"tool_calls": [_call(name, arguments, identity)], "finish_reason": "tool_calls"}


async def _tool_results(owner, run_id):
    from app.services.session_semantic_history import _event_payload

    async with owner() as db:
        calls = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run_id)))
        return {call.provider_tool_use_id: _event_payload(await db.get(ChatTranscriptEvent, call.result_event_id))
            for call in calls if call.result_event_id}


async def test_inline_csv_colleague_result_reaches_parent_continuation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.database as database
    from app.agents import orchestrator
    from app.api import upload
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.runtime import invoker
    from app.services import agent_context, agent_manager, agent_tools, content_input_receipts, runtime_result_sources
    from app.services.agent_manager import AgentManager
    from app.services.agent_tool_domains import code_exec
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_live_input import submit_live_human_input
    from app.memory.write_gate import MemoryWriteDecision
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

    prompt = "Ask Sales colleague for three sales points from my CSV, then summarize its result."
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt=prompt)
    child_agent_id = uuid4()
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        parent = await db.get(Agent, seed["agent_id"])
        parent.max_tool_rounds = 15
        (await db.get(User, seed["user_id"])).role = "member"
        root = await db.get(RuntimeTask, seed["run_id"])
        root.metadata_json = {**root.metadata_json, "source": "websocket_message"}
        await AgentManager().initialize_agent_files(db, parent, original_author_id=seed["user_id"])
        model = await db.get(LLMModel, parent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        model.max_input_tokens = 256_000
        child = Agent(id=child_agent_id, tenant_id=seed["tenant_id"], name="Sales colleague",
            creator_id=seed["user_id"], owner_user_id=seed["user_id"], primary_model_id=model.id)
        db.add(child)
        await db.flush()
        await AgentManager().initialize_agent_files(db, child, original_author_id=seed["user_id"])
        for capability in ("agent.message.send", "agent.async_task.read", "agent.context.read",
            "workspace.file.read", "workspace.file.write", "workspace.code.execute", "agent.memory.write", "agent.memory.read"):
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()

    # Replace only database connections and the model/process transports. The
    # native context, policy, source graph, tools and every lane stay connected.
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(original_content=content, content=content, category=category,
            sensitivity="PL1_public", metadata={"threat_gate_method": "controlled_reference_reviewer"})

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)
    memory_work = SimpleNamespace(tenant=seed["tenant_id"], agent=seed["agent_id"], user=seed["user_id"],
        root=seed["agent_root"], service=agent_tools._get_tool_runtime_service())
    memory_work.session, memory_work.run = await new_owner_work(memory_work, owner_sessionmaker)
    saved, _ = await memory_call(memory_work, app_user_sessionmaker, "save_memory",
        {"category": "user", "content": "Remember to summarize sales in three short points."})
    assert "Saved memory" in saved, saved
    memory_entry = json.loads((seed["agent_root"] / "memory/explicit/manifest.jsonl").read_text().splitlines()[-1])["id"]
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, memory_work.run)).status = "completed"
        (await db.get(RuntimeTask, seed["run_id"])).status = "completed"
        seed["session_id"] = uuid4()
        db.add(ChatSession(id=seed["session_id"], tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            user_id=seed["user_id"], audience_kind="private", source_channel="web"))
        await db.commit()
    # The retained production employees have no platform-skill-file inputs.
    # Omit this newly provisioned optional catalog entry before any turn reads it.
    for agent_id in (seed["agent_id"], child_agent_id):
        (seed["data_root"] / str(agent_id) / "skills/mcp-installer/SKILL.md").unlink()

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)

    async def current_user():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, seed["user_id"])

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(upload.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        uploaded = await client.post("/chat/upload", data={"agent_id": str(seed["agent_id"]),
            "skip_personal_kb": "true"}, files={"file": ("sales.csv", CSV.encode(), "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        accepted = await submit_live_human_input(db=db, agent=await db.get(Agent, seed["agent_id"]),
            user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
            content=prompt, source="web", attachments=[{"path": attachment["workspace_path"],
                "filename": "sales.csv", "original_attachment": attachment["original_attachment"]}])
        await db.commit()
        assert accepted.get("run"), accepted
        seed["run_id"] = UUID(accepted["run"]["run_id"])

    async def execute_in_inherited_sandbox(command, *, work_dir, env, timeout, **_kwargs):
        # suite_full already provides netdeny.sb; nested sandbox-exec is denied.
        process = await asyncio.create_subprocess_exec(*command, cwd=work_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        assert process.returncode == 0 and b"2100" in stdout, (stdout, stderr)
        return CodeExecutionResult(stdout=stdout.decode(), stderr=stderr.decode(), exit_code=process.returncode,
            evidence={"provider": "test_inherited_network_sandbox"})

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr(code_exec, "execute_agent_command", execute_in_inherited_sandbox)

    async def scripted_session_memory(**_kwargs):
        return json.dumps({"current_state": "The scripted sales task completed."})

    monkeypatch.setattr("app.services.session_memory._run_session_memory_llm", scripted_session_memory)
    code = "import csv, io\nrows = list(csv.DictReader(io.StringIO(" + repr(CSV) + ")) )\nprint(sum(int(r['sales']) for r in rows))"
    parent_client = ScriptedLLMClient([
        _round("read_file", {"path": attachment["workspace_path"]}, "parent-csv"),
        _round("load_memory", {"ids": [memory_entry]}, "parent-memory"),
        {"tool_calls": [
            _call("report_progress", {"message": "Asking the sales colleague to check the rows."}, "parent-progress"),
            _call("delegate_to_agent", {"agent_name": "Sales colleague", "expected_output": "Three sales points",
                "message": "List three sales points from these rows:\n" + CSV, "tool_profile": "worker_safe"}, "delegate"),
        ], "finish_reason": "tool_calls"},
        _round("execute_code", {"language": "python", "code": code, "timeout": 10}, "parent-code"),
        {"content": "The colleague is processing the sales rows."},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: parent_client)
    await _execute(owner_sessionmaker, seed["run_id"], "r19b-parent")
    parent_results = await _tool_results(owner_sessionmaker, seed["run_id"])
    assert "Product20,200" in parent_results["parent-csv"]["content"]
    assert "three short points" in parent_results["parent-memory"]["content"]
    assert "2100" in parent_results["parent-code"]["content"]
    async with owner_sessionmaker() as db:
        parent = await db.get(RuntimeTask, seed["run_id"])
        assert parent.status == "completed", parent.result_summary
        child_task = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.root_runtime_task_id == parent.id, RuntimeTask.task_type == "delegation"))).one()
        child_id = child_task.id
        child_task.status, child_task.claimed_by, child_task.claim_version = "running", "r19b-colleague", 1
        child_task.attempt_count = 1
        child_task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()

    child_client = ScriptedLLMClient([
        _round("execute_code", {"language": "python", "code": code, "timeout": 10}, "child-code"),
        _round("write_file", {"path": "workspace/sales-points.md", "content": POINTS}, "child-write"),
        {"content": POINTS},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: child_client)
    fence = set_runtime_task_fence(task_id=child_id, worker_id="r19b-colleague", claim_version=1)
    try:
        assert await orchestrator.dispatch_persisted_async_delegation(child_id.hex, wait_for_completion=True)
    finally:
        reset_runtime_task_fence(fence)
    child_results = await _tool_results(owner_sessionmaker, child_id)
    print("R19B_CHILD_TOOLS", json.dumps({name: {"outcome": result.get("outcome"),
        "parts_count": len(result.get("parts") or []), "metadata": result.get("metadata")}
        for name, result in child_results.items()}, ensure_ascii=False, default=str))
    assert "2100" in child_results["child-code"]["content"]
    assert child_results["child-code"]["metadata"]["skill_input_receipt"]["status"] == "verified"
    async with owner_sessionmaker() as db:
        child_task = await db.get(RuntimeTask, child_id)
        assert child_task.status == "completed", child_task.result_summary
        model = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == child_id)
            .order_by(SessionModelResult.round_id.desc()))).first()
        dependencies = model.model_request_snapshot_json["input_bindings"]["dependencies"]
        print("R19B_CHILD_DEPENDENCIES", json.dumps(dependencies))
        kinds = Counter(source["source_kind"] for source in dependencies)
        print("R19B_CHILD_KINDS", json.dumps(kinds, sort_keys=True))
        assert kinds == Counter({"agent_context_resource": 4, "agent_directory_observation": 2, "agent_identity": 2,
            "delegation_context": 1, "explicit_memory": 1, "owner_action_policy": 2, "session_actor_identity": 2,
            "session_command": 1, "session_tool_invocation": 4, "workspace_artifact": 1})

    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker,
        data_root=seed["data_root"])
    drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r19b-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert drained["delivered"] == 1, drained
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="r19b-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="r19b-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1
    delivered = await service._deliver_page(pages[0])
    assert delivered.get("ok") is True, delivered
    async with owner_sessionmaker() as db:
        notification = (await db.scalars(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.source_run_id == str(child_id)))).one()
        continuation = (await db.scalars(select(RuntimeTask).where(
            RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id)))).one()
        return_id, result_ref = continuation.id, notification.result_ref
        assert result_ref in json.dumps(pages[0].manifest)

    reader_client = ScriptedLLMClient([
        _round("read_runtime_result", {"result_ref": result_ref, "offset": 0, "limit": 12000}, "parent-result"),
        {"content": ANSWER},
    ])
    checking = ContextVar("r19b_result_read", default=False)
    read_routes = set()
    validate_result = runtime_result_sources.validate_runtime_result_sources
    validate_batch = content_input_receipts._validate_input_dependency_batch

    async def observe_result_read(*args, **kwargs):
        token = checking.set(True)
        try:
            return await validate_result(*args, **kwargs)
        finally:
            checking.reset(token)

    async def observe_source_routes(db, tenant_id, sources, **kwargs):
        if checking.get():
            for source in sources:
                read_routes.add((source["source_kind"], source["source_id"], kwargs.get("runtime_request") is None,
                    bool(getattr(content_input_receipts, "_batch_browser_point", lambda *_args: False)(db, kwargs.get("user")))))
        return await validate_batch(db, tenant_id, sources, **kwargs)

    monkeypatch.setattr(runtime_result_sources, "validate_runtime_result_sources", observe_result_read)
    monkeypatch.setattr(content_input_receipts, "_validate_input_dependency_batch", observe_source_routes)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: reader_client)
    await _execute(owner_sessionmaker, return_id, "r19b-reader")
    print("R19B_READ_ROUTES", json.dumps(sorted(read_routes)))
    read = (await _tool_results(owner_sessionmaker, return_id))["parent-result"]
    page = json.loads(read["content"])
    print("R19B_RESULT_READ", json.dumps({key: value for key, value in page.items() if key != "content"}))
    assert page["status"] == "ok", page
    assert page["complete"] is True and POINTS in page["content"], page
    assert page["dependency_coverage"]["complete"] is True
    assert any(POINTS in message.get("content", "") for message in reader_client.requests[1]["messages"]
        if message.get("tool_call_id") == "parent-result")
    async with owner_sessionmaker() as db:
        continuation = await db.get(RuntimeTask, return_id)
        assert continuation.status == "completed" and continuation.result_summary == ANSWER
