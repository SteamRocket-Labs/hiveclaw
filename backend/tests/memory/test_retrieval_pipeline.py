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


def _write_t2_package(data_root: Path, agent_id: uuid.UUID, *, package_id: str = "t2pkg-evidence") -> str:
    package_dir = data_root / str(agent_id) / "memory" / "t2" / "sessions" / "sess-1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": package_id,
                "package_status": "reviewed",
                "session_id": "sess-1",
                "t0_segment_id": "seg-1",
                "created_at": "2026-07-05T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (package_dir / "summary.md").write_text("# Summary\nEvidence for memory runtime.\n", encoding="utf-8")
    (package_dir / "labels.md").write_text(
        f"""<t2_labels schema_version="t2.labels.v1" package_id="{package_id}">
  <continuity_state>standalone</continuity_state>
  <engineering_labels>
    <risk_flags><risk_flag>privacy_sensitive</risk_flag></risk_flags>
    <systems><system>memory</system></systems>
  </engineering_labels>
</t2_labels>""",
        encoding="utf-8",
    )
    return package_id.replace("t2pkg-", "t2-")


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


def test_plane_read_legacy_t3_fallback_is_observable(data_root: Path, agent_id: uuid.UUID) -> None:
    """Before an agent is migrated, flat-T3 data must not disappear silently.

    The fallback is explicitly marked `migration_required` so operators see the
    debt instead of reintroducing legacy flat-T3 as a normal read path.
    """
    from app.memory.plane_read import load_plane_entries, search_plane_facts

    t3_dir = data_root / str(agent_id) / "memory" / "t3"
    t3_dir.mkdir(parents=True)
    (t3_dir / "user.md").write_text(
        "# T3 User\n\n- [2026-04-06] salary planning requires concise summaries\n",
        encoding="utf-8",
    )

    hits = search_plane_facts(data_root, agent_id, "salary planning", limit=3)

    assert hits
    assert hits[0]["source"] == "memory/t3/user.md"
    assert hits[0]["metadata"]["migration_required"] is True
    assert hits[0]["metadata"]["legacy_t3"] is True

    loaded = load_plane_entries(data_root, agent_id, [hits[0]["id"]])
    assert loaded[0]["source"] == "memory/t3/user.md"
    assert loaded[0]["metadata"]["migration_required"] is True


def test_plane_read_parses_profile_and_knowledge_activation_metadata(data_root: Path, agent_id: uuid.UUID) -> None:
    from app.memory.plane_read import list_knowledge_pages, list_profile_entries

    profile_dir = data_root / str(agent_id) / "memory" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "owner.md").write_text(
        """# Owner Profile

### Writing Taste
<!-- id: owner-writing-taste -->
aliases: [tone, voice]
tags: [writing, taste]
lifecycle: active
- Prefers concise architecture explanations.
""",
        encoding="utf-8",
    )
    knowledge_dir = data_root / str(agent_id) / "memory" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "memory-runtime.md").write_text(
        """---
title: Memory Runtime
status: active
aliases: [Attention Router, QKV Runtime]
tags: [memory, runtime]
lifecycle: active
---
## Claim
Runtime activation routes memory candidates.
## Relations
- references [[k:Agent Memory]]
""",
        encoding="utf-8",
    )

    profile_entry = list_profile_entries(data_root, agent_id)[0]
    knowledge_page = list_knowledge_pages(data_root, agent_id)[0]

    assert profile_entry["aliases"] == ["tone", "voice"]
    assert profile_entry["tags"] == ["writing", "taste"]
    assert profile_entry["lifecycle"] == "active"
    assert knowledge_page["aliases"] == ["Attention Router", "QKV Runtime"]
    assert knowledge_page["tags"] == ["memory", "runtime"]
    assert knowledge_page["lifecycle"] == "active"


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
async def test_memory_retriever_projects_agent_memory_activation_candidates(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever
) -> None:
    source_ref = "t0://session/s1/segment/seg-1#seq=1..2"
    _write_overlay_entry(
        data_root,
        agent_id,
        entry_id="ex-candidate",
        content="Salary planning must be handled with owner-first evidence.",
        metadata={"category": "constraint", "target_hint": "worker", "source_refs": source_ref},
    )

    candidates = await retriever.retrieve_candidates(agent_id, "salary planning", session_id=None, tenant_id=None)

    assert len(candidates) == 1
    manifest = candidates[0].to_manifest()
    assert manifest["candidate_kind"] == "agent_memory"
    assert manifest["candidate_ref"]["source_type"] == "explicit_overlay"
    assert manifest["key_features"]["category"] == ["constraint"]
    assert manifest["value_pointer"]["loader"] == "explicit_overlay_entry"
    assert manifest["surface"]["surface_kind"] == "memory_item"
    assert source_ref in manifest["source_refs"]
    assert manifest["score"]["head_scores"]["retrieval"] == 0.98


def test_memory_retriever_gathers_t2_evidence_candidates(data_root: Path, agent_id: uuid.UUID) -> None:
    from app.memory.reference_index import rebuild_reference_index

    short_ref = _write_t2_package(data_root, agent_id)
    rebuild_reference_index(agent_id=agent_id, data_root=data_root)
    retriever = MemoryRetriever(data_root=data_root)

    candidates = retriever.gather_t2_evidence_candidates(
        agent_id,
        keys={"risk_flag": ["privacy_sensitive"], "system": ["memory"]},
    )

    assert len(candidates) == 1
    manifest = candidates[0].to_manifest()
    assert manifest["candidate_kind"] == "agent_memory"
    assert manifest["candidate_ref"]["candidate_id"] == f"agent_memory:t2_package:{short_ref}"
    assert manifest["candidate_ref"]["source_type"] == "t2_package"
    assert manifest["key_features"]["risk_flag"] == ["privacy_sensitive"]
    assert manifest["key_features"]["system"] == ["memory"]
    assert manifest["value_pointer"]["loader"] == "t2_package"
    assert manifest["value_pointer"]["ref"] == short_ref
    assert manifest["surface"]["surface_kind"] == "evidence_pointer"
    assert manifest["source_refs"] == [short_ref]


def test_memory_retriever_gathers_t3_profile_and_knowledge_candidates(
    data_root: Path, agent_id: uuid.UUID
) -> None:
    from app.memory.reference_index import rebuild_reference_index

    profile_dir = data_root / str(agent_id) / "memory" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "owner.md").write_text(
        """## Preferences

### Writing Taste
<!-- id: owner-writing-taste -->
aliases: [tone, voice]
tags: [writing, taste]
lifecycle: active
- Prefers concise architecture explanations.
""",
        encoding="utf-8",
    )
    knowledge_dir = data_root / str(agent_id) / "memory" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "memory-runtime.md").write_text(
        """---
title: Memory Runtime
status: active
aliases: [Attention Router, QKV Runtime]
tags: [memory, runtime]
lifecycle: active
---
## Claim
Runtime activation routes memory candidates.
""",
        encoding="utf-8",
    )
    rebuild_reference_index(agent_id=agent_id, data_root=data_root)
    retriever = MemoryRetriever(data_root=data_root)

    profile = retriever.gather_t3_plane_candidates(agent_id, keys={"alias": ["tone"]}, scopes=("t3_profile",))
    knowledge = retriever.gather_t3_plane_candidates(agent_id, keys={"tag": ["memory"]}, scopes=("t3_knowledge",))

    assert profile[0].to_manifest()["candidate_ref"]["candidate_id"] == "agent_memory:t3_profile:owner-writing-taste"
    assert profile[0].to_manifest()["value_pointer"]["loader"] == "profile_entry"
    assert profile[0].to_manifest()["value_pointer"]["source"] == "memory/profiles/owner.md"
    assert knowledge[0].to_manifest()["candidate_ref"]["candidate_id"] == "agent_memory:t3_knowledge:memory-runtime"
    assert knowledge[0].to_manifest()["value_pointer"]["loader"] == "knowledge_page"
    assert knowledge[0].to_manifest()["value_pointer"]["source"] == "memory/knowledge/memory-runtime.md"


def test_memory_retriever_gathers_explicit_overlay_candidates(data_root: Path, agent_id: uuid.UUID) -> None:
    source_ref = "t0://session/s1/segment/seg-1#seq=1..2"
    _write_overlay_entry(
        data_root,
        agent_id,
        entry_id="ex-explicit",
        content="User explicitly wants red tests before implementation.",
        metadata={"category": "constraint", "target_hint": "worker", "source_refs": source_ref},
    )
    retriever = MemoryRetriever(data_root=data_root)

    candidates = retriever.gather_explicit_overlay_candidates(agent_id, query="red tests")

    assert len(candidates) == 1
    manifest = candidates[0].to_manifest()
    assert manifest["candidate_ref"]["candidate_id"] == "agent_memory:explicit_overlay:ex-explicit"
    assert manifest["candidate_ref"]["source_type"] == "explicit_overlay"
    assert manifest["key_features"]["category"] == ["constraint"]
    assert manifest["value_pointer"]["loader"] == "explicit_overlay_entry"
    assert manifest["surface"]["surface_kind"] == "memory_item"
    assert manifest["source_refs"] == [source_ref]


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
