from __future__ import annotations

import uuid
from pathlib import Path


def test_rebuild_index_writes_entry_manifest_with_stable_ids(tmp_path: Path) -> None:
    from app.memory.md_store import append_t3_entry, build_t3_entry_manifest

    agent_id = uuid.uuid4()
    append_t3_entry(
        tmp_path,
        agent_id,
        category="feedback",
        content="User requires Chinese responses",
        timestamp="2026-05-28",
        metadata={"entry_id": "feedback-entry-1", "sensitivity": "PL1_public"},
    )
    mem_dir = tmp_path / str(agent_id) / "memory"
    (mem_dir / "t3" / "capabilities.md").write_text(
        "# T3 Capabilities\n\n"
        "- [2026-05-27] Railway deploys require external health verification\n",
        encoding="utf-8",
    )
    from app.memory.md_store import rebuild_index

    rebuild_index(tmp_path, agent_id)
    index = (mem_dir / "indexes" / "wiki_map.md").read_text(encoding="utf-8")
    manifest = build_t3_entry_manifest(tmp_path, agent_id)

    assert "## Entry Manifest" in index
    assert "| ID | File | Category | Date | Load | Heat | Summary |" in index
    assert "feedback-entry-1" in index
    assert "Railway deploys require external health verification" in index
    assert [entry.entry_id for entry in manifest if entry.source == "memory/t3/user.md"] == ["feedback-entry-1"]
    legacy = next(entry for entry in manifest if entry.source == "memory/t3/capabilities.md")
    assert legacy.entry_id.startswith("mem_")
    assert legacy.source == "memory/t3/capabilities.md"
    assert legacy.preview == "Railway deploys require external health verification"
    assert not (mem_dir / "INDEX.md").exists()
    assert not (mem_dir / "index.md").exists()
    assert not (mem_dir / "wiki_map.md").exists()
    assert not (mem_dir / ".derived" / "t3_index.md").exists()


def test_load_t3_entries_by_ids_resolves_full_content(tmp_path: Path) -> None:
    from app.memory.md_store import append_t3_entry, load_t3_entries_by_ids

    agent_id = uuid.uuid4()
    append_t3_entry(
        tmp_path,
        agent_id,
        category="strategy",
        content="Use index manifests before expanding old memory entries",
        timestamp="2026-05-28",
        metadata={"entry_id": "strategy-entry-1"},
    )

    entries = load_t3_entries_by_ids(tmp_path, agent_id, ["strategy-entry-1", "missing-id"])

    assert [entry.entry_id for entry in entries] == ["strategy-entry-1"]
    assert entries[0].content == "Use index manifests before expanding old memory entries"
    assert entries[0].source == "memory/t3/capabilities.md"


def test_expired_t3_entries_are_manifested_but_excluded_from_fact_retrieval(tmp_path: Path) -> None:
    from app.memory.md_store import append_t3_entry, build_t3_entry_manifest, parse_t3_facts

    agent_id = uuid.uuid4()
    append_t3_entry(
        tmp_path,
        agent_id,
        category="project",
        content="Temporary launch window closed",
        timestamp="2026-05-28",
        metadata={
            "entry_id": "expired-knowledge-1",
            "expires_at": "2020-01-01T00:00:00+00:00",
        },
    )
    append_t3_entry(
        tmp_path,
        agent_id,
        category="project",
        content="Durable launch checklist",
        timestamp="2026-05-28",
        metadata={"entry_id": "active-knowledge-1"},
    )

    manifest = build_t3_entry_manifest(tmp_path, agent_id)
    expired = next(entry for entry in manifest if entry.entry_id == "expired-knowledge-1")
    facts = parse_t3_facts(tmp_path, agent_id)

    assert expired.metadata["expired"] == "true"
    assert [fact["id"] for fact in facts] == ["active-knowledge-1"]


def test_manifest_validates_local_evidence_refs(tmp_path: Path) -> None:
    from app.memory.md_store import append_t3_entry, build_t3_entry_manifest

    agent_id = uuid.uuid4()
    mem_dir = tmp_path / str(agent_id) / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "source.md").write_text("# source\n", encoding="utf-8")
    append_t3_entry(
        tmp_path,
        agent_id,
        category="project",
        content="Claim with one missing source ref",
        timestamp="2026-05-28",
        metadata={
            "entry_id": "ref-check-1",
            "evidence_refs": "memory/source.md,memory/missing.md,tool:save_memory",
        },
    )

    entry = next(item for item in build_t3_entry_manifest(tmp_path, agent_id) if item.entry_id == "ref-check-1")

    assert entry.metadata["reference_status"] == "invalid"
    assert entry.metadata["invalid_evidence_refs"] == "memory/missing.md"
