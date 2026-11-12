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
from app.memory.retriever import MemoryRetriever
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def retriever(data_root: Path, monkeypatch) -> MemoryRetriever:
    from tests.memory.authority_fixtures import reference_explicit_memory_authority

    reference_explicit_memory_authority(monkeypatch)
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


def _write_t2_package(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    session_id: str = "sess-1",
    segment_id: str = "seg-1",
    package_id: str = "t2pkg-evidence",
    summary: str = "Evidence for memory runtime.",
    source_refs: list[str] | None = None,
) -> str:
    package_dir = data_root / str(agent_id) / "memory" / "t2" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": package_id,
                "package_status": "reviewed",
                "session_id": session_id,
                "t0_segment_id": segment_id,
                "created_at": "2026-07-05T00:00:00+00:00",
                "source_refs": source_refs or [f"t0://session/{session_id}/segment/{segment_id}#seq=1..2"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (package_dir / "summary.md").write_text(f"# Summary\n{summary}\n", encoding="utf-8")
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


@pytest.mark.asyncio
async def test_episodic_retrieval_uses_t2_packages_without_reading_chat_session_summary(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
    monkeypatch,
) -> None:
    current_session_id = str(uuid.uuid4())
    previous_session_id = str(uuid.uuid4())
    current_ref = f"t0://session/{current_session_id}/segment/current#seq=1..2"
    previous_ref = f"t0://session/{previous_session_id}/segment/previous#seq=4..6"
    _write_t2_package(
        data_root,
        agent_id,
        session_id=current_session_id,
        segment_id="current",
        package_id="t2pkg-current",
        summary="Current T2 summary is canonical.",
        source_refs=[current_ref],
    )
    _write_t2_package(
        data_root,
        agent_id,
        session_id=previous_session_id,
        segment_id="previous",
        package_id="t2pkg-previous",
        summary="Previous T2 summary is canonical.",
        source_refs=[previous_ref],
    )

    async def reject_db_tenant_resolution(_agent_id):
        raise AssertionError("episodic prompt retrieval must not read ChatSession.summary")

    monkeypatch.setattr(
        "app.services.tenant_resolver.resolve_tenant_for_agent",
        reject_db_tenant_resolution,
    )

    items = await retriever._retrieve_episodic(agent_id, current_session_id)

    assert len(items) == 2
    by_session = {item.metadata["session_id"]: item for item in items}
    assert set(by_session) == {current_session_id, previous_session_id}
    assert by_session[current_session_id].metadata["is_current_session"] is True
    assert by_session[previous_session_id].metadata["is_current_session"] is False
    assert by_session[current_session_id].metadata["source_type"] == "t2_package"
    assert by_session[current_session_id].metadata["source_refs"] == [current_ref]
    assert "Current T2 summary is canonical." in by_session[current_session_id].content
    assert current_ref in by_session[current_session_id].content
    assert by_session[current_session_id].source == (f"memory/t2/sessions/{current_session_id}/segments/current")


@pytest.mark.asyncio
async def test_episodic_retrieval_preserves_every_authorized_t2_package(
    data_root: Path,
    agent_id: uuid.UUID,
    retriever: MemoryRetriever,
) -> None:
    for index in range(7):
        _write_t2_package(
            data_root,
            agent_id,
            session_id=f"session-{index}",
            segment_id=f"segment-{index}",
            package_id=f"t2pkg-{index}",
            summary=f"Authorized package {index}",
        )

    items = await retriever._retrieve_episodic(agent_id, None, previous_limit=1)

    assert len(items) == 7
    assert {item.metadata["session_id"] for item in items} == {f"session-{index}" for index in range(7)}


def test_episodic_prompt_authority_is_t2_not_chat_session_summary() -> None:
    import inspect

    source = inspect.getsource(MemoryRetriever._retrieve_episodic)

    assert "load_t2_package_snapshots" in source
    assert "render_t2_package_snapshots" in source
    assert "ChatSession" not in source
    assert "tenant_scoped_session" not in source


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
async def test_gather_no_files(data_root: Path, agent_id: uuid.UUID, retriever: MemoryRetriever) -> None:
    items = await retriever._gather_authorized_candidates(agent_id, "anything", None, None)

    assert [item for item in items if item.kind == MemoryKind.SEMANTIC] == []


def test_memory_retriever_keeps_only_the_offline_eval_seam(data_root: Path) -> None:
    retriever = MemoryRetriever(data_root=data_root)

    # Live prompt assembly no longer retrieves through the retriever; the
    # deterministic offline recall eval still walks the gather seam.
    assert callable(retriever._gather_authorized_candidates)
    for dead_api in (
        "retrieve",
        "_select_candidates",
        "_select_with_model",
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

    items = await retriever._gather_authorized_candidates(
        agent_id,
        "salary planning",
        None,
        None,
        activation_context=_activation_context(current_user_id="viewer-1"),
    )

    assert all("owner-only handling" not in item.content for item in items)


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

    items = await retriever._gather_authorized_candidates(
        agent_id,
        "salary planning",
        None,
        None,
        activation_context=_activation_context(),
    )

    item = next(candidate for candidate in items if "open loop" in candidate.content)
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

    items = await retriever._gather_authorized_candidates(
        agent_id,
        "salary planning",
        None,
        None,
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

    items = await retriever._gather_authorized_candidates(agent_id, "salary planning", None, None)

    knowledge = [item for item in items if item.metadata.get("source_type") == "knowledge_ppr"]
    assert knowledge
    assert any("Salary Bands" in item.content for item in knowledge)
