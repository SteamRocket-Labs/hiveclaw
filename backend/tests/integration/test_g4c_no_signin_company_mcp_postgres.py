"""G4c: an approved no-sign-in company MCP service is found and used through
tool search — not read as a web page, and not presented as needing the
member's account.

Production shape (run 17 read-only probe,
hiveclaw-evidence/p1/e2e/run17-ops/g4c_shape_readonly.out):
- company MCPServer: status="connected", auth_status="none", owner_user_id=None,
  name="DeepWiki", server_url=https://mcp.deepwiki.com/mcp, registry "direct";
- three approved tools (platform_generated_reviewed purpose names the
  connection and host) linked through mcp_server_tools;
- one enabled auto server assignment plus enabled agent_tools rows
  (source="system") for the asking bot — the backfilled gating path;
- zero per-member account, grant, or binding rows for the member.
"""

import uuid

import pytest
from sqlalchemy import select

from tests.integration.test_g4b_found_by_name_postgres import _seed_company
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

SERVER_NAME = "DeepWiki"
SERVER_URL = "https://mcp.deepwiki.com/mcp"
SERVER_HOST = "mcp.deepwiki.com"
TOOL_REMOTE_NAMES = ("ask_wiki_question", "read_wiki_contents", "read_wiki_structure")
# The exact query the model sent in run 17 step 7a, plus the two mission queries.
PRODUCTION_QUERY = "deepwiki github repository docs"
NATURAL_QUERIES = (PRODUCTION_QUERY, "DeepWiki", "用 DeepWiki 查仓库")
EXPECTED_TOOL_NAMES = tuple(f"mcp_{remote}" for remote in TOOL_REMOTE_NAMES)


async def _seed_production_shape(owner_sessionmaker, app_user_sessionmaker, *, second_tool_enabled=True):
    """The run 17 DeepWiki rows, stored the way production stores them.

    ``second_tool_enabled=False`` flips one tool's global enable off (the
    quarantine/revocation switch) for the hidden-tool boundary test.
    """
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import (
        AgentMCPServerAssignment,
        MCPServer,
        MCPServerTool,
    )
    from app.models.tool import AgentTool, Tool
    from app.services.mcp_metadata_trust import _neutral_description, current_mcp_fingerprint, review_mcp_metadata

    app, tenant_id, admin, second_id = await _seed_company(owner_sessionmaker, app_user_sessionmaker)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        server = MCPServer(tenant_id=tenant_id, name=SERVER_NAME, server_key="deepwiki",
            registry_source="direct", server_url=SERVER_URL, status="connected", auth_status="none")
        db.add(server)
        await db.flush()
        for index, remote in enumerate(TOOL_REMOTE_NAMES):
            tool = Tool(tenant_id=tenant_id, name=f"mcp_{remote}", display_name=remote,
                description=_neutral_description(tool_name=remote, server_name=SERVER_NAME, server_url=SERVER_URL),
                type="mcp", category="custom", parameters_schema={"type": "object", "properties": {}},
                config={}, mcp_server_url=SERVER_URL, mcp_server_name=SERVER_NAME, mcp_tool_name=remote,
                mcp_raw_description="remote wiki evidence", mcp_raw_schema={}, enabled=True, is_default=False)
            db.add(tool)
            await db.flush()
            review_mcp_metadata(tool, decision="approve", reviewer_id=admin.id,
                expected_fingerprint=current_mcp_fingerprint(tool))
            if index == 1:
                tool.enabled = second_tool_enabled
            db.add(MCPServerTool(tenant_id=tenant_id, mcp_server_id=server.id, tool_id=tool.id,
                mcp_tool_name=remote))
            db.add(AgentTool(agent_id=second_id, tenant_id=tenant_id, tool_id=tool.id,
                enabled=True, source="system", config={}))
        db.add(AgentMCPServerAssignment(tenant_id=tenant_id, agent_id=second_id,
            mcp_server_id=server.id, enabled=True, default_tool_mode="auto", always_load=False))
        await db.commit()
        server_id = server.id
    return app, tenant_id, admin, second_id, server_id


async def test_natural_queries_find_the_no_signin_service_without_a_connection_card(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """The member's real words — including the model's exact run 17 query —
    find the approved tools with the validated host suffix, and a connected
    server that needs no sign-in never gets the connect_if_account_needed
    marker or the ask_user_question connection-card instruction."""
    from app.services import agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", lambda: False)

    for query in NATURAL_QUERIES:
        discovered = await agent_tools.discoverable_tool_names_for_query(second_id, query)
        assert set(EXPECTED_TOOL_NAMES) <= set(discovered), (query, discovered)
        result = await _tool_search(tmp_path, query, agent_id=second_id)
        for name in EXPECTED_TOOL_NAMES:
            # G4c addition 1: the validated host is part of the listing the
            # model reads, so the service is identifiable by name and host.
            assert f"{name} ({SERVER_HOST})" in result, (query, result)
        # auth_status="none" on a connected server means no account is needed:
        # the row carries no connect marker and the whole tool_search output
        # contains neither the marker key nor the connection-card instruction.
        assert f"- {SERVER_NAME} | mcp_server_id: {server_id}" in result.splitlines(), result
        assert "connect_if_account_needed" not in result, result
        assert "ask_user_question" not in result, result


async def test_hidden_tools_stay_hidden_under_the_natural_query(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """The relaxed matching never relaxes governance: a tool whose global
    enable is off (unapproved/revoked quarantine state), a disabled server
    assignment, and a deny override each stay undiscoverable even when a query
    term matches the service name."""
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import AgentMCPServerAssignment, AgentMCPToolOverride
    from app.services import agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker, second_tool_enabled=False)

    scoped_runtime_factories()
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", lambda: False)

    # The globally disabled (revoked/quarantined) tool never appears; the two
    # enabled approved tools still do.
    discovered = await agent_tools.discoverable_tool_names_for_query(second_id, PRODUCTION_QUERY)
    assert "mcp_read_wiki_contents" not in discovered, discovered
    assert {"mcp_ask_wiki_question", "mcp_read_wiki_structure"} <= set(discovered), discovered

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        db.add(AgentMCPToolOverride(tenant_id=tenant_id, agent_id=second_id,
            mcp_server_id=server_id, tool_name="ask_wiki_question", mode="deny"))
        await db.commit()

    discovered = await agent_tools.discoverable_tool_names_for_query(second_id, PRODUCTION_QUERY)
    assert "mcp_ask_wiki_question" not in discovered, discovered
    assert "mcp_read_wiki_structure" in discovered, discovered

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assignment = (await db.execute(select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.agent_id == second_id,
            AgentMCPServerAssignment.mcp_server_id == server_id))).scalar_one()
        assignment.enabled = False
        await db.commit()

    assert await agent_tools.discoverable_tool_names_for_query(second_id, PRODUCTION_QUERY) == []
    result = await _tool_search(tmp_path, PRODUCTION_QUERY, agent_id=second_id)
    assert SERVER_HOST not in result, result


async def test_a_service_that_really_needs_an_account_still_asks_for_one(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """The contrast case: a server with status="needs_auth" (a per-member
    credential genuinely missing) keeps the connect_if_account_needed marker
    and the shared connection-card instruction — side by side with a no-sign-in
    server that gets neither."""
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import MCPServer
    from app.services import agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", lambda: False)

    auth_server_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        db.add(MCPServer(id=auth_server_id, tenant_id=tenant_id, name="Needs Auth Wiki",
            server_key="needs-auth-wiki", registry_source="direct",
            server_url="https://needs-auth.example/mcp", status="needs_auth", auth_status="none"))
        await db.commit()

    result = await _tool_search(tmp_path, PRODUCTION_QUERY, agent_id=second_id)
    deepwiki_row = next(line for line in result.splitlines() if line.startswith(f"- {SERVER_NAME} | mcp_server_id:"))
    assert deepwiki_row == f"- {SERVER_NAME} | mcp_server_id: {server_id}", result
    assert f"- Needs Auth Wiki | mcp_server_id: {auth_server_id} | connect_if_account_needed" in result.splitlines(), result
    assert result.count('"tool": "ask_user_question"') == 1, result


async def test_runtime_prompt_names_installed_company_applications(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """Run 17 step 7c: a bot that never calls tool_search can still connect the
    member's words to the service, because the per-round runtime metadata names
    each reachable company application by its trusted label and validated host,
    with the tool_search route to its tools. Nothing is named when no company
    application is reachable for the bot."""
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import AgentMCPServerAssignment
    from app.services import agent_tools
    from app.services.agent_context import build_agent_runtime_context

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", lambda: False)

    context = await build_agent_runtime_context(second_id, tenant_id=tenant_id)
    section = context.split("## Company Applications", 1)
    assert len(section) == 2, context
    body = section[1].split("\n## ", 1)[0]
    assert f"- {SERVER_NAME} ({SERVER_HOST})" in body, body
    assert "tool_search" in body, body
    # Trusted text only: the raw URL and the remote description never appear.
    assert SERVER_URL not in body and "remote wiki evidence" not in body, body

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assignment = (await db.execute(select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.agent_id == second_id,
            AgentMCPServerAssignment.mcp_server_id == server_id))).scalar_one()
        assignment.enabled = False
        await db.commit()

    context = await build_agent_runtime_context(second_id, tenant_id=tenant_id)
    assert "## Company Applications" not in context or SERVER_NAME not in context, context
