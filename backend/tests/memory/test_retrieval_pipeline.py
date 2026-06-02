"""Tests for the MD-first memory retrieval pipeline."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.memory.retriever import MemoryRetriever, _score_relevance
from app.memory.types import MemoryKind
from app.memory.activation import ActivationContext
from app.memory.understanding_store import UnderstandingStore
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


@pytest.fixture()
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def retriever(data_root: Path) -> MemoryRetriever:
    return MemoryRetriever(data_root=data_root)


def test_legacy_sqlite_store_no_longer_exists(data_root: Path) -> None:
    """The legacy sqlite-backed PersistentMemoryStore has been retired (MD-first only)."""
    import app.memory as memory_pkg

    assert not hasattr(memory_pkg, "PersistentMemoryStore")
    assert not hasattr(memory_pkg, "FileBackedMemoryStore")
    MemoryRetriever(data_root=data_root)
    assert not (data_root / "memory.sqlite3").exists()


def _setup_focus(data_root: Path, agent_id: uuid.UUID, content: str) -> None:
    focus_file = data_root / str(agent_id) / "focus.md"
    focus_file.parent.mkdir(parents=True, exist_ok=True)
    focus_file.write_text(content, encoding="utf-8")


def _setup_t3_file(data_root: Path, agent_id: uuid.UUID, filename: str, content: str) -> None:
    memory_dir = data_root / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / filename).write_text(content, encoding="utf-8")


def _activation_context(*, current_user_id: str = "owner-1", owner_id: str = "owner-1") -> ActivationContext:
    return ActivationContext(
        query="salary planning",
        principal_stack=PrincipalStack(
            company=Principal(PrincipalRole.COMPANY, "company-1", "Acme"),
            direct_owner=Principal(PrincipalRole.OWNER, owner_id, "Alice"),
            current_user=Principal(PrincipalRole.CURRENT_USER, current_user_id, "Current User"),
        ),
        goal_terms=["salary", "planning"],
        owner_terms=["alice", "owner"],
        company_terms=["acme"],
    )


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
async def test_retrieve_uses_semantic_limit_for_semantic_backend(
    monkeypatch,
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    """Semantic backend retrieval must use semantic_limit, not external_limit."""
    from app.runtime.context_budget import ContextBudget, TaskProfile

    observed: dict[str, int] = {}

    async def fake_semantic_backend(*_args, limit: int = 5, **_kwargs):
        observed["semantic_limit"] = limit
        return []

    async def fake_external(*_args, limit: int = 5, **_kwargs):
        observed["external_limit"] = limit
        return []

    monkeypatch.setattr(retriever, "_retrieve_semantic_backend", fake_semantic_backend)
    monkeypatch.setattr(retriever, "_retrieve_external", fake_external)

    profile = ContextBudget(
        task_profile=TaskProfile(name="memory_recall", complexity="medium"),
        system_prompt_budget_chars=60_000,
        active_tool_groups_budget_chars=2_000,
        retrieval_budget_chars=3_000,
        knowledge_budget_chars=3_000,
        memory_budget_chars=4_000,
        skill_catalog_budget_chars=4_000,
        soul_budget_chars=16_000,
        relationships_budget_chars=2_000,
        company_info_budget_chars=5_000,
        org_structure_budget_chars=2_000,
        focus_budget_chars=3_000,
        runtime_triggers_budget_chars=3_000,
        restore_budget_chars=20_000,
        restore_per_file_cap_chars=4_000,
        semantic_limit=9,
        episodic_limit=4,
        external_limit=2,
        rerank_max_select=5,
    )

    await retriever.retrieve(
        agent_id, "memory", session_id=None, tenant_id=str(uuid.uuid4()), retrieval_profile=profile
    )

    assert observed == {"semantic_limit": 9, "external_limit": 2}


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


@pytest.mark.asyncio
async def test_t3_index_first_folds_p1_p2_and_keeps_p0_full(
    data_root: Path,
    agent_id: uuid.UUID,
) -> None:
    from app.memory.md_store import rebuild_index

    _setup_t3_file(data_root, agent_id, "feedback.md", "# Feedback\n- [2026-05-28] User requires Chinese replies\n")
    _setup_t3_file(
        data_root,
        agent_id,
        "knowledge.md",
        "# Knowledge\n- [2026-05-28] Long P1 entry should be loaded by id before relying on full content\n",
    )
    rebuild_index(data_root, agent_id)

    items = await MemoryRetriever(data_root=data_root, use_t3_index_first=True).retrieve(
        agent_id,
        "",
        session_id=None,
        tenant_id=None,
    )

    semantic_items = [item for item in items if item.kind == MemoryKind.SEMANTIC]
    p0 = next(item for item in semantic_items if item.source == "memory/feedback.md")
    p1 = next(item for item in semantic_items if item.source == "memory/knowledge.md")

    assert p0.metadata["source_type"] == "t3_full_entry"
    assert "User requires Chinese replies" in p0.content
    assert p1.metadata["source_type"] == "t3_index_entry"
    assert p1.metadata["indexed_only"] == "true"
    assert "load_memory" in p1.content
    assert p1.metadata["entry_id"].startswith("mem_")


@pytest.mark.asyncio
async def test_activation_context_suppresses_pl3_when_current_user_is_not_owner(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    _setup_t3_file(
        data_root,
        agent_id,
        "knowledge.md",
        "# Knowledge\n- [2026-05-22][sensitivity=PL3_sensitive] Q3 salary planning requires owner-only handling\n",
    )

    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(current_user_id="viewer-1"),
    )

    assert all("salary planning" not in item.content for item in items)


@pytest.mark.asyncio
async def test_activation_context_adds_reasons_and_updates_score(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    _setup_t3_file(
        data_root,
        agent_id,
        "knowledge.md",
        "# Knowledge\n"
        "- [2026-05-22][sensitivity=PL1_public][retention_score=0.5][confidence=0.9] "
        "Salary planning for Acme is an open loop for Alice\n",
    )

    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    semantic_items = [item for item in items if item.kind == MemoryKind.SEMANTIC]
    assert len(semantic_items) == 1
    item = semantic_items[0]
    assert item.metadata["activation_reasons"] == [
        "goal_relevance",
        "principal_relevance",
        "company_relevance",
        "retention_score",
        "confidence_weight",
    ]
    assert item.metadata["activation_score"] == item.score
    assert item.score > 0.8


@pytest.mark.asyncio
async def test_retrieve_includes_relationship_understandings(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    store = UnderstandingStore(data_root / str(agent_id) / "memory")
    store.record(
        subject="agent_a",
        object_="agent_b",
        relation_type="collaborator",
        current_understanding="Agent B is reliable for research but needs an explicit output schema.",
        evidence_refs=["decision/abc123"],
        confidence=0.9,
    )

    items = await retriever.retrieve(
        agent_id,
        "agent b research schema",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    understanding_items = [item for item in items if item.metadata.get("source_type") == "understanding_store"]
    assert len(understanding_items) == 1
    item = understanding_items[0]
    assert item.source == "memory/understandings.md"
    assert "agent_a -[collaborator]-> agent_b" in item.content
    assert "explicit output schema" in item.content
    assert item.metadata["category"] == "understanding"
    assert item.metadata["evidence_refs"] == "decision/abc123"
    assert item.metadata["confidence"] == "0.9"


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
