"""Canonical MCP tool naming — single source for the ``mcp__server__tool`` prefix.

Step 6 of docs/cc-tooling-alignment-and-plugin-system.md. Before this module MCP
``Tool.name`` rows were generated ad-hoc as ``mcp_{server_id}_{tool}`` with
single underscores (ambiguous — you cannot reliably recover server vs tool from
the name) by several drifting call sites in ``resource_discovery.py``, while the
retired ``mcp_server:*`` pseudo-pack used yet another slug. This module is the ONE
place that builds and parses MCP tool names so the model, the DB row, governance,
and discovery all agree.

Design (CC ``mcp__server__tool`` parity, AI-Native L3 model-equality):

* Separator is ``__`` (double underscore). :func:`slugify` collapses every
  non-alphanumeric run to ``-`` and never emits ``_``, so ``__`` can never appear
  inside a slug — splitting on ``__`` is unambiguous.
* Charset is ``[a-z0-9_-]`` and length ``<= MAX_MCP_TOOL_NAME_LEN`` (64) so the
  name is a valid function name for every provider (OpenAI/Gemini cap at 64;
  Anthropic is wider) AND fits ``Tool.name`` ``String(100)``. The name is passed
  to providers verbatim (only parameter schemas are sanitized downstream), so it
  must already satisfy the strictest provider constraint.
* On length overflow the slugs are truncated and a deterministic short hash is
  appended so the result stays unique and stable across runs.
* :func:`build_mcp_tool_name` accepts an optional ``taken`` set so callers
  (discovery import, backfill) can guarantee the ``(tenant_id, name)`` unique
  constraint when two servers/tools slug-collide.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.services.mcp_backfill import slugify

MCP_TOOL_NAME_PREFIX = "mcp__"
MCP_TOOL_NAME_SEPARATOR = "__"
# Strictest common provider function-name length (OpenAI/Gemini = 64). Also fits
# Tool.name String(100). Keep canonical names within this so the name the model
# calls is byte-identical to the persisted Tool.name (execution lookup is exact).
MAX_MCP_TOOL_NAME_LEN = 64
_HASH_LEN = 6


def _short_hash(*parts: str) -> str:
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8).hexdigest()
    return digest[:_HASH_LEN]


def build_mcp_tool_name(server: str | None, tool: str | None, *, taken: set[str] | None = None) -> str:
    """Build the canonical ``mcp__{server}__{tool}`` name for an MCP tool.

    ``server`` is the MCP server display name (``Tool.mcp_server_name``), ``tool``
    is the remote tool name (``Tool.mcp_tool_name``). The result is deterministic
    for a given (server, tool) pair, capped at :data:`MAX_MCP_TOOL_NAME_LEN`, and —
    when ``taken`` is supplied — disambiguated with a numeric suffix so it is
    unique within that set.
    """
    server_slug = slugify(server)
    tool_slug = slugify(tool)
    name = f"{MCP_TOOL_NAME_PREFIX}{server_slug}{MCP_TOOL_NAME_SEPARATOR}{tool_slug}"

    if len(name) > MAX_MCP_TOOL_NAME_LEN:
        # Reserve room for the prefix, separator, and a hash that pins the full
        # (server, tool) identity even after truncation.
        suffix = "-" + _short_hash(server_slug, tool_slug)
        budget = MAX_MCP_TOOL_NAME_LEN - len(MCP_TOOL_NAME_PREFIX) - len(MCP_TOOL_NAME_SEPARATOR) - len(suffix)
        server_budget = max(1, budget // 2)
        tool_budget = max(1, budget - server_budget)
        server_part = server_slug[:server_budget].strip("-") or "s"
        tool_part = tool_slug[:tool_budget].strip("-") or "t"
        name = f"{MCP_TOOL_NAME_PREFIX}{server_part}{MCP_TOOL_NAME_SEPARATOR}{tool_part}{suffix}"

    if not taken:
        return name

    if name not in taken:
        return name
    # Slug collision (e.g. "GitHub" and "git.hub" -> "github"): append a stable
    # numeric suffix that still fits the length cap, mirroring derive_server_key.
    counter = 2
    base = name
    while True:
        suffix = f"-{counter}"
        candidate = base[: MAX_MCP_TOOL_NAME_LEN - len(suffix)].strip("-") + suffix
        if candidate not in taken:
            return candidate
        counter += 1


def is_mcp_tool_name(name: str | None) -> bool:
    """True if ``name`` is a canonical ``mcp__server__tool`` name."""
    if not name or not name.startswith(MCP_TOOL_NAME_PREFIX):
        return False
    return MCP_TOOL_NAME_SEPARATOR in name[len(MCP_TOOL_NAME_PREFIX) :]


def parse_mcp_tool_name(name: str | None) -> tuple[str, str] | None:
    """Reverse ``mcp__{server}__{tool}`` into ``(server_slug, tool_slug)``.

    Returns ``None`` for non-canonical names. The slugs are the slugified forms
    (lossy w.r.t. the original display names), suitable for server-wildcard
    matching and alias resolution, not for reconstructing the exact server name.
    A trailing collision/length suffix stays attached to the tool slug — callers
    that need the bare slug compare against :func:`build_mcp_tool_name` output.
    """
    if not is_mcp_tool_name(name):
        return None
    rest = name[len(MCP_TOOL_NAME_PREFIX) :]
    server_slug, _, tool_slug = rest.partition(MCP_TOOL_NAME_SEPARATOR)
    if not server_slug or not tool_slug:
        return None
    return server_slug, tool_slug


# ── Backfill planner (pure functional core) ───────────────────────────────────


@dataclass(frozen=True)
class McpNameRow:
    """One ``Tool(type="mcp")`` row's naming-relevant fields (tenant-scoped)."""

    tool_id: str
    name: str
    mcp_server_name: str | None
    mcp_tool_name: str | None
    tenant_id: str | None = None


@dataclass(frozen=True)
class McpNameRename:
    """A planned ``Tool.name`` rename to its canonical form."""

    tool_id: str
    tenant_id: str | None
    old_name: str
    new_name: str


def plan_mcp_name_canonicalization(rows: list[McpNameRow]) -> list[McpNameRename]:
    """Plan the canonical ``mcp__server__tool`` renames for existing MCP rows.

    Pure: no DB. Grouped by ``tenant_id`` (the ``(tenant_id, name)`` unique
    scope). Rows already at their plain canonical name keep it (reserved first so
    a legacy sibling cannot steal it); the remaining rows get collision-suffixed
    canonical names. Returns only rows whose name actually changes, ordered
    deterministically by ``tool_id`` so a dry-run and the apply agree.
    """
    by_tenant: dict[str | None, list[McpNameRow]] = {}
    for row in rows:
        by_tenant.setdefault(row.tenant_id, []).append(row)

    renames: list[McpNameRename] = []
    for tenant_id, trows in by_tenant.items():
        taken: set[str] = set()
        pending: list[McpNameRow] = []
        for row in sorted(trows, key=lambda r: r.tool_id):
            plain = build_mcp_tool_name(row.mcp_server_name, row.mcp_tool_name)
            if row.name == plain:
                taken.add(row.name)  # already canonical — reserve, no rename
            else:
                pending.append(row)
        for row in pending:
            new_name = build_mcp_tool_name(row.mcp_server_name, row.mcp_tool_name, taken=taken)
            taken.add(new_name)
            if new_name != row.name:
                renames.append(
                    McpNameRename(
                        tool_id=row.tool_id,
                        tenant_id=tenant_id,
                        old_name=row.name,
                        new_name=new_name,
                    )
                )
    return renames
