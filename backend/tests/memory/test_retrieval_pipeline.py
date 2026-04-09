"""Tests for the MD-first memory retrieval pipeline."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.memory.retriever import MemoryRetriever, _score_relevance
from app.memory.types import MemoryKind


@pytest.fixture()
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def retriever(data_root: Path) -> MemoryRetriever:
    return MemoryRetriever(data_root=data_root)


def test_retriever_init_does_not_touch_legacy_sqlite_store(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MD-first retriever should not initialize the legacy sqlite fact store."""
    import app.memory.retriever as retriever_module

    class _RaisingStore:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("legacy sqlite store should not be initialized")

    monkeypatch.setattr(retriever_module, "PersistentMemoryStore", _RaisingStore, raising=False)

    MemoryRetriever(data_root=data_root)


def _setup_focus(data_root: Path, agent_id: uuid.UUID, content: str) -> None:
    focus_file = data_root / str(agent_id) / "focus.md"
    focus_file.parent.mkdir(parents=True, exist_ok=True)
    focus_file.write_text(content, encoding="utf-8")


def _setup_t3_file(data_root: Path, agent_id: uuid.UUID, filename: str, content: str) -> None:
    memory_dir = data_root / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / filename).write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_retrieve_returns_working_and_t3_direct_layers(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    """Working memory is first, followed by T3 md-backed semantic items."""
    _setup_focus(data_root, agent_id, "Current focus: ship memory engine P1")
    _setup_t3_file(data_root, agent_id, "feedback.md", "# Feedback\n- [2026-04-06] User prefers concise output\n")
    _setup_t3_file(data_root, agent_id, "knowledge.md", "# Knowledge\n- [2026-04-06] Project uses FastAPI and React\n")

    items = await retriever.retrieve(agent_id, "memory engine", session_id=None, tenant_id=None)

    working_items = [i for i in items if i.kind == MemoryKind.WORKING]
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]
    episodic_items = [i for i in items if i.kind == MemoryKind.EPISODIC]
    external_items = [i for i in items if i.kind == MemoryKind.EXTERNAL]

    assert len(working_items) == 1
    assert "ship memory engine P1" in working_items[0].content
    assert len(semantic_items) == 2
    assert semantic_items[0].source == "memory/feedback.md"
    assert semantic_items[1].source == "memory/knowledge.md"
    assert "[feedback]" in semantic_items[0].content
    assert "[knowledge]" in semantic_items[1].content
    assert episodic_items == []
    assert external_items == []

    kinds = [i.kind for i in items]
    assert kinds.index(MemoryKind.WORKING) < kinds.index(MemoryKind.SEMANTIC)


@pytest.mark.asyncio
async def test_retrieve_no_files(data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever) -> None:
    """Retriever returns empty list when no agent data exists."""
    items = await retriever.retrieve(agent_id, "anything", session_id=None, tenant_id=None)
    assert items == []


@pytest.mark.asyncio
async def test_retrieve_empty_focus(data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever) -> None:
    """Empty focus.md produces no working memory items."""
    _setup_focus(data_root, agent_id, "")
    items = await retriever.retrieve(agent_id, "", session_id=None, tenant_id=None)
    working_items = [i for i in items if i.kind == MemoryKind.WORKING]
    assert working_items == []


@pytest.mark.asyncio
async def test_retrieve_ignores_legacy_json_without_t3_files(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    """Prompt injection should not depend on legacy memory.json anymore."""
    memory_file = data_root / str(agent_id) / "memory" / "memory.json"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("not valid json {{{", encoding="utf-8")

    items = await retriever.retrieve(agent_id, "test", session_id=None, tenant_id=None)
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]
    assert semantic_items == []


@pytest.mark.asyncio
async def test_t3_direct_skips_heading_only_files(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    _setup_t3_file(data_root, agent_id, "feedback.md", "# Feedback\n\nUser corrections and constraints.\n")

    items = await retriever.retrieve(agent_id, "", session_id=None, tenant_id=None)
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]
    assert semantic_items == []


@pytest.mark.asyncio
async def test_t3_direct_preserves_priority_order(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    _setup_t3_file(data_root, agent_id, "knowledge.md", "# Knowledge\n- [2026-04-06] Knowledge entry\n")
    _setup_t3_file(data_root, agent_id, "user.md", "# User\n- [2026-04-06] User entry\n")
    _setup_t3_file(data_root, agent_id, "feedback.md", "# Feedback\n- [2026-04-06] Feedback entry\n")

    items = await retriever.retrieve(agent_id, "", session_id=None, tenant_id=None)
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]

    assert [item.source for item in semantic_items] == [
        "memory/feedback.md",
        "memory/knowledge.md",
        "memory/user.md",
    ]
    assert semantic_items[0].score > semantic_items[1].score > semantic_items[2].score


def test_semantic_scoring_relevant_higher() -> None:
    """Relevant facts score higher than irrelevant ones."""
    high_score = _score_relevance("memory engine retrieval pipeline", "memory engine")
    low_score = _score_relevance("user likes dark theme colors", "memory engine")
    assert high_score > low_score


def test_semantic_scoring_exact_match() -> None:
    """Exact query match scores 1.0."""
    score = _score_relevance("memory engine", "memory engine")
    assert score == 1.0


def test_semantic_scoring_no_overlap() -> None:
    """No keyword overlap scores 0.0."""
    score = _score_relevance("dark theme preferences", "memory engine")
    assert score == 0.0


def test_semantic_scoring_empty_query() -> None:
    """Empty query returns 0.0."""
    score = _score_relevance("some content", "")
    assert score == 0.0
