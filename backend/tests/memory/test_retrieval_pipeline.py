"""Tests for the MD-first memory retrieval pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.memory.retriever import MemoryRetriever, _score_relevance
from app.memory.types import MemoryKind
from app.memory.activation import ActivationContext
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


def _setup_t3_file(data_root: Path, agent_id: uuid.UUID, filename: str, content: str) -> None:
    memory_dir = data_root / str(agent_id) / "memory"
    target = memory_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        body = "\n".join(line for line in content.splitlines() if line.strip() and not line.startswith("#"))
        target.write_text(existing.rstrip() + "\n" + body + "\n", encoding="utf-8")
    else:
        target.write_text(content, encoding="utf-8")


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
async def test_retrieve_returns_t3_direct_layer(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    """T3 md-backed semantic items are returned without a working-memory file projection."""
    _setup_t3_file(data_root, agent_id, "t3/user.md", "# T3 User\n- [2026-04-06] User prefers concise output\n")
    _setup_t3_file(
        data_root,
        agent_id,
        "t3/capabilities.md",
        "# T3 Capabilities\n- [2026-04-06] Project uses FastAPI and React\n",
    )

    items = await retriever.retrieve(agent_id, "memory engine", session_id=None, tenant_id=None)

    working_items = [i for i in items if i.kind == MemoryKind.WORKING]
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]
    episodic_items = [i for i in items if i.kind == MemoryKind.EPISODIC]
    external_items = [i for i in items if i.kind == MemoryKind.EXTERNAL]

    assert working_items == []
    assert len(semantic_items) == 2
    assert semantic_items[0].source == "memory/t3/user.md"
    assert semantic_items[1].source == "memory/t3/capabilities.md"
    assert "[user]" in semantic_items[0].content
    assert "[capability]" in semantic_items[1].content
    assert episodic_items == []
    assert external_items == []


@pytest.mark.asyncio
async def test_retrieve_excludes_legacy_high_priority_t2_by_default(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    from app.memory.t2_store import ensure_t2_layout, format_t2_entry

    root = ensure_t2_layout(data_root, agent_id)
    (root / "insights.md").write_text(
        "# Insights\n"
        + format_t2_entry(
            category="feedback",
            content="User wants salary planning answers to include owner approval constraints.",
            source="web",
            weight=0.95,
            metadata={"entry_id": "t2-feedback-1", "sensitivity": "PL1_public"},
        )
        + "\n",
        encoding="utf-8",
    )

    items = await retriever.retrieve(agent_id, "salary planning", session_id=None, tenant_id=None)

    t2_items = [item for item in items if item.metadata.get("lane") == "t2_high_priority"]
    assert t2_items == []


@pytest.mark.asyncio
async def test_legacy_high_priority_t2_is_not_prompt_memory_even_when_enabled(
    data_root: Path,
    agent_id: uuid.UUID,
) -> None:
    from app.memory.t2_store import ensure_t2_layout, format_t2_entry

    root = ensure_t2_layout(data_root, agent_id)
    (root / "insights.md").write_text(
        "# Insights\n"
        + format_t2_entry(
            category="feedback",
            content="User wants salary planning answers to include owner approval constraints.",
            source="web",
            weight=0.95,
            metadata={"entry_id": "t2-feedback-1", "sensitivity": "PL1_public"},
        )
        + "\n",
        encoding="utf-8",
    )

    retriever = MemoryRetriever(data_root=data_root, include_legacy_sources=True)
    items = await retriever.retrieve(agent_id, "salary planning", session_id=None, tenant_id=None)

    t2_items = [item for item in items if item.metadata.get("lane") == "t2_high_priority"]
    assert t2_items == []


@pytest.mark.asyncio
async def test_legacy_high_priority_t2_activation_metadata_is_not_recalled(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    from app.memory.t2_store import ensure_t2_layout, format_t2_entry

    root = ensure_t2_layout(data_root, agent_id)
    (root / "insights.md").write_text(
        "# Insights\n"
        + format_t2_entry(
            category="feedback",
            content="Owner has an open loop to follow up on Railway incident summaries.",
            source="web",
            weight=0.95,
            confidence=0.91,
            metadata={
                "entry_id": "t2-feedback-activation",
                "sensitivity": "PL1_public",
                "open_loop": "true",
                "retention_score": "0.80",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    legacy_retriever = MemoryRetriever(data_root=data_root, include_legacy_sources=True)
    items = await legacy_retriever.retrieve(
        agent_id,
        "Railway incident summaries",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    assert [item for item in items if item.metadata.get("lane") == "t2_high_priority"] == []


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
    _setup_t3_file(data_root, agent_id, "t3/user.md", "# T3 User\n\nUser corrections and constraints.\n")

    items = await retriever.retrieve(agent_id, "", session_id=None, tenant_id=None)
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]
    assert semantic_items == []


@pytest.mark.asyncio
async def test_t3_direct_preserves_priority_order(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    _setup_t3_file(data_root, agent_id, "t3/capabilities.md", "# T3 Capabilities\n- [2026-04-06] Knowledge entry\n")
    _setup_t3_file(data_root, agent_id, "t3/user.md", "# T3 User\n- [2026-04-06] User entry\n")
    _setup_t3_file(data_root, agent_id, "t3/user.md", "# T3 User\n- [2026-04-06] Feedback entry\n")

    items = await retriever.retrieve(agent_id, "", session_id=None, tenant_id=None)
    semantic_items = [i for i in items if i.kind == MemoryKind.SEMANTIC]

    assert [item.source for item in semantic_items] == [
        "memory/t3/user.md",
        "memory/t3/user.md",
        "memory/t3/capabilities.md",
    ]
    assert semantic_items[0].score == semantic_items[1].score
    assert semantic_items[1].score > semantic_items[2].score


@pytest.mark.asyncio
async def test_t3_index_first_folds_p1_p2_and_keeps_p0_full(
    data_root: Path,
    agent_id: uuid.UUID,
) -> None:
    from app.memory.md_store import rebuild_index

    _setup_t3_file(data_root, agent_id, "t3/user.md", "# T3 User\n- [2026-05-28] User requires Chinese replies\n")
    _setup_t3_file(
        data_root,
        agent_id,
        "t3/capabilities.md",
        "# T3 Capabilities\n- [2026-05-28] Long P1 entry should be loaded by id before relying on full content\n",
    )
    rebuild_index(data_root, agent_id)

    items = await MemoryRetriever(data_root=data_root, use_t3_index_first=True).retrieve(
        agent_id,
        "",
        session_id=None,
        tenant_id=None,
    )

    semantic_items = [item for item in items if item.kind == MemoryKind.SEMANTIC]
    p0 = next(item for item in semantic_items if item.source == "memory/t3/user.md")
    p1 = next(item for item in semantic_items if item.source == "memory/t3/capabilities.md")

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
        "t3/capabilities.md",
        "# T3 Capabilities\n- [2026-05-22][sensitivity=PL3_sensitive] Q3 salary planning requires owner-only handling\n",
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
        "t3/capabilities.md",
        "# T3 Capabilities\n"
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
async def test_activation_context_joins_sidecar_access_telemetry_into_usage_heat(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    from app.memory.lifecycle_store import record_active_memory_lifecycle

    _setup_t3_file(
        data_root,
        agent_id,
        "t3/capabilities.md",
        "# T3 Capabilities\n"
        "- [2026-05-22][entry_id=mem_hot] Legacy proxy timeout limit is still relevant\n"
        "- [2026-05-22][entry_id=mem_cold] Legacy proxy timeout limit is still relevant\n",
    )
    record_active_memory_lifecycle(
        data_root,
        agent_id,
        content="Legacy proxy timeout limit is still relevant",
        metadata={
            "entry_id": "mem_hot",
            "sensitivity": "PL1_public",
            "access_count": "12",
            "last_accessed": datetime.now(UTC).isoformat(),
        },
    )
    record_active_memory_lifecycle(
        data_root,
        agent_id,
        content="Legacy proxy timeout limit is still relevant",
        metadata={
            "entry_id": "mem_cold",
            "sensitivity": "PL1_public",
            "access_count": "0",
            "last_accessed": "never",
        },
    )

    items = await retriever.retrieve(
        agent_id,
        "unrelated",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    by_id = {item.metadata.get("entry_id"): item for item in items}
    assert by_id["mem_hot"].score > by_id["mem_cold"].score
    assert "usage_heat" in by_id["mem_hot"].metadata["activation_reasons"]
    assert "usage_heat" not in by_id["mem_cold"].metadata["activation_reasons"]


@pytest.mark.asyncio
async def test_retriever_suppresses_conflicted_and_revalidation_required_t3_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    from app.memory.lifecycle_store import MemoryLifecycleStore, lifecycle_path, record_active_memory_lifecycle

    _setup_t3_file(
        data_root,
        agent_id,
        "t3/capabilities.md",
        "# T3 Capabilities\n"
        "- [2026-05-22][entry_id=mem_conflict] Deploy cadence is daily\n"
        "- [2026-05-22][entry_id=mem_stale] API reference lives at old path\n"
        "- [2026-05-22][entry_id=mem_clean] Durable launch checklist remains valid\n",
    )
    record_active_memory_lifecycle(
        data_root,
        agent_id,
        content="Deploy cadence is daily",
        metadata={"entry_id": "mem_conflict", "sensitivity": "PL1_public"},
    )
    record_active_memory_lifecycle(
        data_root,
        agent_id,
        content="API reference lives at old path",
        metadata={"entry_id": "mem_stale", "sensitivity": "PL1_public"},
    )
    record_active_memory_lifecycle(
        data_root,
        agent_id,
        content="Durable launch checklist remains valid",
        metadata={"entry_id": "mem_clean", "sensitivity": "PL1_public"},
    )
    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))
    store.record_conflict(
        "mem_conflict",
        conflicts_with=["mem_new"],
        reason="newer cadence",
        source_refs=["workspace/new.md"],
    )
    store.mark_reference_revalidation_required(
        "mem_stale",
        reason="missing local source",
        source_refs=["workspace/missing.md"],
    )

    items = await retriever.retrieve(
        agent_id,
        "deploy api launch checklist",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    contents = "\n".join(item.content for item in items)
    assert "Durable launch checklist remains valid" in contents
    assert "Deploy cadence is daily" not in contents
    assert "API reference lives at old path" not in contents


@pytest.mark.asyncio
async def test_retrieve_excludes_legacy_relationship_understandings(
    data_root: Path,
    agent_id: uuid.UUID,
) -> None:
    memory_dir = data_root / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "understandings.md").write_text(
        "<!-- understanding\n"
        "entry_id: legacy-relation\n"
        "subject: agent_a\n"
        "object: agent_b\n"
        "relation_type: collaborator\n"
        "evidence_refs: decision/abc123\n"
        "confidence: 0.9\n"
        "last_confirmed_at: 2026-06-19T00:00:00+00:00\n"
        "-->\n"
        "Agent B is reliable for research but needs an explicit output schema.\n",
        encoding="utf-8",
    )

    items = await MemoryRetriever(data_root=data_root).retrieve(
        agent_id,
        "agent b research schema",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    assert [item for item in items if item.metadata.get("source_type") == "understanding_store"] == []

    derived_retriever = MemoryRetriever(data_root=data_root, include_derived_sources=True)
    items = await derived_retriever.retrieve(
        agent_id,
        "agent b research schema",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
    )

    understanding_items = [item for item in items if item.metadata.get("source_type") == "understanding_store"]
    assert understanding_items == []


def _setup_wiki_page(data_root: Path, agent_id: uuid.UUID, slug: str, body: str) -> None:
    page_dir = data_root / str(agent_id) / "memory" / "wiki"
    page_dir.mkdir(parents=True, exist_ok=True)
    (page_dir / f"{slug}.md").write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_retrieve_excludes_ppr_wiki_pages_by_default_and_requires_derived_opt_in(
    data_root: Path,
    agent_id: uuid.UUID,
) -> None:
    _setup_wiki_page(
        data_root,
        agent_id,
        "deployment-pipeline",
        "---\ntitle: Deployment Pipeline\ntype: concept\ntags: [deploy]\nstatus: active\n---\n\n"
        "## Current Claim\n\nCanary release rollout goes through staged gates.\n\n"
        "## Relations\n\n- depends_on [[Rollback Procedure]]\n",
    )
    _setup_wiki_page(
        data_root,
        agent_id,
        "rollback-procedure",
        "---\ntitle: Rollback Procedure\ntype: concept\ntags: [ops]\nstatus: active\n---\n\n"
        "## Current Claim\n\nRe-pin the previous image digest during failed deployments.\n",
    )

    items = await MemoryRetriever(data_root=data_root).retrieve(
        agent_id,
        "canary release rollout",
        session_id=None,
        tenant_id=None,
    )

    assert [item for item in items if item.metadata.get("source_type") == "wiki_ppr"] == []

    derived_retriever = MemoryRetriever(data_root=data_root, include_derived_sources=True)
    items = await derived_retriever.retrieve(agent_id, "canary release rollout", session_id=None, tenant_id=None)

    ppr_items = [item for item in items if item.metadata.get("source_type") == "wiki_ppr"]
    assert any(item.source == "memory/wiki/rollback-procedure.md" for item in ppr_items)
    rollback = next(item for item in ppr_items if item.source == "memory/wiki/rollback-procedure.md")
    assert rollback.kind == MemoryKind.SEMANTIC
    assert rollback.metadata["page_id"] == "wiki/rollback-procedure"
    assert rollback.metadata["method"] == "ppr"


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
