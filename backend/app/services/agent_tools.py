"""Agent tools — unified file-based tools that give digital employees
access to their own structured workspace.

Design principle: ONE set of file tools covers EVERYTHING.
The agent's workspace uses well-known paths:
  - tasks.json          → task list (auto-synced from DB)
  - soul.md             → personality definition
  - governed memory/** → layered long-term memory and memory sidecars; write only via memory tools
  - skills/<slug>/SKILL.md → skill definitions and auxiliary resources
  - runtime_artifacts/  → platform-managed recovery/audit artifacts
  - workspace/          → general working files, reports, etc.

The agent reads/writes these files directly. No per-concept tools needed.
"""

import logging
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from sqlalchemy import or_, select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.config import get_settings
from app.services.channel_delivery_service import ChannelDeliveryService, channel_delivery_target
from app.services.governance_capability_taxonomy import (
    CORE_TOOL_NAMES,
    iter_runtime_l2_capabilities,
    iter_runtime_l2_capabilities_for_query,
)
from app.services.pack_policy_service import get_agent_pack_policies, is_pack_enabled, policy_pack_names_for_tool
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.tool_visibility import HR_ONLY_TOOL_NAMES, is_hr_agent, is_tool_allowed_for_agent
from app.tools import (
    ToolExecutionRegistry,
    ToolGovernanceResolver,
    ToolRegistry,
    ToolRuntimeService,
    run_tool_governance,
)
from app.tools.result_envelope import ToolContentEnvelope
from app.tools.runtime_tool_groups import normalize_tool_query

logger = logging.getLogger(__name__)

AGENT_TEAM_DEFERRED_TOOL_NAMES: frozenset[str] = frozenset({"team_create"})

_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.AGENT_DATA_DIR)

# ContextVar set by each channel handler so send_channel_file knows where to send
# Value: async callable(file_path: Path) -> None  |  None for web chat (returns URL)
channel_file_sender: ContextVar = ContextVar("channel_file_sender", default=None)
# For web chat: agent_id needed to build download URL
channel_web_agent_id: ContextVar = ContextVar("channel_web_agent_id", default=None)
# Set by Feishu channel handler — open_id of the message sender so calendar tool
# can auto-invite them as attendee when no explicit attendee list is given
channel_feishu_sender_open_id: ContextVar = ContextVar("channel_feishu_sender_open_id", default=None)
ToolEventCallback = Callable[[dict], Awaitable[None] | None]

_TOOL_EXECUTION_REGISTRY = ToolExecutionRegistry()
_TOOL_EXECUTION_REGISTRY_INITIALIZED = False
_TOOL_RUNTIME_SERVICE: ToolRuntimeService | None = None
_COLLECTED_TOOLS = None  # Lazy-initialized by _ensure_tool_execution_registry
_REGISTRY_LOCK = threading.Lock()  # M-09: protect concurrent registry init


def _is_relative_workspace_path(raw_path: str) -> tuple[PurePosixPath | None, str | None]:
    cleaned = str(raw_path or "").strip().replace("\\", "/")
    if not cleaned:
        return None, "❌ file_path is required"
    rel = PurePosixPath(cleaned)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None, "❌ Access denied for this path."
    parts = tuple(part for part in rel.parts if part not in ("", "."))
    if not parts:
        return None, "❌ file_path is required"
    return PurePosixPath(*parts), None


def _channel_file_candidate_paths(ws: Path, rel: PurePosixPath) -> list[Path]:
    parts = rel.parts
    candidates: list[Path] = [ws.joinpath(*parts)]

    if parts[0] != "workspace":
        # Common sandbox output shape: execute_code runs with cwd=workspace/,
        # so writing "report.xlsx" lands at agent_root/workspace/report.xlsx.
        candidates.append(ws / "workspace" / Path(*parts))
    else:
        tail = Path(*parts[1:]) if len(parts) > 1 else None
        if tail is not None:
            # Legacy bad shape: code wrote "workspace/report.xlsx" while cwd was
            # already workspace/, producing workspace/workspace/report.xlsx.
            candidates.append(ws / "workspace" / "workspace" / tail)
        if len(parts) > 1 and parts[1] == "workspace":
            collapsed_tail = Path(*parts[2:]) if len(parts) > 2 else None
            if collapsed_tail is not None:
                candidates.append(ws / "workspace" / collapsed_tail)

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _resolve_channel_file_path(ws: Path, rel_path: str) -> tuple[Path | None, str | None]:
    rel, error = _is_relative_workspace_path(rel_path)
    if error:
        return None, error
    assert rel is not None

    ws_resolved = ws.resolve()
    for candidate in _channel_file_candidate_paths(ws_resolved, rel):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(ws_resolved)
        except ValueError:
            return None, "❌ Access denied for this path."
        if resolved.exists() and resolved.is_file():
            return resolved, None
    return None, f"❌ File not found: {rel_path}"


def _get_collected_tools():
    """Lazy-load collected tools from @tool-decorated handlers."""
    global _COLLECTED_TOOLS
    if _COLLECTED_TOOLS is None:
        from app.tools.collector import collect_tools

        _COLLECTED_TOOLS = collect_tools()
    return _COLLECTED_TOOLS


def get_combined_openai_tools() -> list[dict]:
    """Return the canonical OpenAI tool surface collected from decorators."""
    collected = _get_collected_tools()
    return collected.openai_tools


def _ensure_tool_execution_registry() -> None:
    global _TOOL_EXECUTION_REGISTRY_INITIALIZED
    if _TOOL_EXECUTION_REGISTRY_INITIALIZED:
        return

    with _REGISTRY_LOCK:
        if _TOOL_EXECUTION_REGISTRY_INITIALIZED:
            return

        # Register @tool-decorated handlers (from tools/handlers/)
        collected = _get_collected_tools()
        for name, executor in collected.exec_registry._executors.items():
            _TOOL_EXECUTION_REGISTRY.register(name, executor)

        _TOOL_EXECUTION_REGISTRY_INITIALIZED = True


def _get_tool_runtime_service() -> ToolRuntimeService:
    global _TOOL_RUNTIME_SERVICE
    if _TOOL_RUNTIME_SERVICE is not None:
        return _TOOL_RUNTIME_SERVICE

    from app.tools.resolver import ToolRuntimeResolver

    async def _fallback_execute(tool_name: str, arguments: dict, context) -> str:
        if tool_name.startswith("custom_api__"):
            from app.services.custom_api_connectors import execute_custom_api_tool

            return await execute_custom_api_tool(tool_name, arguments, agent_id=context.agent_id)
        return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)

    async def _direct_fallback_execute(tool_name: str, arguments: dict, context) -> str:
        # Approved/direct fallback is not a second implementation of first-class tools.
        # Known tools must be registered in ToolExecutionRegistry; only unknown tools
        # may fall through to MCP passthrough.
        if tool_name.startswith("custom_api__"):
            from app.services.custom_api_connectors import execute_custom_api_tool

            return await execute_custom_api_tool(tool_name, arguments, agent_id=context.agent_id)
        return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)

    async def _log_activity(*args, **kwargs) -> None:
        from app.services.activity_logger import log_activity

        await log_activity(*args, **kwargs)

    _TOOL_RUNTIME_SERVICE = ToolRuntimeService(
        runtime_resolver=ToolRuntimeResolver(),
        governance_resolver=ToolGovernanceResolver(),
        registry=_TOOL_EXECUTION_REGISTRY,
        ensure_registry=_ensure_tool_execution_registry,
        governance_runner=run_tool_governance,
        fallback_executor=_fallback_execute,
        direct_fallback_executor=_direct_fallback_execute,
        activity_logger=_log_activity,
    )
    return _TOOL_RUNTIME_SERVICE


# Core tools that should always be available to agents regardless of
# DB configuration.
_ALWAYS_INCLUDE_CORE = set(CORE_TOOL_NAMES)
# Feishu tools split into:
# - channel tools: require a configured Feishu channel
# - office read tools: may also run via optional lark-cli auth in cloud environments
_HR_TOOL_NAMES = {
    "create_digital_employee",
    "preview_agent_blueprint",
    "discover_resources",
    "search_clawhub",
    "web_search",
    "exa_search",
    "tavily_search",
    "firecrawl_fetch",
    "xcrawl_scrape",
    "execute_code",
}

_FEISHU_TOOL_NAMES = {
    "send_feishu_message",
    "feishu_user_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_url_resolve",
    "feishu_url_read",
    "feishu_drive_file_read",
    "feishu_doc_delete",
    "feishu_sheet_info",
    "feishu_sheet_read",
    "feishu_base_app_create",
    "feishu_base_field_list",
    "feishu_base_field_create",
    "feishu_base_table_list",
    "feishu_base_record_list",
    "feishu_base_record_delete",
    "feishu_base_record_upload_attachment",
    "feishu_base_record_upsert",
    "feishu_approval_create",
    "feishu_approval_definition",
    "feishu_approval_query",
    "feishu_approval_get",
    "feishu_task_comment",
    "feishu_task_complete",
    "feishu_task_create",
    "feishu_task_list",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_doc_share",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
}
_FEISHU_OFFICE_TOOL_NAMES = {
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_url_resolve",
    "feishu_url_read",
    "feishu_drive_file_read",
    "feishu_base_app_create",
    "feishu_sheet_info",
    "feishu_sheet_read",
    "feishu_base_field_list",
    "feishu_base_field_create",
    "feishu_base_table_list",
    "feishu_base_record_list",
    "feishu_base_record_delete",
    "feishu_base_record_upload_attachment",
    "feishu_base_record_upsert",
    "feishu_task_comment",
    "feishu_task_complete",
    "feishu_task_create",
    "feishu_task_list",
}
_FEISHU_CLI_ONLY_TOOL_NAMES: set[str] = set()
_always_core_tools: list[dict] | None = None
_feishu_tools: list[dict] | None = None
_hr_tools: list[dict] | None = None


async def _provider_available_tools(agent_id: uuid.UUID | None = None) -> set[str]:
    """Return provider-backed tools that are actually configured."""
    available: set[str] = set()

    exa_key = await _get_exa_api_key()
    tavily_key = await _get_tavily_api_key()
    firecrawl_key = await _get_firecrawl_api_key()
    xcrawl_key = await _get_xcrawl_api_key()
    if exa_key:
        available.add("exa_search")
    if tavily_key:
        available.add("tavily_search")
    if firecrawl_key:
        available.add("firecrawl_fetch")
    if xcrawl_key:
        available.add("xcrawl_scrape")

    try:
        from app.services.resource_discovery import _get_modelscope_api_token, _get_smithery_api_key

        smithery_key = await _get_smithery_api_key(agent_id)
        modelscope_token = await _get_modelscope_api_token()
        if smithery_key or modelscope_token:
            available |= {"discover_resources", "import_mcp_server"}
    except Exception:
        logger.debug("[Tools] Provider availability lookup failed", exc_info=True)

    return available


async def _filter_unavailable_tools(agent_id: uuid.UUID, tools: list[dict]) -> list[dict]:
    """Hide externally-backed tools that are not configured in production."""
    provider_backed = {
        "exa_search",
        "tavily_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "discover_resources",
        "import_mcp_server",
    }
    available = await _provider_available_tools(agent_id)
    return [
        tool
        for tool in tools
        if tool["function"]["name"] not in provider_backed or tool["function"]["name"] in available
    ]


def _get_always_core_tools() -> list[dict]:
    global _always_core_tools
    if _always_core_tools is None:
        all_tools = get_combined_openai_tools()
        _always_core_tools = [t for t in all_tools if t["function"]["name"] in _ALWAYS_INCLUDE_CORE]
    return _always_core_tools


def _get_feishu_tools() -> list[dict]:
    global _feishu_tools
    if _feishu_tools is None:
        all_tools = get_combined_openai_tools()
        _feishu_tools = [t for t in all_tools if t["function"]["name"] in _FEISHU_TOOL_NAMES]
    return _feishu_tools


def _filter_feishu_tools_for_access(
    tools: list[dict],
    *,
    has_feishu_channel: bool,
    has_feishu_office_access: bool,
    has_feishu_cli_access: bool,
) -> list[dict]:
    """Select Feishu tools according to channel vs CLI-backed office access."""
    filtered: list[dict] = []
    for tool in tools:
        name = tool["function"]["name"]
        if name in _FEISHU_CLI_ONLY_TOOL_NAMES:
            if has_feishu_cli_access:
                filtered.append(tool)
            continue
        if name in _FEISHU_OFFICE_TOOL_NAMES:
            if has_feishu_office_access:
                filtered.append(tool)
            continue
        if has_feishu_channel:
            filtered.append(tool)
    return filtered


def _get_hr_tools() -> list[dict]:
    global _hr_tools
    if _hr_tools is None:
        all_tools = get_combined_openai_tools()
        _hr_tools = [t for t in all_tools if t["function"]["name"] in _HR_TOOL_NAMES]
    return _hr_tools


async def _agent_has_feishu(agent_id: uuid.UUID) -> bool:
    """Check if agent has a configured Feishu channel."""
    try:
        from app.models.channel_config import ChannelConfig

        # RLS 阶段2b: channel_configs now bears a USING-only policy. Pin the GUC
        # to the agent's tenant so this read survives the non-owner role; a bare
        # session would fail closed and wrongly report "no feishu".
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            r = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured,
                )
            )
            return r.scalar_one_or_none() is not None
    except Exception:
        return False


async def _agent_has_feishu_office_access(agent_id: uuid.UUID) -> bool:
    """Office read access is available via channel creds or optional lark-cli auth."""
    if await _agent_has_feishu(agent_id):
        return True
    return await _agent_has_feishu_cli_access()


async def _agent_has_feishu_cli_access() -> bool:
    """CLI-backed office access is available when lark-cli is enabled and authenticated."""
    from app.services.agent_tool_domains.feishu_cli import _feishu_cli_available

    return await _feishu_cli_available()


# ─── Dynamic Tool Loading from DB ──────────────────────────────


@dataclass(frozen=True, slots=True)
class _MCPGating:
    reachable_by_tool_id: dict[str, bool]
    always_load_tool_names: set[str]


async def _resolve_agent_mcp_gating(db, agent_id: uuid.UUID) -> _MCPGating | None:
    """Batch-resolve MCP-tool reachability from the new assignment tables.

    Returns ``{str(Tool.id): reachable}`` for every MCP tool the agent's
    assignments cover, or ``None`` when this agent has NO rows in the new MCP
    tables (not yet backfilled) so the caller keeps the legacy
    ``AgentTool.enabled`` / ``is_default`` logic for MCP tools — the
    tool-availability parity safety fallback (§8.5).

    One query per table (hot path — runs every invocation). Reachability per
    server reuses the proven ``resolve_reachable_tools`` contract:
    ``enabled = assignment.enabled``, ``deny_tool_names = {override.tool_name
    where mode == "deny"}``. Approval handling is intentionally left to the
    downstream capability gate.
    """
    from app.models.mcp_server import AgentMCPServerAssignment, AgentMCPToolOverride, MCPServerTool
    from app.services.mcp_backfill import resolve_reachable_tools

    assignments_r = await db.execute(
        select(AgentMCPServerAssignment).where(AgentMCPServerAssignment.agent_id == agent_id)
    )
    assignments = list(assignments_r.scalars().all())
    if not assignments:
        # Not yet backfilled for this agent → signal fallback.
        return None

    server_ids = [a.mcp_server_id for a in assignments]
    server_tools_r = await db.execute(select(MCPServerTool).where(MCPServerTool.mcp_server_id.in_(server_ids)))
    server_tools = list(server_tools_r.scalars().all())
    if not server_tools:
        # Assignment rows exist but no server-tool rows → no MCP-tool data to gate.
        return None

    overrides_r = await db.execute(select(AgentMCPToolOverride).where(AgentMCPToolOverride.agent_id == agent_id))
    overrides = list(overrides_r.scalars().all())

    # Group server-tool (tool_id, tool_name) pairs and deny names per server.
    tools_by_server: dict[str, list[tuple[str, str]]] = {}
    for st in server_tools:
        if st.tool_id is None:
            continue
        tools_by_server.setdefault(str(st.mcp_server_id), []).append((str(st.tool_id), st.mcp_tool_name))
    deny_by_server: dict[str, set[str]] = {}
    for ov in overrides:
        if ov.mode == "deny":
            deny_by_server.setdefault(str(ov.mcp_server_id), set()).add(ov.tool_name)

    gating: dict[str, bool] = {}
    always_load_tool_names: set[str] = set()
    for assignment in assignments:
        sid = str(assignment.mcp_server_id)
        pairs = tools_by_server.get(sid, [])
        tool_names = [name for _tid, name in pairs]
        reachable_names = resolve_reachable_tools(
            tool_names,
            bool(assignment.enabled),
            deny_by_server.get(sid, set()),
            getattr(assignment, "default_tool_mode", "auto"),
        )
        if bool(getattr(assignment, "always_load", False)):
            always_load_tool_names.update(reachable_names)
        for tool_id, tool_name in pairs:
            gating[tool_id] = tool_name in reachable_names
    return _MCPGating(reachable_by_tool_id=gating, always_load_tool_names=always_load_tool_names)


def _mcp_tool_matches_query(query: str, tool) -> bool:
    """Match a tool_search query against an MCP tool's name / server / underlying
    tool name (fuzzy via :func:`normalize_tool_query`). An empty query matches
    everything (list all reachable)."""
    normalized = query.strip().lower()
    if not normalized:
        return True
    candidates = [
        tool.name or "",
        getattr(tool, "mcp_server_name", "") or "",
        getattr(tool, "mcp_tool_name", "") or "",
        tool.description or "",
    ]
    blob = " ".join(candidates).lower()
    if normalized in blob:
        return True
    compact = normalize_tool_query(normalized)
    return bool(compact and any(compact in normalize_tool_query(c) for c in candidates))


def _tool_visible_to_agent_tenant(tool: Any, agent: Any | None) -> bool:
    tool_tenant_id = getattr(tool, "tenant_id", None)
    if tool_tenant_id is None:
        return True
    agent_tenant_id = getattr(agent, "tenant_id", None)
    return bool(agent_tenant_id) and str(tool_tenant_id) == str(agent_tenant_id)


def _tool_tenant_predicate(ToolModel: Any, agent: Any | None):
    agent_tenant_id = getattr(agent, "tenant_id", None)
    if agent_tenant_id is None:
        return ToolModel.tenant_id.is_(None)
    return or_(ToolModel.tenant_id.is_(None), ToolModel.tenant_id == agent_tenant_id)


async def discoverable_tool_names_for_query(agent_id: uuid.UUID, query: str) -> list[str]:
    """Single source for "query → discoverable (deferred) tool names" (Step 3).

    Used by BOTH the schema path (``invoker._deferred_tool_names_for_query``) and
    the text path (``workspace._tool_search``) so "what the model is told it can
    discover" == "what actually loads". Excludes CORE tools (already turn-1
    visible) and routes MCP discovery through the DB-aware
    ``list_agent_mcp_deferred_tools``.
    """
    raw_query = query.strip()
    normalized = raw_query.lower()
    select_target = ""
    if normalized.startswith("select:"):
        select_target = raw_query.split(":", 1)[1].strip()
        normalized = select_target.lower()
    compact = normalize_tool_query(normalized)
    pack_policies: dict[str, bool] | None = None
    pack_policy_available = True
    try:
        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            pack_policies = await get_agent_pack_policies(db, tenant_id, agent_id)
    except Exception as exc:
        logger.warning("[Tools] pack policy lookup failed for discovery; failing L2 discovery closed: %s", exc)
        pack_policy_available = False

    def _pack_enabled(pack_name: str) -> bool:
        if not pack_policy_available:
            return False
        if pack_policies is None:
            return False
        return is_pack_enabled(pack_policies, pack_name)

    def _direct_tool_match(tool_name: str) -> bool:
        return bool(normalized) and (tool_name.lower() == normalized or normalize_tool_query(tool_name) == compact)

    if not normalized:
        requested: list[str] = sorted(AGENT_TEAM_DEFERRED_TOOL_NAMES)
        seen: set[str] = set(requested)
    elif any(_direct_tool_match(tool_name) for tool_name in AGENT_TEAM_DEFERRED_TOOL_NAMES):
        return [next(tool_name for tool_name in sorted(AGENT_TEAM_DEFERRED_TOOL_NAMES) if _direct_tool_match(tool_name))]
    else:
        requested = []
        seen = set()

    if normalized:
        for pack in iter_runtime_l2_capabilities():
            if not _pack_enabled(pack.name) and pack_policy_available:
                continue
            for tool_name in pack.tools:
                if _direct_tool_match(tool_name) and tool_name not in CORE_TOOL_NAMES:
                    return [tool_name]
            if not pack_policy_available:
                continue
    for pack in iter_runtime_l2_capabilities_for_query(query):
        if not _pack_enabled(pack.name):
            continue
        for tool_name in pack.tools:
            if tool_name in CORE_TOOL_NAMES or tool_name in seen:
                continue
            requested.append(tool_name)
            seen.add(tool_name)
    mcp_query = select_target or query
    for mcp_name in await list_agent_mcp_deferred_tools(agent_id, mcp_query):
        if mcp_name in CORE_TOOL_NAMES or mcp_name in seen:
            continue
        requested.append(mcp_name)
        seen.add(mcp_name)
    return requested


async def available_deferred_tool_names_for_agent(agent_id: uuid.UUID, *, limit: int = 80) -> list[str]:
    """Stable turn-1 list of deferred tool names the agent may select/load."""
    names = await discoverable_tool_names_for_query(agent_id, "")
    return sorted(dict.fromkeys(names))[:limit]


async def list_agent_mcp_deferred_tools(agent_id: uuid.UUID, query: str = "") -> list[str]:
    """The single DB-aware enumerator of MCP tools an agent can DISCOVER via
    tool_search (J / 🦴#2).

    BOTH the text result (``workspace._tool_search``) and the schema injection
    (``invoker._deferred_tool_names_for_query``) route through here, so "what the
    model is told exists" == "what actually loads".

    Governance listing gate: only REACHABLE MCP tools are listed — denied tools
    and tools on disabled servers/packs never appear (CC parity: denied == not
    reachable). For un-backfilled agents (no MCP assignment rows → ``mcp_gating``
    is None) it falls back to the legacy ``is_default`` / ``AgentTool.enabled``
    decision, so a NON-default MCP tool can never be surfaced — the choke point
    that, with the matching tightening in :func:`get_agent_tools_for_llm`, closes
    the discovery force-enable escalation (§J #1).
    """
    from app.models.tool import AgentTool, Tool

    matched: list[str] = []
    seen: set[str] = set()
    try:
        # RLS 阶段1: `tools`/`agents` are policy-bearing — resolve the agent's
        # tenant first (audited single-row bypass), then scope the whole read.
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
            mcp_gating = await _resolve_agent_mcp_gating(db, agent_id)
            mcp_tools = (
                (
                    await db.execute(
                        select(Tool).where(Tool.enabled, Tool.type == "mcp", _tool_tenant_predicate(Tool, agent))
                    )
                )
                .scalars()
                .all()
            )
            agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
            assignments = {str(at.tool_id): at for at in agent_tools_r.scalars().all()}

            for t in mcp_tools:
                if not _tool_visible_to_agent_tenant(t, agent):
                    continue
                if not is_tool_allowed_for_agent(t, agent):
                    continue
                tid = str(t.id)
                # Reachability (listing gate): backfilled agents use assignment +
                # override gating; un-backfilled fall back to legacy is_default /
                # AgentTool.enabled — a non-default unassigned MCP tool is never listed.
                if mcp_gating is not None and tid in mcp_gating.reachable_by_tool_id:
                    reachable = mcp_gating.reachable_by_tool_id[tid]
                else:
                    at = assignments.get(tid)
                    reachable = at.enabled if at else bool(t.is_default)
                if not reachable:
                    continue
                # MCP visibility is governed solely by assignment + override gating
                # (above); the legacy mcp_server:* pseudo-pack was a no-op (no such
                # policy is ever written) and was retired in Step 6.
                if not _mcp_tool_matches_query(query, t):
                    continue
                if t.name in seen:
                    continue
                seen.add(t.name)
                matched.append(t.name)
    except Exception as e:
        logger.warning(f"[Tools] list_agent_mcp_deferred_tools failed: {e}")
    return matched


async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    core_only: bool = False,
    requested_names: list[str] | None = None,
) -> list[dict]:
    """Load enabled tools for an agent from DB (OpenAI function-calling format).

    Args:
        agent_id: The agent to load tools for.
        core_only: When True, only return tools in CORE_TOOL_NAMES
                   (progressive loading — full set loaded later when agent reads a skill).
        requested_names: When provided, return kernel tools plus only the requested
                   non-kernel tools that are available to the agent.

    Falls back to the collected tool surface if DB is not ready.
    Always includes core system tools (send_channel_file, write_file).
    Feishu office read tools may also be included when lark-cli office auth is available.
    """
    has_feishu_channel = await _agent_has_feishu(agent_id)
    has_feishu_cli_access = await _agent_has_feishu_cli_access()
    has_feishu_office_access = has_feishu_channel or has_feishu_cli_access
    explicit_requested_set = set(requested_names or [])
    requested_set = set(explicit_requested_set)
    if requested_set:
        requested_set |= CORE_TOOL_NAMES

    try:
        from app.models.tool import Tool, AgentTool

        # RLS 阶段1: `tools`/`agents` are policy-bearing — resolve the agent's
        # tenant first (audited single-row bypass), then scope the whole read.
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            hr_agent = is_hr_agent(agent)
            _core = _get_always_core_tools()
            if hr_agent:
                # HR agent: remove tool_search (searches own workspace, useless for HR)
                _core = [t for t in _core if t["function"]["name"] != "tool_search"]
            _always_tools = (
                _core
                + _filter_feishu_tools_for_access(
                    _get_feishu_tools(),
                    has_feishu_channel=has_feishu_channel,
                    has_feishu_office_access=has_feishu_office_access,
                    has_feishu_cli_access=has_feishu_cli_access,
                )
                + (_get_hr_tools() if hr_agent else [])
            )
            pack_policies = await get_agent_pack_policies(db, getattr(agent, "tenant_id", None), agent_id)

            # Get globally enabled tools visible to this agent's tenant.
            all_tools_r = await db.execute(select(Tool).where(Tool.enabled, _tool_tenant_predicate(Tool, agent)))
            all_tools = all_tools_r.scalars().all()

            # Get agent-specific assignments
            agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
            assignments = {str(at.tool_id): at for at in agent_tools_r.scalars().all()}

            # MCP-tool reachability from the new assignment tables (§8.5).
            # None → agent not yet backfilled; keep legacy AgentTool/is_default logic for MCP.
            mcp_gating = await _resolve_agent_mcp_gating(db, agent_id)

            result = []
            db_tool_names = set()
            for t in all_tools:
                if not _tool_visible_to_agent_tenant(t, agent):
                    continue
                if not is_tool_allowed_for_agent(t, agent):
                    continue
                tid = str(t.id)
                at = assignments.get(tid)
                if at is not None:
                    enabled = at.enabled
                elif t.type == "mcp":
                    # 🦴 J #1: tool_search discovery puts reachable MCP names into
                    # explicit_requested_set; that must NOT alone force-enable a
                    # NON-default MCP tool (privilege escalation for un-backfilled
                    # agents). Un-gated MCP tools fall back to is_default only;
                    # reachability is decided below when the agent IS backfilled.
                    enabled = bool(t.is_default)
                else:
                    enabled = t.is_default or t.name in explicit_requested_set
                # MCP tools: when the agent has new-table data, assignment+overrides
                # decide reachability (§8.5). Un-backfilled agents (mcp_gating is None,
                # or tool absent from gating) keep the legacy decision above.
                if t.type == "mcp" and mcp_gating is not None and tid in mcp_gating.reachable_by_tool_id:
                    enabled = mcp_gating.reachable_by_tool_id[tid]
                if not enabled:
                    continue

                if t.category == "feishu":
                    if t.name in _FEISHU_CLI_ONLY_TOOL_NAMES and not has_feishu_cli_access:
                        continue
                    if t.name in _FEISHU_OFFICE_TOOL_NAMES and not has_feishu_office_access:
                        continue
                    if (
                        t.name not in _FEISHU_OFFICE_TOOL_NAMES
                        and t.name not in _FEISHU_CLI_ONLY_TOOL_NAMES
                        and not has_feishu_channel
                    ):
                        continue

                # MCP tools are gated by assignment reachability (above), not by a
                # pack policy — the mcp_server:* pseudo-pack was retired in Step 6.
                # Non-MCP tools still respect their static pack policy.
                static_packs = set(policy_pack_names_for_tool(t.name))
                if static_packs and not any(is_pack_enabled(pack_policies, pack_name) for pack_name in static_packs):
                    continue

                # Build OpenAI function-calling format
                # NOTE: sanitization happens once in ToolRegistry.to_openai_tools() downstream
                raw_params = t.parameters_schema or {"type": "object", "properties": {}}
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": raw_params,
                    },
                }
                result.append(tool_def)
                db_tool_names.add(t.name)

            if result:
                # Append always-available system tools that aren't already in the DB list
                for t in _always_tools:
                    if t["function"]["name"] not in db_tool_names:
                        result.append(t)
                if core_only:
                    keep = CORE_TOOL_NAMES | (_HR_TOOL_NAMES if hr_agent else set())
                    if mcp_gating is not None:
                        keep |= mcp_gating.always_load_tool_names
                    result = [t for t in result if t["function"]["name"] in keep]
                elif requested_set:
                    result = [t for t in result if t["function"]["name"] in requested_set]
                result = await _filter_unavailable_tools(agent_id, result)
                return ToolRegistry.from_openai_tools(result).to_openai_tools()
    except Exception as e:
        logger.error(f"[Tools] DB load failed, using fallback: {e}")

    # Fallback to the collected tool surface when DB is unavailable.
    # Route through ToolRegistry to ensure schemas are sanitized (Gemini compatibility).
    fallback = get_combined_openai_tools()
    allowed_feishu_names = {
        tool["function"]["name"]
        for tool in _filter_feishu_tools_for_access(
            [tool for tool in fallback if tool["function"]["name"] in _FEISHU_TOOL_NAMES],
            has_feishu_channel=has_feishu_channel,
            has_feishu_office_access=has_feishu_office_access,
            has_feishu_cli_access=has_feishu_cli_access,
        )
    }
    fallback = [
        tool
        for tool in fallback
        if tool["function"]["name"] not in _FEISHU_TOOL_NAMES or tool["function"]["name"] in allowed_feishu_names
    ]
    fallback = [tool for tool in fallback if tool["function"]["name"] not in HR_ONLY_TOOL_NAMES]
    if core_only:
        fallback = [t for t in fallback if t["function"]["name"] in CORE_TOOL_NAMES]
    elif requested_set:
        fallback = [t for t in fallback if t["function"]["name"] in requested_set]
    fallback = await _filter_unavailable_tools(agent_id, fallback)
    return ToolRegistry.from_openai_tools(fallback).to_openai_tools()


# ─── Tool Executors ─────────────────────────────────────────────


async def execute_approved_tool(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    *,
    approved_by_user_id: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
) -> str | ToolContentEnvelope:
    """Execute a tool after an explicit approval decision.

    Approval handling is a governance boundary, so callers should use this
    public entrypoint instead of reaching into ToolRuntimeService internals.
    """
    return await _get_tool_runtime_service().execute_approved(
        tool_name,
        arguments,
        agent_id=agent_id,
        approved_by_user_id=approved_by_user_id,
        approval_id=approval_id,
    )


async def execute_tool(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    tool_call_id: str | None = None,
    event_callback: ToolEventCallback | None = None,
    delegation_token: Any | None = None,
    session_id: str | None = None,
    permission_profile: Any | None = None,
    turn_id: str | None = None,
    runtime_task_id: str | None = None,
    budget_run_id: str | None = None,
    origin_channel: str | None = None,
    round_state: dict[str, Any] | None = None,
    t0_refs: tuple[str, ...] = (),
    plan_mode_interactive_available: bool = False,
    emit_runtime_hooks: bool = True,
    trace_metadata_sink: dict[str, Any] | None = None,
    workspace_override: Path | str | None = None,
) -> str | ToolContentEnvelope:
    """Execute a tool call and return the result (str, or a typed content envelope)."""
    return await _get_tool_runtime_service().execute(
        tool_name,
        arguments,
        agent_id=agent_id,
        user_id=user_id,
        tool_call_id=tool_call_id,
        event_callback=event_callback,
        delegation_token=delegation_token,
        session_id=session_id,
        permission_profile=permission_profile,
        turn_id=turn_id,
        runtime_task_id=runtime_task_id,
        budget_run_id=budget_run_id,
        origin_channel=origin_channel,
        round_state=round_state,
        t0_refs=t0_refs,
        plan_mode_interactive_available=plan_mode_interactive_available,
        emit_runtime_hooks=emit_runtime_hooks,
        trace_metadata_sink=trace_metadata_sink,
        workspace_override=workspace_override,
    )


async def execute_session_permission_tool(
    tool_name: str,
    arguments: dict,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    permission_profile: Any,
    tool_call_id: str | None = None,
    turn_id: str | None = None,
    runtime_task_id: str | None = None,
    origin_channel: str | None = None,
    round_state: dict[str, Any] | None = None,
    t0_refs: tuple[str, ...] = (),
) -> str | ToolContentEnvelope:
    """Replay a session-approved tool through the public governed runtime boundary."""
    return await execute_tool(
        tool_name,
        arguments,
        agent_id=agent_id,
        user_id=user_id,
        tool_call_id=tool_call_id,
        session_id=session_id,
        permission_profile=permission_profile,
        turn_id=turn_id,
        runtime_task_id=runtime_task_id,
        origin_channel=origin_channel,
        round_state=round_state,
        t0_refs=t0_refs,
    )


async def _execute_tool_inner(
    tool_name: str,
    arguments: dict,
    context,
) -> str | ToolContentEnvelope:
    """Inner tool dispatch — called with timeout wrapper from execute_tool()."""
    return await _get_tool_runtime_service().execute_with_context(
        tool_name,
        arguments,
        context,
    )


async def _send_channel_file(agent_id: uuid.UUID, ws: Path, arguments: dict) -> str:
    """Send a file to the current requester via unified channel delivery."""
    rel_path = arguments.get("file_path", "").strip()
    accompany_msg = arguments.get("message", "")
    file_path, error = _resolve_channel_file_path(ws, rel_path)
    if error:
        return error
    assert file_path is not None

    reply_target = channel_delivery_target.get()
    if reply_target is not None:
        try:
            # RLS 阶段1: delivery reads users/agents (policy-bearing) — scope to
            # the agent's tenant (audited single-row bypass to resolve it).
            tid = await resolve_tenant_for_agent(agent_id)
            async with tenant_scoped_session(tid) as db:
                result = await ChannelDeliveryService.send_file(
                    db=db,
                    agent_id=agent_id,
                    reply_target=reply_target,
                    file_path=file_path,
                    message=accompany_msg,
                    delivery_mode="live",
                    extra_detail={"tool_name": "send_channel_file"},
                )
            if result.ok:
                return f"✅ File '{file_path.name}' sent to user via {result.channel}."
            logger.warning("[ChannelFile] Unified delivery failed: %s", result.message)
        except Exception as exc:
            logger.warning("[ChannelFile] Unified delivery error: %s", exc)

    sender = channel_file_sender.get()
    if sender is not None:
        # Channel mode: call the channel-specific send function
        try:
            await sender(file_path, accompany_msg)
            return f"✅ File '{file_path.name}' sent to user via channel."
        except Exception as e:
            return f"❌ Failed to send file: {e}"
    else:
        # Web chat mode: return a download URL
        aid = channel_web_agent_id.get() or str(agent_id)
        base_abs = ws.resolve()
        file_rel = file_path.resolve().relative_to(base_abs).as_posix()
        from app.config import get_settings as _gs

        _s = _gs()
        base_url = getattr(_s, "BASE_URL", "").rstrip("/") or ""
        download_url = f"{base_url}/api/agents/{aid}/files/download?path={quote(file_rel, safe='/')}"
        msg = f"✅ File ready: [{file_path.name}]({download_url})"
        if accompany_msg:
            msg = accompany_msg + "\n\n" + msg
        return msg


async def _send_channel_message(agent_id: uuid.UUID, arguments: dict) -> str:
    """Send a text message to the current requester / persisted reply target."""
    message = str(arguments.get("message", "") or "").strip()
    if not message:
        return "❌ message is required"

    reply_target = channel_delivery_target.get()
    if reply_target is None:
        return "❌ No current channel delivery target is available."

    # RLS 阶段1: delivery reads users/agents (policy-bearing) — scope to the
    # agent's tenant (audited single-row bypass to resolve it).
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        result = await ChannelDeliveryService.send_text(
            db=db,
            agent_id=agent_id,
            reply_target=reply_target,
            text=message,
            delivery_mode="live",
            extra_detail={"tool_name": "send_channel_message"},
        )

    if result.ok:
        return f"✅ {result.message}"
    return f"❌ {result.message}"


# ─── Domain module re-exports ──────────────────────────────────
# All business logic lives in agent_tool_domains/. These re-exports
# preserve backward compatibility for existing import sites.
from app.services.agent_tool_domains.workspace import (  # noqa: E402
    _build_skill_registry as _build_skill_registry,
    _delete_file as _delete_file,
    _edit_file as _edit_file,
    _glob_search as _glob_search,
    _grep_search as _grep_search,
    _list_files as _list_files,
    _load_skill as _load_skill,
    _read_document as _read_document,
    _read_file as _read_file,
    _tool_search as _tool_search,
    _write_file as _write_file,
)
from app.services.agent_tool_domains.tasks import (  # noqa: E402
    _manage_tasks as _manage_tasks,
)
from app.services.agent_tool_domains.plaza import (  # noqa: E402
    _plaza_get_new_posts as _plaza_get_new_posts,
    _plaza_create_post as _plaza_create_post,
    _plaza_add_comment as _plaza_add_comment,
)
from app.services.agent_tool_domains.code_exec import (  # noqa: E402
    _check_code_safety as _check_code_safety,
    _execute_code as _execute_code,
    _run_command as _run_command,
)
from app.services.agent_tool_domains.image_upload import (  # noqa: E402
    _upload_image as _upload_image,
)
from app.services.agent_tool_domains.email import (  # noqa: E402
    _get_email_config as _get_email_config,
    _handle_email_tool as _handle_email_tool,
)
from app.services.agent_tool_domains.triggers import (  # noqa: E402
    MAX_TRIGGERS_PER_AGENT as MAX_TRIGGERS_PER_AGENT,
    VALID_TRIGGER_TYPES as VALID_TRIGGER_TYPES,
    _handle_set_trigger as _handle_set_trigger,
    _handle_update_trigger as _handle_update_trigger,
    _handle_cancel_trigger as _handle_cancel_trigger,
    _handle_list_triggers as _handle_list_triggers,
)
from app.services.agent_tool_domains.messaging import (  # noqa: E402
    A2A_SYSTEM_PROMPT_SUFFIX as A2A_SYSTEM_PROMPT_SUFFIX,
    _send_feishu_message as _send_feishu_message,
    _send_web_message as _send_web_message,
    _persist_agent_tool_call as _persist_agent_tool_call,
    _build_agent_message_tool_executor as _build_agent_message_tool_executor,
    _invoke_agent_message_runtime as _invoke_agent_message_runtime,
    _send_message_to_agent as _send_message_to_agent,
    _delegate_to_agent_async as _delegate_to_agent_async,
    _check_async_task as _check_async_task,
    _cancel_async_task as _cancel_async_task,
    _list_async_tasks as _list_async_tasks,
    _get_current_time as _get_current_time,
)
from app.services.agent_tool_domains.feishu_helpers import (  # noqa: E402
    _get_feishu_token as _get_feishu_token,
    _get_agent_calendar_id as _get_agent_calendar_id,
    _feishu_resolve_open_id as _feishu_resolve_open_id,
    _iso_to_ts as _iso_to_ts,
)
from app.services.agent_tool_domains.feishu_wiki import (  # noqa: E402
    _feishu_wiki_get_node as _feishu_wiki_get_node,
    _feishu_wiki_list as _feishu_wiki_list,
)
from app.services.agent_tool_domains.feishu_docs import (  # noqa: E402
    _feishu_doc_read as _feishu_doc_read,
    _feishu_doc_create as _feishu_doc_create,
    _parse_inline_markdown as _parse_inline_markdown,
    _markdown_to_feishu_blocks as _markdown_to_feishu_blocks,
    _feishu_doc_append as _feishu_doc_append,
    _feishu_doc_delete as _feishu_doc_delete,
)
from app.services.agent_tool_domains.feishu_drive import (  # noqa: E402
    _feishu_url_resolve as _feishu_url_resolve,
    _feishu_url_read as _feishu_url_read,
    _feishu_drive_file_read as _feishu_drive_file_read,
)
from app.services.agent_tool_domains.feishu_sheets import (  # noqa: E402
    _feishu_sheet_info as _feishu_sheet_info,
    _feishu_sheet_read as _feishu_sheet_read,
)
from app.services.agent_tool_domains.feishu_base import (  # noqa: E402
    _feishu_base_app_create as _feishu_base_app_create,
    _feishu_base_field_list as _feishu_base_field_list,
    _feishu_base_field_create as _feishu_base_field_create,
    _feishu_base_table_list as _feishu_base_table_list,
    _feishu_base_record_delete as _feishu_base_record_delete,
    _feishu_base_record_list as _feishu_base_record_list,
    _feishu_base_record_upload_attachment as _feishu_base_record_upload_attachment,
    _feishu_base_record_upsert as _feishu_base_record_upsert,
)
from app.services.agent_tool_domains.feishu_approval import (  # noqa: E402
    _feishu_approval_create as _feishu_approval_create,
    _feishu_approval_definition as _feishu_approval_definition,
    _feishu_approval_query as _feishu_approval_query,
    _feishu_approval_get as _feishu_approval_get,
)
from app.services.agent_tool_domains.feishu_tasks import (  # noqa: E402
    _feishu_task_comment as _feishu_task_comment,
    _feishu_task_complete as _feishu_task_complete,
    _feishu_task_create as _feishu_task_create,
    _feishu_task_list as _feishu_task_list,
)
from app.services.agent_tool_domains.feishu_sharing import (  # noqa: E402
    _feishu_doc_share as _feishu_doc_share,
)
from app.services.agent_tool_domains.feishu_calendar import (  # noqa: E402
    _feishu_calendar_list as _feishu_calendar_list,
    _feishu_calendar_create as _feishu_calendar_create,
    _feishu_calendar_update as _feishu_calendar_update,
    _feishu_calendar_delete as _feishu_calendar_delete,
)
from app.services.agent_tool_domains.feishu_users import (  # noqa: E402
    _feishu_user_search as _feishu_user_search,
    _feishu_contacts_refresh as _feishu_contacts_refresh,
)


from app.services.agent_tool_domains.web_mcp import (  # noqa: E402
    _discover_resources as _discover_resources,
    _execute_mcp_tool as _execute_mcp_tool,
    _execute_via_smithery_connect as _execute_via_smithery_connect,
    _firecrawl_fetch as _firecrawl_fetch,
    _get_exa_api_key as _get_exa_api_key,
    _get_firecrawl_api_key as _get_firecrawl_api_key,
    _get_tavily_api_key as _get_tavily_api_key,
    _get_xcrawl_api_key as _get_xcrawl_api_key,
    _import_mcp_server as _import_mcp_server,
    _search_exa as _search_exa,
    _search_bing as _search_bing,
    _search_duckduckgo as _search_duckduckgo,
    _search_google as _search_google,
    _search_tavily as _search_tavily,
    _smithery_auto_recover as _smithery_auto_recover,
    _web_fetch as _web_fetch,
    _web_search as _web_search,
    _xcrawl_scrape as _xcrawl_scrape,
)
