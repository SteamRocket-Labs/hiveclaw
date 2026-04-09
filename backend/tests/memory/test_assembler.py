"""Tests for the memory assembler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.memory.assembler import MemoryAssembler, _freshness_suffix
from app.memory.types import MemoryItem, MemoryKind


def _make_item(
    kind: MemoryKind, content: str, score: float = 0.5, **metadata
) -> MemoryItem:
    return MemoryItem(kind=kind, content=content, score=score, source="test", metadata=metadata)


class TestAssembleGroupsByKind:
    """Output has section headers in correct order."""

    def test_all_sections_present(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.WORKING, "Current focus: deploy v2"),
            _make_item(MemoryKind.EPISODIC, "Previously discussed auth flow"),
            _make_item(MemoryKind.EXTERNAL, "Viking: project architecture doc"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "[Working Memory]" in result
        assert "[Episodic Memory]" in result
        assert "[Semantic Memory]" in result
        assert "[External Memory]" in result

    def test_section_order(self) -> None:
        """Working -> Episodic -> Semantic -> External."""
        items = [
            _make_item(MemoryKind.EXTERNAL, "external fact"),
            _make_item(MemoryKind.SEMANTIC, "semantic fact"),
            _make_item(MemoryKind.WORKING, "working focus"),
            _make_item(MemoryKind.EPISODIC, "episodic summary"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        working_pos = result.index("[Working Memory]")
        episodic_pos = result.index("[Episodic Memory]")
        semantic_pos = result.index("[Semantic Memory]")
        external_pos = result.index("[External Memory]")

        assert working_pos < episodic_pos < semantic_pos < external_pos

    def test_empty_items(self) -> None:
        assembler = MemoryAssembler()
        result = assembler.assemble([])
        assert result == ""

    def test_single_kind(self) -> None:
        items = [_make_item(MemoryKind.SEMANTIC, "fact one"), _make_item(MemoryKind.SEMANTIC, "fact two")]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "[Semantic Memory]" in result
        assert "- fact one" in result
        assert "- fact two" in result
        assert "[Working Memory]" not in result

    def test_working_memory_no_bullet(self) -> None:
        """Working memory content is rendered without bullet prefix."""
        items = [_make_item(MemoryKind.WORKING, "Current focus: ship feature")]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "Current focus: ship feature" in result
        assert "- Current focus" not in result

    def test_higher_score_items_render_first_within_section(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "low score fact", score=0.2),
            _make_item(MemoryKind.SEMANTIC, "high score fact", score=0.9),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.index("high score fact") < result.index("low score fact")


class TestAssembleBudgetTrim:
    """Output respects budget_chars limit."""

    def test_trim_to_budget(self) -> None:
        items = [_make_item(MemoryKind.SEMANTIC, f"fact number {i} with extra padding words") for i in range(100)]
        assembler = MemoryAssembler()
        result = assembler.assemble(items, budget_chars=200)

        assert len(result) <= 250  # header + some slack for section header
        assert "[Semantic Memory]" in result

    def test_small_budget_still_produces_output(self) -> None:
        items = [_make_item(MemoryKind.WORKING, "short")]
        assembler = MemoryAssembler()
        result = assembler.assemble(items, budget_chars=50)

        assert "[Working Memory]" in result
        assert "short" in result

    def test_budget_prioritizes_earlier_sections(self) -> None:
        """When budget is tight, working memory (first) gets included over external (last)."""
        items = [
            _make_item(MemoryKind.WORKING, "A" * 100),
            _make_item(MemoryKind.EXTERNAL, "B" * 100),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items, budget_chars=120)

        assert "[Working Memory]" in result
        # External may be trimmed out due to budget
        assert "B" * 100 not in result

    def test_budget_keeps_highest_scored_items_first(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "very long but high score " + ("A" * 80), score=0.95),
            _make_item(MemoryKind.SEMANTIC, "very long but low score " + ("B" * 80), score=0.10),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items, budget_chars=120)

        assert "very long but high score" in result
        assert "very long but low score" not in result


class TestAssembleDedup:
    """Duplicate items are removed."""

    def test_exact_duplicates_removed(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.count("User prefers dark mode") == 1

    def test_case_insensitive_dedup(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.SEMANTIC, "user prefers dark mode"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        # Both normalize to same hash, so only one kept
        count = result.lower().count("user prefers dark mode")
        assert count == 1

    def test_different_content_preserved(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "fact alpha"),
            _make_item(MemoryKind.SEMANTIC, "fact beta"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "fact alpha" in result
        assert "fact beta" in result

    def test_cross_kind_dedup(self) -> None:
        """Same content in different kinds is still deduplicated."""
        items = [
            _make_item(MemoryKind.SEMANTIC, "shared fact"),
            _make_item(MemoryKind.EXTERNAL, "shared fact"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.count("shared fact") == 1


class TestFreshnessSuffix:
    """_freshness_suffix appends age warnings for stale memories."""

    def test_no_timestamp_returns_empty(self) -> None:
        item = _make_item(MemoryKind.SEMANTIC, "fact")
        assert _freshness_suffix(item) == ""

    def test_none_timestamp_returns_empty(self) -> None:
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=None)
        assert _freshness_suffix(item) == ""

    def test_recent_memory_no_warning(self) -> None:
        now_iso = datetime.now(UTC).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=now_iso)
        assert _freshness_suffix(item) == ""

    def test_old_memory_gets_warning(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=old)
        suffix = _freshness_suffix(item)
        assert "10d ago" in suffix
        assert "verify before acting" in suffix

    def test_exactly_seven_days_no_warning(self) -> None:
        seven_days = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=seven_days)
        assert _freshness_suffix(item) == ""

    def test_eight_days_has_warning(self) -> None:
        eight_days = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=eight_days)
        assert "8d ago" in _freshness_suffix(item)

    def test_naive_datetime_does_not_crash(self) -> None:
        """Raw naive datetime in metadata should not raise TypeError."""
        naive_old = datetime.now() - timedelta(days=10)
        item = MemoryItem(
            kind=MemoryKind.SEMANTIC, content="fact", score=0.5,
            source="test", metadata={"timestamp": naive_old},
        )
        suffix = _freshness_suffix(item)
        # Age may vary by 1 day depending on time-of-day and tz offset
        assert "d ago" in suffix
        assert "verify before acting" in suffix


class TestAssembleFreshnessIntegration:
    """Full assembler renders freshness warnings on stale items."""

    def test_stale_semantic_gets_warning_in_output(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        items = [_make_item(MemoryKind.SEMANTIC, "old fact", timestamp=old)]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)
        assert "10d ago" in result
        assert "verify before acting" in result

    def test_fresh_semantic_no_warning_in_output(self) -> None:
        now = datetime.now(UTC).isoformat()
        items = [_make_item(MemoryKind.SEMANTIC, "new fact", timestamp=now)]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)
        assert "verify before acting" not in result

    def test_working_memory_never_gets_warning(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        items = [_make_item(MemoryKind.WORKING, "focus", timestamp=old)]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)
        assert "verify before acting" not in result

    def test_category_prefix_rendered(self) -> None:
        """B-06: Non-general categories should appear as [type] prefix."""
        items = [_make_item(MemoryKind.SEMANTIC, "Always run tests", category="feedback")]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)
        assert "[feedback]" in result

    def test_general_category_no_prefix(self) -> None:
        items = [_make_item(MemoryKind.SEMANTIC, "some fact", category="general")]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)
        assert "[general]" not in result
