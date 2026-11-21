"""Pin every input to the exact-secret boundary under the app's RLS role."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.custom_api_account import CustomApiAccount
from app.models.email_account import EmailAccount
from app.models.llm import LLMModel
from app.models.mcp_server import AgentMCPServerAssignment, MCPServer
from app.models.tenant_channel_config import TenantChannelConfig
from app.models.tenant_tool_config import TenantToolConfig
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.credential_boundary_loader import load_exact_secret_boundary, load_requester_exact_secret_boundary
from app.services.exact_secret_boundary import ExactSecretBoundary
from app.services.mcp_oauth import OAuthTokenSet, encrypt_token_set, encrypt_value
from app.services.secrets_provider import get_secrets_provider
from tests.services.test_session_input_control_v2 import _seed_session


@contextmanager
def count_queries():
    calls = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        if not statement.startswith("SET LOCAL "):
            calls.append(None)

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield calls
    finally:
        event.remove(Engine, "before_cursor_execute", record)


@pytest.fixture
async def populated_boundary(owner_sessionmaker):
    tenant, requester, agent, _, _ = await _seed_session(owner_sessionmaker)
    foreign_tenant, _, _, _, _ = await _seed_session(owner_sessionmaker)
    model, channel, tool, server, personal, api, email, other_user = (uuid4() for _ in range(8))
    shared = "fixture-synthetic-shared-secret"
    provider = get_secrets_provider()
    async with owner_sessionmaker() as db:
        db.add_all([
            User(id=other_user, tenant_id=tenant, username=other_user.hex,
                 email=f"{other_user.hex}@fixture.test", password_hash="fixture", display_name="Other member"),
            LLMModel(id=model, tenant_id=tenant, provider="fixture", model="fixture", label="Enabled",
                     api_key_encrypted=provider.encrypt(shared), enabled=True, supports_vision=True),
            LLMModel(tenant_id=tenant, provider="fixture", model="fixture", label="Disabled",
                     api_key_encrypted=provider.encrypt("fixture-disabled-model"), enabled=False),
            LLMModel(tenant_id=foreign_tenant, provider="fixture", model="fixture", label="Foreign",
                     api_key_encrypted=provider.encrypt("fixture-foreign-model"), enabled=True),
            ChannelConfig(id=channel, agent_id=agent, tenant_id=tenant, channel_type="feishu", app_secret=shared,
                          is_configured=True,
                          extra_config={"nested": {"bot_token": "fixture-agent-channel-token"}}),
            TenantChannelConfig(tenant_id=tenant, channel_type="wecom", verification_token="fixture-company-token"),
            TenantChannelConfig(tenant_id=tenant, channel_type="feishu", app_secret="fixture-disabled-channel",
                                is_active=False),
            Tool(id=tool, tenant_id=tenant, name="fixture-f3", display_name="F3 Fixture",
                 config={"api_key": "fixture-base-key", "bearer_token": "fixture-base-bearer"},
                 config_schema={"fields": [{"key": "api_key", "type": "password"}]}),
            MCPServer(id=server, tenant_id=tenant, server_key="fixture-f3-root", name="F3 MCP Root",
                      config_json={"oauth": {
                          "token": encrypt_token_set(OAuthTokenSet("fixture-mcp-access", refresh_token="fixture-mcp-refresh")),
                          "client_secret": encrypt_value("fixture-mcp-client"),
                          "pending": {"verifier": encrypt_value("fixture-mcp-verifier"),
                                      "client_secret": encrypt_value("fixture-mcp-pending-client")},
                      }}),
        ])
        await db.flush()
        (await db.get(Agent, agent)).primary_model_id = model
        db.add_all([
            TenantToolConfig(tenant_id=tenant, tool_id=tool, config={"api_key": "fixture-tenant-tool-key"}),
            AgentTool(tenant_id=tenant, agent_id=agent, tool_id=tool, config={"api_key": "fixture-agent-tool-key"}),
            AgentMCPServerAssignment(tenant_id=tenant, agent_id=agent, mcp_server_id=server),
            MCPServer(id=personal, tenant_id=tenant, owner_user_id=requester, parent_server_id=server,
                      server_key="fixture-f3-personal", name="F3 MCP Account",
                      config_json={"oauth": {"token": encrypt_token_set(OAuthTokenSet("fixture-mcp-personal"))}}),
            MCPServer(tenant_id=tenant, owner_user_id=other_user, parent_server_id=server,
                      server_key="fixture-f3-other", name="Other MCP Account",
                      config_json={"oauth": {"client_secret": encrypt_value("fixture-other-mcp-account")}}),
            CustomApiAccount(id=api, tenant_id=tenant, owner_user_id=requester, tool_id=tool, name="F3 API Account",
                             config={"api_key": shared, "bearer_token": "fixture-personal-api-bearer",
                                     "basic_password": "fixture-personal-api-password"}),
            EmailAccount(id=email, tenant_id=tenant, owner_user_id=requester, name="F3 Mailbox",
                         config={"auth_code": "fixture-personal-email-auth"}),
            EmailAccount(tenant_id=tenant, owner_user_id=other_user, name="Other Mailbox",
                         config={"auth_code": "fixture-other-email-auth"}),
        ])
        await db.commit()

    # The pre-F3 loaders produce these bindings in this category order.
    shared_bindings = [
        (f"llm-model://{model}/api_key", shared),
        (f"tool-config://{tenant}/{agent}/{tool}/api_key", "fixture-agent-tool-key"),
        (f"tool-config://{tenant}/{agent}/{tool}/bearer_token", "fixture-base-bearer"),
        (f"mcp-server://{server}/oauth/access_token", "fixture-mcp-access"),
        (f"mcp-server://{server}/oauth/refresh_token", "fixture-mcp-refresh"),
        (f"mcp-server://{server}/oauth/client_secret", "fixture-mcp-client"),
        (f"mcp-server://{server}/oauth/pkce_verifier", "fixture-mcp-verifier"),
        (f"mcp-server://{server}/oauth/pending_client_secret", "fixture-mcp-pending-client"),
        (f"channel-config://{agent}/feishu/app_secret", shared),
        (f"channel-config://{agent}/feishu/extra_config/nested/bot_token", "fixture-agent-channel-token"),
        (f"channel-config://tenant-{tenant}/wecom/verification_token", "fixture-company-token"),
    ]
    requester_bindings = [
        (f"custom-api-account://{api}/api_key", shared),
        (f"custom-api-account://{api}/bearer_token", "fixture-personal-api-bearer"),
        (f"custom-api-account://{api}/basic_password", "fixture-personal-api-password"),
        (f"email-account://{email}/auth_code", "fixture-personal-email-auth"),
        (f"mcp-server://{personal}/oauth/access_token", "fixture-mcp-personal"),
    ]
    return tenant, agent, requester, shared_bindings, requester_bindings, channel


@pytest.mark.parametrize("include_requester", [False, True], ids=["agent", "requester"])
async def test_populated_secret_boundary_matches_pre_f3_and_uses_one_statement(
    populated_boundary, include_requester, app_user_sessionmaker,
):
    tenant, agent, requester, shared_bindings, requester_bindings, _ = populated_boundary
    expected = ExactSecretBoundary.from_pairs(shared_bindings + (requester_bindings if include_requester else []))
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        with count_queries() as calls:
            boundary = (
                await load_requester_exact_secret_boundary(db, tenant_id=tenant, agent_id=agent, requester_user_id=requester)
                if include_requester else await load_exact_secret_boundary(db, tenant_id=tenant, agent_id=agent)
            )

    assert boundary._bindings == expected._bindings
    assert len(calls) == 1


async def test_inventory_leaves_full_channel_config_in_session(populated_boundary, app_user_sessionmaker):
    tenant, agent, _, _, _, channel_id = populated_boundary
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        loaded_channels = []

        @event.listens_for(db.sync_session, "loaded_as_persistent")
        def keep_channel_alive(_session, instance):
            # Keep the first load alive through the identity map's weak reference.
            if isinstance(instance, ChannelConfig):
                loaded_channels.append(instance)

        await load_exact_secret_boundary(db, tenant_id=tenant, agent_id=agent)
        (loaded_channel,) = loaded_channels
        channel = await db.get(ChannelConfig, channel_id)
        assert channel is loaded_channel
        assert channel.tenant_id == tenant
        assert channel.is_configured is True


@pytest.mark.parametrize("expire_on_commit", [False, True], ids=["retained", "expired"])
async def test_inventory_leaves_full_model_after_commit(populated_boundary, app_user_sessionmaker, expire_on_commit):
    tenant, agent, *_ = populated_boundary
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        db.sync_session.expire_on_commit = expire_on_commit
        employee = await db.get(Agent, agent)
        model = await db.get(LLMModel, employee.primary_model_id)
        await db.commit()

        await load_exact_secret_boundary(db, tenant_id=tenant, agent_id=agent)

        assert model.supports_vision is True
        assert model.provider == "fixture"
