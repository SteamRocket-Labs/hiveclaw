"""Tool surface equivalence and metadata propagation tests."""

from __future__ import annotations


def test_combined_openai_tools_matches_canonical_surface():
    """The collected tool surface should expose the canonical builtin tool set."""
    from app.services.agent_tools import get_combined_openai_tools

    combined = get_combined_openai_tools()
    combined_names = {t["function"]["name"] for t in combined}

    assert combined_names == {
        "cancel_trigger",
        "create_digital_employee",
        "delete_file",
        "discover_resources",
        "delegate_to_agent",
        "edit_file",
        "execute_code",
        "run_command",
        "feishu_calendar_create",
        "feishu_calendar_delete",
        "feishu_calendar_list",
        "feishu_calendar_update",
        "feishu_doc_append",
        "feishu_doc_create",
        "feishu_doc_read",
        "feishu_doc_share",
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
        "check_async_task",
        "cancel_async_task",
        "firecrawl_fetch",
        "get_current_time",
        "list_mcp_resources",
        "list_async_tasks",
        "read_mcp_resource",
        "list_files",
        "list_triggers",
        "load_skill",
        "save_skill",
        "plaza_add_comment",
        "plaza_create_post",
        "plaza_get_new_posts",
        "preview_agent_blueprint",
        "read_document",
        "read_emails",
        "read_file",
        "reply_email",
        "save_memory",
        "search_clawhub",
        "search_memory",
        "send_channel_file",
        "send_email",
        "send_feishu_message",
        "send_message_to_agent",
        "send_web_message",
        "set_trigger",
        "tool_search",
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
    assert "save_memory" in SAFE_TOOLS
    assert "save_skill" in SAFE_TOOLS
    assert "search_memory" in SAFE_TOOLS
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
    assert "read_file" in PARALLEL_SAFE_TOOL_NAMES
    assert "xcrawl_scrape" in PARALLEL_SAFE_TOOL_NAMES
    assert "search_memory" in PARALLEL_SAFE_TOOL_NAMES


def test_alias_metadata_available_without_runtime_registry_init():
    """Alias read-only/parallel-safe metadata should not depend on runtime init side effects."""
    from app.tools.registry import is_parallel_safe_tool, is_read_only_tool

    assert is_parallel_safe_tool("bing_search")
    assert is_read_only_tool("bing_search")
