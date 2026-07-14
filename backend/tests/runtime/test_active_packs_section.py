"""Active capability groups must remain complete in model-visible context."""

from __future__ import annotations

import re

from app.runtime.prompt_sections.active_tool_groups import build_active_tool_groups_section
from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


# ── Behaviour ─────────────────────────────────────────────────


def test_empty_packs_returns_empty_string() -> None:
    assert build_active_tool_groups_section([]) == ""


def test_short_pack_renders_inline_summary_and_tools() -> None:
    section = build_active_tool_groups_section(
        [{"name": "web", "summary": "Web search", "tools": ["web_search", "firecrawl_fetch"]}]
    )
    assert "## Active Runtime Tool Groups" in section
    assert "- web: Web search" in section
    assert "Tools: web_search, firecrawl_fetch" in section
    assert "+0 more" not in section  # no remainder marker when nothing trimmed


def test_long_summary_is_preserved() -> None:
    long_summary = "A" * 200 + " END_OF_SUMMARY"
    section = build_active_tool_groups_section([{"name": "p", "summary": long_summary, "tools": []}])
    assert long_summary in section


def test_summary_collapses_whitespace() -> None:
    """Multi-line summary must not split the bullet across lines."""
    section = build_active_tool_groups_section([{"name": "p", "summary": "line1\nline2\n  line3", "tools": []}])
    assert "- p: line1 line2 line3" in section


def test_tools_list_preserves_every_callable_name() -> None:
    tools = [f"tool_{i}" for i in range(30)]
    section = build_active_tool_groups_section([{"name": "feishu", "tools": tools}])
    assert all(tool in section for tool in tools)


def test_pack_without_summary_omits_colon() -> None:
    section = build_active_tool_groups_section([{"name": "p", "tools": ["a"]}])
    head_line = next(line for line in section.splitlines() if line.startswith("- p"))
    assert head_line == "- p"


def test_section_budget_is_advisory_and_does_not_remove_model_visible_groups() -> None:
    packs = [{"name": f"pack_{i}", "summary": "x" * 80, "tools": [f"t_{j}" for j in range(10)]} for i in range(20)]
    section = build_active_tool_groups_section(packs, budget_chars=300)
    assert "pack_0" in section
    assert "pack_19" in section
    assert "t_9" in section
    assert "trimmed" not in section


def test_typical_three_packs_preserve_the_complete_tool_surface() -> None:
    packs = [
        {
            "name": "web",
            "summary": "Web search and crawl tools",
            "tools": ["web_search", "firecrawl_fetch", "xcrawl_scrape"],
        },
        {"name": "feishu", "summary": "Feishu office suite", "tools": [f"feishu_op_{i}" for i in range(35)]},
        {"name": "email", "summary": "SMTP/IMAP email", "tools": ["smtp_send", "imap_fetch", "imap_search"]},
    ]
    section = build_active_tool_groups_section(packs)
    assert "feishu_op_34" in section
    assert "imap_search" in section


def test_runtime_tool_group_prompt_metadata_is_english_only() -> None:
    for group in RUNTIME_TOOL_GROUPS:
        assert not _HAN_RE.search(group.summary), group.name
        assert not _HAN_RE.search(group.activation_mode), group.name


def test_real_runtime_tool_group_section_is_english_only() -> None:
    section = build_active_tool_groups_section(
        [
            {"name": group.name, "summary": group.summary, "tools": list(group.tools)}
            for group in RUNTIME_TOOL_GROUPS
            if group.source != "mcp"
        ]
    )

    assert not _HAN_RE.search(section)
