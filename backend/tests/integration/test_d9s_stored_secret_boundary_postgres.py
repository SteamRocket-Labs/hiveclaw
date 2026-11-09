"""Opaque requester-owned credentials are protected on Web and model boundaries."""

import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.custom_api_account import CustomApiAccount
from app.models.email_account import EmailAccount
from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionTurnInput
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services import mcp_oauth
from tests.services.test_pasted_secret_web_entries import stored_rows
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
TOKENS = ("a8F2b9C3d7E1f6G4h5J0k8L2m9N3p7Q1r6S4t5U0",
    "v7W3x9Y1z5A8b2C6d0E4f7G3h9J1k5L8m2N6p0Q4",
    "r5S9t3U7v1W4x8Y2z6A0b5C9d3E7f1G4h8J2k6L0")


async def _stored_accounts(owner_sessionmaker):
    tenant, owner, agent, session, _ = await _seed_session(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        other = User(id=uuid.uuid4(), tenant_id=tenant, username=f"d9s-{uuid.uuid4().hex}",
            email=f"{uuid.uuid4().hex}@d9s.test", password_hash="x", display_name="D9s other member")
        db.add(other)
        tool = Tool(id=uuid.uuid4(), tenant_id=tenant, name=f"custom_api__d9s__{uuid.uuid4().hex}",
            type="custom_api", display_name="D9s API", enabled=True, config={"base_url": "https://api.example.test",
                "auth": {"scheme": "bearer"}, "action": {"path": "/read"}})
        parent = MCPServer(id=uuid.uuid4(), tenant_id=tenant, name="D9s company", server_key=uuid.uuid4().hex,
            server_url="https://mcp.example.test/sse", status="connected")
        db.add_all([tool, parent])
        await db.flush()
        db.add_all([
            CustomApiAccount(tenant_id=tenant, owner_user_id=owner, tool_id=tool.id,
                name="D9s API", config={"bearer_token": TOKENS[0]}),
            EmailAccount(tenant_id=tenant, owner_user_id=owner, name="D9s mail",
                config={"email_address": "owner@example.test", "auth_code": TOKENS[1]}),
            MCPServer(tenant_id=tenant, owner_user_id=owner, parent_server_id=parent.id,
                name="D9s personal", server_key=uuid.uuid4().hex, server_url=parent.server_url,
                status="connected", auth_status="configured", config_json={"oauth": {
                    "token": mcp_oauth.encrypt_token_set(mcp_oauth.OAuthTokenSet(TOKENS[2], refresh_token="D9sRefreshOpaqueValue")),
                    "client_secret": mcp_oauth.encrypt_value("D9sClientOpaqueValue"),
                    "pending": {"verifier": mcp_oauth.encrypt_value("D9sVerifierOpaqueValue")}}}),
            AgentMCPServerAssignment(tenant_id=tenant, agent_id=agent, mcp_server_id=parent.id),
            AgentTool(tenant_id=tenant, agent_id=agent, tool_id=tool.id, enabled=True,
                config={"bearer_token": "D9sSyntheticCompanyValue"}),
        ])
        await db.commit()
        return tenant, owner, other.id, agent, session, tool.id


async def _invoke(monkeypatch, *, tenant, user, agent, content):
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.services.llm_client import LLMResponse

    captured, prepared = [], []

    class Provider:
        async def stream(self, **kwargs):
            captured.append(kwargs)
            return LLMResponse(content="Synthetic provider completed", tool_calls=[], usage={"total_tokens": 1})

        async def close(self):
            pass

    async def prepare(**kwargs):
        prepared.append(kwargs)

    monkeypatch.setattr("app.runtime.invoker.create_llm_client", lambda **kwargs: Provider())
    monkeypatch.setattr("app.runtime.invoker.record_token_usage", lambda *args, **kwargs: None)
    result = await invoke_agent(AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="D9sSyntheticModelKey",
            base_url=None, max_output_tokens=256, temperature=0),
        tenant_id=tenant, agent_id=agent, user_id=user,
        messages=[{"role": "user", "content": content}],
        agent_name="D9s", role_description="D9s credential regression",
        standalone_system_prompt="Complete the fixture request.", initial_tools=[],
        model_request_prepare=prepare,
    ))
    assert result.content == "Synthetic provider completed", result
    assert len(captured) == len(prepared) == 1
    return str(captured), str(prepared)


@pytest.mark.parametrize("kind", [0, 1, 2], ids=["custom_api", "email", "personal_mcp"])
async def test_own_opaque_secret_never_reaches_web_storage_or_model(owner_sessionmaker, monkeypatch, kind):
    from app.services.session_live_input import submit_live_human_input

    tenant, owner, _, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    token = TOKENS[kind]
    async with owner_sessionmaker() as db:
        agent, user, session = await db.get(Agent, agent_id), await db.get(User, owner), await db.get(ChatSession, session_id)
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session,
            content=token, display_content=token, parts=[{"type": "text", "text": token}], source="web")
        await db.commit()
        assert receipt["dispatch_status"] == "dispatched"
        row = await db.get(SessionTurnInput, uuid.UUID(receipt["input_id"]))
        task = await db.get(RuntimeTask, uuid.UUID(receipt["run"]["run_id"]))
        persisted = json.dumps(await stored_rows(db, session_id, agent_id), ensure_ascii=False)
        assert token not in persisted
        assert "[REDACTED_SECRET]" in task.prompt
        assert receipt["pasted_credential_notice"] is True
        content = row.content_parts_json[0]["text"]
    captured, prepared = await _invoke(monkeypatch, tenant=tenant, user=owner, agent=agent_id, content=content)
    assert token not in captured + prepared


@pytest.mark.parametrize("kind", [0, 1, 2], ids=["custom_api", "email", "personal_mcp"])
async def test_invocation_itself_loads_own_secret_without_ingress(owner_sessionmaker, monkeypatch, kind):
    tenant, owner, _, agent, _, _ = await _stored_accounts(owner_sessionmaker)
    captured, prepared = await _invoke(monkeypatch, tenant=tenant, user=owner, agent=agent, content=TOKENS[kind])
    assert TOKENS[kind] not in captured + prepared
    assert "[REDACTED_SECRET]" in captured + prepared


async def test_other_members_opaque_secret_is_not_loaded(owner_sessionmaker, monkeypatch):
    from app.services.session_live_input import submit_live_human_input

    tenant, _, other, agent_id, session_id, _ = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        agent, user, session = await db.get(Agent, agent_id), await db.get(User, other), await db.get(ChatSession, session_id)
        agent.creator_id = other
        agent.owner_user_id = other
        session.user_id = other
        await db.commit()
        receipt = await submit_live_human_input(db=db, agent=agent, user=user, session=session,
            content=" ".join(TOKENS), source="web")
        row = await db.get(SessionTurnInput, uuid.UUID(receipt["input_id"]))
        assert all(token in str(row.content_parts_json) for token in TOKENS)
    captured, prepared = await _invoke(monkeypatch, tenant=tenant, user=other, agent=agent_id, content=" ".join(TOKENS))
    assert all(token in captured + prepared for token in TOKENS)


@pytest.mark.parametrize("path", ["https://other.example/x", "//evil.example/x"])
async def test_tool_rejects_cross_origin_before_provider(owner_sessionmaker, monkeypatch, path):
    from app.services import custom_api_connectors as service

    tenant, _, _, agent, _, tool_id = await _stored_accounts(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        tool = await db.get(Tool, tool_id)
        tool.config = {**tool.config, "action": {"path": path}}
        await db.commit()
        name = tool.name
    calls = []

    async def provider(*args, **kwargs):
        calls.append(kwargs)
        return "Synthetic effect"

    monkeypatch.setattr(service, "tenant_scoped_session", lambda tid: tenant_scoped_session(tid, session_factory=owner_sessionmaker))
    monkeypatch.setattr(service, "execute_prepared_custom_api_request", provider)
    result = await service.execute_custom_api_tool(name, {}, agent_id=agent)
    assert calls == []
    assert "连接地址与配置不一致" in result
    assert "D9sSyntheticCompanyValue" not in result
