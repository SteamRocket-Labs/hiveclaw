"""Step 1 tool-contract invariants.

Single-source read_only/parallel_safe classification (decorator only), the new
ToolMeta.destructive flag (concurrency defense), and per-tool
ToolMeta.max_result_chars eviction (replaces the hardcoded _EVICTION_EXEMPT_TOOLS
set). All classification now flows from @tool decorators — no parallel static
name lists.
"""

from __future__ import annotations


def test_static_classification_lists_removed():
    """The hardcoded _STATIC_READ_ONLY / _STATIC_PARALLEL_SAFE drifting name
    lists are gone — classification is single-sourced from @tool decorators."""
    import app.tools.registry as registry

    assert not hasattr(registry, "_STATIC_READ_ONLY_TOOL_NAMES")
    assert not hasattr(registry, "_STATIC_PARALLEL_SAFE_TOOL_NAMES")


def test_classification_is_decorator_sourced():
    from app.tools.collector import collect_tools
    from app.tools.registry import PARALLEL_SAFE_TOOL_NAMES, READ_ONLY_TOOL_NAMES

    c = collect_tools()
    assert set(READ_ONLY_TOOL_NAMES) == set(c.read_only_names)
    assert set(PARALLEL_SAFE_TOOL_NAMES) == set(c.parallel_safe_names)
    # sanity: classification is non-trivial
    assert "read_file" in READ_ONLY_TOOL_NAMES
    assert "web_search" in PARALLEL_SAFE_TOOL_NAMES


def test_destructive_flag_collected_and_queryable():
    from app.tools.registry import is_destructive_tool

    for name in ("delete_file", "retire_memory", "feishu_doc_delete"):
        assert is_destructive_tool(name), name
    for name in ("read_file", "web_search", "list_files"):
        assert not is_destructive_tool(name), name


def test_destructive_tool_never_concurrency_safe():
    """A destructive tool must never run concurrently even if it carried a
    parallel_safe flag (CC isDestructive parity / concurrency defense)."""
    from app.kernel.engine import _is_concurrency_safe_tool

    assert _is_concurrency_safe_tool("read_file") is True
    assert _is_concurrency_safe_tool("delete_file") is False  # destructive
    assert _is_concurrency_safe_tool("write_file") is False  # not parallel_safe


def test_max_result_chars_unlimited_for_read_tools():
    from app.tools.decorator import RESULT_CHARS_UNLIMITED
    from app.tools.registry import result_char_limit_for_tool

    assert result_char_limit_for_tool("read_file") == RESULT_CHARS_UNLIMITED
    assert result_char_limit_for_tool("read_document") == RESULT_CHARS_UNLIMITED
    # an unset tool returns None (caller falls back to the global default)
    assert result_char_limit_for_tool("send_feishu_message") is None


def test_eviction_threshold_resolution():
    from app.kernel.engine import _TOOL_RESULT_EVICTION_THRESHOLD, _resolve_eviction_threshold

    # unlimited tools never evict
    assert _resolve_eviction_threshold("read_file") is None
    # unset tools use the global default
    assert _resolve_eviction_threshold("send_feishu_message") == _TOOL_RESULT_EVICTION_THRESHOLD


def test_declared_tool_result_threshold_is_clamped(monkeypatch):
    from app.kernel import engine
    from app.kernel.engine import _TOOL_RESULT_EVICTION_THRESHOLD, _resolve_eviction_threshold

    monkeypatch.setattr(engine, "result_char_limit_for_tool", lambda _name: _TOOL_RESULT_EVICTION_THRESHOLD * 20)

    assert _resolve_eviction_threshold("oversized_tool") == _TOOL_RESULT_EVICTION_THRESHOLD


def test_eviction_replaces_legacy_exempt_set():
    """Every tool formerly in the hardcoded _EVICTION_EXEMPT_TOOLS set now
    resolves to unlimited via its decorator (single source)."""
    from app.kernel.engine import _resolve_eviction_threshold

    former_exempt = [
        "list_files",
        "read_file",
        "load_skill",
        "tool_search",
        "fs_read",
        "fs_list",
        "discover_resources",
        "list_triggers",
        "get_current_time",
        "check_async_task",
        "list_async_tasks",
        "web_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "read_document",
    ]
    for name in former_exempt:
        assert _resolve_eviction_threshold(name) is None, name


def test_read_file_result_not_evicted_but_default_tool_is():
    from app.kernel.engine import _TOOL_RESULT_EVICTION_THRESHOLD, _maybe_evict_tool_result

    big = "x" * (_TOOL_RESULT_EVICTION_THRESHOLD + 1000)
    # read_file is unlimited → kept inline
    assert _maybe_evict_tool_result("read_file", "tc1", big) == big
    # a tool with no max_result_chars uses the default threshold → evicted
    evicted = _maybe_evict_tool_result("send_feishu_message", "tc2", big)
    assert evicted != big
    assert "truncated" in evicted or "saved to" in evicted
