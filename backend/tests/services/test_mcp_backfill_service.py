"""Part 2b — MCP backfill DB shell (mock-session, Hive unit-first style).

Validates that the shell reads legacy rows, delegates to the functional core,
and writes the right records — including the parity-preserving deny override
for a tool that was disabled before the migration. No live DB; the session is a
queued spy mirroring the pattern in test_skill_tenant_isolation.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.mcp_server import (
    AgentMCPServerAssignment,
    AgentMCPToolOverride,
    MCPServer,
    MCPServerTool,
)
from app.services.mcp_backfill_service import backfill_tenant_mcp_servers

_SENTINEL = object()


class _Result:
    def __init__(self, scalar=_SENTINEL, rows=None, scalars=None):
        self._scalar = scalar
        self._rows = rows or []
        self._scalars = scalars

    def scalar_one_or_none(self):
        return None if self._scalar is _SENTINEL else self._scalar

    def all(self):
        return self._rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars or []))


class _BackfillDB:
    def __init__(self, results):
        self._queue = list(results)
        self.added: list = []
        self.committed = False

    async def execute(self, _stmt):
        return self._queue.pop(0)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _row(tool_id, tool_name, agent_id, enabled, *, name="GitHub", url="https://gh", is_default=False):
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


def _existing_server(server_key, *, name="GitHub", url="https://gh"):
    server = MCPServer(tenant_id=uuid4(), name=name, server_key=server_key, server_url=url)
    server.id = uuid4()
    return server


@pytest.mark.asyncio
async def test_backfill_writes_records_with_parity_overrides():
    tenant_id = uuid4()
    agent_id = uuid4()
    t1, t2 = uuid4(), uuid4()
    rows = [
        _row(t1, "issue_search", agent_id, enabled=True),
        _row(t2, "repo_read", agent_id, enabled=False),  # disabled before → must be denied after
    ]
    db = _BackfillDB([_Result(scalar=None), _Result(rows=rows)])

    summary = await backfill_tenant_mcp_servers(db, tenant_id)

    servers = [o for o in db.added if isinstance(o, MCPServer)]
    tools = [o for o in db.added if isinstance(o, MCPServerTool)]
    assigns = [o for o in db.added if isinstance(o, AgentMCPServerAssignment)]
    overrides = [o for o in db.added if isinstance(o, AgentMCPToolOverride)]

    assert len(servers) == 1
    assert servers[0].server_key == "github"
    assert servers[0].registry_source == "migration"
    assert servers[0].tenant_id == tenant_id
    assert len(tools) == 2
    assert len(assigns) == 1
    assert assigns[0].enabled is True
    assert len(overrides) == 1
    assert overrides[0].tool_name == "repo_read"
    assert overrides[0].mode == "deny"
    assert db.committed is True
    assert summary["servers"] == 1
    assert summary["overrides"] == 1


@pytest.mark.asyncio
async def test_backfill_skips_already_backfilled_tenant():
    tenant_id = uuid4()
    agent_id = uuid4()
    existing = _existing_server("github", name="GitHub", url="https://gh")
    db = _BackfillDB(
        [
            _Result(scalars=[existing]),
            _Result(rows=[_row(uuid4(), "issue_search", agent_id, enabled=True, name="GitHub", url="https://gh")]),
        ]
    )
    summary = await backfill_tenant_mcp_servers(db, tenant_id)
    assert summary["servers"] == 0
    assert summary["tools"] == 0
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_backfill_is_incremental_when_some_servers_already_exist():
    tenant_id = uuid4()
    agent_id = uuid4()
    existing = _existing_server("github", name="GitHub", url="https://gh")
    t_existing, t_new = uuid4(), uuid4()
    rows = [
        _row(t_existing, "issue_search", agent_id, enabled=True, name="GitHub", url="https://gh"),
        _row(t_new, "ticket_search", agent_id, enabled=True, name="Linear", url="https://linear"),
    ]
    db = _BackfillDB(
        [
            _Result(scalars=[existing]),  # existing MCPServer rows for this tenant
            _Result(rows=rows),
        ]
    )

    summary = await backfill_tenant_mcp_servers(db, tenant_id)

    servers = [o for o in db.added if isinstance(o, MCPServer)]
    tools = [o for o in db.added if isinstance(o, MCPServerTool)]
    assert len(servers) == 1
    assert servers[0].server_key == "linear"
    assert len(tools) == 1
    assert tools[0].mcp_tool_name == "ticket_search"
    assert summary["servers"] == 1
    assert db.committed is True


@pytest.mark.asyncio
async def test_backfill_empty_tenant_writes_nothing():
    db = _BackfillDB([_Result(scalars=[]), _Result(rows=[])])
    summary = await backfill_tenant_mcp_servers(db, uuid4())
    assert summary["servers"] == 0
    assert db.added == []
