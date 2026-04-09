from __future__ import annotations


def test_builtin_tool_seed_list_includes_new_kernel_primitives():
    from app.services.tool_seeder import BUILTIN_TOOLS

    names = {tool["name"] for tool in BUILTIN_TOOLS}

    assert {"edit_file", "glob_search", "grep_search", "tool_search"}.issubset(names)


def test_builtin_tool_seed_list_tracks_combined_openai_surface():
    from app.services.agent_tools import get_combined_openai_tools
    from app.services.tool_seeder import BUILTIN_TOOLS

    combined_names = {tool["function"]["name"] for tool in get_combined_openai_tools()}
    builtin_names = {tool["name"] for tool in BUILTIN_TOOLS}

    assert builtin_names == combined_names
    assert "web_search" in builtin_names
    assert "load_skill" in builtin_names


def test_stale_builtin_cleanup_uses_current_seed_surface() -> None:
    from app.services.tool_seeder import _names_of_stale_builtin_tools

    stale = _names_of_stale_builtin_tools({"legacy_search", "web_search", "read_file"})

    assert stale == {"legacy_search"}
