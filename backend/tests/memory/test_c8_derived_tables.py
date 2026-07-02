"""Part I red tests: C8 derived tables (spec §6.4).

``memory/indexes/index.sqlite`` already carries refs / id_resolution /
tombstones (Part G). C8 completes the derived set with the last two tables:

- ``t2_label_axes`` — composite T2 labels split per axis so observability can
  query "which segments carry risk_flag=privacy_sensitive" or "which segments
  feed the knowledge plane" without re-parsing every labels.md.
- ``consolidation_debt_history`` — the debt ledger as a table. The append-only
  observation record is ``memory/control/consolidation_debt_history.jsonl``
  (written by ``refresh_consolidation_debt``); the table is derived from it.

Both tables are pure derivations: deleting index.sqlite and rebuilding from
MD + jsonl restores them. The read surface is
``knowledge_read_model.build_memory_observability`` (admin observability).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _write_labeled_package(
    tmp_path: Path,
    agent_id,
    *,
    session_id: str = "sess-1",
    segment_id: str = "seg-1",
    package_id: str | None = None,
    labels_body: str | None = None,
) -> str:
    package_dir = _mem_dir(tmp_path, agent_id) / "t2" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    resolved = package_id or f"t2pkg-{segment_id}"
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": resolved,
                "package_status": "reviewed",
                "session_id": session_id,
                "t0_segment_id": segment_id,
                "created_at": (NOW - timedelta(hours=2)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (package_dir / "summary.md").write_text("# summary\n测试段包。\n", encoding="utf-8")
    default_labels = f"""<t2_labels schema_version="t2.labels.v1" package_id="{resolved}">
  <continuity_state>standalone</continuity_state>
  <engineering_labels>
    <confidence>0.85</confidence>
    <source_integrity>complete</source_integrity>
    <risk_flags><risk_flag>privacy_sensitive</risk_flag><risk_flag>evidence_gap</risk_flag></risk_flags>
    <systems><system>memory</system><system>workflow</system></systems>
  </engineering_labels>
  <event_labels>
    <event_label event_ref="evt-1"><memory_domain>decision</memory_domain></event_label>
  </event_labels>
  <four_plane_signals>
    <self_signal present="true">首次独立完成研报拆解。</self_signal>
    <nutrients>
      <nutrient plane="self">能力证据</nutrient>
      <nutrient plane="knowledge">L2 概念</nutrient>
    </nutrients>
    <milestone_signal criteria="first_success">该类任务首次成功。</milestone_signal>
  </four_plane_signals>
</t2_labels>"""
    (package_dir / "labels.md").write_text(labels_body or default_labels, encoding="utf-8")
    (package_dir / "review.md").write_text(
        f"""<t2_review schema_version="t2.review.v1" package_id="{resolved}">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
</t2_review>""",
        encoding="utf-8",
    )
    return resolved


def _axis_rows(tmp_path: Path, agent_id) -> set[tuple[str, str, str]]:
    from app.memory.reference_index import index_db_path

    with sqlite3.connect(index_db_path(tmp_path, agent_id)) as conn:
        return {(row[0], row[1], row[2]) for row in conn.execute("SELECT package_ref, axis, value FROM t2_label_axes")}


def test_rebuild_populates_label_axes_from_t2_labels(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index

    agent_id = uuid4()
    _write_labeled_package(tmp_path, agent_id)

    report = rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    assert report.label_axis_rows >= 9
    rows = _axis_rows(tmp_path, agent_id)
    refs = {ref for ref, _, _ in rows}
    assert len(refs) == 1
    ref = next(iter(refs))
    assert ref.startswith("t2-")
    assert (ref, "continuity_state", "standalone") in rows
    assert (ref, "confidence", "0.85") in rows
    assert (ref, "source_integrity", "complete") in rows
    assert (ref, "risk_flag", "privacy_sensitive") in rows
    assert (ref, "risk_flag", "evidence_gap") in rows
    assert (ref, "system", "memory") in rows
    assert (ref, "system", "workflow") in rows
    assert (ref, "memory_domain", "decision") in rows
    assert (ref, "nutrient_plane", "self") in rows
    assert (ref, "nutrient_plane", "knowledge") in rows
    assert (ref, "self_signal", "true") in rows
    assert (ref, "milestone_criteria", "first_success") in rows


def test_minimal_labels_produce_only_present_axes(tmp_path: Path) -> None:
    """Missing axes stay absent — no guessed rows (evidence gap discipline)."""
    from app.memory.reference_index import rebuild_reference_index

    agent_id = uuid4()
    resolved = "t2pkg-minimal"
    _write_labeled_package(
        tmp_path,
        agent_id,
        segment_id="seg-min",
        package_id=resolved,
        labels_body=f"""<t2_labels schema_version="t2.labels.v1" package_id="{resolved}">
  <continuity_state>low_signal</continuity_state>
</t2_labels>""",
    )

    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    rows = _axis_rows(tmp_path, agent_id)
    axes = {axis for _, axis, _ in rows}
    assert axes == {"continuity_state"}


def test_label_axes_rebuild_is_pure_derivation(tmp_path: Path) -> None:
    from app.memory.reference_index import index_db_path, rebuild_reference_index

    agent_id = uuid4()
    _write_labeled_package(tmp_path, agent_id)
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)
    before = _axis_rows(tmp_path, agent_id)
    assert before

    index_db_path(tmp_path, agent_id).unlink()
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    assert _axis_rows(tmp_path, agent_id) == before


@pytest.mark.asyncio
async def test_refresh_debt_appends_history_and_populates_table(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt
    from app.memory.reference_index import index_db_path

    agent_id = uuid4()
    _write_labeled_package(tmp_path, agent_id)

    await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)
    await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW + timedelta(hours=1))

    history_path = _mem_dir(tmp_path, agent_id) / "control" / "consolidation_debt_history.jsonl"
    lines = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["pending_packages"] == 1
    assert lines[0]["assessed_at"] != lines[1]["assessed_at"]

    with sqlite3.connect(index_db_path(tmp_path, agent_id)) as conn:
        table_rows = conn.execute(
            "SELECT assessed_at, pending_packages, stalled FROM consolidation_debt_history ORDER BY assessed_at"
        ).fetchall()
    assert len(table_rows) == 2
    assert table_rows[0][1] == 1


@pytest.mark.asyncio
async def test_debt_table_rebuilds_from_history_jsonl(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt
    from app.memory.reference_index import index_db_path, rebuild_reference_index

    agent_id = uuid4()
    _write_labeled_package(tmp_path, agent_id)
    await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)
    await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW + timedelta(hours=1))

    index_db_path(tmp_path, agent_id).unlink()
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    with sqlite3.connect(index_db_path(tmp_path, agent_id)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM consolidation_debt_history").fetchone()[0]
    assert count == 2


@pytest.mark.asyncio
async def test_memory_observability_read_model(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt
    from app.memory.reference_index import rebuild_reference_index
    from app.services.knowledge_read_model import build_memory_observability

    agent_id = uuid4()
    _write_labeled_package(tmp_path, agent_id)
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)
    await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    observability = build_memory_observability(tmp_path, agent_id)

    assert observability["debt"]["pending_packages"] == 1
    assert observability["debt"]["assessed_at"]
    assert len(observability["debt_history"]) == 1
    axes = observability["label_axes"]
    assert axes["risk_flag"]["privacy_sensitive"] == 1
    assert axes["nutrient_plane"]["knowledge"] == 1
    assert axes["continuity_state"]["standalone"] == 1


def test_plane_evidence_refs_count_for_retention(tmp_path: Path) -> None:
    """Part H gap closed here: two-plane citations must reach the reverse
    index. A knowledge page or self.md entry citing ``t2-<hash>`` counts as a
    live referrer under the canonical ``t2://`` ref, so retention keeps the
    package hot instead of treating it as unreferenced."""
    from app.memory.reference_index import rebuild_reference_index, reference_count

    agent_id = uuid4()
    _write_labeled_package(tmp_path, agent_id, package_id="t2pkg-a1b2c3d4")
    mem = _mem_dir(tmp_path, agent_id)
    (mem / "knowledge").mkdir(parents=True, exist_ok=True)
    (mem / "knowledge" / "l2-rollup.md").write_text(
        "---\ntitle: L2 Rollup\nstatus: active\n---\n## Current Claim\n链下计算扩容。\n"
        "## Evidence\nt2-a1b2c3d4\n\n## Relations\n- is_a [[k:scaling]]\n",
        encoding="utf-8",
    )
    (mem / "self").mkdir(parents=True, exist_ok=True)
    (mem / "self" / "self.md").write_text(
        "## 能力\n\n### 深度研究 — 熟练\n<!-- id: cap-deep-research -->\n拆解与检索。\n- 证据: t2-a1b2c3d4\n",
        encoding="utf-8",
    )

    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    canonical = "t2://session/sess-1/segment/seg-1"
    assert reference_count(agent_id=agent_id, data_root=tmp_path, ref=canonical) == 2
    assert reference_count(agent_id=agent_id, data_root=tmp_path, ref="t2-a1b2c3d4") == 2


def test_observability_empty_agent_is_calm(tmp_path: Path) -> None:
    """No packages, no history — observability reports empty, never raises."""
    from app.services.knowledge_read_model import build_memory_observability

    agent_id = uuid4()
    _mem_dir(tmp_path, agent_id).mkdir(parents=True)

    observability = build_memory_observability(tmp_path, agent_id)

    assert observability["debt"] == {}
    assert observability["debt_history"] == []
    assert observability["label_axes"] == {}
