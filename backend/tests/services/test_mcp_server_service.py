"""Part 4 — server-first MCP extension shell (mock-session, Hive unit-first style).

Validates that the shell reads/writes the new MCP tables, returns server-first
DTOs that carry NO ``pack`` / ``pack_name`` field, and upserts agent assignments
with the right fields. No live DB; the session is a queued spy mirroring the
pattern in test_mcp_backfill_service.py / test_skill_tenant_isolation.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.mcp_server import AgentMCPServerAssignment
from app.services import mcp_server_service


class _Result:
    def __init__(self, *, scalars=None, scalar=None, rows=None):
        self._scalars = scalars
        self._scalar = scalar
        self._rows = rows or []

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars or []))

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


class _SpyDB:
    def __init__(self, results):
        self._queue = list(results)
        self.added: list = []
        self.deleted: list = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._queue:
            raise AssertionError("Unexpected execute() call")
        return self._queue.pop(0)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


def _server(name="GitHub", key="github", status="connected", auth="configured", transport="sse"):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        server_key=key,
        status=status,
        auth_status=auth,
        transport=transport,
    )


_NO_PACK_KEYS = {"pack", "pack_name"}


@pytest.mark.asyncio
async def test_list_tenant_servers_dto_is_server_first_without_pack():
    tenant_id = uuid4()
    server = _server()
    agent_id = uuid4()
    db = _SpyDB(
        [
            _Result(scalars=[server]),  # MCPServer rows
            _Result(rows=[(server.id, 3)]),  # tool counts
            _Result(rows=[(server.id, True, agent_id, "Engineer")]),  # assignment join Agent
        ]
    )

    result = await mcp_server_service.list_tenant_servers(db, tenant_id)

    assert len(result) == 1
    dto = result[0]
    assert dto == {
        "id": str(server.id),
        "name": "GitHub",
        "server_key": "github",
        "status": "connected",
        "auth_status": "configured",
        "transport": "sse",
        "tool_count": 3,
        "agent_count": 1,
        "agents": [{"id": str(agent_id), "name": "Engineer", "enabled": True}],
    }
    assert not (_NO_PACK_KEYS & dto.keys())


@pytest.mark.asyncio
async def test_list_tenant_servers_empty_returns_empty():
    db = _SpyDB([_Result(scalars=[])])
    assert await mcp_server_service.list_tenant_servers(db, uuid4()) == []


@pytest.mark.asyncio
async def test_get_agent_mcp_servers_shape_and_tool_mode():
    agent_id = uuid4()
    server = _server(status="connected")
    row = SimpleNamespace(
        id=server.id,
        name="GitHub",
        status="connected",
        enabled=True,
        default_tool_mode="auto",
    )
    db = _SpyDB(
        [
            _Result(rows=[row]),  # server+assignment join
            _Result(rows=[(server.id, 18)]),  # tool counts
        ]
    )

    result = await mcp_server_service.get_agent_mcp_servers(db, agent_id)

    assert result == [
        {
            "id": str(server.id),
            "name": "GitHub",
            "status": "connected",
            "enabled": True,
            "tool_count": 18,
            "default_tool_mode": "auto",
        }
    ]
    assert not (_NO_PACK_KEYS & result[0].keys())


@pytest.mark.asyncio
async def test_get_agent_mcp_servers_empty_returns_empty():
    db = _SpyDB([_Result(rows=[])])
    assert await mcp_server_service.get_agent_mcp_servers(db, uuid4()) == []


@pytest.mark.asyncio
async def test_get_agent_extensions_has_both_keys(monkeypatch):
    agent_id = uuid4()

    async def fake_skills(_agent_id):
        return [{"id": "market-research", "name": "market-research", "source": "workspace", "status": "available"}]

    monkeypatch.setattr(mcp_server_service, "_list_agent_workspace_skills", fake_skills)
    db = _SpyDB([_Result(rows=[])])  # get_agent_mcp_servers → no assignments

    result = await mcp_server_service.get_agent_extensions(db, agent_id)

    assert set(result.keys()) == {"skills", "mcp_servers"}
    assert result["skills"][0]["source"] == "workspace"
    assert result["mcp_servers"] == []
    assert "pack_name" not in result


@pytest.mark.asyncio
async def test_set_agent_mcp_assignment_creates_row():
    tenant_id = uuid4()
    agent_id = uuid4()
    server_id = uuid4()
    db = _SpyDB([_Result(scalar=None)])  # no existing assignment → create

    result = await mcp_server_service.set_agent_mcp_assignment(
        db, tenant_id, agent_id, server_id, enabled=True, default_tool_mode="approval"
    )

    created = [o for o in db.added if isinstance(o, AgentMCPServerAssignment)]
    assert len(created) == 1
    assert created[0].tenant_id == tenant_id
    assert created[0].agent_id == agent_id
    assert created[0].mcp_server_id == server_id
    assert created[0].enabled is True
    assert created[0].default_tool_mode == "approval"
    assert db.committed is True
    assert result["enabled"] is True
    assert result["default_tool_mode"] == "approval"
    assert result["server_id"] == str(server_id)


@pytest.mark.asyncio
async def test_set_agent_mcp_assignment_updates_existing_row():
    tenant_id = uuid4()
    agent_id = uuid4()
    server_id = uuid4()
    existing = AgentMCPServerAssignment(
        tenant_id=tenant_id, agent_id=agent_id, mcp_server_id=server_id, enabled=True, default_tool_mode="auto"
    )
    existing.id = uuid4()
    db = _SpyDB([_Result(scalar=existing)])

    result = await mcp_server_service.set_agent_mcp_assignment(
        db, tenant_id, agent_id, server_id, enabled=False, default_tool_mode="deny"
    )

    assert db.added == []  # update path, no insert
    assert existing.enabled is False
    assert existing.default_tool_mode == "deny"
    assert db.committed is True
    assert result["enabled"] is False
    assert result["default_tool_mode"] == "deny"


@pytest.mark.asyncio
async def test_trigger_tenant_backfill_delegates(monkeypatch):
    tenant_id = uuid4()
    sentinel = {"tenant_id": str(tenant_id), "servers": 2}

    async def fake_backfill(_db, _tenant_id):
        assert _tenant_id == tenant_id
        return sentinel

    monkeypatch.setattr(mcp_server_service, "backfill_tenant_mcp_servers", fake_backfill)
    db = _SpyDB([])

    assert await mcp_server_service.trigger_tenant_backfill(db, tenant_id) == sentinel


# ── delete_tenant_server ───────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_tenant_server_cascades_records_and_orphan_tool():
    from app.models.tool import Tool

    tenant_id = uuid4()
    server_id = uuid4()
    tool_id = uuid4()
    server = _server()
    server.id = server_id
    leftover_tool = Tool(name="mcp_gh_issue", display_name="GH issue", type="mcp")
    leftover_tool.id = tool_id

    db = _SpyDB(
        [
            _Result(scalar=server),  # select MCPServer by id (tenant-scoped)
            _Result(rows=[(tool_id,)]),  # linked MCPServerTool.tool_id rows
            _Result(),  # delete AgentMCPToolOverride
            _Result(),  # delete AgentMCPServerAssignment
            _Result(),  # delete MCPServerTool
            _Result(),  # delete AgentTool (tenant agents)
            _Result(scalar=None),  # remaining AgentTool for tool_id → none → orphan
            _Result(scalar=leftover_tool),  # select Tool to delete
        ]
    )

    result = await mcp_server_service.delete_tenant_server(db, tenant_id, server_id)

    assert result == {"status": "deleted", "server_id": str(server_id)}
    # The MCPServer and the now-orphaned underlying Tool are both deleted.
    assert server in db.deleted
    assert leftover_tool in db.deleted
    assert db.committed is True


@pytest.mark.asyncio
async def test_delete_tenant_server_keeps_tool_with_remaining_agenttool():
    from app.models.tool import Tool

    tenant_id = uuid4()
    server_id = uuid4()
    tool_id = uuid4()
    server = _server()
    server.id = server_id

    db = _SpyDB(
        [
            _Result(scalar=server),  # select MCPServer
            _Result(rows=[(tool_id,)]),  # linked tool ids
            _Result(),  # delete overrides
            _Result(),  # delete assignments
            _Result(),  # delete server tools
            _Result(),  # delete tenant AgentTool rows
            _Result(scalar=SimpleNamespace(id=uuid4())),  # an AgentTool still references the tool → keep it
        ]
    )

    await mcp_server_service.delete_tenant_server(db, tenant_id, server_id)

    # Only the server is deleted; the shared Tool survives (no Tool select issued).
    assert server in db.deleted
    assert not any(isinstance(o, Tool) for o in db.deleted)


@pytest.mark.asyncio
async def test_delete_tenant_server_missing_raises_value_error():
    db = _SpyDB([_Result(scalar=None)])  # no MCPServer row
    with pytest.raises(ValueError, match="not found"):
        await mcp_server_service.delete_tenant_server(db, uuid4(), uuid4())


# ── register_imported_server (per-server, incremental, idempotent) ──


def _tool_state_row(tool_id, tool_name, agent_id, enabled, *, name="GitHub", url="https://gh", is_default=False):
    return SimpleNamespace(
        id=tool_id,
        mcp_tool_name=tool_name,
        display_name=f"GH {tool_name}",
        mcp_server_name=name,
        mcp_server_url=url,
        is_default=is_default,
        agent_id=agent_id,
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_register_imported_server_creates_records():
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer, MCPServerTool

    tenant_id = uuid4()
    agent_id = uuid4()
    t1 = uuid4()
    db = _SpyDB(
        [
            _Result(rows=[_tool_state_row(t1, "issue_search", agent_id, enabled=True)]),  # _read_tenant_mcp_tools_for_server
            _Result(scalar=None),  # select existing MCPServer by key → none → create
            _Result(rows=[]),  # taken server_keys
            _Result(rows=[]),  # existing MCPServerTool names
            _Result(rows=[]),  # existing assigned agents
        ]
    )

    result = await mcp_server_service.register_imported_server(db, tenant_id, "GitHub", "https://gh")

    servers = [o for o in db.added if isinstance(o, MCPServer)]
    tools = [o for o in db.added if isinstance(o, MCPServerTool)]
    assigns = [o for o in db.added if isinstance(o, AgentMCPServerAssignment)]
    assert len(servers) == 1
    assert servers[0].server_key == "github"
    assert servers[0].registry_source == "direct"
    assert len(tools) == 1
    assert len(assigns) == 1
    assert assigns[0].enabled is True
    assert db.committed is True
    assert result["server_key"] == "github"
    assert "pack_name" not in result


@pytest.mark.asyncio
async def test_register_imported_server_idempotent_on_rerun():
    from app.models.mcp_server import AgentMCPServerAssignment, MCPServer, MCPServerTool

    tenant_id = uuid4()
    agent_id = uuid4()
    t1 = uuid4()
    existing = MCPServer(
        tenant_id=tenant_id, name="GitHub", server_key="github", server_url="https://gh", status="connected"
    )
    existing.id = uuid4()

    db = _SpyDB(
        [
            _Result(rows=[_tool_state_row(t1, "issue_search", agent_id, enabled=True)]),  # read tools
            _Result(scalar=existing),  # select existing MCPServer by key → found, no create
            _Result(rows=[("issue_search",)]),  # MCPServerTool already linked
            _Result(rows=[(agent_id,)]),  # agent already assigned
        ]
    )

    result = await mcp_server_service.register_imported_server(db, tenant_id, "GitHub", "https://gh")

    # Nothing new written — tool + assignment already exist.
    assert not any(isinstance(o, MCPServer) for o in db.added)
    assert not any(isinstance(o, MCPServerTool) for o in db.added)
    assert not any(isinstance(o, AgentMCPServerAssignment) for o in db.added)
    assert result["id"] == str(existing.id)


@pytest.mark.asyncio
async def test_register_imported_server_no_tools_returns_none():
    db = _SpyDB([_Result(rows=[])])  # no Tool rows for this server
    assert await mcp_server_service.register_imported_server(db, uuid4(), "GitHub", "https://gh") is None
