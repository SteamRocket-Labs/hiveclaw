from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def all(self):
        return self._values


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.deleted = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    async def delete(self, value):
        self.deleted.append(value)


def test_tools_router_is_registered_in_app_surface():
    project_root = Path(__file__).resolve().parents[3]
    main_source = (project_root / "backend/app/main.py").read_text()
    tools_api_path = project_root / "backend/app/api/tools.py"

    assert "from app.api.tools import router as tools_router" in main_source
    assert "tools_router" in main_source
    assert tools_api_path.exists()


@pytest.mark.asyncio
async def test_create_tool_assigns_new_tool_to_current_tenant_agents():
    import app.api.tools as tools_api

    tenant_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tenant_id)
    agent_one = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    agent_two = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    db = _FakeDB([
        _ListResult([agent_one, agent_two]),
        _ScalarResult(None),
        _ScalarResult(None),
    ])

    payload = tools_api.ToolCreateIn(
        name="mcp_demo_tool",
        display_name="Demo Tool",
        description="Example",
        type="mcp",
        category="custom",
        icon="·",
        mcp_server_url="https://example.com/sse",
        mcp_server_name="Demo MCP",
        mcp_tool_name="demo",
        parameters_schema={"type": "object"},
        is_default=False,
    )

    result = await tools_api.create_tool(data=payload, current_user=current_user, db=db)

    assert result["name"] == "mcp_demo_tool"
    assert result["tenant_id"] == str(tenant_id)
    assert len(db.added) == 3
    agent_tool_rows = [item for item in db.added if getattr(item, "agent_id", None)]
    assert len(agent_tool_rows) == 2
    assert {row.agent_id for row in agent_tool_rows} == {agent_one.id, agent_two.id}
    assert db.committed is True


@pytest.mark.asyncio
async def test_remove_agent_tool_uses_agent_manage_access_not_admin_role():
    import app.api.tools as tools_api

    agent_tool_id = uuid4()
    tool_id = uuid4()
    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())
    agent_tool = SimpleNamespace(id=agent_tool_id, agent_id=agent_id, tool_id=tool_id)
    tool = SimpleNamespace(id=tool_id, type="mcp")
    db = _FakeDB([
        _ScalarResult(agent_tool),
        _ScalarResult(None),
        _ScalarResult(tool),
    ])

    async def fake_require_manage_access(db_session, user, target_agent_id):
        assert db_session is db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tools_api, "_require_manage_access", fake_require_manage_access)

    try:
        result = await tools_api.remove_agent_tool(
            agent_tool_id=agent_tool_id,
            current_user=current_user,
            db=db,
        )
    finally:
        monkeypatch.undo()

    assert result == {"status": "deleted", "agent_tool_id": str(agent_tool_id)}
    assert db.deleted == [agent_tool, tool]
    assert db.committed is True


@pytest.mark.asyncio
async def test_test_category_config_reports_feishu_cli_status(monkeypatch):
    import app.api.tools as tools_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())
    db = _FakeDB([])

    async def fake_require_manage_access(db_session, user, target_agent_id):
        assert db_session is db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id)

    async def fake_cli_available() -> bool:
        return True

    monkeypatch.setattr(tools_api, "_require_manage_access", fake_require_manage_access)
    monkeypatch.setattr("app.services.agent_tool_domains.feishu_cli._feishu_cli_available", fake_cli_available)
    monkeypatch.setattr("app.api.tools.get_settings", lambda: SimpleNamespace(FEISHU_CLI_ENABLED=True, FEISHU_CLI_BIN="lark-cli"))

    result = await tools_api.test_category_config(
        agent_id=agent_id,
        category="feishu",
        current_user=current_user,
        db=db,
    )

    assert result["ok"] is True
    assert result["cli_enabled"] is True
    assert result["cli_available"] is True
    assert result["cli_bin"] == "lark-cli"


@pytest.mark.asyncio
async def test_get_feishu_runtime_status_reports_global_cli(monkeypatch):
    import app.api.tools as tools_api

    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=uuid4())

    async def fake_cli_available() -> bool:
        return True

    monkeypatch.setattr("app.services.agent_tool_domains.feishu_cli._feishu_cli_available", fake_cli_available)
    monkeypatch.setattr("app.api.tools.get_settings", lambda: SimpleNamespace(FEISHU_CLI_ENABLED=True, FEISHU_CLI_BIN="lark-cli"))

    result = await tools_api.get_feishu_runtime_status(current_user=current_user)

    assert result["ok"] is True
    assert result["scope"] == "global"
    assert result["cli_enabled"] is True
    assert result["cli_available"] is True
    assert result["cli_bin"] == "lark-cli"
    assert result["base_tasks_ready"] is True


@pytest.mark.asyncio
async def test_get_agent_feishu_runtime_status_reports_agent_access(monkeypatch):
    import app.api.tools as tools_api

    agent_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4())
    db = _FakeDB([])

    async def fake_require_manage_access(db_session, user, target_agent_id):
        assert db_session is db
        assert user is current_user
        assert target_agent_id == agent_id
        return SimpleNamespace(id=agent_id)

    async def fake_agent_has_feishu(target_agent_id):
        assert target_agent_id == agent_id
        return True

    async def fake_agent_has_feishu_office_access(target_agent_id):
        assert target_agent_id == agent_id
        return True

    async def fake_agent_has_feishu_cli_access():
        return False

    monkeypatch.setattr(tools_api, "_require_manage_access", fake_require_manage_access)
    monkeypatch.setattr("app.api.tools.get_settings", lambda: SimpleNamespace(FEISHU_CLI_ENABLED=True, FEISHU_CLI_BIN="lark-cli"))
    monkeypatch.setattr("app.services.agent_tool_domains.feishu_cli._feishu_cli_available", fake_agent_has_feishu_cli_access)
    monkeypatch.setattr("app.services.agent_tools._agent_has_feishu", fake_agent_has_feishu)
    monkeypatch.setattr("app.services.agent_tools._agent_has_feishu_office_access", fake_agent_has_feishu_office_access)
    monkeypatch.setattr("app.services.agent_tools._agent_has_feishu_cli_access", fake_agent_has_feishu_cli_access)

    result = await tools_api.get_agent_feishu_runtime_status(
        agent_id=agent_id,
        current_user=current_user,
        db=db,
    )

    assert result["scope"] == "agent"
    assert result["channel_configured"] is True
    assert result["office_access"] is True
    assert result["cli_available"] is False
    assert result["base_tasks_ready"] is True  # Base/Tasks now use Open API, same as office_access


# ── MCP tool dedup tests ──────────────────────────────────────────────


def _make_tool(*, name, display_name, tool_type="mcp", tenant_id=None, mcp_server_name=None, mcp_tool_name=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        display_name=display_name,
        type=tool_type,
        category="mcp" if tool_type == "mcp" else "general",
        tenant_id=tenant_id,
        mcp_server_name=mcp_server_name,
        mcp_tool_name=mcp_tool_name,
    )


def test_list_tools_dedup_removes_mcp_duplicates_by_structural_identity():
    """MCP tools with same (mcp_server_name, mcp_tool_name) are deduped;
    tools from different servers with the same mcp_tool_name are kept."""
    tid = uuid4()
    # Two tools from same server, same tool — different internal name (different import path)
    t1 = _make_tool(
        name="mcp_smithery_twitter_LIKE",
        display_name="Twitter: LIKE",
        tenant_id=tid,
        mcp_server_name="Twitter",
        mcp_tool_name="LIKE",
    )
    t2 = _make_tool(
        name="mcp_composio_dev_LIKE",
        display_name="Twitter: LIKE",
        tenant_id=tid,
        mcp_server_name="Twitter",
        mcp_tool_name="LIKE",
    )
    # Tool from a DIFFERENT server with same mcp_tool_name — must NOT be deduped
    t3 = _make_tool(
        name="mcp_other_LIKE",
        display_name="Other: LIKE",
        tenant_id=tid,
        mcp_server_name="Other",
        mcp_tool_name="LIKE",
    )
    # Builtin tool — should never be deduped
    t4 = _make_tool(name="web_search", display_name="Web Search", tool_type="builtin", tenant_id=tid)

    tools = [t1, t2, t3, t4]

    # Apply the same dedup logic used in list_tools
    seen_mcp: set[tuple[str | None, str | None]] = set()
    deduped = []
    for tool in tools:
        if tool.type == "mcp":
            key = (tool.mcp_server_name, tool.mcp_tool_name) if tool.mcp_tool_name else (tool.display_name, None)
            if key in seen_mcp:
                continue
            seen_mcp.add(key)
        deduped.append(tool)

    # t2 is the duplicate; t1, t3, t4 survive
    assert len(deduped) == 3
    assert deduped[0] is t1
    assert deduped[1] is t3
    assert deduped[2] is t4


def test_list_tools_dedup_falls_back_to_display_name_when_mcp_tool_name_is_none():
    """Legacy MCP tools without mcp_tool_name fall back to display_name dedup."""
    tid = uuid4()
    t1 = _make_tool(name="mcp_old_a", display_name="OldServer", tenant_id=tid, mcp_server_name="Old", mcp_tool_name=None)
    t2 = _make_tool(name="mcp_old_b", display_name="OldServer", tenant_id=tid, mcp_server_name="Old", mcp_tool_name=None)

    seen_mcp: set[tuple[str | None, str | None]] = set()
    deduped = []
    for tool in [t1, t2]:
        if tool.type == "mcp":
            key = (tool.mcp_server_name, tool.mcp_tool_name) if tool.mcp_tool_name else (tool.display_name, None)
            if key in seen_mcp:
                continue
            seen_mcp.add(key)
        deduped.append(tool)

    assert len(deduped) == 1
    assert deduped[0] is t1


@pytest.mark.asyncio
async def test_dedup_mcp_endpoint_merges_duplicates_and_preserves_agent_links():
    """POST /tools/dedup-mcp merges duplicate Tool rows and re-points AgentTool."""
    import app.api.tools as tools_api

    tid = uuid4()
    keeper_id = uuid4()
    dup_id = uuid4()
    agent_id = uuid4()

    keeper = SimpleNamespace(
        id=keeper_id, display_name="Twitter: LIKE", type="mcp",
        mcp_server_name="Twitter", mcp_tool_name="LIKE",
        tenant_id=tid, created_at=None,
    )
    dup = SimpleNamespace(
        id=dup_id, display_name="Twitter: LIKE", type="mcp",
        mcp_server_name="Twitter", mcp_tool_name="LIKE",
        tenant_id=tid, created_at=None,
    )
    dup_agent_tool = SimpleNamespace(id=uuid4(), agent_id=agent_id, tool_id=dup_id)

    current_user = SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=tid)

    db = _FakeDB([
        # 1. select MCP tools for tenant (ordered by created_at asc)
        _ListResult([keeper, dup]),
        # 2. select AgentTool where tool_id == dup.id
        _ListResult([dup_agent_tool]),
        # 3. check if keeper already has link for this agent → no
        _ScalarResult(None),
    ])

    result = await tools_api.dedup_mcp_tools(tenant_id=str(tid), current_user=current_user, db=db)

    assert result["merged"] == 1
    # dup_agent_tool should be re-pointed to keeper
    assert dup_agent_tool.tool_id == keeper_id
    # dup Tool should be deleted
    assert dup in db.deleted
    assert db.committed is True
