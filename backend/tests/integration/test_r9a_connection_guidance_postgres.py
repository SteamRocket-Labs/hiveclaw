"""Compiled requests and discovery results distinguish connections from tool schemas."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.channel_config import ChannelConfig
from app.models.mcp_server import MCPServer
from app.models.tenant_channel_config import TenantChannelConfig
from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
from tests.integration.test_m1g5g_session_mcp_configuration_postgres import _seed_mcp
from tests.integration.test_r5e_unavailable_app_postgres import app_work as app_work
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("connection", ["none", "employee", "company"])
@pytest.mark.parametrize("app_work", ["用一句话说说你是谁、能帮我做什么"], indirect=True)
async def test_member_request_only_claims_verified_connections_and_respects_answer_length(
    app_work, owner_sessionmaker, monkeypatch, tmp_path, connection,
):
    import app.runtime.invoker as invoker
    from app.services.web_chat_runtime import execute_web_chat_run

    s = app_work
    foreign = await _seed_run(owner_sessionmaker, tmp_path / "foreign")
    async with owner_sessionmaker() as db:
        db.add_all([
            ChannelConfig(agent_id=s["agent_id"], tenant_id=s["tenant_id"], channel_type="feishu",
                is_configured=connection == "employee", app_id="employee-app", app_secret="local-secret"),
            ChannelConfig(agent_id=s["agent_id"], tenant_id=s["tenant_id"], channel_type="dingtalk",
                is_configured=False),
            TenantChannelConfig(tenant_id=s["tenant_id"], channel_type="feishu",
                is_active=connection == "company", app_id="company-app", app_secret="local-secret"),
            TenantChannelConfig(tenant_id=s["tenant_id"], channel_type="dingtalk",
                is_active=True, app_id="directory-only-app", app_secret="directory-secret",
                extra_config={"corp_id": "directory-company", "messaging_enabled": False}),
            TenantChannelConfig(tenant_id=foreign["tenant_id"], channel_type="dingtalk",
                is_active=True, app_id="foreign-app", app_secret="foreign-secret"),
        ])
        await db.commit()

    client = ScriptedLLMClient([{"content": "我是你的助理，可以帮你分析问题和整理资料。"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(token)

    assert len(client.requests) == 1
    request = client.requests[0]
    prompt = "\n".join(str(message["content"]) for message in request["messages"] if message["role"] == "system")
    assert "Only claim a service capability after verifying its connection for the current tenant and requesting member" in prompt
    assert "tool schemas, deferred catalogs and examples are not connection evidence" in prompt
    assert "including exactly one sentence when asked" in prompt
    assert "Never name MCP, A2A, tool names or registries to the member" in prompt
    assert "Do not probe browser sessions, email or importable" in prompt
    assert "This configuration list does not establish mailbox or personal-account access for the requesting member" in prompt
    assert "A configured channel does not establish an authorized messaging target for this member" in prompt
    channels = prompt.split("### Channel Integrations\n", 1)[1].split("\n\n", 1)[0]
    if connection == "none":
        assert "Configured channels: none." in channels
    else:
        assert "You have feishu channel(s) configured." in channels
    assert "dingtalk" not in channels
    assert "email" not in channels
    assert "with a full toolkit" not in prompt
    assert any(message["role"] == "user" and "用一句话" in str(message["content"]) for message in request["messages"])
    # Catalog presence is deliberately not treated as a connection: these
    # schemas may remain discoverable so the existing setup flow stays usable.
    assert "ask_user_question" in {tool["function"]["name"] for tool in request["tools"]}


@pytest.mark.parametrize("connection", ["absent", "registered", "connected"])
async def test_discovery_returns_connection_steps_and_preserves_connected_execution(
    app_work, owner_sessionmaker, monkeypatch, tmp_path, connection,
):
    from app.services import mcp_oauth
    from app.services.agent_tools import _get_tool_runtime_service

    s = app_work
    server_id, tool_id, calendar_id = uuid4(), uuid4(), uuid4()
    foreign = await _seed_run(owner_sessionmaker, tmp_path / "foreign")
    async with owner_sessionmaker() as db:
        if connection == "absent":
            db.add_all([
                MCPServer(id=calendar_id, tenant_id=s["tenant_id"], name="Calendar", server_key="calendar",
                    server_url="https://calendar.test/sse", status="needs_auth"),
                MCPServer(tenant_id=s["tenant_id"], name="GitHub personal", server_key="personal",
                    owner_user_id=s["user_id"], server_url="https://personal.test/sse", status="needs_auth"),
                MCPServer(tenant_id=foreign["tenant_id"], name="GitHub", server_key="github",
                    server_url="https://foreign.test/sse", status="needs_auth"),
            ])
        else:
            tool = await _seed_mcp(db, s["tenant_id"], server_id, tool_id, s["user_id"])
            server = await db.get(MCPServer, server_id)
            server.name = "GitHub"
            if connection == "connected":
                server.config_json = {"oauth": {"token": mcp_oauth.encrypt_token_set(
                    mcp_oauth.OAuthTokenSet("local-company-bearer"))}}
                server.auth_status = "configured"
        await db.commit()

    service = _get_tool_runtime_service()
    context = dict(agent_id=s["agent_id"], user_id=s["user_id"], session_id=str(s["session_id"]),
        runtime_task_id=str(s["run_id"]), emit_runtime_hooks=False)
    result = str(await service.execute("tool_search", {"query": "GitHub" if connection == "absent" else "Company documents"}, **context))
    assert "Connection actions:\n" in result, result
    actions = json.loads(result.split("Connection actions:\n", 1)[1].split("\n", 1)[0])
    assert actions["not_connected"]["connect_action"] == {"location": "company connections", "actor": "administrator"}
    assert "not connected" in result
    assert "Do not probe browser sessions, email or importable integrations as workarounds" in result
    if connection == "absent":
        assert actions["account_required"] == [{"name": "Calendar", "connect_action": {
            "tool": "ask_user_question", "arguments": {"configuration": {"mcp_server_id": str(calendar_id)}}}}]
        assert "GitHub personal" not in result
        assert "MCP" not in result
        assert "import_mcp_server" not in result
        return

    action = next(item["connect_action"] for item in actions["account_required"] if item["name"] == "GitHub")
    assert action == {"tool": "ask_user_question", "arguments": {"configuration": {"mcp_server_id": str(server_id)}}}
    if connection == "registered":
        import app.runtime.invoker as invoker
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.services.session_user_question import question_payload
        from app.services.web_chat_runtime import execute_web_chat_run

        arguments = {**action["arguments"],
            "questions": [{"question": "请连接 GitHub，之后我会继续查看通知。", "options": []}]}
        client = ScriptedLLMClient([{"tool_calls": [{"id": "connect-github", "type": "function", "function": {
            "name": action["tool"], "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
        try:
            await execute_web_chat_run(s["run_id"])
        finally:
            reset_runtime_task_fence(token)
        async with owner_sessionmaker() as db:
            event = await db.scalar(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == s["session_id"], ChatTranscriptEvent.item_kind == "user_question"))
            assert event is not None
            payload = question_payload(event)
            assert payload["configuration_request"]["mcp_server_id"] == str(server_id)
            assert payload["requested_by_user_id"] == str(s["user_id"])
        return

    received = []

    class Transport:
        def __init__(self, url, *, api_key):
            received.append((url, api_key))

        async def call_tool(self, name, arguments):
            return "Recent GitHub notification"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    assert str(await service.execute(tool.name, {}, **context)) == "Recent GitHub notification"
    assert received == [("https://mcp.example.test/sse", "local-company-bearer")]
