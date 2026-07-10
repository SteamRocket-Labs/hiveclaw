"""P1-W2-6 — active capability packs section size discipline.

A single bloated pack (e.g. feishu with 30+ tools) used to spend ~600
chars per pack on an enumerable tool list the model never reads
verbatim. The section now:
  - caps each summary to 100 chars (single line)
  - shows the first 5 tools then "(+N more)"
  - has a default budget of 1200 chars (was 2000)

These tests pin those invariants so future contributors can't quietly
re-bloat the section.
"""

from __future__ import annotations

import re

from app.runtime.prompt_sections.active_tool_groups import (
    _DEFAULT_BUDGET_CHARS,
    _SUMMARY_MAX_CHARS,
    _TOOLS_PREVIEW_COUNT,
    build_active_tool_groups_section,
)
from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


# ── Constants pinned ──────────────────────────────────────────


def test_default_budget_is_1200_chars() -> None:
    assert _DEFAULT_BUDGET_CHARS == 1200


def test_summary_capped_at_100_chars() -> None:
    assert _SUMMARY_MAX_CHARS == 100


def test_tools_preview_capped_at_5() -> None:
    assert _TOOLS_PREVIEW_COUNT == 5


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


def test_long_summary_is_trimmed_to_cap_with_ellipsis() -> None:
    long_summary = "A" * 200
    section = build_active_tool_groups_section([{"name": "p", "summary": long_summary, "tools": []}])
    head_line = next(line for line in section.splitlines() if line.startswith("- p"))
    # `- p: ` (5 chars) + summary body (≤100, ending with ellipsis)
    assert len(head_line) <= 5 + _SUMMARY_MAX_CHARS
    assert head_line.endswith("…")


def test_summary_collapses_whitespace() -> None:
    """Multi-line summary must not split the bullet across lines."""
    section = build_active_tool_groups_section([{"name": "p", "summary": "line1\nline2\n  line3", "tools": []}])
    assert "- p: line1 line2 line3" in section


def test_tools_list_truncated_with_remainder_count() -> None:
    """30-tool feishu-style pack should expose 5 + count, not the full list."""
    tools = [f"tool_{i}" for i in range(30)]
    section = build_active_tool_groups_section([{"name": "feishu", "tools": tools}])
    assert "tool_0, tool_1, tool_2, tool_3, tool_4 (+25 more)" in section
    # Verify no later tool name leaks through.
    assert "tool_15" not in section
    assert "tool_29" not in section


def test_pack_without_summary_omits_colon() -> None:
    section = build_active_tool_groups_section([{"name": "p", "tools": ["a"]}])
    head_line = next(line for line in section.splitlines() if line.startswith("- p"))
    assert head_line == "- p"


def test_section_respects_explicit_budget_with_truncation_marker() -> None:
    packs = [{"name": f"pack_{i}", "summary": "x" * 80, "tools": [f"t_{j}" for j in range(10)]} for i in range(20)]
    section = build_active_tool_groups_section(packs, budget_chars=300)
    assert len(section) <= 300
    assert section.rstrip().endswith("...(trimmed)")


def test_total_size_for_typical_three_packs_stays_under_500_chars() -> None:
    """Three realistic packs with full summaries + toolsets should fit
    well under 500 chars — the cap-and-preview logic is what makes that
    possible. Catches regressions where someone re-enumerates tools."""
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
    assert len(section) < 500


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
