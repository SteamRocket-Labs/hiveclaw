"""Tool surface equivalence and metadata propagation tests."""

from __future__ import annotations


def test_combined_openai_tools_matches_registered_builtin_surface():
    """The collected tool surface exposes the registered builtin schemas.

    Turn-1/minimal visibility is enforced later by CORE_TOOL_NAMES, requested
    schema expansion, pack policy, and provider-availability filtering.
    """
    from app.services.agent_tools import get_combined_openai_tools

    combined = get_combined_openai_tools()
    combined_names = {t["function"]["name"] for t in combined}

    assert combined_names == {
        "anysearch_batch_search",
        "anysearch_extract",
        "anysearch_get_sub_domains",
        "anysearch_search",
        "cancel_trigger",
        "create_digital_employee",
        "deep_research_cancel",
        "deep_research_check",
        "deep_research_export",
        "deep_research_run",
        "deep_research_start",
        "delete_file",
        "discover_resources",
        "delegate_to_agent",
        "edit_file",
        "execute_code",
        "exa_search",
        "exit_plan_mode",
        "ask_user_question",
        "request_plan_mode",
        "run_command",
        "spawn_subagent",
        "check_subagent",
        "preview_workflow",
        "start_workflow",
        "feishu_approval_create",
        "feishu_approval_definition",
        "feishu_approval_get",
        "feishu_approval_query",
        "feishu_base_app_create",
        "feishu_base_field_create",
        "feishu_base_record_delete",
        "feishu_calendar_create",
        "feishu_calendar_delete",
        "feishu_calendar_list",
        "feishu_calendar_update",
        "feishu_doc_append",
        "feishu_doc_create",
        "feishu_doc_delete",
        "feishu_doc_read",
        "feishu_doc_share",
        "feishu_url_resolve",
        "feishu_url_read",
        "feishu_drive_file_read",
        "feishu_base_field_list",
        "feishu_base_record_list",
        "feishu_base_record_upload_attachment",
        "feishu_base_record_upsert",
        "feishu_base_table_list",
        "feishu_sheet_info",
        "feishu_sheet_read",
        "feishu_task_comment",
        "feishu_task_complete",
        "feishu_task_create",
        "feishu_task_list",
        "feishu_user_search",
        "feishu_wiki_list",
        "glob_search",
        "grep_search",
        "import_mcp_server",
        "call_mcp_tool",
        "check_async_task",
        "cancel_async_task",
        "firecrawl_fetch",
        "get_current_time",
        "list_mcp_tools",
        "inspect_mcp_tool",
        "mcp_list_resources",
        "mcp_read_resource",
        "list_async_tasks",
        "list_files",
        "fs_read",
        "fs_write",
        "fs_list",
        "list_triggers",
        "load_memory",
        "load_skill",
        "office_document_apply",
        "office_document_create",
        "office_document_dump",
        "office_document_query",
        "office_document_validate",
        "office_document_view",
        "pin_skill",
        "save_skill",
        "plaza_add_comment",
        "plaza_create_post",
        "plaza_get_new_posts",
        "preview_agent_blueprint",
        "read_document",
        "read_emails",
        "read_file",
        "read_ledger",
        "record_finding",
        "reply_email",
        "retire_memory",
        "save_memory",
        "search_clawhub",
        "search_memory",
        "send_channel_file",
        "send_channel_message",
        "send_email",
        "send_feishu_message",
        "send_message_to_agent",
        "send_web_message",
        "set_trigger",
        "submit_t3_consolidation_pitch",
        "submit_t3_memory_gate_review",
        "submit_t3_revised_patch",
        "tavily_search",
        "tool_search",
        "track_todo",
        "update_memory",
        "update_trigger",
        "upload_image",
        "web_fetch",
        "web_search",
        "write_file",
        "xcrawl_scrape",
    }


def test_combined_has_no_duplicates():
    """No duplicate tool names in the combined list."""
    from app.services.agent_tools import get_combined_openai_tools

    combined = get_combined_openai_tools()
    names = [t["function"]["name"] for t in combined]
    assert len(names) == len(set(names)), f"Duplicates: {[n for n in names if names.count(n) > 1]}"
    assert "web_search" in names


def test_governance_sets_include_canonical_metadata_without_runtime_init():
    """SAFE_TOOLS and SENSITIVE_TOOLS should reflect the canonical tool metadata."""
    from app.tools.governance import SAFE_TOOLS, SENSITIVE_TOOLS

    assert "list_files" in SAFE_TOOLS
    assert "read_file" in SAFE_TOOLS
    assert "web_search" in SAFE_TOOLS
    assert "web_fetch" in SAFE_TOOLS
    assert "search_memory" in SAFE_TOOLS
    assert "load_memory" in SAFE_TOOLS
    assert "feishu_approval_definition" in SAFE_TOOLS
    assert "read_ledger" in SAFE_TOOLS
    # F-2: list_tasks/get_task (safe) and manage_tasks (sensitive) were retired
    # from the agent tool face — the agent board is Work Ledger only.
    assert "list_tasks" not in SAFE_TOOLS
    assert "get_task" not in SAFE_TOOLS
    assert "manage_tasks" not in SENSITIVE_TOOLS
    assert "save_memory" in SENSITIVE_TOOLS
    assert "save_skill" in SENSITIVE_TOOLS
    assert "send_feishu_message" in SENSITIVE_TOOLS
    assert "feishu_task_comment" in SENSITIVE_TOOLS
    assert "feishu_task_complete" in SENSITIVE_TOOLS
    assert "feishu_task_create" in SENSITIVE_TOOLS
    assert "feishu_base_record_upload_attachment" in SENSITIVE_TOOLS
    assert "feishu_base_record_upsert" in SENSITIVE_TOOLS
    assert "delete_file" in SENSITIVE_TOOLS
    assert "create_digital_employee" in SENSITIVE_TOOLS


def test_read_only_and_parallel_safe_sets_include_canonical_metadata_without_runtime_init():
    """READ_ONLY and PARALLEL_SAFE metadata should be available without runtime init."""
    from app.tools.registry import READ_ONLY_TOOL_NAMES, PARALLEL_SAFE_TOOL_NAMES

    assert "read_file" in READ_ONLY_TOOL_NAMES
    assert "web_search" in READ_ONLY_TOOL_NAMES
    assert "web_fetch" in READ_ONLY_TOOL_NAMES
    assert "firecrawl_fetch" in READ_ONLY_TOOL_NAMES
    assert "discover_resources" in READ_ONLY_TOOL_NAMES
    assert "search_memory" in READ_ONLY_TOOL_NAMES
    assert "load_memory" in READ_ONLY_TOOL_NAMES
    assert "feishu_approval_definition" in READ_ONLY_TOOL_NAMES
    assert "read_file" in PARALLEL_SAFE_TOOL_NAMES
    assert "xcrawl_scrape" in PARALLEL_SAFE_TOOL_NAMES
    assert "search_memory" in PARALLEL_SAFE_TOOL_NAMES
    assert "load_memory" in PARALLEL_SAFE_TOOL_NAMES


def test_alias_metadata_available_without_runtime_registry_init():
    """Alias read-only/parallel-safe metadata should not depend on runtime init side effects."""
    from app.tools.registry import is_parallel_safe_tool, is_read_only_tool

    assert is_parallel_safe_tool("bing_search")
    assert is_read_only_tool("bing_search")
