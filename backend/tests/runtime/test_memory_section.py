"""Tests for prompt_sections/memory.py (PR-8).

Guards the prompt text that agents actually read against two historical drifts:
- The "FTS5" wording that lingered after the SQLite shadow store was retired.
- The soft "use sparingly" guidance on save_memory that didn't warn agents
  they were bypassing the entire curation pipeline.
"""

from __future__ import annotations

import pytest

from app.runtime.prompt_sections.memory import build_memory_section


class TestMemorySectionProperties:
    def test_does_not_mention_fts5(self) -> None:
        """SQLite FTS5 was retired; any mention of it in the prompt is a bug."""
        out = build_memory_section()
        assert "FTS5" not in out
        assert "fts5" not in out.lower()

    def test_search_memory_describes_progressive_disclosure(self) -> None:
        """Agents need to filter by ID before expanding full memories."""
        out = build_memory_section()
        assert "save_memory" in out  # tools documented
        assert "search_memory" in out
        assert "load_memory" in out
        assert "id=" in out

    def test_save_memory_warns_about_bypass(self) -> None:
        """Prompt must call save_memory an escape hatch that bypasses curation."""
        out = build_memory_section()
        assert "Escape hatch" in out or "escape hatch" in out
        assert "bypass" in out.lower()

    def test_save_memory_lists_imperative_triggers(self) -> None:
        """Give agents concrete user signals so they know WHEN to invoke it."""
        out = build_memory_section()
        # One of the direct-imperative cues must be present (Chinese or English).
        assert ("记住" in out) or ("remember this" in out.lower())

    def test_documents_automatic_pipeline(self) -> None:
        """Agent should understand everything else flows automatically."""
        out = build_memory_section()
        # T0 → T2 → T3 pipeline description.
        assert "T0" in out and "T2" in out and "T3" in out
        assert "heartbeat" in out.lower()

    def test_renders_embedded_snapshot(self) -> None:
        out = build_memory_section("- [feedback] user prefers concise answers")
        assert "### Current Memory State" in out
        assert "user prefers concise answers" in out

    def test_empty_snapshot_uses_placeholder(self) -> None:
        out = build_memory_section("")
        assert "(no memory loaded)" in out


class TestMemorySectionRegressionAgainstOldWording:
    """Freeze the old misleading phrases so they cannot regress in."""

    OLD_PHRASES = [
        "Search T3 via FTS5",
        "Directly write to T3 (use sparingly, heartbeat handles most curation)",
    ]

    @pytest.mark.parametrize("phrase", OLD_PHRASES)
    def test_old_phrase_absent(self, phrase: str) -> None:
        assert phrase not in build_memory_section()
