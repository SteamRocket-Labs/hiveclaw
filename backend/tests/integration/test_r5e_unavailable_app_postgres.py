"""Unavailable applications and browsers expose a truthful, recoverable tool surface."""

import json
import os
import sys
import time
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.models.environment_browser import EnvironmentBrowser
from app.models.execution_environment import EnvironmentLease
from app.models.mcp_server import MCPServer
from app.models.session_v2 import SessionToolInvocation
from app.models.user import User
from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from tests.integration.test_m2ad_browser_control_postgres import browser_case as browser_case
from tests.integration.test_m2ad_browser_tools_postgres import browser_tools as browser_tools, operation
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def app_work(owner_sessionmaker, app_user_sessionmaker, tmp_path, monkeypatch, request):
    import app.database as database
    import app.services.secrets_provider as secrets_provider
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager

    s = await _seed_run(owner_sessionmaker, tmp_path,
        prompt=getattr(request, "param", "帮我看看我 GitHub 上最近的通知"))
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(s["data_root"]))
    monkeypatch.setattr(secrets_provider, "_provider", secrets_provider._provider)
    secrets_provider.init_secrets_provider(os.environ["SECRETS_MASTER_KEY"])
    (s["agent_root"] / "workspace").rmdir()
    s["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, s["agent_id"])
        (await db.get(User, s["user_id"])).role = "member"
        await AgentManager().initialize_agent_files(db, agent, personality="Member assistant",
            original_author_id=s["user_id"])
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = secrets_provider.get_secrets_provider().encrypt("local-scripted-model")
        model.max_input_tokens = 256_000
        await db.commit()
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", s["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", s["data_root"])
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", s["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", s["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    return s


def _error_payload(result):
    return json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])


async def test_member_absent_company_app_finishes_from_truthful_discovery(
    app_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.services.agent_tools import get_agent_tools_for_llm
    from app.services.web_chat_runtime import execute_web_chat_run

    s = app_work
    async with owner_sessionmaker() as db:
        (await db.get(User, s["user_id"])).role = "member"
        await db.commit()
    visible = {item["function"]["name"] for item in await get_agent_tools_for_llm(s["agent_id"], core_only=False)}
    assert "import_mcp_server" not in visible

    class AppLookupModel(ScriptedLLMClient):
        async def stream(self, **kwargs):
            messages = kwargs["messages"]
            tool_results = [str(m.content) for m in messages if m.role == "tool"]
            complete = bool(tool_results and "company_applications:\n- none" in tool_results[-1]
                and "No matching deferred tool schemas are available" in tool_results[-1])
            if complete or len(self.requests) >= 5:
                self.script.append({"content": "公司尚未连接 GitHub，请管理员在「公司连接」中连接后再试。"})
            else:
                self.script.append({"tool_calls": [{"id": f"lookup-{len(self.requests)}", "type": "function",
                    "function": {"name": "tool_search", "arguments": json.dumps({"query": "GitHub"})}}],
                    "finish_reason": "tool_calls"})
            return await super().stream(**kwargs)

    client = AppLookupModel([])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    started = time.monotonic()
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(token)
    elapsed = time.monotonic() - started
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        calls = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == s["run_id"])))
        assert 1 <= len(calls) <= 5 and {call.tool_name for call in calls} == {"tool_search"}
        assert await db.scalar(select(func.count()).select_from(MCPServer)) == 0
    assert len(client.requests) == 2, "One discovery round must give the model enough facts to finish"
    result = next(m["content"] for m in client.requests[-1]["messages"] if m["role"] == "tool")
    assert "No matching deferred tool schemas are available" in result
    assert "company_applications:\n- none" in result
    assert "administrator" in result and "company connections" in result
    assert "Tool search discovered deferred capabilities" not in result
    assert elapsed < 60, f"platform time {elapsed:.3f}s"
    print(f"R5E_ABSENT_APP calls={len(calls)} model_rounds={len(client.requests)} platform_seconds={elapsed:.3f}")


@pytest.mark.parametrize("name", ["MCP Tool Installer", "skills/mcp-installer/SKILL.md", "mcp_admin_pack"])
async def test_member_loading_unavailable_installer_gets_guidance_not_installation_steps(app_work, name):
    from app.services.agent_tools import _get_tool_runtime_service

    s = app_work
    result = str(await _get_tool_runtime_service().execute("load_skill", {"name": name},
        agent_id=s["agent_id"], user_id=s["user_id"], session_id=str(s["session_id"]),
        runtime_task_id=str(s["run_id"]), emit_runtime_hooks=False))
    assert "skill_tools_unavailable" in result, result
    assert "cannot install" in result and "administrator" in result
    assert "company connections" in result
    assert "## Tool Reference" not in result


async def test_mcp_admin_can_still_load_installer_and_call_its_tools(app_work, owner_sessionmaker):
    from app.services.agent_tools import _get_tool_runtime_service, discoverable_tool_names_for_query
    from app.services.capability_group_policy_service import set_tenant_capability_group_policy

    s = app_work
    async with owner_sessionmaker() as db:
        await set_tenant_capability_group_policy(db, s["tenant_id"], "mcp_admin_pack", enabled=True)
        await db.commit()
    names = await discoverable_tool_names_for_query(s["agent_id"], "mcp_admin_pack")
    assert {"import_mcp_server", "list_mcp_tools"} <= set(names)
    service = _get_tool_runtime_service()
    context = dict(agent_id=s["agent_id"], user_id=s["user_id"], session_id=str(s["session_id"]),
        runtime_task_id=str(s["run_id"]), emit_runtime_hooks=False)
    loaded = str(await service.execute("load_skill", {"name": "MCP Tool Installer"}, **context))
    assert "## Tool Reference" in loaded and "skill_tools_unavailable" not in loaded
    listed = str(await service.execute("list_mcp_tools", {}, **context))
    assert listed == "No MCP resources found for this agent. Use import_mcp_server to add one."


@pytest.mark.parametrize("query", ["select:import_mcp_server", "mcp_admin_pack", "MCP"])
async def test_search_does_not_advertise_an_unavailable_installer(app_work, query):
    from app.services.agent_tools import _get_tool_runtime_service

    s = app_work
    result = str(await _get_tool_runtime_service().execute("tool_search", {"query": query},
        agent_id=s["agent_id"], user_id=s["user_id"], session_id=str(s["session_id"]),
        runtime_task_id=str(s["run_id"]), emit_runtime_hooks=False))
    if query != "MCP":
        assert result.startswith("No matching deferred tool schemas are available")
    assert "import_mcp_server" not in result
    assert "MCP Tool Installer" not in result
    assert "administrator" in result and "company_applications:\n- none" in result


@pytest.mark.parametrize("app_work", ["帮我在公司文档中查一下本周的通知"], indirect=True)
async def test_registered_app_still_produces_the_requesters_personal_connection_card(
    app_work, owner_sessionmaker, monkeypatch,
):
    import app.runtime.invoker as invoker
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.session_user_question import question_payload
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp

    s = app_work
    server_id, tool_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        tool = await _seed_mcp(db, s["tenant_id"], server_id, tool_id, s["user_id"])
        server = await db.get(MCPServer, server_id)
        server.config_json = {"oauth": {"authorization_endpoint": "https://auth.test/authorize",
            "token_endpoint": "https://auth.test/token", "client_id": "documents",
            "redirect_uri": "https://hive.test/api/enterprise/mcp/oauth/callback"}}
        await db.commit()
    def call(name, arguments):
        return {"tool_calls": [{"id": f"d2-{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}
    client = ScriptedLLMClient([
        call("tool_search", {"query": "Company documents"}),
        call(tool.name, {}),
        call("ask_user_question", {"questions": [{"question": "请连接你的公司文档账号，以便查看通知。", "options": []}],
            "configuration": {"mcp_server_id": str(server_id)}}),
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(token)
    assert len(client.requests) == 3
    discovery = next(m["content"] for m in client.requests[1]["messages"] if m["role"] == "tool")
    assert f"mcp_server_id: {server_id}" in discovery
    assert "configuration.mcp_server_id" in discovery
    assert all("import_mcp_server" not in {t["function"]["name"] for t in r["tools"]} for r in client.requests)
    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == s["session_id"],
            ChatTranscriptEvent.item_kind == "user_question"))
        assert event is not None
        payload = question_payload(event)
        assert payload["configuration_request"]["mcp_server_id"] == str(server_id)
        assert payload["requested_by_user_id"] == str(s["user_id"])


async def test_repeated_browser_failure_warning_reaches_the_next_real_model_round(
    app_work, owner_sessionmaker, monkeypatch,
):
    import app.runtime.invoker as invoker
    from app.models.capability_policy import CapabilityPolicy
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import OWNER_ACTION_IDS, save_owner_action_policy
    from app.services.web_chat_runtime import execute_web_chat_run

    s = app_work
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=s["tenant_id"], capability="computer.browser.control",
            allowed=True, requires_approval=False))
        await save_owner_action_policy(db, agent_id=s["agent_id"], tenant_id=s["tenant_id"],
            changed_by_user_id=s["user_id"], actions={action: CharterZone.FULL_AUTHORITY for action in OWNER_ACTION_IDS})
        await db.commit()
    script = [{"tool_calls": [{"id": f"repeat-{revision}", "type": "function", "function": {
        "name": "browser_manage", "arguments": json.dumps({"action": "start", "expected_revision": revision,
            "expected_epoch": 0})}}], "finish_reason": "tool_calls"} for revision in range(4)]
    client = ScriptedLLMClient([*script, {"content": "当前没有可用浏览器，暂时无法查看。"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(token)
    assert len(client.requests) == 5
    warnings = [str(m["content"]) for m in client.requests[-1]["messages"]
        if "[Loop Guard Warning]" in str(m["content"])]
    assert len(warnings) == 1
    assert "browser_manage failed repeatedly" in warnings[0]
    assert "<tool_error>" not in warnings[0]


async def test_no_device_is_plain_and_does_not_create_a_browser_or_lease(
    browser_case, browser_tools, owner_sessionmaker, monkeypatch,
):
    from app.config import get_settings
    from app.services.browser_pipe import BrowserPipeError

    s, _, _, _, _ = browser_case
    monkeypatch.setattr(get_settings(), "BROWSER_DEVICE_PROFILE", "disabled")
    def unavailable(*_args):
        raise BrowserPipeError("browser_device_not_configured", "not_started")
    monkeypatch.setattr("app.services.browser_control.browser_device_spec", unavailable)
    for tool_name, arguments in [("browser_observe", {}), ("browser_manage", operation("start"))]:
        error = _error_payload(await browser_tools(tool_name, arguments))
        assert error["ok"] is False and error["reason_code"] == "browser_device_not_configured"
        assert "No browser is configured" in error["message"]
        assert "retrying will not help" in error["actionable_hint"]
        assert error["retryable"] is False and error["effect"] == "not_started"
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(EnvironmentBrowser)
            .where(EnvironmentBrowser.agent_id == s["agent_id"])) == 0
        assert await db.scalar(select(func.count()).select_from(EnvironmentLease)
            .where(EnvironmentLease.runtime_task_id == s["run_id"])) == 0


async def test_revision_conflict_returns_current_state_without_a_failed_attempt_lease(
    browser_case, browser_tools, owner_sessionmaker, monkeypatch,
):
    from app.config import get_settings

    s, _, _, _, _ = browser_case
    settings = get_settings()
    monkeypatch.setattr(settings, "BROWSER_DEVICE_PROFILE", "trusted_host")
    monkeypatch.setattr(settings, "BROWSER_DEVICE_EXECUTABLE", "/synthetic-browser")
    monkeypatch.setattr(settings, "BROWSER_PRIVATE_PROFILE_DIR", "/synthetic-private-root")
    wrong = {"action": "start", "expected_revision": 77, "expected_epoch": 88}
    absent = _error_payload(await browser_tools("browser_manage", wrong))
    assert absent["reason_code"] == "browser_revision_conflict"
    assert (absent["current_revision"], absent["current_epoch"]) == (0, 0)
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(EnvironmentLease)
            .where(EnvironmentLease.runtime_task_id == s["run_id"])) == 0
    view = json.loads(str(await browser_tools("browser_manage", {**wrong,
        "expected_revision": absent["current_revision"], "expected_epoch": absent["current_epoch"]}))) ["browser"]
    stopped = json.loads(str(await browser_tools("browser_manage", operation("stop", view))))["browser"]
    async with owner_sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(EnvironmentLease)
            .where(EnvironmentLease.runtime_task_id == s["run_id"]))
    conflict = _error_payload(await browser_tools("browser_manage", operation("start", view)))
    assert (conflict["current_revision"], conflict["current_epoch"]) == (stopped["revision"], stopped["control_epoch"])
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(EnvironmentLease)
            .where(EnvironmentLease.runtime_task_id == s["run_id"])) == before
    retried = json.loads(str(await browser_tools("browser_manage", {"action": "start",
        "expected_revision": conflict["current_revision"], "expected_epoch": conflict["current_epoch"]})))
    assert retried["ok"] is True and retried["browser"]["status"] == "ready"


async def test_revision_change_after_admission_discards_only_the_unexecuted_lease(
    browser_case, browser_tools, owner_sessionmaker, monkeypatch,
):
    s, _, manager, _, _ = browser_case
    ready = json.loads(str(await browser_tools("browser_manage", operation("start"))))["browser"]
    stopped = json.loads(str(await browser_tools("browser_manage", operation("stop", ready))))["browser"]
    original = manager._provider

    async def concurrent_change(*args):
        provider = await original(*args)
        async with owner_sessionmaker() as db:
            row = await db.get(EnvironmentBrowser, UUID(stopped["id"]))
            row.revision += 1
            row.control_epoch += 1
            await db.commit()
        return provider

    async with owner_sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(EnvironmentLease)
            .where(EnvironmentLease.runtime_task_id == s["run_id"]))
    monkeypatch.setattr(manager, "_provider", concurrent_change)
    conflict = _error_payload(await browser_tools("browser_manage", operation("start", stopped)))
    assert conflict["reason_code"] == "browser_revision_conflict" and conflict["effect"] == "not_started"
    assert (conflict["current_revision"], conflict["current_epoch"]) == (stopped["revision"] + 1, stopped["control_epoch"] + 1)
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(EnvironmentLease)
            .where(EnvironmentLease.runtime_task_id == s["run_id"])) == before
    monkeypatch.setattr(manager, "_provider", original)
    retry = await browser_tools("browser_manage", {"action": "start", "expected_revision": conflict["current_revision"],
        "expected_epoch": conflict["current_epoch"]})
    assert json.loads(str(retry))["browser"]["status"] == "ready"
