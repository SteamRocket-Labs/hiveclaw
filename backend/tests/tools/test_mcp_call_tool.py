"""P1-W3-4 — call_mcp_tool actually invokes the remote MCP server.

Before this handler the agent could only read MCP tool *metadata* from
the database — there was no path to actually run a tool against the
remote server. These tests pin the round trip:

  - missing tool_name returns a clear `bad_arguments` error envelope
  - non-imported tool name returns a `not_found` envelope
  - disabled tool returns a `forbidden` envelope
  - happy path constructs an MCPClient with the row's url + api_key
    and forwards remote_name + arguments
  - failures from the client surface as `operation_failed` envelopes

The MCPClient itself is monkeypatched so the test stays hermetic — we
only care that this handler wires DB lookup → client → result correctly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.agent_tool_domains.web_mcp import _execute_mcp_tool
from app.tools.handlers.mcp import call_mcp_tool, read_mcp_resource


# ── DB fake ───────────────────────────────────────────────────


class _FakeQueryResult:
    def __init__(self, row=None, *, scalars=None, rows=None):
        self._row = row
        self._scalars = scalars
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars or []), first=lambda: (self._scalars or [None])[0])

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, row_or_results):
        if isinstance(row_or_results, list):
            self._results = list(row_or_results)
        else:
            self._results = [_FakeQueryResult(row_or_results)]
        self.executed_statements: list = []

    async def execute(self, stmt):
        # RLS 阶段1: tenant_scoped_session issues `SET LOCAL app.current_tenant_id`
        # before the business query. Don't let the GUC statement consume a
        # pre-loaded result or pollute executed_statements assertions.
        if "app.current_tenant_id" in str(stmt):
            return _FakeQueryResult(None, scalars=[])
        self.executed_statements.append(stmt)
        if self._results:
            result = self._results.pop(0)
            return result if isinstance(result, _FakeQueryResult) else _FakeQueryResult(result)
        return _FakeQueryResult(None, scalars=[])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _compiled_sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": False})).lower()


@pytest.fixture
def install_fake_session(monkeypatch):
    """Install an async_session() factory that yields a fake DB pre-loaded
    with the given row (or None for "not found")."""

    def _install(row):
        session = _FakeSession(row)
        monkeypatch.setattr("app.database.async_session", lambda: session)
        monkeypatch.setattr("app.services.agent_tool_domains.web_mcp.async_session", lambda: session)

        # RLS 阶段1: the migrated mcp handlers resolve tenant then open a
        # tenant-scoped session. Route both through the same fake DB so the
        # business-query result sequence is unchanged.
        import contextlib

        @contextlib.asynccontextmanager
        async def _fake_tenant_scoped_session(*_a, **_k):
            yield session

        async def _fake_resolve_tenant_for_agent(*_a, **_k):
            return uuid.uuid4()

        monkeypatch.setattr("app.database.tenant_scoped_session", _fake_tenant_scoped_session)
        monkeypatch.setattr(
            "app.services.tenant_resolver.resolve_tenant_for_agent",
            _fake_resolve_tenant_for_agent,
        )
        # web_mcp imports both symbols at module level, so the source-module
        # patches above don't rebind its already-imported names — patch them
        # on the web_mcp module directly. (mcp.py imports them inside the
        # functions, so it picks up the source-module patches.)
        monkeypatch.setattr(
            "app.services.agent_tool_domains.web_mcp.tenant_scoped_session",
            _fake_tenant_scoped_session,
        )
        monkeypatch.setattr(
            "app.services.agent_tool_domains.web_mcp.resolve_tenant_for_agent",
            _fake_resolve_tenant_for_agent,
        )
        return session

    return _install


# ── MCPClient fake ───────────────────────────────────────────


class _SpyClient:
    instances: list["_SpyClient"] = []

    def __init__(self, server_url: str, api_key: str | None = None):
        self.server_url = server_url
        self.api_key = api_key
        self.call_args: tuple[str, dict] | None = None
        self.raise_on_call: Exception | None = None
        type(self).instances.append(self)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        self.call_args = (tool_name, arguments)
        if self.raise_on_call:
            raise self.raise_on_call
        return f"OK from {tool_name} with {arguments}"


@pytest.fixture(autouse=True)
def reset_spy_clients():
    _SpyClient.instances.clear()
    yield
    _SpyClient.instances.clear()


@pytest.fixture
def patch_mcp_client(monkeypatch):
    monkeypatch.setattr("app.services.mcp_client.MCPClient", _SpyClient)


# ── Validation paths ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_tool_name_returns_bad_arguments_error():
    out = await call_mcp_tool(uuid.uuid4(), {})
    assert "bad_arguments" in out or "tool_name is required" in out


@pytest.mark.asyncio
async def test_non_dict_arguments_returns_bad_arguments_error(install_fake_session):
    install_fake_session(None)
    out = await call_mcp_tool(uuid.uuid4(), {"tool_name": "x", "arguments": "not a dict"})
    assert "bad_arguments" in out or "must be an object" in out


@pytest.mark.asyncio
async def test_unknown_tool_returns_not_found(install_fake_session):
    install_fake_session(None)
    out = await call_mcp_tool(uuid.uuid4(), {"tool_name": "nope"})
    assert "not_found" in out or "is not imported" in out


@pytest.mark.asyncio
async def test_disabled_tool_returns_forbidden(install_fake_session):
    row = SimpleNamespace(
        name="weather",
        enabled=False,
        mcp_server_url="https://mcp.example.com",
        mcp_tool_name="get_weather",
        config={},
    )
    install_fake_session(row)
    out = await call_mcp_tool(uuid.uuid4(), {"tool_name": "weather"})
    assert "forbidden" in out or "disabled" in out


@pytest.mark.asyncio
async def test_missing_server_url_returns_bad_state(install_fake_session):
    row = SimpleNamespace(
        name="weather",
        enabled=True,
        mcp_server_url=None,
        mcp_tool_name="get_weather",
        config={},
    )
    install_fake_session(row)
    out = await call_mcp_tool(uuid.uuid4(), {"tool_name": "weather"})
    assert "bad_state" in out or "no server URL" in out


@pytest.mark.asyncio
async def test_call_mcp_tool_lookup_is_scoped_to_enabled_agent_assignment(install_fake_session, patch_mcp_client):
    row = SimpleNamespace(
        name="weather",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_tool_name="get_weather",
        config={},
    )
    session = install_fake_session(row)
    agent_id = uuid.uuid4()

    await call_mcp_tool(agent_id, {"tool_name": "weather"})

    sql = _compiled_sql(session.executed_statements[0])
    assert "join agent_tools" in sql
    assert "agent_tools.agent_id" in sql
    assert "agent_tools.enabled" in sql


@pytest.mark.asyncio
async def test_call_mcp_tool_refuses_disabled_server_assignment(install_fake_session, patch_mcp_client):
    agent_id = uuid.uuid4()
    server_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    row = SimpleNamespace(
        id=tool_id,
        type="mcp",
        name="weather",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_tool_name="get_weather",
        config={},
    )
    install_fake_session(
        [
            row,
            _FakeQueryResult(scalars=[SimpleNamespace(mcp_server_id=server_id, mcp_tool_name="get_weather")]),
            _FakeQueryResult(SimpleNamespace(enabled=False, default_tool_mode="auto")),
        ]
    )

    out = await call_mcp_tool(agent_id, {"tool_name": "weather"})

    assert "forbidden" in out or "disabled" in out or "denied" in out
    assert _SpyClient.instances == []


@pytest.mark.asyncio
async def test_read_mcp_resource_lookup_is_scoped_to_enabled_agent_assignment(install_fake_session):
    row = SimpleNamespace(
        name="weather",
        display_name="Weather",
        description="Weather lookup",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_server_name="weather-server",
        mcp_tool_name="get_weather",
        parameters_schema={"type": "object"},
    )
    session = install_fake_session(row)
    agent_id = uuid.uuid4()

    out = await read_mcp_resource(agent_id, {"tool_name": "weather"})

    sql = _compiled_sql(session.executed_statements[0])
    assert "## MCP Tool: weather" in out
    assert "join agent_tools" in sql
    assert "agent_tools.agent_id" in sql
    assert "agent_tools.enabled" in sql


@pytest.mark.asyncio
async def test_execute_mcp_tool_fallback_refuses_deny_override(install_fake_session, patch_mcp_client):
    agent_id = uuid.uuid4()
    server_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    row = SimpleNamespace(
        id=tool_id,
        type="mcp",
        name="weather",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_server_name="weather-server",
        mcp_tool_name="get_weather",
        config={},
    )
    install_fake_session(
        [
            row,
            _FakeQueryResult(SimpleNamespace(config={})),  # legacy AgentTool config lookup
            _FakeQueryResult(scalars=[SimpleNamespace(mcp_server_id=server_id, mcp_tool_name="get_weather")]),
            _FakeQueryResult(SimpleNamespace(enabled=True, default_tool_mode="auto")),
            _FakeQueryResult(SimpleNamespace(mode="deny")),
        ]
    )

    out = await _execute_mcp_tool("weather", {}, agent_id=agent_id)

    assert "denied" in out or "forbidden" in out
    assert _SpyClient.instances == []


# ── Happy path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_forwards_to_mcp_client(install_fake_session, patch_mcp_client):
    row = SimpleNamespace(
        name="weather",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_tool_name="get_weather",
        config={"api_key": "secret-key"},
    )
    install_fake_session(row)

    out = await call_mcp_tool(uuid.uuid4(), {"tool_name": "weather", "arguments": {"city": "NYC"}})

    assert len(_SpyClient.instances) == 1
    client = _SpyClient.instances[0]
    assert client.server_url == "https://mcp.example.com"
    assert client.api_key == "secret-key"
    assert client.call_args == ("get_weather", {"city": "NYC"})
    assert "OK from get_weather" in out


@pytest.mark.asyncio
async def test_falls_back_to_hive_name_when_mcp_tool_name_unset(install_fake_session, patch_mcp_client):
    """Some imports populate `name` but leave `mcp_tool_name` unset; the
    Hive-side name doubles as the remote name in that case."""
    row = SimpleNamespace(
        name="get_data",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_tool_name=None,
        config={},
    )
    install_fake_session(row)

    await call_mcp_tool(uuid.uuid4(), {"tool_name": "get_data"})

    assert _SpyClient.instances[0].call_args == ("get_data", {})


@pytest.mark.asyncio
async def test_client_failure_surfaces_as_operation_failed(install_fake_session, patch_mcp_client):
    row = SimpleNamespace(
        name="weather",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_tool_name="get_weather",
        config={},
    )
    install_fake_session(row)

    # Patch the spy class so the next instance raises on call.
    original_init = _SpyClient.__init__

    def init_with_failure(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.raise_on_call = ConnectionError("upstream down")

    _SpyClient.__init__ = init_with_failure  # type: ignore[method-assign]
    try:
        out = await call_mcp_tool(uuid.uuid4(), {"tool_name": "weather"})
    finally:
        _SpyClient.__init__ = original_init  # type: ignore[method-assign]

    assert "operation_failed" in out or "MCP call failed" in out


# ── Capability mapping ───────────────────────────────────────


def test_call_mcp_tool_is_in_capability_map() -> None:
    """Sanity: the new handler must have a capability entry so the
    capability gate doesn't drop it into the unmapped bucket."""
    from app.services.capability_gate import CAPABILITY_MAP

    assert CAPABILITY_MAP.get("call_mcp_tool") == "agent.mcp.call"


# ── Closure A2: approval gates execution, not discovery ──────────────


@pytest.mark.asyncio
async def test_read_mcp_resource_approval_mode_annotates_not_blocks(install_fake_session, monkeypatch):
    """Metadata semantics: the schema stays readable under approval mode but
    carries an explicit notice — visibility is not executability."""
    row = SimpleNamespace(
        name="weather",
        display_name="Weather",
        description="Weather lookup",
        enabled=True,
        mcp_server_url="https://mcp.example.com",
        mcp_server_name="weather-server",
        mcp_tool_name="get_weather",
        parameters_schema={"type": "object"},
    )
    install_fake_session(row)

    async def fake_mode(db, aid, tool):
        return "approval"

    monkeypatch.setattr("app.services.mcp_server_service.resolve_agent_mcp_tool_mode", fake_mode)

    out = await read_mcp_resource(uuid.uuid4(), {"tool_name": "weather"})

    assert "## MCP Tool: weather" in out  # still readable
    assert "requires approval" in out  # explicitly annotated


@pytest.mark.asyncio
async def test_list_mcp_resources_marks_approval_tools(install_fake_session, monkeypatch):
    from app.tools.handlers.mcp import list_mcp_resources

    rows = [
        SimpleNamespace(
            name="weather",
            display_name="Weather",
            description="Weather lookup",
            mcp_server_name="weather-server",
            mcp_server_url="https://mcp.example.com",
        )
    ]
    install_fake_session([_FakeQueryResult(scalars=rows)])

    async def fake_mode(db, aid, tool):
        return "approval"

    monkeypatch.setattr("app.services.mcp_server_service.resolve_agent_mcp_tool_mode", fake_mode)

    out = await list_mcp_resources(uuid.uuid4(), {})

    assert "weather" in out  # approval tools stay discoverable
    assert "[approval required]" in out  # but the cost is visible up front
