"""MCP backfill — functional core (pure, no DB).

Transforms the legacy ``Tool(type="mcp")`` + ``AgentTool`` rows into stable
MCP server records (``MCPServer`` / ``MCPServerTool`` / ``AgentMCPServerAssignment``
/ ``AgentMCPToolOverride``). Everything here is a pure function so the two
parity properties can be proven without a live database:

  * Registry parity — the new server grouping reproduces what
    ``mcp_registry_service.build_mcp_server_registry`` returns today (server
    name, url, tool set, agent set), differing only in the legacy
    pack-derived identity.
  * Tool-availability parity — for every agent, the set of MCP tools reachable
    *after* the migration (via assignment + overrides) equals the set reachable
    *before* it. The "before" reachability of one (agent, tool) is
    ``AgentTool.enabled if a row exists else tool.is_default`` — exactly the
    persisted half of ``get_agent_tools_for_llm`` (agent_tools.py:426).

``resolve_reachable_tools`` is the gating contract Part 5 reuses at runtime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def slugify(value: str | None) -> str:
    """Lowercase, dash-separated slug. Mirrors mcp_registry_service._slugify."""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return normalized or "server"


# ── Input rows (plain dicts, as read from DB by the shell) ─────


@dataclass(frozen=True)
class McpToolRow:
    """One ``Tool(type="mcp")`` row, tenant-scoped."""

    tool_id: str
    mcp_tool_name: str
    display_name: str
    mcp_server_name: str
    mcp_server_url: str
    is_default: bool = False


@dataclass(frozen=True)
class AgentToolState:
    """One agent's persisted access to one MCP tool, before migration."""

    agent_id: str
    tool_id: str
    has_row: bool  # an AgentTool row exists
    enabled: bool  # AgentTool.enabled (only meaningful when has_row)


# ── Output specs (what the shell will persist) ─────────────────


@dataclass(frozen=True)
class ServerSpec:
    server_key: str
    name: str
    server_url: str
    tool_ids: tuple[str, ...]
    tool_names: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentSpec:
    agent_id: str
    server_key: str
    enabled: bool
    deny_tool_names: tuple[str, ...] = field(default_factory=tuple)


def derive_server_key(name: str, server_url: str, taken: set[str]) -> str:
    """Stable, tenant-unique server_key. Never uses the ``mcp_server:*`` pack namespace.

    Collisions on the normalized name (same tenant, same name, different url)
    get a numeric suffix so the ``(tenant_id, server_key)`` unique constraint holds.
    """
    base = slugify(name or server_url)
    key = base
    counter = 2
    while key in taken:
        key = f"{base}-{counter}"
        counter += 1
    return key


def group_mcp_tools(rows: list[McpToolRow]) -> list[ServerSpec]:
    """Group MCP tool rows into one ServerSpec per (name, url), tenant-unique key.

    Grouping key matches ``build_mcp_server_registry``: ``(server_name, server_url)``.
    """
    grouped: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        name = row.mcp_server_name or "MCP Server"
        url = row.mcp_server_url or ""
        key = (name, url)
        if key not in grouped:
            grouped[key] = {"name": name, "url": url, "tool_ids": [], "tool_names": []}
            order.append(key)
        grouped[key]["tool_ids"].append(row.tool_id)
        grouped[key]["tool_names"].append(row.mcp_tool_name)

    taken: set[str] = set()
    specs: list[ServerSpec] = []
    # Deterministic order by server name, mirroring build_mcp_server_registry's sort.
    for key in sorted(order, key=lambda k: k[0].lower()):
        entry = grouped[key]
        server_key = derive_server_key(entry["name"], entry["url"], taken)
        taken.add(server_key)
        specs.append(
            ServerSpec(
                server_key=server_key,
                name=entry["name"],
                server_url=entry["url"],
                tool_ids=tuple(entry["tool_ids"]),
                tool_names=tuple(entry["tool_names"]),
            )
        )
    return specs


def reachable_before(state: AgentToolState | None, is_default: bool) -> bool:
    """Persisted pre-migration reachability of one (agent, tool).

    Mirrors agent_tools.py:426 (persisted half): AgentTool.enabled if a row
    exists, else the tool's is_default flag.
    """
    if state is not None and state.has_row:
        return state.enabled
    return is_default


def resolve_reachable_tools(server_tool_names: list[str], enabled: bool, deny_tool_names: set[str]) -> set[str]:
    """Gating contract (reused by Part 5). Reachable tools for one agent+server.

    Server disabled → nothing. Server enabled → all server tools minus per-tool
    deny overrides.
    """
    if not enabled:
        return set()
    return {name for name in server_tool_names if name not in deny_tool_names}


def plan_agent_assignment(
    server: ServerSpec,
    states_by_tool_id: dict[str, AgentToolState],
    is_default_by_tool_id: dict[str, bool],
    agent_id: str,
) -> AssignmentSpec | None:
    """Build one (agent, server) assignment that exactly reproduces pre-migration reachability.

    Returns None when the agent has no relationship to this server (no rows and
    nothing default-reachable) — no assignment is created in that case.
    """
    reachable: dict[str, bool] = {}
    any_known = False
    for tool_id, tool_name in zip(server.tool_ids, server.tool_names):
        state = states_by_tool_id.get(tool_id)
        if state is not None and state.has_row:
            any_known = True
        is_default = is_default_by_tool_id.get(tool_id, False)
        reachable[tool_name] = reachable_before(state, is_default)

    reachable_names = {name for name, ok in reachable.items() if ok}
    # No persisted relationship and nothing default-reachable → skip entirely.
    if not any_known and not reachable_names:
        return None

    enabled = bool(reachable_names)
    # When enabled, every server tool is reachable by default; deny the ones that
    # were NOT reachable before so the after-set equals the before-set exactly.
    deny = tuple(name for name, ok in reachable.items() if not ok) if enabled else ()
    return AssignmentSpec(agent_id=agent_id, server_key=server.server_key, enabled=enabled, deny_tool_names=deny)
