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

    def test_save_memory_is_explicit_overlay_not_accepted_t3(self) -> None:
        """Prompt must explain save_memory writes overlay, not accepted T3."""
        out = build_memory_section()
        assert "Explicit memory only" in out
        assert "memory/explicit/" in out
        assert "not accepted T3" in out

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
        assert "Segment Package" in out
        assert "sealed T0 session segment" in out
        assert "heartbeat" in out.lower()
        assert "TURN_STOP" in out
        assert "SESSION_CLOSE" in out

    def test_t3_is_described_as_two_plane_layout(self) -> None:
        """Runtime guidance must not teach the retired flat-T3 four-file layout."""
        out = build_memory_section()
        for target in (
            "memory/self/self.md",
            "memory/profiles/owner.md",
            "memory/profiles/collaborators.md",
            "memory/profiles/domain.md",
            "memory/knowledge/<slug>.md",
            "memory/milestones/<slug>.md",
        ):
            assert target in out
        for retired in (
            "memory/t3/episodes.md",
            "memory/t3/user.md",
            "memory/t3/worker.md",
            "memory/t3/capabilities.md",
        ):
            assert retired not in out

    def test_trusting_recall_requires_file_claim_revalidation(self) -> None:
        out = build_memory_section()
        assert "TRUSTING_RECALL" in out
        assert "grep" in out
        assert "file" in out.lower()
        assert "function" in out.lower()
        assert "flag" in out.lower()
        assert "schema" in out.lower()

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
        "extractor picks salient bits",
        "T2 extractions",
    ]

    @pytest.mark.parametrize("phrase", OLD_PHRASES)
    def test_old_phrase_absent(self, phrase: str) -> None:
        assert phrase not in build_memory_section()


# ── C3: trims must signpost the retrieval path (docs/agent-lifecycle-cc-alignment.md 主题 C) ──


def test_memory_context_budget_is_advisory_and_preserves_decisive_tail() -> None:
    from app.runtime.prompt_sections.memory import build_memory_section

    decisive_tail = "MEMORY_DECISIVE_TAIL"
    snapshot = ("fact line\n" * 500) + decisive_tail
    section = build_memory_section(snapshot, budget_chars=300)

    assert snapshot in section
    assert decisive_tail in section
