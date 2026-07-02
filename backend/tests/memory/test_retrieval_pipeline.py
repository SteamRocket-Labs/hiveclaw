"""Retrieval pipeline invariants after the two-plane cutover (Part H).

The legacy flat-T3 read paths (direct/index-first/shadow), the derived
wiki/scenes opt-in, and the LLM rerank are retired — prompt memory is the
resident profile plane (loaded in memory_service) plus knowledge-plane PPR,
explicit overlay, and episodic recall, with activation scoring on top.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.memory.activation import ActivationContext
from app.memory.retriever import MemoryRetriever
from app.memory.types import MemoryKind
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def retriever(data_root: Path) -> MemoryRetriever:
    return MemoryRetriever(data_root=data_root)


def _write_overlay_entry(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    entry_id: str,
    content: str,
    sensitivity: str = "PL1_public",
    metadata: dict | None = None,
) -> None:
    overlay = data_root / str(agent_id) / "memory" / "explicit"
    (overlay / "entries").mkdir(parents=True, exist_ok=True)
    record = {
        "id": entry_id,
        "status": "active",
        "category": "general",
        "sensitivity": sensitivity,
        **(metadata or {}),
    }
    with (overlay / "manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    (overlay / "entries" / f"{entry_id}.md").write_text(
        f"<normalized_memory>{content}</normalized_memory>", encoding="utf-8"
    )


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


def test_legacy_sqlite_store_no_longer_exists(data_root: Path) -> None:
    """The legacy sqlite-backed PersistentMemoryStore stays retired (MD-first only)."""
    import app.memory as memory_pkg

    assert not hasattr(memory_pkg, "PersistentMemoryStore")
    assert not hasattr(memory_pkg, "FileBackedMemoryStore")
    MemoryRetriever(data_root=data_root)
    assert not (data_root / "memory.sqlite3").exists()


def test_legacy_read_knobs_are_retired() -> None:
    """Part H cutover: flat-T3/derived/index-first knobs must not come back."""
    import inspect

    params = inspect.signature(MemoryRetriever.__init__).parameters
    for retired in ("use_t3_index_first", "include_legacy_sources", "include_derived_sources"):
        assert retired not in params
    for retired_method in ("_retrieve_t3_direct", "_retrieve_t3_index_first", "retrieve_t3_index_shadow"):
        assert not hasattr(MemoryRetriever, retired_method)


@pytest.mark.asyncio
async def test_retrieve_no_files(data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever) -> None:
    items = await retriever.retrieve(agent_id, "anything", session_id=None, tenant_id=None)

    assert [item for item in items if item.kind == MemoryKind.SEMANTIC] == []


@pytest.mark.asyncio
async def test_rerank_config_is_ignored_reads_run_zero_llm(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever, monkeypatch
) -> None:
    """spec §4.2: reads never run an LLM — a passed rerank config is inert."""

    def _boom(*_a, **_k):  # any LLM client construction would be a violation
        raise AssertionError("retrieval must not construct an LLM client")

    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", _boom, raising=False)
    _write_overlay_entry(data_root, agent_id, entry_id="ex-1", content="salary planning note")

    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        rerank_model_config={"provider": "fake", "model": "fake"},
    )

    assert any("salary planning" in item.content for item in items)


@pytest.mark.asyncio
async def test_activation_context_suppresses_pl3_when_current_user_is_not_owner(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever
) -> None:
    _write_overlay_entry(
        data_root,
        agent_id,
        entry_id="ex-pl3",
        content="Q3 salary planning requires owner-only handling",
        sensitivity="PL3_sensitive",
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
async def test_activation_context_adds_reasons_and_score(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever
) -> None:
    _write_overlay_entry(
        data_root,
        agent_id,
        entry_id="ex-open-loop",
        content="Salary planning for Acme is an open loop for Alice",
        metadata={"retention_score": "0.5", "confidence": "0.9"},
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
    assert "goal_relevance" in item.metadata["activation_reasons"]
    assert item.metadata["activation_score"] == item.score


@pytest.mark.asyncio
async def test_activation_suppresses_conflicted_entries(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever
) -> None:
    _write_overlay_entry(
        data_root,
        agent_id,
        entry_id="ex-conflicted",
        content="salary planning conflicted claim",
        metadata={"conflict_status": "needs_review"},
    )

    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    assert all("conflicted claim" not in item.content for item in items)


@pytest.mark.asyncio
async def test_knowledge_plane_feeds_semantic_items(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever
) -> None:
    kdir = data_root / str(agent_id) / "memory" / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "salary-bands.md").write_text(
        "---\ntitle: Salary Bands\nstatus: active\n---\n## Current Claim\nsalary planning bands annually calibrated.\n"
        "## Relations\n- references [[k:Compensation]]\n",
        encoding="utf-8",
    )

    items = await retriever.retrieve(agent_id, "salary planning", session_id=None, tenant_id=None)

    knowledge = [item for item in items if item.metadata.get("source_type") == "knowledge_ppr"]
    assert knowledge
    assert any("Salary Bands" in item.content for item in knowledge)
