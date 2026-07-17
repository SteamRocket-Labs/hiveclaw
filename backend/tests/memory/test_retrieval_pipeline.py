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
from types import SimpleNamespace

import pytest

from app.memory.activation import ActivationContext
from app.memory.retriever import MAX_SELECTION_DESCRIPTOR_BYTES, MemoryRetriever, _selection_descriptor
from app.memory.types import MemoryItem, MemoryKind
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


def test_memory_selector_manifest_uses_bounded_descriptors_not_full_bodies() -> None:
    body = "full-body-prefix " + ("sensitive-tail " * 200)
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content=body,
        score=0.9,
        source="memory/knowledge/payroll.md",
        metadata={
            "selection_candidate_id": "memory_candidate:semantic:0:abc",
            "title": "Payroll exception",
            "description": "One-line hook for the governed payroll exception.",
            "entry_id": "knowledge/payroll",
            "source_type": "knowledge_ppr",
        },
    )

    descriptor = _selection_descriptor(item)
    serialized = json.dumps(descriptor, ensure_ascii=False)

    assert descriptor["name"] == "Payroll exception"
    assert descriptor["description"] == "One-line hook for the governed payroll exception."
    assert len(descriptor["description"].encode("utf-8")) <= MAX_SELECTION_DESCRIPTOR_BYTES
    assert "full-body-prefix" not in serialized
    assert "sensitive-tail" not in serialized
    assert descriptor["load_ref"]["entry_id"] == "knowledge/payroll"


@pytest.mark.asyncio
async def test_model_selector_call_receives_only_descriptor_manifest(monkeypatch, tmp_path) -> None:
    observed: dict[str, str] = {}
    candidate_id = "memory_candidate:semantic:0:abc"
    item = MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content="FULL_MEMORY_BODY_DO_NOT_SEND_TO_SELECTOR " + ("tail " * 1000),
        score=0.9,
        source="memory/knowledge/payroll.md",
        metadata={
            "selection_candidate_id": candidate_id,
            "title": "Payroll exception",
            "description": "Bounded manifest hook",
            "entry_id": "knowledge/payroll",
            "source_type": "knowledge_ppr",
        },
    )

    async def fake_covered_input(*, sections, **_kwargs):
        payload = "\n".join(section for _section_id, section in sections)
        observed["manifest"] = payload
        return payload

    class _Client:
        async def complete(self, *, messages, **_kwargs):
            observed["final_prompt"] = messages[-1].content
            return SimpleNamespace(content=json.dumps({"selected_ids": [candidate_id], "reason": "manifest match"}))

        async def close(self):
            return None

    monkeypatch.setattr(
        "app.services.semantic_input_coverage.prepare_covered_semantic_input",
        fake_covered_input,
    )
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _config: _Client())
    monkeypatch.setattr("app.services.llm_client.with_llm_usage_context", lambda config, **_kwargs: config)

    selected, reason = await MemoryRetriever(data_root=tmp_path)._select_with_model(
        items=[item],
        agent_id=uuid.uuid4(),
        tenant_id=None,
        query="payroll",
        model_config={"provider": "fake", "model": "fake"},
        selection_run_id="selection-test",
    )

    assert selected == [candidate_id]
    assert reason == "manifest match"
    assert "Bounded manifest hook" in observed["manifest"]
    assert "FULL_MEMORY_BODY_DO_NOT_SEND_TO_SELECTOR" not in observed["manifest"]
    assert "FULL_MEMORY_BODY_DO_NOT_SEND_TO_SELECTOR" not in observed["final_prompt"]


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


def _write_complete_overlay_fixture(data_root: Path, agent_id: uuid.UUID) -> None:
    for index in range(12):
        content = (
            "The decisive exception is preserved only in the final record."
            if index == 11
            else f"unrelated archived observation {index}"
        )
        _write_overlay_entry(data_root, agent_id, entry_id=f"ex-{index}", content=content)


@pytest.mark.asyncio
async def test_model_selector_sees_every_authorized_candidate_and_can_choose_a_low_mechanical_tail(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever, monkeypatch
) -> None:
    """Lexical score and fixed top-k are observations, never semantic authority."""

    _write_complete_overlay_fixture(data_root, agent_id)
    observed: dict[str, object] = {}

    async def fake_selector(*, items, **_kwargs):
        observed["contents"] = [item.content for item in items]
        decisive = next(item for item in items if "decisive exception" in item.content)
        return [decisive.metadata["selection_candidate_id"]], "tail exception is decisive"

    monkeypatch.setattr(retriever, "_select_with_model", fake_selector, raising=False)
    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        rerank_model_config={"provider": "fake", "model": "fake"},
    )

    assert len(observed["contents"]) == 12
    assert len(items) == 1
    assert "decisive exception" in items[0].content
    assert items[0].metadata["semantic_selection_status"] == "model_selected"
    assert retriever.last_selection_status == "model_selected"


@pytest.mark.asyncio
async def test_model_selector_failure_surfaces_no_bodies_but_keeps_observable_coverage(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever, monkeypatch
) -> None:
    _write_complete_overlay_fixture(data_root, agent_id)

    async def failed_selector(**_kwargs):
        raise RuntimeError("selector unavailable")

    monkeypatch.setattr(retriever, "_select_with_model", failed_selector, raising=False)
    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        rerank_model_config={"provider": "fake", "model": "fake"},
    )

    assert items == []
    assert retriever.last_selection_status == "failed"
    assert retriever.last_selection_error == "RuntimeError"
    assert retriever.last_selection_receipt


@pytest.mark.asyncio
async def test_missing_selector_model_surfaces_no_bodies_instead_of_dumping_candidates(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever
) -> None:
    _write_complete_overlay_fixture(data_root, agent_id)

    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        rerank_model_config=None,
    )

    assert items == []
    assert retriever.last_selection_status == "model_unavailable"
    assert retriever.last_selection_receipt


@pytest.mark.asyncio
async def test_selector_cannot_bypass_five_item_auto_surface_contract(
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever, monkeypatch
) -> None:
    _write_complete_overlay_fixture(data_root, agent_id)

    async def oversized_selection(*, items, **_kwargs):
        return [item.metadata["selection_candidate_id"] for item in items[:6]], "six items"

    monkeypatch.setattr(retriever, "_select_with_model", oversized_selection, raising=False)
    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        rerank_model_config={"provider": "fake", "model": "fake"},
    )

    assert items == []
    assert retriever.last_selection_status == "failed"
    assert retriever.last_selection_error == "ValueError"


def test_memory_retriever_exposes_only_live_retrieval_entrypoints(data_root: Path) -> None:
    retriever = MemoryRetriever(data_root=data_root)

    assert callable(retriever.retrieve)
    for dead_api in (
        "retrieve_candidates",
        "gather_explicit_overlay_candidates",
        "gather_t2_evidence_candidates",
        "gather_t3_plane_candidates",
    ):
        assert not hasattr(retriever, dead_api)


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
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever, monkeypatch
) -> None:
    _write_overlay_entry(
        data_root,
        agent_id,
        entry_id="ex-open-loop",
        content="Salary planning for Acme is an open loop for Alice",
        metadata={"retention_score": "0.5", "confidence": "0.9"},
    )

    async def select_open_loop(*, items, **_kwargs):
        candidate = next(item for item in items if "open loop" in item.content)
        return [candidate.metadata["selection_candidate_id"]], "open loop is relevant"

    monkeypatch.setattr(retriever, "_select_with_model", select_open_loop, raising=False)
    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        activation_context=_activation_context(),
        rerank_model_config={"provider": "fake", "model": "fake"},
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
    data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever, monkeypatch
) -> None:
    kdir = data_root / str(agent_id) / "memory" / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "salary-bands.md").write_text(
        "---\ntitle: Salary Bands\nstatus: active\n---\n## Current Claim\nsalary planning bands annually calibrated.\n"
        "## Relations\n- references [[k:Compensation]]\n",
        encoding="utf-8",
    )

    async def select_knowledge(*, items, **_kwargs):
        candidate = next(item for item in items if item.metadata.get("source_type") == "knowledge_ppr")
        return [candidate.metadata["selection_candidate_id"]], "knowledge claim is relevant"

    monkeypatch.setattr(retriever, "_select_with_model", select_knowledge, raising=False)
    items = await retriever.retrieve(
        agent_id,
        "salary planning",
        session_id=None,
        tenant_id=None,
        rerank_model_config={"provider": "fake", "model": "fake"},
    )

    knowledge = [item for item in items if item.metadata.get("source_type") == "knowledge_ppr"]
    assert knowledge
    assert any("Salary Bands" in item.content for item in knowledge)
