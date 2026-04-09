"""Tests for the tool collector — auto-discovery and data structure building."""

from __future__ import annotations

import importlib
from pathlib import Path

from app.tools.decorator import ToolMeta, clear_registry, tool
from app.tools.collector import HANDLER_MODULES, collect_tools


def setup_function():
    clear_registry()


def _register_sample_tools():
    """Register a few sample tools for testing."""

    @tool(ToolMeta(
        name="web_search",
        description="Search the web",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        category="search",
        display_name="Web Search",
        icon="\U0001f50d",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        adapter="args_only",
    ))
    async def web_search(arguments: dict) -> str:
        return f"results for {arguments['q']}"

    @tool(ToolMeta(
        name="write_file",
        description="Write a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
        category="file",
        display_name="Write File",
        governance="sensitive",
        adapter="workspace_args",
    ))
    async def write_file(workspace, arguments, tenant_id=None) -> str:
        return "written"

    @tool(ToolMeta(
        name="firecrawl_fetch",
        description="Fetch via Firecrawl",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        category="search",
        display_name="Firecrawl Fetch",
        read_only=True,
        parallel_safe=True,
        governance="safe",
        pack="web_pack",
        aliases=("bing_search",),
        adapter="args_only",
    ))
    async def firecrawl_fetch(arguments: dict) -> str:
        return "firecrawl results"


def test_collect_builds_openai_tools():
    _register_sample_tools()
    collected = collect_tools()

    names = {t["function"]["name"] for t in collected.openai_tools}
    assert "web_search" in names
    assert "write_file" in names
    assert "firecrawl_fetch" in names
    # Aliases should NOT appear as separate OpenAI tools
    assert "bing_search" not in names


def test_collect_builds_seed_list():
    _register_sample_tools()
    collected = collect_tools()

    seed_names = {s["name"] for s in collected.seed_list}
    assert "web_search" in seed_names
    assert seed_names == {"web_search", "write_file", "firecrawl_fetch"}

    web_seed = next(s for s in collected.seed_list if s["name"] == "web_search")
    assert web_seed["display_name"] == "Web Search"
    assert web_seed["category"] == "search"
    assert web_seed["icon"] == "\U0001f50d"
    assert web_seed["parameters_schema"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }


def test_collect_builds_governance_sets():
    _register_sample_tools()
    collected = collect_tools()

    assert "web_search" in collected.safe_tools
    assert "firecrawl_fetch" in collected.safe_tools
    assert "bing_search" in collected.safe_tools  # alias inherits governance
    assert "write_file" in collected.sensitive_tools
    assert "write_file" not in collected.safe_tools


def test_collect_builds_read_only_and_parallel_safe():
    _register_sample_tools()
    collected = collect_tools()

    assert "web_search" in collected.read_only_names
    assert "firecrawl_fetch" in collected.read_only_names
    assert "write_file" not in collected.read_only_names

    assert "web_search" in collected.parallel_safe_names
    assert "write_file" not in collected.parallel_safe_names


def test_collect_builds_pack_groups():
    _register_sample_tools()
    collected = collect_tools()

    assert "web_pack" in collected.pack_tool_groups
    assert set(collected.pack_tool_groups["web_pack"]) == {"web_search", "firecrawl_fetch"}
    # write_file has no pack
    assert "write_file" not in [t for tools in collected.pack_tool_groups.values() for t in tools]


def test_collect_registers_executors():
    _register_sample_tools()
    collected = collect_tools()

    # Canonical names registered
    assert collected.exec_registry._executors.get("web_search") is not None
    assert collected.exec_registry._executors.get("firecrawl_fetch") is not None
    assert collected.exec_registry._executors.get("write_file") is not None
    # Alias registered
    assert collected.exec_registry._executors.get("bing_search") is not None


def test_collect_empty_registry():
    collected = collect_tools()
    names = {tool["function"]["name"] for tool in collected.openai_tools}
    assert "load_skill" in names
    assert "web_search" in names
    assert collected.seed_list
    assert collected.safe_tools


def test_collect_real_handlers_include_memory_tools():
    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))

    collected = collect_tools()
    names = {tool["function"]["name"] for tool in collected.openai_tools}

    assert "save_skill" in names
    assert "save_memory" in names
    assert "search_memory" in names

    delegate_schema = next(tool for tool in collected.openai_tools if tool["function"]["name"] == "delegate_to_agent")
    profile_enum = delegate_schema["function"]["parameters"]["properties"]["tool_profile"]["enum"]
    assert profile_enum == ["worker_safe", "memory_readonly", "review_readonly", "research_readonly"]


def test_handler_module_manifest_matches_handlers_directory():
    actual = {
        path.stem
        for path in Path(__file__).resolve().parents[2].joinpath("app", "tools", "handlers").glob("*.py")
        if path.stem != "__init__"
    }
    expected = {module_name.rsplit(".", 1)[-1] for module_name in HANDLER_MODULES}

    assert actual == expected
