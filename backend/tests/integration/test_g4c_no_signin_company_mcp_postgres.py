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
from tests.integration.test_r5e_unavailable_app_postgres import app_work as app_work  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

SERVER_NAME = "DeepWiki"
SERVER_URL = "https://mcp.deepwiki.com/mcp"
SERVER_HOST = "mcp.deepwiki.com"
TOOL_REMOTE_NAMES = ("ask_wiki_question", "read_wiki_contents", "read_wiki_structure")
# The exact query the model sent in run 17 step 7a, plus the natural-language
# variants: with spaces, without spaces (CJK/Latin boundary), and with a
# trailing full-width comma.
PRODUCTION_QUERY = "deepwiki github repository docs"
NATURAL_QUERIES = (PRODUCTION_QUERY, "DeepWiki", "用 DeepWiki 查仓库", "用DeepWiki查仓库", "DeepWiki，")
EXPECTED_TOOL_NAMES = tuple(f"mcp_{remote}" for remote in TOOL_REMOTE_NAMES)
# A second approved company application whose descriptions use the CURRENT
# neutral template ("from the company connection ...") — the flood probe shape.
DECOY_NAME = "Linear"
DECOY_URL = "https://mcp.linear.app/mcp"
DECOY_REMOTE_NAMES = ("create_issue", "search_issues")
DECOY_TOOL_NAMES = tuple(f"mcp_{remote}" for remote in DECOY_REMOTE_NAMES)
# Generic words that appear inside every platform description template, in the
# tool-name prefix, or in the host TLD — none of them may return an MCP tool.
FLOOD_QUERIES = ("search the web", "company knowledge base", "send a message to the team",
    "read data from spreadsheet", "and use", "mcp", "com", "on", "use", "with", "the")


def _production_description(remote: str) -> str:
    """The pre-G4b template the production DeepWiki rows still carry (191-193
    chars per the run 17 read-only probe)."""
    return (
        f"External MCP operation '{remote}' provided by endpoint host '{SERVER_HOST}'. "
        "Remote metadata is untrusted and omitted. "
        "Use only with reviewed arguments and normal Hive governance."
    )


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
                description=_production_description(remote),
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

        # The decoy application (current neutral template) so template-word
        # floods and cross-application precision are observable.
        decoy = MCPServer(tenant_id=tenant_id, name=DECOY_NAME, server_key="linear",
            registry_source="direct", server_url=DECOY_URL, status="connected", auth_status="none")
        db.add(decoy)
        await db.flush()
        for remote in DECOY_REMOTE_NAMES:
            tool = Tool(tenant_id=tenant_id, name=f"mcp_{remote}", display_name=remote,
                description=_neutral_description(tool_name=remote, server_name=DECOY_NAME, server_url=DECOY_URL),
                type="mcp", category="custom", parameters_schema={"type": "object", "properties": {}},
                config={}, mcp_server_url=DECOY_URL, mcp_server_name=DECOY_NAME, mcp_tool_name=remote,
                mcp_raw_description="remote linear evidence", mcp_raw_schema={}, enabled=True, is_default=False)
            db.add(tool)
            await db.flush()
            review_mcp_metadata(tool, decision="approve", reviewer_id=admin.id,
                expected_fingerprint=current_mcp_fingerprint(tool))
            db.add(MCPServerTool(tenant_id=tenant_id, mcp_server_id=decoy.id, tool_id=tool.id,
                mcp_tool_name=remote))
            db.add(AgentTool(agent_id=second_id, tenant_id=tenant_id, tool_id=tool.id,
                enabled=True, source="system", config={}))
        db.add(AgentMCPServerAssignment(tenant_id=tenant_id, agent_id=second_id,
            mcp_server_id=decoy.id, enabled=True, default_tool_mode="auto", always_load=False))
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
        # G4c r3 (B1): identity scope — the query names DeepWiki, so only
        # DeepWiki's tools answer; the Linear decoy (same neutral template
        # description shape) must not ride along on a template word.
        assert set(discovered) == set(EXPECTED_TOOL_NAMES), (query, discovered)
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


async def test_template_words_and_generic_tokens_return_no_mcp_tool(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """G4c r3 (B1 flood): every approved company MCP tool shares the platform's
    description templates and the mcp_/host naming scheme. Words from those
    templates ("and use", "the", "on"), the tool-name prefix ("mcp") and the
    host TLD ("com") must not list any MCP tool — only identity tokens
    (service name, host labels, tool-name runs) match."""
    from app.services import agent_tools

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()
    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", _async_false())

    for query in FLOOD_QUERIES:
        discovered = await agent_tools.discoverable_tool_names_for_query(second_id, query)
        # The acceptance is about MCP tools; deferred L2 capability packs have
        # their own (pre-existing, out-of-scope) query matching.
        mcp_tools = [name for name in discovered if name.startswith(("mcp_", "mcp__"))]
        assert mcp_tools == [], (query, mcp_tools)


def _async_false():
    async def _no_cli() -> bool:
        return False
    return _no_cli


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
    """Run 17 step 7c, round 3: a bot that never calls tool_search can still
    connect the member's words to the service. The section rides the per-turn
    deferred-catalog enumeration (``available_deferred_tool_candidates_for_agent``
    → the approval-gated MCP listing) — the same single governed query — and
    renders as its own ledger section next to the deferred index."""
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import AgentMCPServerAssignment
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix
    from app.services import agent_tools
    from app.services.agent_tools import available_deferred_tool_candidates_for_agent

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()

    async def _no_feishu_cli() -> bool:
        return False

    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", _no_feishu_cli)

    # The per-turn enumeration hands the pairs over with zero extra statements.
    connections: list[tuple[str, str]] = []
    await available_deferred_tool_candidates_for_agent(
        second_id, bound_only=True, source_unbound=[], company_connections=connections)
    assert (SERVER_NAME, SERVER_HOST) in connections, connections
    assert (DECOY_NAME, "mcp.linear.app") in connections, connections

    suffix = build_dynamic_prompt_suffix(
        available_deferred_tools=[{"name": "spawn_subagent"}],
        company_connections=connections,
    )
    section = suffix.split("## Company Applications", 1)
    assert len(section) == 2, suffix
    body = section[1].split("\n## ", 1)[0]
    assert f"- {SERVER_NAME} ({SERVER_HOST})" in body, body
    assert "tool_search" in body and "do not browse the application's website" in body, body
    # Trusted text only: raw URLs and remote descriptions never appear.
    assert SERVER_URL not in body and DECOY_URL not in body, body

    # Governance unchanged: with the server assignment disabled the pairs are
    # gone, so the section disappears.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assignment = (await db.execute(select(AgentMCPServerAssignment).where(
            AgentMCPServerAssignment.agent_id == second_id,
            AgentMCPServerAssignment.mcp_server_id == server_id))).scalar_one()
        assignment.enabled = False
        await db.commit()

    after: list[tuple[str, str]] = []
    await available_deferred_tool_candidates_for_agent(
        second_id, bound_only=True, source_unbound=[], company_connections=after)
    assert (SERVER_NAME, SERVER_HOST) not in after, after
    assert (DECOY_NAME, "mcp.linear.app") in after, after
    suffix_after = build_dynamic_prompt_suffix(
        available_deferred_tools=[{"name": "spawn_subagent"}],
        company_connections=after,
    )
    assert f"- {SERVER_NAME} ({SERVER_HOST})" not in suffix_after, suffix_after
    assert f"- {DECOY_NAME} (mcp.linear.app)" in suffix_after, suffix_after


async def test_company_applications_section_reaches_the_model_wire(
    app_work, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    """End to end: one scripted web-chat turn with the production shape — the
    compiled model request carries the Company Applications section naming
    DeepWiki by trusted label and host, with no tool_search call needed."""
    import app.runtime.invoker as invoker
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer, MCPServerTool
    from app.models.tool import AgentTool, Tool
    from app.services.mcp_metadata_trust import _neutral_description, current_mcp_fingerprint, review_mcp_metadata
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    s = app_work
    async with owner_sessionmaker() as db:
        await pin_rls_tenant_context(db, s["tenant_id"])
        server = MCPServer(tenant_id=s["tenant_id"], name=SERVER_NAME, server_key="deepwiki-e2e",
            registry_source="direct", server_url=SERVER_URL, status="connected", auth_status="none")
        db.add(server)
        await db.flush()
        for remote in TOOL_REMOTE_NAMES:
            tool = Tool(tenant_id=s["tenant_id"], name=f"mcp_{remote}", display_name=remote,
                description=_neutral_description(tool_name=remote, server_name=SERVER_NAME, server_url=SERVER_URL),
                type="mcp", category="custom", parameters_schema={"type": "object", "properties": {}},
                config={}, mcp_server_url=SERVER_URL, mcp_server_name=SERVER_NAME, mcp_tool_name=remote,
                mcp_raw_description="remote wiki evidence", mcp_raw_schema={}, enabled=True, is_default=False)
            db.add(tool)
            await db.flush()
            review_mcp_metadata(tool, decision="approve", reviewer_id=s["user_id"],
                expected_fingerprint=current_mcp_fingerprint(tool))
            db.add(MCPServerTool(tenant_id=s["tenant_id"], mcp_server_id=server.id, tool_id=tool.id,
                mcp_tool_name=remote))
            db.add(AgentTool(agent_id=s["agent_id"], tenant_id=s["tenant_id"], tool_id=tool.id,
                enabled=True, source="system", config={}))
        db.add(AgentMCPServerAssignment(tenant_id=s["tenant_id"], agent_id=s["agent_id"],
            mcp_server_id=server.id, enabled=True, default_tool_mode="auto", always_load=False))
        await db.commit()

    client = ScriptedLLMClient([{"content": "我先看看有什么可用能力。"}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    token = set_runtime_task_fence(task_id=s["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(s["run_id"])
    finally:
        reset_runtime_task_fence(token)

    assert len(client.requests) == 1
    compiled = "\n".join(str(message["content"]) for message in client.requests[0]["messages"])
    assert "## Company Applications" in compiled, compiled[-2000:]
    assert f"- {SERVER_NAME} ({SERVER_HOST})" in compiled, compiled[-2000:]
    assert SERVER_URL not in compiled.split("## Company Applications", 1)[1].split("\n## ", 1)[0]


async def test_drifted_or_pending_tools_are_never_listed(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """G4c r3 (S2): the listing gate must equal the schema gate. A tool whose
    stored metadata fingerprint no longer matches (the admin edited the URL or
    config after approval), or that is enabled while still pending review, is
    listed by neither tool_search text nor schema."""
    from app.database import pin_rls_tenant_context
    from app.models.tool import Tool
    from app.services import agent_tools

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()

    async def _no_feishu_cli() -> bool:
        return False

    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", _no_feishu_cli)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        drifted = (await db.execute(select(Tool).where(Tool.name == "mcp_ask_wiki_question"))).scalar_one()
        drifted.mcp_server_url = "https://mcp.deepwiki.com/changed-endpoint"
        pending = (await db.execute(select(Tool).where(Tool.name == "mcp_read_wiki_contents"))).scalar_one()
        pending.mcp_trust_status = "pending_review"
        pending.mcp_reviewed_fingerprint = None
        pending.enabled = True  # an admin flipped the global toggle without review
        await db.commit()

    discovered = await agent_tools.discoverable_tool_names_for_query(second_id, PRODUCTION_QUERY)
    assert discovered == ["mcp_read_wiki_structure"], discovered
    # The schema path agrees (text == schema).
    tools = await agent_tools.get_agent_tools_for_llm(second_id, core_only=False, requested_names=list(EXPECTED_TOOL_NAMES))
    assert {tool["function"]["name"] for tool in tools if tool["function"]["name"].startswith("mcp_")} == {
        "mcp_read_wiki_structure"}, tools


async def test_token_paste_parent_with_member_accounts_keeps_the_connect_card(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """G4c r3 (S3): a parent server whose members paste tokens (child accounts
    under parent_server_id) has the same connected/none/no-oauth shape as
    DeepWiki, but it is NOT a no-sign-in service — with at least one child
    account it keeps the connect_if_account_needed marker and the card."""
    from app.database import pin_rls_tenant_context
    from app.models.mcp_server import MCPServer
    from app.services import agent_tools
    from app.services.agent_tool_domains.workspace import _tool_search

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()

    async def _no_feishu_cli() -> bool:
        return False

    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", _no_feishu_cli)

    token_parent_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        db.add(MCPServer(id=token_parent_id, tenant_id=tenant_id, name="Paste Token Wiki",
            server_key="paste-token-wiki", registry_source="direct",
            server_url="https://paste-token.example/mcp", status="connected", auth_status="none"))
        # One member already pasted a token: a child account under the parent.
        db.add(MCPServer(tenant_id=tenant_id, name="Paste Token Wiki (member)",
            server_key="paste-token-member", owner_user_id=admin.id, parent_server_id=token_parent_id,
            server_url="https://paste-token.example/mcp", status="connected", auth_status="configured"))
        await db.commit()

    result = await _tool_search(tmp_path, PRODUCTION_QUERY, agent_id=second_id)
    deepwiki_row = next(line for line in result.splitlines() if line.startswith(f"- {SERVER_NAME} | mcp_server_id:"))
    assert deepwiki_row == f"- {SERVER_NAME} | mcp_server_id: {server_id}", result
    assert f"- Paste Token Wiki | mcp_server_id: {token_parent_id} | connect_if_account_needed" in result.splitlines(), result
    assert result.count('"tool": "ask_user_question"') == 1, result


async def test_foreign_tenant_and_unassigned_legacy_tools_stay_hidden(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """G4c r3 (S4): identity matching never relaxes governance. A full
    DeepWiki-shaped application in another tenant, and an approved, enabled,
    same-identity tool with no server row and no assignment (un-backfilled
    legacy shape, non-default), are both undiscoverable."""
    from app.database import pin_rls_tenant_context
    from app.models.tenant import Tenant
    from app.models.tool import Tool
    from app.services import agent_tools

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()

    async def _no_feishu_cli() -> bool:
        return False

    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", _no_feishu_cli)

    foreign_tenant = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=foreign_tenant, name="g4c-foreign", slug=f"g4c-foreign-{foreign_tenant.hex[:8]}"))
        await db.flush()
        # Foreign-tenant DeepWiki lookalike: same name, host and identity.
        db.add(Tool(tenant_id=foreign_tenant, name="mcp_foreign_wiki_question", display_name="ask_wiki_question",
            description=_production_description("ask_wiki_question"), type="mcp", category="custom",
            parameters_schema={"type": "object", "properties": {}}, config={},
            mcp_server_url=SERVER_URL, mcp_server_name=SERVER_NAME, mcp_tool_name="foreign_wiki_question",
            mcp_raw_description="remote", mcp_raw_schema={}, enabled=True, is_default=False,
            mcp_trust_status="approved", mcp_trust_tier="platform_generated_reviewed"))
        # Same-tenant approved+enabled tool with DeepWiki identity but NO
        # mcp_server_tools row and NO assignment (legacy non-default shape).
        orphan = Tool(tenant_id=tenant_id, name="mcp_deepwiki_orphan", display_name="orphan",
            description=_production_description("orphan"), type="mcp", category="custom",
            parameters_schema={"type": "object", "properties": {}}, config={},
            mcp_server_url=SERVER_URL, mcp_server_name=SERVER_NAME, mcp_tool_name="deepwiki_orphan",
            mcp_raw_description="remote", mcp_raw_schema={}, enabled=True, is_default=False,
            mcp_trust_status="approved", mcp_trust_tier="platform_generated_reviewed")
        db.add_all([orphan])
        await db.flush()
        from app.services.mcp_metadata_trust import current_mcp_fingerprint

        fingerprint = current_mcp_fingerprint(orphan)
        orphan.mcp_metadata_fingerprint = fingerprint
        orphan.mcp_reviewed_fingerprint = fingerprint
        await db.commit()

    discovered = await agent_tools.discoverable_tool_names_for_query(second_id, PRODUCTION_QUERY)
    assert set(discovered) == set(EXPECTED_TOOL_NAMES), discovered


async def test_calling_a_no_signin_tool_reaches_the_transport(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, scoped_runtime_factories, tmp_path,  # noqa: F811
):
    """G4c acceptance ("call one of them"): with a stubbed transport, calling a
    DeepWiki tool on the connected/none server sends the request with no
    credentials and returns the transport's answer — never an authorization
    complaint. A 401 with no member account and no company key tells the model
    the member can connect their own account (account-label limit)."""
    from app.services import agent_tools
    from app.tools.handlers.mcp import call_mcp_tool

    app, tenant_id, admin, second_id, server_id = await _seed_production_shape(
        owner_sessionmaker, app_user_sessionmaker)

    scoped_runtime_factories()

    async def _no_feishu_cli() -> bool:
        return False

    monkeypatch.setattr(agent_tools, "_agent_has_feishu_cli_access", _no_feishu_cli)

    async def public_dns(_host, _port):
        return ("93.184.216.34",)

    monkeypatch.setattr("app.services.governed_egress.resolve_public_addresses", public_dns)

    constructed = []

    class _StubClient:
        def __init__(self, url, *, api_key=None):
            constructed.append((url, api_key))

        async def call_tool(self, name, arguments):
            return f"stub answer for {name}: {arguments.get('repo_name')}"

    import app.services.mcp_client as mcp_client_module

    monkeypatch.setattr(mcp_client_module, "MCPClient", _StubClient)

    result = await call_mcp_tool(second_id, {
        "tool_name": "mcp_ask_wiki_question",
        "arguments": {"repo_name": "facebook/react"},
    })
    assert "stub answer for ask_wiki_question: facebook/react" in str(result), result
    assert "authorization" not in str(result).lower(), result
    # The connected/no-sign-in server answers without any credential.
    assert constructed and constructed[0][1] is None, constructed
    assert SERVER_HOST in constructed[0][0], constructed

    class _UnauthorizedClient:
        def __init__(self, url, *, api_key=None):
            pass

        async def call_tool(self, name, arguments):
            raise Exception("HTTP 401")

    monkeypatch.setattr(mcp_client_module, "MCPClient", _UnauthorizedClient)
    denied = str(await call_mcp_tool(second_id, {
        "tool_name": "mcp_ask_wiki_question",
        "arguments": {"repo_name": "facebook/react"},
    }))
    assert "auth_required" in denied, denied
    assert "The member can connect their account through the card in this conversation" in denied, denied
