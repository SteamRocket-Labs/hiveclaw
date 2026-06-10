"""Agent tools — unified file-based tools that give digital employees
access to their own structured workspace.

Design principle: ONE set of file tools covers EVERYTHING.
The agent's workspace uses well-known paths:
  - tasks.json          → task list (auto-synced from DB)
  - soul.md             → personality definition
  - memory/*.md         → layered long-term memory (feedback / knowledge / strategies / blocked / user)
  - skills/             → skill definitions (markdown files)
  - workspace/          → general working files, reports, etc.

The agent reads/writes these files directly. No per-concept tools needed.
"""

import logging
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select

from app.database import async_session, tenant_scoped_session
from app.models.agent import Agent
from app.config import get_settings
from app.services.channel_delivery_service import ChannelDeliveryService, channel_delivery_target
from app.services.pack_policy_service import get_tenant_pack_policies, is_pack_enabled
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.tool_visibility import HR_ONLY_TOOL_NAMES, is_hr_agent, is_tool_allowed_for_agent
from app.tools import (
    ToolExecutionRegistry,
    ToolGovernanceResolver,
    ToolRegistry,
    ToolRuntimeService,
    run_tool_governance,
)
from app.tools.runtime_tool_groups import (
    make_mcp_server_pack_name,
    normalize_tool_query,
    static_runtime_tool_group_names_for_tool,
)

logger = logging.getLogger(__name__)

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
        return await _execute_mcp_tool(tool_name, arguments, agent_id=context.agent_id)

    async def _direct_fallback_execute(tool_name: str, arguments: dict, context) -> str:
        # Approved/direct fallback is not a second implementation of first-class tools.
        # Known tools must be registered in ToolExecutionRegistry; only unknown tools
        # may fall through to MCP passthrough.
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


# Minimal-by-default kernel tools. Everything else should be introduced
# explicitly via skills, channel capabilities, or MCP-linked expansion.
CORE_TOOL_NAMES = {
    "execute_code",
    "run_command",
    "list_files",
    "read_file",
    "write_file",
    "edit_file",
    "glob_search",
    "grep_search",
    "fs_read",
    "fs_write",
    "fs_list",
    "load_skill",
    "save_skill",
    "search_memory",
    "load_memory",
    "save_memory",
    "set_trigger",
    "update_trigger",
    "cancel_trigger",
    "list_triggers",
    "send_message_to_agent",
    "delegate_to_agent",
    "check_async_task",
    "cancel_async_task",
    "list_async_tasks",
    "get_current_time",
    "exit_plan_mode",
    # CC-align Phase B: clarification is a turn-1 capability so the agent can SEE
    # and call it (plan mode + normal chat); subagents exclude it (no user channel).
    "ask_user_question",
    "send_channel_message",
    "send_channel_file",
    "tool_search",
    "web_fetch",
    # Source capabilities (T1.1, execution-mode-spectrum §4.6): subagent and
    # workflow are runtime primitives — their schemas must be turn-1 visible,
    # never gated behind a skill-activated pack. Call-time governance
    # (capability gate / plan gate / approval) remains the execution boundary.
    "spawn_subagent",
    "preview_workflow",
    "start_workflow",
    # Work ledger (T1.2): working memory is an agent thinking tool — it must
    # not depend on DB Tool.is_default/assignment rows to exist. The
    # should_enable_work_ledger gate keeps governing reminder frequency only.
    "track_todo",
    "record_finding",
    "read_ledger",
}

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
    firecrawl_key = await _get_firecrawl_api_key()
    xcrawl_key = await _get_xcrawl_api_key()
    if exa_key:
        available.add("web_search")
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
    provider_backed = {"firecrawl_fetch", "xcrawl_scrape", "discover_resources", "import_mcp_server"}
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

        async with async_session() as db:
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
            pack_policies = await get_tenant_pack_policies(db, getattr(agent, "tenant_id", None))
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
                # The server's pack must be enabled (same gate the schema-load path applies).
                pack_name = make_mcp_server_pack_name(t.mcp_server_name, t.mcp_server_url)
                if not is_pack_enabled(pack_policies, pack_name):
                    continue
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
            pack_policies = await get_tenant_pack_policies(db, getattr(agent, "tenant_id", None))

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

                static_packs = set(static_runtime_tool_group_names_for_tool(t.name))
                if t.type == "mcp":
                    static_packs.add(make_mcp_server_pack_name(t.mcp_server_name, t.mcp_server_url))
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
) -> str:
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
    event_callback: ToolEventCallback | None = None,
    delegation_token: Any | None = None,
    session_id: str | None = None,
    plan_mode_interactive_available: bool = False,
) -> str:
    """Execute a tool call and return the result as a string."""
    return await _get_tool_runtime_service().execute(
        tool_name,
        arguments,
        agent_id=agent_id,
        user_id=user_id,
        event_callback=event_callback,
        delegation_token=delegation_token,
        session_id=session_id,
        plan_mode_interactive_available=plan_mode_interactive_available,
    )


async def _execute_tool_inner(
    tool_name: str,
    arguments: dict,
    context,
) -> str:
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
    if not rel_path:
        return "❌ file_path is required"

    # Resolve file path within agent workspace
    file_path = (ws / rel_path).resolve()
    ws_resolved = ws.resolve()
    if not str(file_path).startswith(str(ws_resolved)):
        # Also allow workspace/ prefix pointing to same location
        file_path = (WORKSPACE_ROOT / str(agent_id) / rel_path).resolve()
        if not file_path.exists():
            return f"❌ File not found: {rel_path}"
    if not file_path.exists():
        return f"❌ File not found: {rel_path}"

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
        base_abs = (WORKSPACE_ROOT / str(agent_id)).resolve()
        try:
            file_rel = str(file_path.resolve().relative_to(base_abs))
        except ValueError:
            file_rel = rel_path
        from app.config import get_settings as _gs

        _s = _gs()
        base_url = getattr(_s, "BASE_URL", "").rstrip("/") or ""
        download_url = f"{base_url}/api/agents/{aid}/files/download?path={file_rel}"
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
