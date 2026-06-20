"""Tests for semantic near-dedup on T3 entries and saved skills."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.memory.md_store import (
    MEMORY_DEDUP_THRESHOLD,
    SKILL_DEDUP_THRESHOLD,
    append_t3_entry,
    find_similar_t3_entries,
    jaccard_similarity,
)


def test_jaccard_identity():
    assert jaccard_similarity("abc def", "abc def") == 1.0


def test_jaccard_completely_different():
    assert jaccard_similarity("apple banana cherry", "mercury venus earth") < 0.10


def test_jaccard_english_paraphrase_above_memory_threshold():
    """'User prefers short replies' paraphrases must cross memory dedup threshold."""
    score = jaccard_similarity(
        "User prefers short replies without lists",
        "user likes short replies no lists",
    )
    assert score >= MEMORY_DEDUP_THRESHOLD, f"expected >= {MEMORY_DEDUP_THRESHOLD}, got {score}"


def test_jaccard_cjk_uses_bigrams():
    """CJK strings have no whitespace; bigram fallback must keep similarity meaningful."""
    score = jaccard_similarity("用户偏好简短回复", "用户喜欢简短回复")
    assert score > 0.3, f"CJK bigram fallback failed, got {score}"


def test_find_similar_t3_filters_by_category(tmp_path: Path):
    agent_id = uuid.uuid4()
    append_t3_entry(
        tmp_path,
        agent_id,
        category="feedback",
        content="User prefers short replies without lists",
    )
    append_t3_entry(
        tmp_path,
        agent_id,
        category="project",
        content="Launch deadline is 2026-05-01",
    )

    # Searching under 'feedback' must not return the unrelated project fact
    hits = find_similar_t3_entries(
        tmp_path,
        agent_id,
        content="user likes short replies no lists",
        category="feedback",
        threshold=MEMORY_DEDUP_THRESHOLD,
    )
    assert len(hits) == 1
    assert "short replies" in hits[0]["content"]

    # A distinct fact under feedback returns no hits
    hits_distinct = find_similar_t3_entries(
        tmp_path,
        agent_id,
        content="Avoid using production secrets in tests",
        category="feedback",
        threshold=MEMORY_DEDUP_THRESHOLD,
    )
    assert hits_distinct == []


@pytest.mark.asyncio
async def test_save_memory_rejects_english_paraphrase(tmp_path: Path, monkeypatch):
    """End-to-end handler: second save with paraphrased content must return [Skipped]."""
    from app.config import Settings
    from app.tools.handlers.memory import save_memory

    class _StubSettings(Settings):
        model_config = {"extra": "allow"}

    stub = _StubSettings(AGENT_DATA_DIR=str(tmp_path))
    monkeypatch.setattr("app.config.get_settings", lambda: stub)

    agent_id = uuid.uuid4()

    r1 = await save_memory(
        agent_id,
        {"content": "User prefers short replies without lists", "category": "feedback"},
    )
    assert r1.startswith("Saved to explicit memory overlay")

    r2 = await save_memory(
        agent_id,
        {"content": "user likes short replies no lists", "category": "feedback"},
    )
    assert "[Skipped]" in r2
    assert "similar explicit memory already exists" in r2.lower()

    # Distinct fact is accepted
    r3 = await save_memory(
        agent_id,
        {"content": "Launch deadline is 2026-05-01", "category": "project"},
    )
    assert r3.startswith("Saved to explicit memory overlay")


def test_save_skill_rejects_near_duplicate_skill(tmp_path: Path):
    """Second save_skill call with paraphrased name+description must be blocked."""
    from app.services.agent_tool_domains.workspace import _submit_skill_activation_candidate

    ws = tmp_path.resolve()
    r1 = _submit_skill_activation_candidate(
        ws,
        agent_id=None,
        name="Research Brief",
        description="Do web research and write a brief summary",
        instructions="Search, fetch, then summarize into workspace/brief.md.",
    )
    assert "submitted for review" in r1
    assert not (ws / "skills" / "research-brief" / "SKILL.md").exists()

    r2 = _submit_skill_activation_candidate(
        ws,
        agent_id=None,
        name="Research Briefing",
        description="Do web research and write brief summaries",
        instructions="Do it.",
    )
    assert "similar_skill_exists" in r2
    assert "Research Brief" in r2

    # Clearly distinct skill is accepted
    r3 = _submit_skill_activation_candidate(
        ws,
        agent_id=None,
        name="PDF Extractor",
        description="Extract tables from PDF invoices",
        instructions="Read pdf and emit csv.",
    )
    assert "submitted for review" in r3


def test_skill_dedup_threshold_ordering():
    """Guard rails: skill threshold must remain looser than memory threshold
    because skill descriptions are longer and more varied than single facts."""
    assert SKILL_DEDUP_THRESHOLD <= MEMORY_DEDUP_THRESHOLD + 0.15
    assert SKILL_DEDUP_THRESHOLD >= 0.40
