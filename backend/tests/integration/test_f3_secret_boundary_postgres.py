"""Pin exact-secret inventory bindings and ORM state on PostgreSQL."""

from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event
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


@dataclass(frozen=True)
class BoundaryCase:
    agent_id: UUID | None
    requester_id: UUID | None
    expected: ExactSecretBoundary


@dataclass(frozen=True)
class PopulatedBoundary:
    tenant: UUID
    agent: UUID
    channel: UUID
    cases: dict[str, BoundaryCase]


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
    foreign_tenant, _, foreign_agent, _, _ = await _seed_session(owner_sessionmaker)
    model, channel, tool, server, personal, other_mcp, api, email, other_user = (uuid4() for _ in range(9))
    global_model, global_tool, foreign_tool, foreign_server, other_agent, empty_agent = (uuid4() for _ in range(6))
    extra_servers = {
        name: uuid4()
        for name in ("mcp-disabled-assignment", "mcp-other-agent", "mcp-foreign-assignment", "mcp-parentless")
    }
    shared = "fixture-synthetic-shared-secret"
    provider = get_secrets_provider()
    async with owner_sessionmaker() as db:
        db.add_all([
            User(id=other_user, tenant_id=tenant, username=other_user.hex,
                 email=f"{other_user.hex}@fixture.test", password_hash="fixture", display_name="Other member"),
            Agent(id=other_agent, tenant_id=tenant, creator_id=requester, name="Other employee"),
            Agent(id=empty_agent, tenant_id=tenant, creator_id=requester, name="Employee without MCP assignments"),
            LLMModel(id=model, tenant_id=tenant, provider="fixture", model="fixture", label="Enabled",
                     api_key_encrypted=provider.encrypt(shared), enabled=True, supports_vision=True),
            LLMModel(tenant_id=tenant, provider="fixture", model="fixture", label="Disabled",
                     api_key_encrypted=provider.encrypt("fixture-disabled-model"), enabled=False),
            LLMModel(tenant_id=foreign_tenant, provider="fixture", model="fixture", label="Foreign",
                     api_key_encrypted=provider.encrypt("fixture-foreign-model"), enabled=True),
            LLMModel(id=global_model, provider="fixture", model="fixture", label="Platform model",
                     api_key_encrypted=provider.encrypt("fixture-global-model"), enabled=True),
            TenantChannelConfig(tenant_id=tenant, channel_type="wecom", verification_token="fixture-company-token"),
            TenantChannelConfig(tenant_id=tenant, channel_type="feishu", app_secret="fixture-disabled-channel",
                                is_active=False),
            TenantChannelConfig(tenant_id=foreign_tenant, channel_type="wecom", app_secret="fixture-foreign-tenant-channel"),
            Tool(id=tool, tenant_id=tenant, name="fixture-f3", display_name="F3 Fixture",
                 config={"api_key": "fixture-base-key", "bearer_token": "fixture-base-bearer"},
                 config_schema={"fields": [{"key": "api_key", "type": "password"}]}),
            Tool(tenant_id=tenant, name="fixture-f3-disabled", display_name="Disabled tool", enabled=False,
                 config={"api_key": "fixture-disabled-tool"}),
            Tool(id=global_tool, name="fixture-f3-global", display_name="Platform tool",
                 config={"api_key": "fixture-global-tool"}),
            Tool(id=foreign_tool, tenant_id=foreign_tenant, name="fixture-f3-foreign", display_name="Foreign tool",
                 config={"api_key": "fixture-foreign-tool"}),
            MCPServer(id=server, tenant_id=tenant, server_key="fixture-f3-root", name="F3 MCP Root",
                      config_json={"oauth": {
                          "token": encrypt_token_set(OAuthTokenSet("fixture-mcp-access", refresh_token="fixture-mcp-refresh")),
                          "client_secret": encrypt_value("fixture-mcp-client"),
                          "pending": {"verifier": encrypt_value("fixture-mcp-verifier"),
                                      "client_secret": encrypt_value("fixture-mcp-pending-client")},
                      }}),
            MCPServer(id=foreign_server, tenant_id=foreign_tenant, server_key="fixture-f3-foreign", name="Foreign server",
                      config_json={"oauth": {"client_secret": encrypt_value("fixture-foreign-mcp-server")}}),
            *(
                MCPServer(id=server_id, tenant_id=tenant, server_key=name, name=name,
                          owner_user_id=requester if name == "mcp-parentless" else None,
                          config_json={"oauth": {"client_secret": encrypt_value(f"fixture-{name}")}})
                for name, server_id in extra_servers.items()
            ),
        ])
        await db.flush()
        (await db.get(Agent, agent)).primary_model_id = model
        db.add_all([
            ChannelConfig(id=channel, agent_id=agent, tenant_id=tenant, channel_type="feishu", app_secret=shared,
                          is_configured=True,
                          extra_config={"nested": {"bot_token": "fixture-agent-channel-token"}}),
            ChannelConfig(agent_id=other_agent, tenant_id=tenant, channel_type="feishu",
                          app_secret="fixture-other-agent-channel"),
            ChannelConfig(agent_id=foreign_agent, tenant_id=foreign_tenant, channel_type="feishu",
                          app_secret="fixture-foreign-agent-channel"),
            TenantToolConfig(tenant_id=tenant, tool_id=tool, config={"api_key": "fixture-tenant-tool-key"}),
            TenantToolConfig(tenant_id=tenant, tool_id=global_tool, enabled=False,
                             config={"api_key": "fixture-disabled-tenant-override"}),
            TenantToolConfig(tenant_id=foreign_tenant, tool_id=global_tool,
                             config={"api_key": "fixture-foreign-tenant-override"}),
            AgentTool(tenant_id=tenant, agent_id=agent, tool_id=tool, config={"api_key": "fixture-agent-tool-key"}),
            AgentTool(tenant_id=tenant, agent_id=agent, tool_id=global_tool, enabled=False,
                      config={"api_key": "fixture-disabled-agent-tool"}),
            AgentTool(tenant_id=tenant, agent_id=other_agent, tool_id=global_tool,
                      config={"api_key": "fixture-other-agent-tool"}),
            AgentMCPServerAssignment(tenant_id=tenant, agent_id=agent, mcp_server_id=server),
            AgentMCPServerAssignment(tenant_id=tenant, agent_id=agent, enabled=False,
                                     mcp_server_id=extra_servers["mcp-disabled-assignment"]),
            AgentMCPServerAssignment(tenant_id=tenant, agent_id=other_agent, mcp_server_id=extra_servers["mcp-other-agent"]),
            MCPServer(id=personal, tenant_id=tenant, owner_user_id=requester, parent_server_id=server,
                      server_key="fixture-f3-personal", name="F3 MCP Account",
                      config_json={"oauth": {"token": encrypt_token_set(OAuthTokenSet("fixture-mcp-personal"))}}),
            MCPServer(id=other_mcp, tenant_id=tenant, owner_user_id=other_user, parent_server_id=server,
                      server_key="fixture-f3-other", name="Other MCP Account",
                      config_json={"oauth": {"client_secret": encrypt_value("fixture-other-mcp-account")}}),
            CustomApiAccount(id=api, tenant_id=tenant, owner_user_id=requester, tool_id=tool, name="F3 API Account",
                             config={"api_key": shared, "bearer_token": "fixture-personal-api-bearer",
                                     "basic_password": "fixture-personal-api-password"}),
            CustomApiAccount(tenant_id=tenant, owner_user_id=other_user, tool_id=tool, name="Other API Account",
                             config={"api_key": "fixture-other-api-account"}),
            EmailAccount(id=email, tenant_id=tenant, owner_user_id=requester, name="F3 Mailbox",
                         config={"auth_code": "fixture-personal-email-auth"}),
            EmailAccount(tenant_id=tenant, owner_user_id=other_user, name="Other Mailbox",
                         config={"auth_code": "fixture-other-email-auth"}),
            # The schema allows these foreign-tenant rows to reference local IDs.
            # The owner connection makes each tenant predicate observable on its own.
            AgentTool(tenant_id=foreign_tenant, agent_id=empty_agent, tool_id=global_tool,
                      config={"api_key": "fixture-foreign-agent-override"}),
            AgentMCPServerAssignment(tenant_id=foreign_tenant, agent_id=agent,
                                     mcp_server_id=extra_servers["mcp-foreign-assignment"]),
            MCPServer(tenant_id=foreign_tenant, owner_user_id=requester, parent_server_id=foreign_server,
                      server_key="fixture-f3-foreign-account", name="Foreign MCP Account",
                      config_json={"oauth": {"client_secret": encrypt_value("fixture-foreign-mcp-account")}}),
            CustomApiAccount(tenant_id=foreign_tenant, owner_user_id=requester, tool_id=foreign_tool,
                             name="Foreign API Account", config={"api_key": "fixture-foreign-api-account"}),
            EmailAccount(tenant_id=foreign_tenant, owner_user_id=requester, name="Foreign Mailbox",
                         config={"auth_code": "fixture-foreign-email-auth"}),
        ])
        await db.commit()

    # Keep the pre-F3 reference order when one secret occurs in several sources.
    models = [
        (f"llm-model://{model}/api_key", shared),
        (f"llm-model://{global_model}/api_key", "fixture-global-model"),
    ]

    def tool_bindings(scope, key):
        prefix = f"tool-config://{tenant}/{scope}"
        return [
            (f"{prefix}/{tool}/api_key", key),
            (f"{prefix}/{tool}/bearer_token", "fixture-base-bearer"),
            (f"{prefix}/{global_tool}/api_key", "fixture-global-tool"),
        ]

    assigned_mcp = [
        (f"mcp-server://{server}/oauth/access_token", "fixture-mcp-access"),
        (f"mcp-server://{server}/oauth/refresh_token", "fixture-mcp-refresh"),
        (f"mcp-server://{server}/oauth/client_secret", "fixture-mcp-client"),
        (f"mcp-server://{server}/oauth/pkce_verifier", "fixture-mcp-verifier"),
        (f"mcp-server://{server}/oauth/pending_client_secret", "fixture-mcp-pending-client"),
    ]
    personal_mcp = (f"mcp-server://{personal}/oauth/access_token", "fixture-mcp-personal")
    tenant_mcp = [
        *assigned_mcp,
        personal_mcp,
        (f"mcp-server://{other_mcp}/oauth/client_secret", "fixture-other-mcp-account"),
        *[(f"mcp-server://{server_id}/oauth/client_secret", f"fixture-{name}") for name, server_id in extra_servers.items()],
    ]
    agent_channel = [
        (f"channel-config://{agent}/feishu/app_secret", shared),
        (f"channel-config://{agent}/feishu/extra_config/nested/bot_token", "fixture-agent-channel-token"),
    ]
    tenant_channel = (f"channel-config://tenant-{tenant}/wecom/verification_token", "fixture-company-token")
    requester_bindings = [
        (f"custom-api-account://{api}/api_key", shared),
        (f"custom-api-account://{api}/bearer_token", "fixture-personal-api-bearer"),
        (f"custom-api-account://{api}/basic_password", "fixture-personal-api-password"),
        (f"email-account://{email}/auth_code", "fixture-personal-email-auth"),
        personal_mcp,
    ]
    agent_bindings = [
        *models, *tool_bindings(agent, "fixture-agent-tool-key"), *assigned_mcp, *agent_channel, tenant_channel,
    ]
    tenant_bindings = [
        *models, *tool_bindings("tenant", "fixture-tenant-tool-key"), *tenant_mcp, *agent_channel,
        (f"channel-config://{other_agent}/feishu/app_secret", "fixture-other-agent-channel"), tenant_channel,
    ]
    empty_agent_bindings = [*models, *tool_bindings(empty_agent, "fixture-tenant-tool-key"), tenant_channel]
    try:
        yield PopulatedBoundary(tenant, agent, channel, {
            "agent": BoundaryCase(agent, None, ExactSecretBoundary.from_pairs(agent_bindings)),
            "requester": BoundaryCase(agent, requester, ExactSecretBoundary.from_pairs(agent_bindings + requester_bindings)),
            "tenant": BoundaryCase(None, None, ExactSecretBoundary.from_pairs(tenant_bindings)),
            "tenant-requester": BoundaryCase(None, requester, ExactSecretBoundary.from_pairs(tenant_bindings + requester_bindings)),
            "empty-agent": BoundaryCase(empty_agent, None, ExactSecretBoundary.from_pairs(empty_agent_bindings)),
        })
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(delete(LLMModel).where(LLMModel.id == global_model))
            await db.execute(delete(Tool).where(Tool.id == global_tool))
            await db.commit()


@pytest.mark.parametrize("role", ["app", "owner"])
@pytest.mark.parametrize("scope", ["agent", "requester", "tenant", "tenant-requester", "empty-agent"])
async def test_populated_secret_boundary_matches_pre_f3_and_uses_one_statement(
    populated_boundary, scope, role, app_user_sessionmaker, owner_sessionmaker,
):
    tenant = populated_boundary.tenant
    case = populated_boundary.cases[scope]
    factory = app_user_sessionmaker if role == "app" else owner_sessionmaker
    async with tenant_scoped_session(tenant, session_factory=factory) as db:
        with count_queries() as calls:
            boundary = (
                await load_requester_exact_secret_boundary(
                    db, tenant_id=tenant, agent_id=case.agent_id, requester_user_id=case.requester_id,
                )
                if case.requester_id is not None else await load_exact_secret_boundary(db, tenant_id=tenant, agent_id=case.agent_id)
            )

    excluded = {
        "fixture-disabled-model", "fixture-disabled-channel", "fixture-disabled-tool", "fixture-disabled-tenant-override",
        "fixture-disabled-agent-tool", "fixture-other-agent-tool", "fixture-other-api-account", "fixture-other-email-auth",
        "fixture-foreign-model", "fixture-foreign-agent-channel", "fixture-foreign-tenant-channel", "fixture-foreign-tool",
        "fixture-foreign-tenant-override", "fixture-foreign-agent-override", "fixture-foreign-mcp-server",
        "fixture-foreign-mcp-account", "fixture-foreign-api-account", "fixture-foreign-email-auth",
    }
    if case.agent_id is not None:
        excluded.update({
            "fixture-other-agent-channel", "fixture-mcp-disabled-assignment", "fixture-mcp-other-agent",
            "fixture-mcp-foreign-assignment", "fixture-mcp-parentless", "fixture-other-mcp-account",
        })
    assert not excluded.intersection(binding.value for binding in boundary._bindings)
    assert boundary._bindings == case.expected._bindings
    assert len(calls) == 1


async def test_inventory_leaves_full_channel_config_in_session(populated_boundary, app_user_sessionmaker):
    tenant, agent = populated_boundary.tenant, populated_boundary.agent
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        loaded_channels = []

        @event.listens_for(db.sync_session, "loaded_as_persistent")
        def keep_channel_alive(_session, instance):
            # Keep the first load alive through the identity map's weak reference.
            if isinstance(instance, ChannelConfig):
                loaded_channels.append(instance)

        await load_exact_secret_boundary(db, tenant_id=tenant, agent_id=agent)
        (loaded_channel,) = loaded_channels
        channel = await db.get(ChannelConfig, populated_boundary.channel)
        assert channel is loaded_channel
        assert channel.tenant_id == tenant
        assert channel.is_configured is True


@pytest.mark.parametrize("expire_on_commit", [False, True], ids=["retained", "expired"])
async def test_inventory_leaves_full_model_after_commit(populated_boundary, app_user_sessionmaker, expire_on_commit):
    tenant, agent = populated_boundary.tenant, populated_boundary.agent
    async with tenant_scoped_session(tenant, session_factory=app_user_sessionmaker) as db:
        db.sync_session.expire_on_commit = expire_on_commit
        employee = await db.get(Agent, agent)
        model = await db.get(LLMModel, employee.primary_model_id)
        await db.commit()

        await load_exact_secret_boundary(db, tenant_id=tenant, agent_id=agent)

        assert model.supports_vision is True
        assert model.provider == "fixture"
