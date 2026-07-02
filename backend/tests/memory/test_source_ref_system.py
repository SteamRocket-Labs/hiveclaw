"""Part G red tests: source-ref system — short ids + live-ref tombstones (spec §4.1).

Two kinds of source, never mixed: evidence chains point at IMMUTABLE snapshots
(t2- / ex- / fb-) and must always resolve — even after archival; narrative
anchors and live refs ([[ms-]] / [[k:]] / entry ids) point at things that
change, so convergence merges leave a tombstone (old id → surviving id) whose
truth source is an append-only jsonl; SQLite stays a rebuildable projection.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _write_segment_package(tmp_path: Path, agent_id, *, session_id: str = "sess-1", segment_id: str = "seg-1") -> str:
    package_dir = _mem_dir(tmp_path, agent_id) / "t2" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{session_id}\0{segment_id}".encode("utf-8")).hexdigest()[:16]
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": f"t2pkg-{digest}",
                "package_status": "absorbed",
                "session_id": session_id,
                "t0_segment_id": segment_id,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (package_dir / "summary.md").write_text("# summary\n", encoding="utf-8")
    return f"t2-{digest}"


# --- short-id resolution across the whole id family ---


def test_t2_short_id_resolves_to_package(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, resolve_memory_ref

    agent_id = uuid4()
    short_id = _write_segment_package(tmp_path, agent_id)
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    resolved = resolve_memory_ref(agent_id=agent_id, data_root=tmp_path, ref=short_id)

    assert resolved is not None
    assert resolved.kind == "t2_package"
    assert resolved.path.endswith("t2/sessions/sess-1/segments/seg-1")
    assert resolved.archived_at == ""


@pytest.mark.asyncio
async def test_t2_short_id_still_resolves_after_archival(tmp_path: Path) -> None:
    """证据永不悬空 is unconditional (spec §3.6/§4.1)."""
    from app.memory.reference_index import resolve_memory_ref
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    short_id = _write_segment_package(tmp_path, agent_id)
    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=None, archive_after_days=30.0)
    assert report.archived  # old + unreferenced + absorbed → archived

    resolved = resolve_memory_ref(agent_id=agent_id, data_root=tmp_path, ref=short_id)

    assert resolved is not None
    assert resolved.archived_at
    assert ".archive" in resolved.path
    assert (tmp_path / str(agent_id) / resolved.path / "manifest.json").exists()


def test_ms_and_full_uri_refs_resolve(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, resolve_memory_ref

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id)
    mdir = _mem_dir(tmp_path, agent_id) / "milestones"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "ms-web3-report.md").write_text("---\ntitle: 首胜\nstatus: active\n---\n里程碑。\n", encoding="utf-8")
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    milestone = resolve_memory_ref(agent_id=agent_id, data_root=tmp_path, ref="ms-web3-report")
    assert milestone is not None
    assert milestone.kind == "milestone"
    assert milestone.path == "memory/milestones/ms-web3-report.md"

    full = resolve_memory_ref(agent_id=agent_id, data_root=tmp_path, ref="t2://session/sess-1/segment/seg-1")
    assert full is not None
    assert full.kind == "t2_package"


def test_explicit_entry_ref_resolves(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, resolve_memory_ref

    agent_id = uuid4()
    overlay = _mem_dir(tmp_path, agent_id) / "explicit"
    (overlay / "entries").mkdir(parents=True, exist_ok=True)
    (overlay / "manifest.jsonl").write_text(
        json.dumps({"id": "ex-lang", "status": "active", "category": "preference"}) + "\n", encoding="utf-8"
    )
    (overlay / "entries" / "ex-lang.md").write_text("<normalized_memory>中文。</normalized_memory>", encoding="utf-8")
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    resolved = resolve_memory_ref(agent_id=agent_id, data_root=tmp_path, ref="ex-lang")

    assert resolved is not None
    assert resolved.kind == "explicit_entry"
    assert resolved.path == "memory/explicit/entries/ex-lang.md"


# --- live-ref tombstones (convergence merges keep old ids resolvable) ---


def test_tombstone_records_and_resolves_chain(tmp_path: Path) -> None:
    from app.memory.reference_index import (
        rebuild_reference_index,
        record_entry_tombstones,
        resolve_entry_id,
    )

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id)
    record_entry_tombstones(
        agent_id=agent_id,
        data_root=tmp_path,
        tombstones=[("cap-3", "cap-merged"), ("cap-5", "cap-merged")],
        job_id="t3job-x",
    )
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    assert resolve_entry_id(agent_id=agent_id, data_root=tmp_path, entry_id="cap-3") == "cap-merged"
    assert resolve_entry_id(agent_id=agent_id, data_root=tmp_path, entry_id="cap-merged") == "cap-merged"
    # truth source is the append-only jsonl
    log_path = _mem_dir(tmp_path, agent_id) / "control" / "tombstones.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {row["old_id"] for row in rows} == {"cap-3", "cap-5"}


def test_tombstone_chain_and_cycle_safety(tmp_path: Path) -> None:
    from app.memory.reference_index import record_entry_tombstones, resolve_entry_id

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id)
    record_entry_tombstones(agent_id=agent_id, data_root=tmp_path, tombstones=[("a", "b")], job_id="j1")
    record_entry_tombstones(agent_id=agent_id, data_root=tmp_path, tombstones=[("b", "c"), ("c", "a")], job_id="j2")

    # chain a→b→c, then cycle back to a — resolution must terminate
    resolved = resolve_entry_id(agent_id=agent_id, data_root=tmp_path, entry_id="a")
    assert resolved in {"a", "b", "c"}  # terminates without infinite loop


def test_rewrite_file_tombstones_flow_into_index(tmp_path: Path) -> None:
    """The convergence rewrite declares merges; the gate records tombstones."""
    from app.memory.reference_index import resolve_entry_id
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid4()
    self_dir = _mem_dir(tmp_path, agent_id) / "self"
    self_dir.mkdir(parents=True, exist_ok=True)
    self_path = self_dir / "self.md"
    self_path.write_text(
        "## 能力\n\n### 条目甲 — 一般\n<!-- id: cap-a -->\n内容。\n\n### 条目乙 — 一般\n<!-- id: cap-b -->\n内容。\n",
        encoding="utf-8",
    )
    sha = file_sha256(self_path)
    scores = "\n".join(
        f'<score name="{name}" value="4"><rationale>ok</rationale>'
        f"<source_refs><source_ref>t2://session/s1/segment/seg-1</source_ref></source_refs></score>"
        for name in ("evidence_strength", "scope_clarity", "stability", "future_utility", "conflict_safety")
    )
    patch_md = f"""<t3_consolidation_patch schema_version="t3.consolidation_patch.v1">
  <target_files><target_file path="memory/self/self.md"/></target_files>
  <base_revisions><base_revision path="memory/self/self.md" sha256="{sha}"/></base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1"/></source_packages>
  <evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence>
  <target_view_labels>
    <target_view>self</target_view><consolidation_mode>merge</consolidation_mode>
    <source_coverage>multi_session</source_coverage><stability>stable</stability>
    <behavior_impact>memory_policy</behavior_impact><prompt_priority>p0_if_relevant</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <rewrite_file target="memory/self/self.md" convergence_note="甲乙合并为主母题">
      <file_content><![CDATA[## 能力

### 合并母题 — 一般
<!-- id: cap-main -->
甲乙合并;refs 并集。
]]></file_content>
      <tombstone old="cap-a" new="cap-main"/>
      <tombstone old="cap-b" new="cap-main"/>
    </rewrite_file>
  </proposed_changes>
</t3_consolidation_patch>"""
    review_md = (
        '<memory_gate_review schema_version="t3.review.v1"><decision>accept</decision>'
        '<memory_gate_rubric schema_version="memory_gate_rubric.v1">'
        f"{scores}<decision>merge_required</decision></memory_gate_rubric></memory_gate_review>"
    )

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="t3job-merge",
        revised_patch_md=patch_md,
        review_md=review_md,
    )

    assert result.status == "committed", result.issues
    assert resolve_entry_id(agent_id=agent_id, data_root=tmp_path, entry_id="cap-a") == "cap-main"
    assert resolve_entry_id(agent_id=agent_id, data_root=tmp_path, entry_id="cap-b") == "cap-main"


def test_tombstones_survive_index_rebuild(tmp_path: Path) -> None:
    from app.memory.reference_index import (
        index_db_path,
        rebuild_reference_index,
        record_entry_tombstones,
        resolve_entry_id,
    )

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id)
    record_entry_tombstones(agent_id=agent_id, data_root=tmp_path, tombstones=[("old-1", "new-1")], job_id="j1")

    index_db_path(tmp_path, agent_id).unlink(missing_ok=True)
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    assert resolve_entry_id(agent_id=agent_id, data_root=tmp_path, entry_id="old-1") == "new-1"
