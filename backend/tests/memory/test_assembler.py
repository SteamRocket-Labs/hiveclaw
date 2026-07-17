"""Tests for the memory assembler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.memory.assembler import MemoryAssembler, _freshness_suffix
from app.memory.types import MemoryItem, MemoryKind


def _make_item(kind: MemoryKind, content: str, score: float = 0.5, **metadata) -> MemoryItem:
    return MemoryItem(kind=kind, content=content, score=score, source="test", metadata=metadata)


class TestAssembleGroupsByKind:
    """Output has section headers in correct order."""

    def test_all_sections_present(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.EPISODIC, "Previously discussed auth flow"),
            _make_item(MemoryKind.EXTERNAL, "Viking: project architecture doc"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "[Objective Projection]" not in result
        assert "[Working Memory]" not in result
        assert "[Episodic Memory]" in result
        assert "[Semantic Memory]" in result
        assert "[External Memory]" in result

    def test_section_order(self) -> None:
        """Episodic -> Semantic -> External."""
        items = [
            _make_item(MemoryKind.EXTERNAL, "external fact"),
            _make_item(MemoryKind.SEMANTIC, "semantic fact"),
            _make_item(MemoryKind.EPISODIC, "episodic summary"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        episodic_pos = result.index("[Episodic Memory]")
        semantic_pos = result.index("[Semantic Memory]")
        external_pos = result.index("[External Memory]")

        assert episodic_pos < semantic_pos < external_pos

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
        assert "[Objective Projection]" not in result

    def test_model_selected_order_is_preserved_within_section(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "low score fact", score=0.2),
            _make_item(MemoryKind.SEMANTIC, "high score fact", score=0.9),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.index("low score fact") < result.index("high score fact")

    def test_activation_scores_do_not_reorder_model_selection(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "raw score loser", score=1.0, activation_raw_score=1.2),
            _make_item(MemoryKind.SEMANTIC, "raw score winner", score=1.0, activation_raw_score=2.4),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.index("raw score loser") < result.index("raw score winner")


class TestAssembleBoundedAutomaticSurfacing:
    """Automatic body surfacing is bounded; full memory stays reachable by ref."""

    def test_more_than_five_model_selected_items_is_a_contract_error(self) -> None:
        items = [_make_item(MemoryKind.SEMANTIC, f"fact {index}", entry_id=f"memory-{index}") for index in range(6)]

        with pytest.raises(ValueError, match="at most 5"):
            MemoryAssembler().assemble(items)

    def test_single_item_is_at_most_four_kib_and_two_hundred_lines(self) -> None:
        content = "\n".join(f"line-{index}-" + ("界" * 80) for index in range(500))
        item = _make_item(MemoryKind.SEMANTIC, content, entry_id="memory-large")

        result = MemoryAssembler().assemble([item], budget_chars=20_000)

        assert len(result.encode("utf-8")) <= 4096
        assert len(result.splitlines()) <= 200
        assert "memory-large" in result
        assert 'load_memory(ids=["memory-large"])' in result
        assert "line-499" not in result

    def test_five_items_are_at_most_twenty_kib_per_turn(self) -> None:
        items = [_make_item(MemoryKind.SEMANTIC, "证据" * 5000, entry_id=f"memory-{index}") for index in range(5)]

        result = MemoryAssembler().assemble(items, budget_chars=100_000)

        assert len(result.encode("utf-8")) <= 5 * 4096
        assert all(f"memory-{index}" in result for index in range(5))

    def test_representation_budget_keeps_selected_refs_without_dumping_all_bodies(self) -> None:
        items = [
            _make_item(MemoryKind.EPISODIC, "A" * 1000, entry_id="memory-a"),
            _make_item(MemoryKind.EXTERNAL, "B" * 1000, entry_id="memory-b"),
        ]
        result = MemoryAssembler().assemble(items, budget_chars=700)

        assert len(result.encode("utf-8")) <= 700
        assert "memory-a" in result
        assert "memory-b" in result
        assert "A" * 1000 not in result
        assert "B" * 1000 not in result

    def test_exhausted_session_budget_surfaces_no_dynamic_body(self) -> None:
        item = _make_item(MemoryKind.SEMANTIC, "short", entry_id="memory-short")

        assert MemoryAssembler().assemble([item], budget_chars=0) == ""

    def test_all_activation_reasons_are_visible(self) -> None:
        item = _make_item(MemoryKind.SEMANTIC, "fact")
        item.metadata["activation_reasons"] = [f"reason-{index}" for index in range(8)]

        result = MemoryAssembler().assemble([item])

        assert "reason-0" in result
        assert "reason-7" in result


class TestAssemblePreservesModelSelection:
    """Assembly formats the model-selected set without making another choice."""

    def test_exact_duplicates_with_distinct_provenance_are_preserved(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.count("User prefers dark mode") == 3

    def test_case_variants_are_not_mechanically_collapsed(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "User prefers dark mode"),
            _make_item(MemoryKind.SEMANTIC, "user prefers dark mode"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        count = result.lower().count("user prefers dark mode")
        assert count == 2

    def test_different_content_preserved(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "fact alpha"),
            _make_item(MemoryKind.SEMANTIC, "fact beta"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "fact alpha" in result
        assert "fact beta" in result

    def test_cross_kind_provenance_is_preserved(self) -> None:
        items = [
            _make_item(MemoryKind.SEMANTIC, "shared fact"),
            _make_item(MemoryKind.EXTERNAL, "shared fact"),
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert result.count("shared fact") == 2


class TestFreshnessSuffix:
    """_freshness_suffix appends age warnings for stale memories."""

    def test_no_timestamp_returns_empty(self) -> None:
        item = _make_item(MemoryKind.SEMANTIC, "fact")
        assert _freshness_suffix(item) == ""

    def test_none_timestamp_returns_empty(self) -> None:
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=None)
        assert _freshness_suffix(item) == ""

    def test_recent_memory_renders_age_without_warning(self) -> None:
        now_iso = datetime.now(UTC).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=now_iso)
        suffix = _freshness_suffix(item)
        assert "0d ago" in suffix
        assert "verify before acting" not in suffix

    def test_old_memory_gets_warning(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=old)
        suffix = _freshness_suffix(item)
        assert "10d ago" in suffix
        assert "verify before acting" in suffix

    def test_exactly_seven_days_renders_age_without_warning(self) -> None:
        seven_days = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=seven_days)
        suffix = _freshness_suffix(item)
        assert "7d ago" in suffix
        assert "verify before acting" not in suffix

    def test_eight_days_has_warning(self) -> None:
        eight_days = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        item = _make_item(MemoryKind.SEMANTIC, "fact", timestamp=eight_days)
        assert "8d ago" in _freshness_suffix(item)

    def test_naive_datetime_does_not_crash(self) -> None:
        """Raw naive datetime in metadata should not raise TypeError."""
        naive_old = datetime.now() - timedelta(days=10)
        item = MemoryItem(
            kind=MemoryKind.SEMANTIC,
            content="fact",
            score=0.5,
            source="test",
            metadata={"timestamp": naive_old},
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
        assert "0d ago" in result
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

    def test_activation_reasons_rendered_compactly(self) -> None:
        items = [
            _make_item(
                MemoryKind.SEMANTIC,
                "Salary planning for Acme is an open loop",
                activation_reasons=["goal_relevance", "open_loop_pressure"],
            )
        ]
        assembler = MemoryAssembler()
        result = assembler.assemble(items)

        assert "[why=goal_relevance,open_loop_pressure]" in result
