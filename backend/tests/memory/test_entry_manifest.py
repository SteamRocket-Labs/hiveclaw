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
    (mem_dir / "knowledge.md").write_text(
        "# Knowledge\n\n- [2026-05-27] Railway deploys require external health verification\n",
        encoding="utf-8",
    )
    from app.memory.md_store import rebuild_index

    rebuild_index(tmp_path, agent_id)
    index = (mem_dir / "INDEX.md").read_text(encoding="utf-8")
    manifest = build_t3_entry_manifest(tmp_path, agent_id)

    assert "## Entry Manifest" in index
    assert "| ID | File | Category | Date | Load | Heat | Summary |" in index
    assert "feedback-entry-1" in index
    assert "Railway deploys require external health verification" in index
    assert [entry.entry_id for entry in manifest if entry.category == "feedback"] == ["feedback-entry-1"]
    legacy = next(entry for entry in manifest if entry.category == "project")
    assert legacy.entry_id.startswith("mem_")
    assert legacy.source == "memory/knowledge.md"
    assert legacy.preview == "Railway deploys require external health verification"


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
    assert entries[0].source == "memory/strategies.md"
