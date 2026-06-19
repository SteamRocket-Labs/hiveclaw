"""Prompt contract for Dream after T3 consolidation split.

Dream no longer performs accepted-T3 maintenance. It inspects accepted T3 and
explicit overlay evidence, then proposes soul-level changes as JSON. T3 edits
belong to the T3 Consolidator -> Memory Gate -> Platform Gate lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "app" / "templates" / "DREAM.md"


@pytest.fixture(scope="module")
def template_text() -> str:
    return _TEMPLATE_PATH.read_text(encoding="utf-8")


def test_dream_names_new_role_and_boundaries(template_text: str) -> None:
    assert "# Dream — Soul Reconsolidation Protocol" in template_text
    assert "background maintenance cycle" in template_text
    assert "not a user" in template_text
    assert "You are not the T3 writer" in template_text
    assert "T3 Consolidator" in template_text
    assert "Memory Gate Agent" in template_text
    assert "Platform Gate" in template_text


def test_dream_uses_canonical_inputs_only(template_text: str) -> None:
    for path in (
        "memory/t3/episodes.md",
        "memory/t3/user.md",
        "memory/t3/worker.md",
        "memory/t3/capabilities.md",
        "memory/explicit/",
    ):
        assert path in template_text
    for legacy in ("memory/feedback.md", "memory/knowledge.md", "memory/strategies.md", "memory/blocked.md"):
        assert legacy not in template_text


def test_dream_forbids_direct_t3_and_overlay_mutation(template_text: str) -> None:
    assert "Do not write `memory/t3/**` directly" in template_text
    assert "Do not write `memory/explicit/**` directly" in template_text
    assert "Do not directly write, edit, deduplicate, cap, or reorder" in template_text
    assert "Do not create `memory/t3/index.md`" in template_text
    assert "chapters/**" in template_text


def test_dream_promotes_only_stable_identity_rules(template_text: str) -> None:
    assert "Promote only stable, repeatedly evidenced patterns" in template_text
    assert "accepted in T3 or explicitly saved by the user" in template_text
    assert "does not conflict with frozen mission/charter boundaries" in template_text
    assert "source references are precise enough" in template_text
    assert "When uncertain, hold" in template_text


def test_dream_feedback_to_t3_is_concern_not_patch(template_text: str) -> None:
    assert "If accepted T3 looks duplicated, stale, contradictory, or too broad, do not fix" in template_text
    assert "held T3 patch concern" in template_text
    assert "The next T3 Consolidation Batch" in template_text


def test_dream_output_is_raw_json_only(template_text: str) -> None:
    assert "<required_output>" in template_text
    assert "</required_output>" in template_text
    assert "raw JSON only" in template_text
    assert "Do not output prose, Markdown, or tool instructions" in template_text
