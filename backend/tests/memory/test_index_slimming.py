"""D7: the generated T3 index is a lightweight nav, not semantic truth.

The Entry Manifest table must carry a short summary (not the 160-char
preview that bloated the old production INDEX.md to 38KB) plus a `Heat`
column that reuses the D1 sidecar telemetry via `compute_entry_heat`.
Legacy root `INDEX.md` and orphan second index `MEMORY_INDEX.md` must never be
written.
"""

from __future__ import annotations

import uuid
from pathlib import Path

INDEX_SUMMARY_MAX_CHARS = 60


def _seed_long_entry(tmp_path: Path) -> tuple[uuid.UUID, Path, str]:
    from app.memory.md_store import append_t3_entry

    agent_id = uuid.uuid4()
    long_content = (
        "Railway deploys require external health verification because the build "
        "layer caches silently and a green CI run does not prove the container "
        "actually boots in production with the new code path enabled."
    )
    append_t3_entry(
        tmp_path,
        agent_id,
        category="knowledge",
        content=long_content,
        timestamp="2026-05-28",
        metadata={"entry_id": "knowledge-long-1", "sensitivity": "PL1_public"},
    )
    mem_dir = tmp_path / str(agent_id) / "memory"
    return agent_id, mem_dir, long_content


def test_entry_manifest_row_uses_short_summary_not_full_preview(tmp_path: Path) -> None:
    from app.memory.md_store import rebuild_index

    agent_id, mem_dir, long_content = _seed_long_entry(tmp_path)
    rebuild_index(tmp_path, agent_id)
    index = (mem_dir / "indexes" / "wiki_map.md").read_text(encoding="utf-8")

    # The full 160-char preview must NOT be mirrored into the nav index.
    assert long_content not in index

    # The summary cell for the entry must be short (<= 60 chars of content).
    manifest_lines = [ln for ln in index.splitlines() if "knowledge-long-1" in ln]
    assert manifest_lines, "entry row missing from Entry Manifest"
    summary_cell = manifest_lines[0].split("|")[-2].strip()
    assert len(summary_cell.replace("…", "")) <= INDEX_SUMMARY_MAX_CHARS


def test_entry_manifest_carries_heat_column(tmp_path: Path) -> None:
    from app.memory.access_log import bump_access
    from app.memory.md_store import compute_entry_heat, rebuild_index, read_access_telemetry

    agent_id, mem_dir, _ = _seed_long_entry(tmp_path)
    # Drive sidecar telemetry (D1) so heat is non-trivial and provably joined.
    bump_access(tmp_path, agent_id, file_relpath="memory/knowledge.md", entry_id="knowledge-long-1")
    bump_access(tmp_path, agent_id, file_relpath="memory/knowledge.md", entry_id="knowledge-long-1")

    rebuild_index(tmp_path, agent_id)
    index = (mem_dir / "indexes" / "wiki_map.md").read_text(encoding="utf-8")

    assert "| ID | File | Category | Date | Load | Heat | Summary |" in index

    telemetry = read_access_telemetry(tmp_path, agent_id)
    expected_heat = compute_entry_heat(telemetry.get("knowledge-long-1", {}))
    assert expected_heat > 0.0

    row = next(ln for ln in index.splitlines() if "knowledge-long-1" in ln)
    heat_cell = row.split("|")[-3].strip()
    assert heat_cell == str(expected_heat)


def test_rebuild_index_writes_only_canonical_derived_index(tmp_path: Path) -> None:
    from app.memory.md_store import rebuild_index

    agent_id, mem_dir, _ = _seed_long_entry(tmp_path)
    rebuild_index(tmp_path, agent_id)

    assert (mem_dir / "indexes" / "wiki_map.md").exists()
    assert not (mem_dir / "wiki_map.md").exists()
    assert not (mem_dir / "INDEX.md").exists()
    assert not (mem_dir / "index.md").exists()
    assert not (mem_dir / ".derived" / "t3_index.md").exists()
    assert not (mem_dir / "MEMORY_INDEX.md").exists()
