"""Runtime tool group metadata for minimal-by-default expansion."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class RuntimeToolGroupSpec:
    name: str
    summary: str
    source: str
    activation_mode: str
    tools: tuple[str, ...]
    infer_from_tools: bool = True


RUNTIME_TOOL_GROUPS: tuple[RuntimeToolGroupSpec, ...] = (
    RuntimeToolGroupSpec(
        name="web_pack",
        summary=(
            "Advanced web search and read/extract tooling: AnySearch MCP provides vertical search/discovery "
            "for finance, social media, academic, legal, health, business, security, code, and related data, "
            "plus known-URL Markdown extraction as a read step; Exa provides AI-native search with search types "
            "and category verticals; Tavily provides real-time agent/RAG search with topic, freshness, answer, "
            "and raw-content options; Firecrawl/XCrawl handle page extraction when web_fetch is insufficient."
        ),
        source="system",
        activation_mode=(
            "Discover schemas through tool_search; start with CORE web_search/web_fetch, escalate to provider "
            "search when results are insufficient, and use extract tools only after selecting a known URL."
        ),
        tools=(
            "anysearch_get_sub_domains",
            "anysearch_search",
            "anysearch_batch_search",
            "anysearch_extract",
            "exa_search",
            "tavily_search",
            "firecrawl_fetch",
            "xcrawl_scrape",
        ),
    ),
    RuntimeToolGroupSpec(
        name="feishu_pack",
        summary="Feishu messaging, docs, wiki, sheets, Base, approvals, tasks, and calendar tools.",
        source="channel",
        activation_mode="Discover schemas through tool_search; load the Feishu skill only when method guidance is needed.",
        tools=(
            "send_feishu_message",
            "feishu_user_search",
            "feishu_wiki_list",
            "feishu_doc_read",
            "feishu_url_resolve",
            "feishu_url_read",
            "feishu_drive_file_read",
            "feishu_doc_create",
            "feishu_doc_append",
            "feishu_doc_share",
            "feishu_doc_delete",
            "feishu_sheet_info",
            "feishu_sheet_read",
            "feishu_base_app_create",
            "feishu_base_field_list",
            "feishu_base_field_create",
            "feishu_base_table_list",
            "feishu_base_record_list",
            "feishu_base_record_upload_attachment",
            "feishu_base_record_upsert",
            "feishu_base_record_delete",
            "feishu_approval_create",
            "feishu_approval_definition",
            "feishu_approval_query",
            "feishu_approval_get",
            "feishu_task_comment",
            "feishu_task_complete",
            "feishu_task_create",
            "feishu_task_list",
            "feishu_calendar_list",
            "feishu_calendar_create",
            "feishu_calendar_update",
            "feishu_calendar_delete",
        ),
    ),
    RuntimeToolGroupSpec(
        name="plaza_pack",
        summary="Shared plaza feed tools for reading posts, publishing posts, and comments in the public collaboration feed.",
        source="system",
        activation_mode="Activate on demand for shared public collaboration feed workflows.",
        tools=(
            "plaza_get_new_posts",
            "plaza_create_post",
            "plaza_add_comment",
        ),
    ),
    RuntimeToolGroupSpec(
        name="email_pack",
        summary="Email sending, reading, and replying through SMTP/IMAP connections.",
        source="system",
        activation_mode="Discover schemas through tool_search; load the email guide skill only when method guidance is needed.",
        tools=("send_email", "read_emails", "reply_email"),
    ),
    RuntimeToolGroupSpec(
        name="mcp_admin_pack",
        summary="MCP resource discovery, server import, tool inspection, tool calls, and resource reading.",
        source="mcp",
        activation_mode="Enable explicitly only for platform extension or external capability installation workflows.",
        tools=(
            "discover_resources",
            "import_mcp_server",
            "list_mcp_tools",
            "inspect_mcp_tool",
            "call_mcp_tool",
            "mcp_list_resources",
            "mcp_read_resource",
        ),
    ),
    RuntimeToolGroupSpec(
        name="office_pack",
        summary="Office productivity tools for DOCX, XLSX, PPTX, PDF, meeting notes, weekly reports, and pitch decks.",
        source="system",
        activation_mode=(
            "Discover schemas through tool_search; load the Office Productivity skill only when method guidance is needed."
        ),
        tools=(
            "read_document",
            "office_document_create",
            "office_document_view",
            "office_document_query",
            "office_document_apply",
            "office_document_validate",
            "office_document_dump",
        ),
        infer_from_tools=False,
    ),
    RuntimeToolGroupSpec(
        name="deep_research_pack",
        summary="Dedicated Deep Research tools for planning, retrieval, extraction, evidence ledgers, evaluation, and report generation.",
        source="system",
        activation_mode=(
            "Discover dedicated deep_research_* schemas through tool_search; load the Deep Research skill only when "
            "method guidance is needed."
        ),
        tools=(
            "deep_research_run",
            "deep_research_start",
            "deep_research_check",
            "deep_research_cancel",
            "deep_research_export",
        ),
        infer_from_tools=False,
    ),
    RuntimeToolGroupSpec(
        name="command_pack",
        summary=(
            "CC/Codex-style command-layer tools for session task bookkeeping, runtime task output/stop, "
            "bounded goals, enterable teams, and advanced planning handoff."
        ),
        source="system",
        activation_mode=(
            "Discover schemas through tool_search when a user asks for slash-command-like Task, Team, Goal, "
            "or Advanced Plan behavior; TaskCreate/TaskUpdate are Work Ledger bookkeeping and never start execution."
        ),
        tools=(
            "task_create",
            "task_update",
            "task_list",
            "task_get",
            "task_output",
            "task_stop",
            "goal_start",
            "team_create",
            "advanced_plan",
            "verify_plan",
        ),
        infer_from_tools=False,
    ),
)

_ADMIN_PACK_QUERY_KEYWORDS = (
    "mcp",
    "server",
    "servers",
    "resource",
    "resources",
    "import",
    "oauth",
    "smithery",
    "modelscope",
)


def normalize_tool_query(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value.strip().lower())


def _matches_runtime_query(query: str, candidate: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return True
    candidate_lower = candidate.lower()
    if normalized in candidate_lower:
        return True
    compact_query = normalize_tool_query(normalized)
    return bool(compact_query and compact_query in normalize_tool_query(candidate_lower))


def _query_targets_admin_pack(query: str) -> bool:
    normalized = query.strip().lower()
    if not normalized:
        return False
    return any(keyword in normalized for keyword in _ADMIN_PACK_QUERY_KEYWORDS)


def iter_runtime_tool_groups(query: str = "") -> tuple[RuntimeToolGroupSpec, ...]:
    normalized = query.strip().lower()
    if not normalized:
        return tuple(pack for pack in RUNTIME_TOOL_GROUPS if pack.source != "mcp")
    return tuple(
        pack
        for pack in RUNTIME_TOOL_GROUPS
        if (pack.source != "mcp" or _query_targets_admin_pack(normalized))
        if _matches_runtime_query(normalized, pack.name)
        or _matches_runtime_query(normalized, pack.summary)
        or any(_matches_runtime_query(normalized, tool) for tool in pack.tools)
    )


def runtime_tool_group_for_name(name: str) -> RuntimeToolGroupSpec | None:
    for pack in RUNTIME_TOOL_GROUPS:
        if pack.name == name:
            return pack
    return None


def static_runtime_tool_group_names_for_tool(tool_name: str) -> tuple[str, ...]:
    return tuple(pack.name for pack in RUNTIME_TOOL_GROUPS if pack.infer_from_tools and tool_name in pack.tools)


def infer_static_runtime_tool_group_names(tool_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for tool_name in tool_names:
        for pack_name in static_runtime_tool_group_names_for_tool(tool_name):
            if pack_name not in seen:
                names.append(pack_name)
                seen.add(pack_name)
    return tuple(names)
