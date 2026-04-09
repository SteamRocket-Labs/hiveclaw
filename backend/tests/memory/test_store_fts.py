"""Tests for PersistentMemoryStore FTS5 search and typed memory."""

import json
import uuid

import pytest

from app.memory.store import MEMORY_CATEGORIES, PersistentMemoryStore


@pytest.fixture
def store(tmp_path):
    return PersistentMemoryStore(data_root=tmp_path)


@pytest.fixture
def agent_id():
    return uuid.uuid4()


@pytest.fixture
def seeded_store(store, agent_id):
    """Store with pre-seeded facts."""
    facts = [
        {"content": "User prefers Python over JavaScript", "subject": "preferences"},
        {"content": "Weekly standup is every Monday at 10am", "subject": "schedule"},
        {"content": "Project deadline is March 30th", "subject": "project"},
        {"content": "Database uses PostgreSQL with asyncpg", "subject": "tech"},
        {"content": "User speaks Chinese and English", "subject": "language"},
        {"content": "API rate limit is 100 requests per minute", "subject": "limits"},
    ]
    store.replace_semantic_facts(agent_id, facts)
    return store


def test_search_facts_returns_matching_results(seeded_store, agent_id):
    results = seeded_store.search_facts(agent_id, "Python")
    assert len(results) >= 1
    contents = [r["content"] for r in results]
    assert any("Python" in c for c in contents)


def test_search_facts_respects_limit(seeded_store, agent_id):
    results = seeded_store.search_facts(agent_id, "User", limit=1)
    assert len(results) <= 1


def test_search_facts_empty_query_returns_all(seeded_store, agent_id):
    results = seeded_store.search_facts(agent_id, "", limit=10)
    assert len(results) == 6


def test_search_facts_no_match_returns_empty(seeded_store, agent_id):
    results = seeded_store.search_facts(agent_id, "nonexistent_xyzzy_term")
    assert len(results) == 0


def test_search_facts_matches_subject(seeded_store, agent_id):
    results = seeded_store.search_facts(agent_id, "schedule")
    assert len(results) >= 1
    assert any("Monday" in r["content"] for r in results)


def test_fts_rebuilt_after_replace(store, agent_id):
    store.replace_semantic_facts(agent_id, [{"content": "alpha bravo charlie"}])
    results = store.search_facts(agent_id, "bravo")
    assert len(results) == 1
    assert results[0]["content"] == "alpha bravo charlie"

    # Replace with new facts — old FTS entries should be gone
    store.replace_semantic_facts(agent_id, [{"content": "delta echo foxtrot"}])
    results = store.search_facts(agent_id, "bravo")
    assert len(results) == 0
    results = store.search_facts(agent_id, "echo")
    assert len(results) == 1


def test_fts_works_with_legacy_json_import(store, agent_id, tmp_path):
    """FTS should be populated even when facts come from legacy memory.json."""
    memory_dir = tmp_path / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True)
    facts = [
        {"content": "Legacy fact about Kubernetes"},
        {"content": "Another fact about Docker"},
    ]
    (memory_dir / "memory.json").write_text(json.dumps(facts))

    results = store.search_facts(agent_id, "Kubernetes")
    assert len(results) >= 1
    assert "Kubernetes" in results[0]["content"]


def test_search_preserves_metadata(store, agent_id):
    facts = [
        {"content": "Meeting notes from sprint review", "subject": "meetings", "timestamp": "2026-03-20"},
    ]
    store.replace_semantic_facts(agent_id, facts)
    results = store.search_facts(agent_id, "sprint")
    assert len(results) == 1
    assert results[0]["subject"] == "meetings"
    assert results[0]["timestamp"] == "2026-03-20"


# ── P1.1: Typed memory (category field) ──────────────────────────


class TestTypedMemoryCategory:
    """semantic_facts stores and retrieves category field."""

    def test_category_stored_and_loaded(self, store, agent_id) -> None:
        facts = [
            {"content": "User prefers dark mode", "category": "user"},
            {"content": "Always run tests before commit", "category": "feedback"},
            {"content": "Sprint ends March 30", "category": "project"},
        ]
        store.replace_semantic_facts(agent_id, facts)
        loaded = store.load_semantic_facts(agent_id)
        assert loaded[0]["category"] == "user"
        assert loaded[1]["category"] == "feedback"
        assert loaded[2]["category"] == "project"

    def test_default_category_is_general(self, store, agent_id) -> None:
        facts = [{"content": "no category specified"}]
        store.replace_semantic_facts(agent_id, facts)
        loaded = store.load_semantic_facts(agent_id)
        assert loaded[0]["category"] == "general"

    def test_invalid_category_falls_back_to_general(self, store, agent_id) -> None:
        facts = [{"content": "bad category", "category": "nonsense"}]
        store.replace_semantic_facts(agent_id, facts)
        loaded = store.load_semantic_facts(agent_id)
        assert loaded[0]["category"] == "general"

    def test_search_returns_category(self, store, agent_id) -> None:
        facts = [
            {"content": "Slack channel for bugs is #bugs", "category": "reference"},
            {"content": "User likes coffee", "category": "user"},
        ]
        store.replace_semantic_facts(agent_id, facts)
        results = store.search_facts(agent_id, "bugs")
        assert len(results) >= 1
        assert results[0]["category"] == "reference"

    def test_category_survives_legacy_import(self, store, agent_id) -> None:
        """Legacy JSON without category should get 'general' on import."""
        memory_dir = store.data_root / str(agent_id) / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        legacy = [{"content": "old fact from JSON"}]
        (memory_dir / "memory.json").write_text(json.dumps(legacy))
        loaded = store.load_semantic_facts(agent_id)
        assert len(loaded) == 1
        assert loaded[0]["category"] == "general"

    def test_all_valid_categories_accepted(self, store, agent_id) -> None:
        facts = [{"content": f"fact for {cat}", "category": cat} for cat in MEMORY_CATEGORIES]
        store.replace_semantic_facts(agent_id, facts)
        loaded = store.load_semantic_facts(agent_id)
        categories = {f["category"] for f in loaded}
        assert categories == MEMORY_CATEGORIES
