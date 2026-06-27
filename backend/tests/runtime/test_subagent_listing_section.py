"""Prompt-facing session-worker type listing."""

from __future__ import annotations


def test_subagent_listing_section_renders_builtin_types_and_when_to_use() -> None:
    from app.runtime.prompt_sections.subagent_listing import build_subagent_listing_section

    section = build_subagent_listing_section()

    assert "## Session Worker Types" in section
    assert "spawn_subagent" in section
    for name in ("general-purpose", "explorer", "worker", "critic"):
        assert f"`{name}`" in section

    assert "Default general-purpose session-local worker" in section
    assert "Fast read-only agent" in section
    assert "verify that work is correct" in section
    assert "not A2A employees" in section
