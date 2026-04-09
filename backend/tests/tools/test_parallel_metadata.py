"""Tests for parallel/read-only metadata on ToolDefinition and ToolRegistry."""

from __future__ import annotations

from app.tools.registry import ToolRegistry


def _make_openai_tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Tool: {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _build_registry(*names: str) -> ToolRegistry:
    return ToolRegistry.from_openai_tools([_make_openai_tool(n) for n in names])


def test_read_file_is_parallel_safe():
    registry = _build_registry("read_file")
    tool = registry.get("read_file")
    assert tool.parallel_safe is True


def test_read_file_is_read_only():
    registry = _build_registry("read_file")
    tool = registry.get("read_file")
    assert tool.read_only is True


def test_write_file_is_not_parallel_safe():
    registry = _build_registry("write_file")
    tool = registry.get("write_file")
    assert tool.parallel_safe is False


def test_write_file_is_not_read_only():
    registry = _build_registry("write_file")
    tool = registry.get("write_file")
    assert tool.read_only is False


def test_is_parallel_safe_method():
    registry = _build_registry("read_file", "write_file")
    assert registry.is_parallel_safe("read_file") is True
    assert registry.is_parallel_safe("write_file") is False


def test_is_read_only_method():
    registry = _build_registry("read_file", "write_file")
    assert registry.is_read_only("read_file") is True
    assert registry.is_read_only("write_file") is False


def test_unknown_tool_not_parallel_safe():
    registry = _build_registry("read_file")
    assert registry.is_parallel_safe("unknown_tool") is False


def test_unknown_tool_not_read_only():
    registry = _build_registry("read_file")
    assert registry.is_read_only("unknown_tool") is False


def test_all_parallel_safe_tools():
    parallel_names = [
        "read_file",
        "glob_search",
        "grep_search",
        "read_document",
        "list_files",
        "list_triggers",
        "web_search",
        "web_fetch",
        "firecrawl_fetch",
        "xcrawl_scrape",
    ]
    non_parallel_names = ["write_file", "edit_file", "delete_file", "execute_code"]
    registry = _build_registry(*(parallel_names + non_parallel_names))

    for name in parallel_names:
        assert registry.is_parallel_safe(name) is True, f"{name} should be parallel_safe"
        assert registry.is_read_only(name) is True, f"{name} should be read_only"

    for name in non_parallel_names:
        assert registry.is_parallel_safe(name) is False, f"{name} should NOT be parallel_safe"
        assert registry.is_read_only(name) is False, f"{name} should NOT be read_only"


def test_tool_search_is_read_only_but_not_parallel_safe():
    registry = _build_registry("tool_search")
    assert registry.is_read_only("tool_search") is True
    assert registry.is_parallel_safe("tool_search") is False
