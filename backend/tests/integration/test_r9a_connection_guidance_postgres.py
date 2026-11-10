"""Compiled requests and discovery results distinguish connections from tool schemas."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.channel_config import ChannelConfig
from app.models.email_account import EmailAccount
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
    from app.services.pack_policy_service import get_agent_pack_policies
    from app.services.web_chat_runtime import execute_web_chat_run

    s = app_work
    foreign = await _seed_run(owner_sessionmaker, tmp_path / "foreign")
    async with owner_sessionmaker() as db:
        if connection != "none":
            db.add_all([
                ChannelConfig(agent_id=s["agent_id"], tenant_id=s["tenant_id"], channel_type="feishu",
                    is_configured=connection == "employee", app_id="employee-app", app_secret="local-secret"),
                ChannelConfig(agent_id=s["agent_id"], tenant_id=s["tenant_id"], channel_type="dingtalk",
                    is_configured=False),
            ])
        db.add_all([
            TenantChannelConfig(tenant_id=s["tenant_id"], channel_type="feishu",
                is_active=connection == "company", app_id="company-app", app_secret="local-secret"),
            TenantChannelConfig(tenant_id=s["tenant_id"], channel_type="dingtalk",
                is_active=True, app_id="directory-only-app", app_secret="directory-secret",
                extra_config={"corp_id": "directory-company", "messaging_enabled": False}),
            TenantChannelConfig(tenant_id=foreign["tenant_id"], channel_type="dingtalk",
                is_active=True, app_id="foreign-app", app_secret="foreign-secret"),
        ])
        await db.commit()
        assert await db.scalar(select(func.count()).select_from(EmailAccount).where(
            EmailAccount.tenant_id == s["tenant_id"])) == 0
        if connection == "none":
            assert await db.scalar(select(func.count()).select_from(ChannelConfig).where(
                ChannelConfig.tenant_id == s["tenant_id"])) == 0
            assert await get_agent_pack_policies(db, s["tenant_id"], s["agent_id"]) == {}

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
    # R9a round 3: the deferred index must agree with the connection facts too,
    # even with default pack policy. Keep the tools discoverable, condition loading.
    # The dynamic index travels in its own API message outside the frozen prefix.
    compiled = "\n".join(str(message["content"]) for message in request["messages"])
    deferred = compiled.split("## Available Deferred Tools\n", 1)[1].split("\n\n", 1)[0]
    assert "send_feishu_message" in deferred
    assert "send_email" in deferred
    assert ("Feishu and email names in this list are not a connection; load them only after "
        "Channel Integrations confirms the channel or mailbox access is verified for this member.") in deferred
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
    # R9a round 2: a correct channel subsection must not mask contradictory
    # instructions elsewhere in the compiled prefix or the actual CORE schemas.
    normalized = " ".join(prompt.split())
    methodology = normalized.split("<work_methodology>", 1)[1].split("</work_methodology>", 1)[0]
    assert "Use only channels confirmed by Channel Integrations, and email only with verified mailbox access for this member" in methodology
    platform = normalized.split("<platform_integration>", 1)[1].split("</platform_integration>", 1)[0]
    assert "Feishu/email only with a verified channel or mailbox connection for this member" in platform
    governance = normalized.split("<tool_governance>", 1)[1].split("</tool_governance>", 1)[0]
    assert "feishu/email only with a verified channel or mailbox connection for this member" in governance
    tools_section = normalized.split("## Using Your Tools", 1)[1]
    assert "feishu/email only with a verified channel or mailbox connection for this member" in tools_section
    assert "Use Feishu, email, or web messaging as appropriate" not in prompt
    core = {tool["function"]["name"]: tool["function"]["description"] for tool in request["tools"]}
    # Presence is not connection evidence; preserve the real callable surface.
    assert {"ask_user_question", "send_channel_message", "send_channel_file", "tool_search"} <= core.keys()
    assert "only for a configured channel confirmed by Channel Integrations" in core["send_channel_message"]
    assert "send_feishu_message only when Feishu is configured" in core["send_channel_message"]
    assert "only through a connected channel confirmed by Channel Integrations" in core["send_channel_file"]
    assert "For administrator-requested installation or import" in core["tool_search"]
    assert "Never use installation to resolve a member's missing connection" in core["tool_search"]
    assert "Use an already-present, connected tool directly; do not offer configuration" in normalized
    search_schema = next(tool["function"] for tool in request["tools"] if tool["function"]["name"] == "tool_search")
    assert search_schema["parameters"]["properties"]["query"]["description"] == "Optional query like 'web research'"


def _account_action(result, name, actions):
    row = next(line for line in result.splitlines() if line.startswith(f"- {name} | mcp_server_id:"))
    assert row.endswith(" | connect_if_account_needed"), row
    action = actions["connect_if_account_needed"]
    assert action["arguments"]["configuration"] == {"mcp_server_id": "<mcp_server_id from the company_applications row>"}
    # Apply the template's instructed row-ID substitution, without repairing it.
    action["arguments"]["configuration"]["mcp_server_id"] = row.split("mcp_server_id: ", 1)[1].split(" |", 1)[0]
    return action


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
        db.add(MCPServer(id=calendar_id, tenant_id=s["tenant_id"], name="Calendar", server_key="calendar",
            server_url="https://calendar.test/sse", status="needs_auth"))
        if connection == "absent":
            db.add_all([
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
    assert actions["not_connected"]["connect_action"] == {"location": "公司连接", "actor": "administrator"}
    assert "not connected" in result
    assert "Do not probe browser sessions, email or importable integrations as workarounds" in result
    if connection == "absent":
        # Round 1 incorrectly labelled every registered app account_required.
        # Registration alone establishes neither a missing account nor access.
        assert "account_required" not in actions
        assert _account_action(result, "Calendar", actions) == {"tool": "ask_user_question", "arguments": {
            "questions": [{"question": "<Write a connection question in the member's language>", "options": []}],
            "configuration": {"mcp_server_id": str(calendar_id)}}}
        assert "GitHub personal" not in result
        assert "MCP" not in result
        assert "import_mcp_server" not in result
        return

    # Round 2 required a complete connect call even on configured GitHub,
    # encoding this defect. Only an unconfigured row may offer the shared call.
    github = next(line for line in result.splitlines() if line.startswith("- GitHub | mcp_server_id:"))
    if connection == "connected":
        assert github == f"- GitHub | mcp_server_id: {server_id}"
    assert f"- Calendar | mcp_server_id: {calendar_id} | connect_if_account_needed" in result.splitlines()
    assert result.count('"tool": "ask_user_question"') == 1
    if connection == "registered":
        import app.runtime.invoker as invoker
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.services.session_user_question import question_payload
        from app.services.web_chat_runtime import execute_web_chat_run
        from app.tools.handlers.plan_mode import ask_user_question
        from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

        action = _account_action(result, "GitHub", actions)
        assert action["tool"] == "ask_user_question"
        assert action["arguments"]["configuration"] == {"mcp_server_id": str(server_id)}
        # After substituting the row ID, execute the emitted call verbatim,
        # filling only its question text.
        # Do not synthesize missing questions: that hid round 1's missing card.
        arguments = action["arguments"]
        if arguments.get("questions"):
            arguments["questions"][0]["question"] = "请连接 GitHub，之后我会继续查看通知。"
        handler_result = json.loads(await ask_user_question(ToolExecutionRequest(
            tool_name=action["tool"], arguments=arguments, context=ToolExecutionContext(
                agent_id=s["agent_id"], user_id=s["user_id"], tenant_id=str(s["tenant_id"]),
                workspace=s["agent_root"], session_id=str(s["session_id"]), runtime_task_id=str(s["run_id"]),
            ))))
        client = ScriptedLLMClient([{"tool_calls": [{"id": "connect-github", "type": "function", "function": {
            "name": action["tool"], "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"},
            {"content": "请连接后继续。"}])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
        token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
        try:
            await execute_web_chat_run(s["run_id"])
        finally:
            reset_runtime_task_fence(token)
        async with owner_sessionmaker() as db:
            event = await db.scalar(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == s["session_id"], ChatTranscriptEvent.item_kind == "user_question"))
            tool_results = list(await db.scalars(select(ChatTranscriptEvent.content).where(
                ChatTranscriptEvent.session_id == s["session_id"], ChatTranscriptEvent.item_kind == "tool_result")))
            assert event is not None, {"handler_result": handler_result, "tool_results": tool_results}
            assert handler_result["status"] == "awaiting_user_clarification"
            payload = question_payload(event)
            assert payload["questions"][0]["question"] == arguments["questions"][0]["question"]
            assert payload["configuration_request"] == {"mcp_server_id": str(server_id), "target_agent_id": str(s["agent_id"])}
            assert payload["requested_by_user_id"] == str(s["user_id"])
        return

    # A configured company token already works: remove the old assertion that
    # put it under account_required, which encoded the defect rather than policy.
    assert "account_required" not in actions
    received = []

    class Transport:
        def __init__(self, url, *, api_key):
            received.append((url, api_key))

        async def call_tool(self, name, arguments):
            return "Recent GitHub notification"

    monkeypatch.setattr("app.services.mcp_client.MCPClient", Transport)
    assert str(await service.execute(tool.name, {}, **context)) == "Recent GitHub notification"
    assert received == [("https://mcp.example.test/sse", "local-company-bearer")]
