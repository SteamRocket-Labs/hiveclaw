"""Colleague requests return immediately, resume with results, and share stop scope."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.agents import orchestrator
from app.config import get_settings
from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.capability_policy import CapabilityPolicy
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.services.agent_manager import AgentManager
from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from app.services.web_chat_runtime import execute_web_chat_run
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run
from tests.services.test_r2_stop_settlement import _cancel

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

COLLEAGUE = "周报助手"
RESULT = "已核实三点：报表核对完成；发票补录进行中；合同待确认。"


class PausedClient(ScriptedLLMClient):
    def __init__(self, script, *, pause_at):
        super().__init__(script)
        self.pause_at = pause_at
        self.started, self.release, self.cancelled = (asyncio.Event() for _ in range(3))

    async def stream(self, **kwargs):
        if len(self.requests) == self.pause_at:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return await super().stream(**kwargs)


def _tool(name, arguments):
    return {"tool_calls": [{"id": f"call-{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}], "finish_reason": "tool_calls"}


async def _setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, *, tool, paused=False):
    import app.database as database
    import app.runtime.invoker as invoker
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.secrets_provider import get_secrets_provider

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="请周报助手给出三点，并用它们写总结。")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    seed["agent_root"].joinpath("workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        parent = await db.get(Agent, seed["agent_id"])
        parent.name = "My Assistant"
        model = await db.get(LLMModel, parent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-only")
        child_model = LLMModel(id=uuid4(), tenant_id=seed["tenant_id"], provider="openai",
            model="fake-colleague", label="Synthetic colleague", enabled=True,
            api_key_encrypted=model.api_key_encrypted)
        db.add(child_model)
        child = Agent(id=uuid4(), tenant_id=seed["tenant_id"], name=COLLEAGUE,
            role_description="Summarize the supplied business facts.", creator_id=seed["user_id"],
            owner_user_id=seed["user_id"], primary_model_id=child_model.id)
        db.add(child)
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="agent.message.send",
            allowed=True, requires_approval=False))
        await db.flush()
        for agent in (parent, child):
            await AgentManager().initialize_agent_files(db, agent,
                personality="Use the current employee display name.", original_author_id=seed["user_id"])
        seed["child_agent_id"] = child.id
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    parent_script = [_tool(tool, {"agent_name": COLLEAGUE, "message": "请核实并返回三点业务进展。", "msg_type": "consult"}),
        {"content": "周报助手正在处理，完成后我会用它的结果整理总结。"}]
    parent_client = PausedClient(parent_script, pause_at=1 if paused else 99)
    child_client = PausedClient([{"content": RESULT}], pause_at=0)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kwargs:
        child_client if kwargs["model"] == "fake-colleague" else parent_client)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config",
        lambda *_args, **_kwargs: ScriptedLLMClient([{"content": '{"summary": "Synthetic colleague work."}'}]))
    return seed, parent_client, child_client


async def _execute(run_id, factory, *, worker, delegation=False):
    async with factory() as db:
        run = await db.get(RuntimeTask, run_id)
        run.status, run.claimed_by, run.claim_version = "running", worker, 1
        run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
    token = set_runtime_task_fence(task_id=run_id, worker_id=worker, claim_version=1)
    try:
        if delegation:
            assert await orchestrator.dispatch_persisted_async_delegation(run_id.hex, wait_for_completion=True)
        else:
            await execute_web_chat_run(run_id)
    finally:
        reset_runtime_task_fence(token)


async def _child(factory, seed):
    async with factory() as db:
        return await db.scalar(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "delegation", RuntimeTask.root_runtime_task_id == seed["run_id"]))


async def _session_api(factory, seed, *, resource="transcript"):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import chat_sessions
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.user import User

    async def scoped_db():
        async with factory() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db

    async def viewer():
        async for db in scoped_db():
            return await db.get(User, seed["user_id"])

    app = FastAPI()
    app.include_router(chat_sessions.router)
    app.dependency_overrides[get_current_user] = viewer
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        response = await client.get(f"/agents/{seed['agent_id']}/sessions/{seed['session_id']}/{resource}",
            params={"schema_version": 2, "limit": 1000})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("tool", ["send_message_to_agent", "delegate_to_agent"])
async def test_colleague_request_returns_and_automatically_resumes_with_its_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, tool,
):
    import app.runtime.invoker as invoker
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    seed, parent, child_client = await _setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool=tool)
    await asyncio.wait_for(_execute(seed["run_id"], owner_sessionmaker, worker="r11c-parent"), 20)
    assert not child_client.started.is_set(), "The parent must return before the colleague needs a provider reply"
    child = await _child(owner_sessionmaker, seed)
    assert child is not None
    wire = json.dumps(parent.requests[1]["messages"], ensure_ascii=False)
    assert "Completion automatically returns to this conversation" in wire
    assert "Do not sleep, poll, or resend to wait" in wire
    assert "use check_async_task only" not in wire
    descriptions = json.dumps(parent.requests[0]["tools"], ensure_ascii=False)
    assert "synchronous collaboration" not in descriptions
    (tmp_path / "parent-provider-wire.json").write_text(json.dumps(parent.requests, ensure_ascii=False, indent=2))

    child_client.release.set()
    await _execute(child.id, owner_sessionmaker, worker="r11c-colleague", delegation=True)
    child_wire = "\n".join(str(message["content"]) for message in child_client.requests[0]["messages"])
    assert f'Your current display name is "{COLLEAGUE}".' in child_wire
    assert 'You are "My Assistant"' not in child_wire
    processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker,
        data_root=seed["data_root"])
    boundaries = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r11c-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert boundaries["delivered"] == 1, boundaries
    service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await service.claim_batch(worker_id="r11c-return", limit=100)
    pages = await service.prepare_integration_pages(worker_id="r11c-return",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert pages
    for page in pages:
        assert (await service._deliver_page(page))["ok"] is True
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        notice = await db.scalar(select(RuntimeNotificationOutbox).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"],
            RuntimeNotificationOutbox.source_run_id == str(child.id)))
        assert notice.parent_session_id == seed["session_id"]
        continuation = await db.scalar(select(RuntimeTask).where(
            RuntimeTask.tenant_id == seed["tenant_id"], RuntimeTask.parent_agent_id == seed["agent_id"],
            RuntimeTask.metadata_json["integration_page_id"].astext.in_([str(page.id) for page in pages])))
        assert continuation is not None and continuation.parent_session_id == str(seed["session_id"])
        result_ref = notice.result_ref
    resumed = ScriptedLLMClient([_tool("read_runtime_result", {"result_ref": result_ref}),
        {"content": f"根据周报助手核实的结果整理：{RESULT}"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: resumed)
    await _execute(continuation.id, owner_sessionmaker, worker="r11c-return-model")
    assert RESULT in json.dumps(resumed.requests[1]["messages"], ensure_ascii=False)
    async with owner_sessionmaker() as db:
        parent_result = await db.get(RuntimeTask, continuation.id)
        assert parent_result.status == "completed" and RESULT in parent_result.result_summary
    events = await _session_api(app_user_sessionmaker, seed)
    finals = [event for event in events if event.get("item_kind") == "assistant_final"
        and event["lifecycle"] == "completed" and event["scope"]["run_id"] == str(continuation.id)]
    assert len(finals) == 1, finals
    sources = finals[0]["payload"]["source_blocks"]
    assert sources
    displayed = "".join(event["payload"].get("content", "") for source in sources for event in events
        if event.get("item_id") == source["item_id"])
    assert RESULT in displayed


async def test_member_stop_cancels_the_running_colleague_and_preserves_other_runs(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    seed, parent, child_client = await _setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="delegate_to_agent", paused=True)
    parent_task = asyncio.create_task(_execute(seed["run_id"], owner_sessionmaker, worker="r11c-parent-stop"))
    child_task = None
    try:
        await asyncio.wait_for(parent.started.wait(), 20)
        child = await _child(owner_sessionmaker, seed)
        assert child is not None
        unrelated_id = uuid4()
        async with owner_sessionmaker() as db:
            db.add(RuntimeTask(id=unrelated_id, tenant_id=seed["tenant_id"], task_type="delegation",
                status="pending", parent_agent_id=seed["agent_id"], child_agent_id=seed["child_agent_id"],
                root_user_id=seed["user_id"], parent_session_id=str(seed["session_id"]),
                child_session_id=str(uuid4()), root_runtime_task_id=uuid4()))
            await db.commit()
        child_task = asyncio.create_task(_execute(child.id, owner_sessionmaker,
            worker="r11c-child-stop", delegation=True))
        await asyncio.wait_for(child_client.started.wait(), 20)
        async with owner_sessionmaker() as db:
            receipt = await _cancel(db, seed["agent_id"], seed["session_id"], seed["run_id"], seed["user_id"])
        assert receipt["status"] == "applying"
        await asyncio.wait_for(child_client.cancelled.wait(), 5)
        parent.release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(parent_task, child_task, return_exceptions=True), 20)
        assert not isinstance(outcomes[0], BaseException)
        assert isinstance(outcomes[1], asyncio.CancelledError)
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, child.id)).status == "killed"
            assert (await db.get(RuntimeTask, seed["run_id"])).status == "killed"
            assert (await db.get(RuntimeTask, unrelated_id)).status == "pending"
        child_view = await _session_api(app_user_sessionmaker,
            {**seed, "agent_id": child.child_agent_id, "session_id": UUID(child.child_session_id)}, resource="workbench")
        assert any(run["id"] == str(child.id) and run["status"] == "killed" and run["user_status"] == "已停止"
            for run in child_view["runtime_tasks"]), child_view
    finally:
        parent.release.set()
        child_client.release.set()
        for task in (child_task, parent_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (child_task, parent_task) if task is not None), return_exceptions=True)
