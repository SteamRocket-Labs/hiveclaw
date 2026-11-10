"""Company connection gaps reach the provider as a complete, bounded answer."""

import json
from uuid import uuid4

import pytest

from app.models.mcp_server import MCPServer
from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
from tests.integration.test_r5e_unavailable_app_postgres import app_work as app_work
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _call(name, arguments):
    return {"tool_calls": [{"id": f"r11b-{name}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}


async def _run(s, client, monkeypatch):
    from app.runtime import invoker
    from app.services.web_chat_runtime import execute_web_chat_run

    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(token)


async def test_absent_company_service_stop_guidance_reaches_provider_wire(app_work, monkeypatch):
    client = ScriptedLLMClient([
        _call("tool_search", {"query": "GitHub"}),
        {"content": "GitHub 尚未连接，管理员可在「公司连接」中添加。"},
    ])
    await _run(app_work, client, monkeypatch)
    assert len(client.requests) == 2
    first, second = client.requests
    assert any(m["role"] == "user" and "GitHub" in str(m["content"]) for m in first["messages"])
    system = " ".join(str(m["content"]) for m in first["messages"] if m["role"] == "system")
    discovery = next(str(m["content"]) for m in second["messages"] if m["role"] == "tool")
    assert "company_applications:\n- none" in discovery
    actions = json.loads(discovery.split("Connection actions:\n", 1)[1].split("\n", 1)[0])
    surfaces = {"system": system, "lookup": discovery}
    missing = {name: [text for text in ("one sentence", "not connected", "administrator", "公司连接", "CLIs", "other routes")
        if text not in content] for name, content in surfaces.items()}
    assert not any(missing.values()), missing
    assert "resourcefulness, retries and three-strike variations never override this stop" in system
    assert "including the one-sentence missing-connection answer above" in system
    assert "If one approach fails, try another — you have many capabilities." not in system
    assert "If one approach fails, try another within verified connections" in system
    assert "then stop attempts to access that service" in system
    assert "then stop attempts to access that service" in discovery
    assert actions["not_connected"]["connect_action"] == {"location": "公司连接", "actor": "administrator"}


async def test_browser_not_configured_keeps_absent_service_stop_guidance_on_provider_wire(
    app_work, owner_sessionmaker, monkeypatch,
):
    from app.config import get_settings
    from app.models.capability_policy import CapabilityPolicy
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import OWNER_ACTION_IDS, save_owner_action_policy

    s = app_work
    async with owner_sessionmaker() as db:
        for capability in ("computer.browser.read", "computer.browser.control"):
            db.add(CapabilityPolicy(tenant_id=s["tenant_id"], capability=capability,
                allowed=True, requires_approval=False))
        await save_owner_action_policy(db, agent_id=s["agent_id"], tenant_id=s["tenant_id"],
            changed_by_user_id=s["user_id"], actions={action: CharterZone.FULL_AUTHORITY for action in OWNER_ACTION_IDS})
        await db.commit()
    monkeypatch.setattr(get_settings(), "BROWSER_DEVICE_PROFILE", "disabled")
    client = ScriptedLLMClient([
        _call("tool_search", {"query": "GitHub"}),
        _call("browser_observe", {}),
        {"content": "GitHub 尚未连接，管理员可在「公司连接」中添加。"},
    ])
    await _run(s, client, monkeypatch)
    assert len(client.requests) == 3
    after_lookup, after_browser = client.requests[1:]
    discovery = next(str(m["content"]) for m in after_lookup["messages"] if m["role"] == "tool")
    assert "company_applications:\n- none" in discovery
    browser_result = next(str(m["content"]) for m in reversed(after_browser["messages"]) if m["role"] == "tool")
    error = json.loads(browser_result.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert error["tool_name"] == "browser_observe"
    assert error["reason_code"] == "browser_device_not_configured"
    assert error["error_class"] == "not_configured" and error["retryable"] is False
    for instruction in ("use an available non-browser path", "try another route", "use another route"):
        assert instruction not in browser_result.lower()
    hint = error["diagnostic_hint"]
    for text in ("member's language", "one sentence", "not connected", "administrator", "公司连接",
        "completes the request", "stop attempts to access that service", "do not try other routes"):
        assert text in hint
    system = " ".join(str(m["content"]) for m in after_browser["messages"] if m["role"] == "system")
    assert "If one approach fails, try another — you have many capabilities." not in system
    assert "If one approach fails, try another within verified connections" in system


async def test_connected_service_keeps_its_schema_and_executes_on_provider_path(
    app_work, owner_sessionmaker, monkeypatch,
):
    from app.services import mcp_oauth

    s = app_work
    server_id, tool_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        tool = await _seed_mcp(db, s["tenant_id"], server_id, tool_id, s["user_id"])
        server = await db.get(MCPServer, server_id)
        server.name = "GitHub"
        server.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(
            mcp_oauth.OAuthTokenSet("local-company-bearer"))}}
        server.auth_status = "configured"
        await db.commit()

    received = []

    class Transport:
        def __init__(self, url, *, api_key):
            received.append((url, api_key))

        async def call_tool(self, name, arguments):
            assert name == "find" and arguments == {}
            return "Recent GitHub notification"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    client = ScriptedLLMClient([
        _call("tool_search", {"query": "Company documents"}),
        _call(tool.name, {}),
        {"content": "最近有一条 GitHub 通知。"},
    ])
    await _run(s, client, monkeypatch)
    assert len(client.requests) == 3
    first, discovered, executed = client.requests
    discovery = next(str(m["content"]) for m in discovered["messages"] if m["role"] == "tool")
    assert f"- GitHub | mcp_server_id: {server_id}" in discovery.splitlines()
    core_names = {t["function"]["name"] for t in first["tools"]}
    assert {"tool_search", "run_command", "browser_manage", "browser_action", "browser_observe"} <= core_names
    assert core_names <= {t["function"]["name"] for t in discovered["tools"]}
    schema = next(t["function"] for t in discovered["tools"] if t["function"]["name"] == tool.name)
    assert schema["description"] == "Find a document"
    assert schema["parameters"] == {"type": "object", "properties": {}}
    assert received == [("https://mcp.example.test/sse", "local-company-bearer")]
    result = next(str(m["content"]) for m in reversed(executed["messages"]) if m["role"] == "tool")
    assert result == "Recent GitHub notification"
