"""C9-3 red tests: reference-counted T2 retention with archival (spec §3.6/§4.1/§6.2.3).

T2 packages are the evidence backbone: referenced packages stay hot forever,
unreferenced ones eventually archive — but NOTHING is ever hard-deleted and a
``t2://`` ref must resolve for the rest of the agent's life. Contract:

- ``app.memory.reference_index`` — SQLite reverse-reference index at
  ``memory/indexes/index.sqlite``, rebuilt entirely from Markdown truth
  (T3 accepted blocks, active explicit overlay entries, episode manifests,
  live + archived package dirs). Derived only: deleting the DB loses nothing.
- ``app.memory.t2_retention`` — archive executor: a package with zero
  references, older than the threshold, not in the consolidation pipeline and
  not decision/permission-domain moves (never deletes) to
  ``memory/.archive/t2/**`` with an append-only archive log; its ref still
  resolves to the archived path afterwards.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _iso_days_ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _seg_ref(session_id: str, segment_id: str) -> str:
    return f"t2://session/{session_id}/segment/{segment_id}"


def _write_segment_package(
    tmp_path: Path,
    agent_id,
    *,
    session_id: str = "sess-1",
    segment_id: str = "seg-1",
    package_status: str = "absorbed",
    allowed_next: str = "t3_intake",
    created_days_ago: float = 60.0,
    memory_domain: str = "preference_memory",
    package_id: str | None = None,
) -> Path:
    package_dir = _mem_dir(tmp_path, agent_id) / "t2" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    resolved_package_id = package_id or f"t2pkg-{segment_id}"
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": resolved_package_id,
                "package_status": package_status,
                "session_id": session_id,
                "t0_segment_id": segment_id,
                "created_at": _iso_days_ago(created_days_ago),
            }
        ),
        encoding="utf-8",
    )
    (package_dir / "summary.md").write_text("# summary\n测试段包。\n", encoding="utf-8")
    (package_dir / "labels.md").write_text(
        f"""<t2_labels schema_version="t2.labels.v1" package_id="{resolved_package_id}">
  <control_metadata><package_status>closed</package_status></control_metadata>
  <event_labels>
    <event_label event_ref="evt-1"><memory_domain>{memory_domain}</memory_domain></event_label>
  </event_labels>
</t2_labels>""",
        encoding="utf-8",
    )
    (package_dir / "review.md").write_text(
        f"""<t2_review schema_version="t2.review.v1" package_id="{resolved_package_id}">
  <decision>approved</decision>
  <allowed_next>{allowed_next}</allowed_next>
</t2_review>""",
        encoding="utf-8",
    )
    return package_dir


def _write_t3_block_with_ref(tmp_path: Path, agent_id, *, ref: str, target: str = "user.md") -> None:
    t3_dir = _mem_dir(tmp_path, agent_id) / "t3"
    t3_dir.mkdir(parents=True, exist_ok=True)
    path = t3_dir / target
    block = f"""<t3_user_preference id="blk-{abs(hash(ref)) % 10_000}">
  <claim>来自证据的偏好结论。</claim>
  <evidence><source_ref>{ref}</source_ref></evidence>
</t3_user_preference>
"""
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n" + block)


def _write_explicit_entry_with_ref(
    tmp_path: Path, agent_id, *, entry_id: str, ref: str, status: str = "active"
) -> None:
    overlay_dir = _mem_dir(tmp_path, agent_id) / "explicit"
    (overlay_dir / "entries").mkdir(parents=True, exist_ok=True)
    with (overlay_dir / "manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": entry_id,
                    "status": status,
                    "category": "general",
                    "created_at": _iso_days_ago(1.0),
                    "source_refs": ref,
                }
            )
            + "\n"
        )
    (overlay_dir / "entries" / f"{entry_id}.md").write_text(
        "<normalized_memory>用户明确要求记住。</normalized_memory>",
        encoding="utf-8",
    )


def _write_episode_package_referencing(
    tmp_path: Path,
    agent_id,
    *,
    session_id: str = "sess-1",
    episode_id: str = "ep-1",
    source_package_ids: list[str],
) -> Path:
    episode_dir = _mem_dir(tmp_path, agent_id) / "t2" / "sessions" / session_id / "episodes" / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.episode-stitch.manifest.v1",
                "episode_id": episode_id,
                "trigger_package_id": source_package_ids[0],
                "source_packages": source_package_ids,
                "package_status": "reviewed",
                "session_id": session_id,
                "created_at": _iso_days_ago(1.0),
            }
        ),
        encoding="utf-8",
    )
    return episode_dir


# --- reference index (SQLite, rebuilt from MD) ---


def test_rebuild_counts_t3_explicit_and_episode_references(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, reference_count

    agent_id = uuid4()
    ref_a = _seg_ref("sess-1", "seg-a")
    ref_b = _seg_ref("sess-1", "seg-b")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-a", package_id="t2pkg-seg-a")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-b", package_id="t2pkg-seg-b")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-none", package_id="t2pkg-seg-none")
    _write_t3_block_with_ref(tmp_path, agent_id, ref=ref_a)
    _write_explicit_entry_with_ref(tmp_path, agent_id, entry_id="ex-1", ref=ref_a)
    _write_episode_package_referencing(tmp_path, agent_id, source_package_ids=["t2pkg-seg-b"])

    report = rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    assert report.referrers >= 3
    assert reference_count(agent_id=agent_id, data_root=tmp_path, ref=ref_a) == 2
    assert reference_count(agent_id=agent_id, data_root=tmp_path, ref=ref_b) == 1
    assert reference_count(agent_id=agent_id, data_root=tmp_path, ref=_seg_ref("sess-1", "seg-none")) == 0


def test_rebuild_is_derived_and_idempotent(tmp_path: Path) -> None:
    from app.memory.reference_index import index_db_path, rebuild_reference_index, reference_count

    agent_id = uuid4()
    ref = _seg_ref("sess-1", "seg-a")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-a")
    _write_t3_block_with_ref(tmp_path, agent_id, ref=ref)

    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)
    first = reference_count(agent_id=agent_id, data_root=tmp_path, ref=ref)

    index_db_path(tmp_path, agent_id).unlink()
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)
    second = reference_count(agent_id=agent_id, data_root=tmp_path, ref=ref)

    assert first == second == 1


def test_inactive_explicit_entries_do_not_count(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, reference_count

    agent_id = uuid4()
    ref = _seg_ref("sess-1", "seg-a")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-a")
    _write_explicit_entry_with_ref(tmp_path, agent_id, entry_id="ex-gone", ref=ref, status="absorbed")

    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    assert reference_count(agent_id=agent_id, data_root=tmp_path, ref=ref) == 0


def test_resolve_ref_returns_live_package_path(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, resolve_ref

    agent_id = uuid4()
    package_dir = _write_segment_package(tmp_path, agent_id, segment_id="seg-a")
    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    resolved = resolve_ref(agent_id=agent_id, data_root=tmp_path, ref=_seg_ref("sess-1", "seg-a"))

    assert resolved is not None
    assert resolved.archived_at == ""
    assert (tmp_path / str(agent_id) / resolved.path) == package_dir


# --- retention: reference-counted archival, never hard delete ---


@pytest.mark.asyncio
async def test_referenced_package_is_never_archived(tmp_path: Path) -> None:
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    package_dir = _write_segment_package(tmp_path, agent_id, segment_id="seg-hot", created_days_ago=365.0)
    _write_t3_block_with_ref(tmp_path, agent_id, ref=_seg_ref("sess-1", "seg-hot"))

    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    assert package_dir.exists()
    assert report.archived == ()
    assert report.kept_referenced >= 1


@pytest.mark.asyncio
async def test_unreferenced_recent_package_is_kept(tmp_path: Path) -> None:
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    package_dir = _write_segment_package(tmp_path, agent_id, segment_id="seg-young", created_days_ago=3.0)

    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    assert package_dir.exists()
    assert report.archived == ()


@pytest.mark.asyncio
async def test_unreferenced_overdue_absorbed_package_archives_and_ref_still_resolves(tmp_path: Path) -> None:
    from app.memory.reference_index import resolve_ref
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    ref = _seg_ref("sess-1", "seg-cold")
    package_dir = _write_segment_package(
        tmp_path, agent_id, segment_id="seg-cold", package_status="absorbed", created_days_ago=90.0
    )

    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    assert report.archived == (ref,)
    assert not package_dir.exists()
    archive_dir = _mem_dir(tmp_path, agent_id) / ".archive" / "t2" / "sessions" / "sess-1" / "segments" / "seg-cold"
    assert (archive_dir / "manifest.json").exists()
    assert (archive_dir / "summary.md").read_text(encoding="utf-8")

    log_path = _mem_dir(tmp_path, agent_id) / ".archive" / "t2" / "archive_log.jsonl"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert entries[-1]["ref"] == ref
    assert entries[-1]["reason"]

    resolved = resolve_ref(agent_id=agent_id, data_root=tmp_path, ref=ref)
    assert resolved is not None
    assert resolved.archived_at
    assert (tmp_path / str(agent_id) / resolved.path / "manifest.json").exists()


@pytest.mark.asyncio
async def test_pipeline_pending_packages_are_protected(tmp_path: Path) -> None:
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    intake_dir = _write_segment_package(
        tmp_path,
        agent_id,
        segment_id="seg-intake",
        package_status="reviewed",
        allowed_next="t3_intake",
        created_days_ago=90.0,
    )
    stitch_dir = _write_segment_package(
        tmp_path,
        agent_id,
        segment_id="seg-stitch",
        package_status="reviewed",
        allowed_next="episode_stitching",
        created_days_ago=90.0,
    )

    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    assert intake_dir.exists()
    assert stitch_dir.exists()
    assert report.archived == ()
    assert report.kept_pipeline == 2


@pytest.mark.asyncio
async def test_reviewed_dead_end_package_archives(tmp_path: Path) -> None:
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    _write_segment_package(
        tmp_path,
        agent_id,
        segment_id="seg-dead",
        package_status="reviewed",
        allowed_next="none",
        created_days_ago=90.0,
    )

    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    assert report.archived == (_seg_ref("sess-1", "seg-dead"),)


@pytest.mark.asyncio
async def test_decision_and_permission_domains_never_archive(tmp_path: Path) -> None:
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    decision_dir = _write_segment_package(
        tmp_path,
        agent_id,
        segment_id="seg-decision",
        package_status="absorbed",
        created_days_ago=365.0,
        memory_domain="decision_memory",
    )
    permission_dir = _write_segment_package(
        tmp_path,
        agent_id,
        segment_id="seg-permission",
        package_status="absorbed",
        created_days_ago=365.0,
        memory_domain="permission_memory",
    )

    report = await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    assert decision_dir.exists()
    assert permission_dir.exists()
    assert report.archived == ()
    assert report.kept_protected_domain == 2


@pytest.mark.asyncio
async def test_archived_package_survives_index_rebuild(tmp_path: Path) -> None:
    from app.memory.reference_index import rebuild_reference_index, resolve_ref
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    ref = _seg_ref("sess-1", "seg-cold")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-cold", created_days_ago=90.0)
    await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    rebuild_reference_index(agent_id=agent_id, data_root=tmp_path)

    resolved = resolve_ref(agent_id=agent_id, data_root=tmp_path, ref=ref)
    assert resolved is not None
    assert resolved.archived_at
    assert ".archive" in resolved.path


@pytest.mark.asyncio
async def test_retention_writes_control_report(tmp_path: Path) -> None:
    from app.memory.t2_retention import run_t2_retention

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id, segment_id="seg-cold", created_days_ago=90.0)

    await run_t2_retention(agent_id=agent_id, data_root=tmp_path, now=NOW, archive_after_days=30.0)

    report_path = _mem_dir(tmp_path, agent_id) / "control" / "t2_retention.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "t2_retention.v1"
    assert payload["archived"] == [_seg_ref("sess-1", "seg-cold")]
    assert payload["generated_at"]


# --- production wiring ---


@pytest.mark.asyncio
async def test_heartbeat_maintenance_runs_retention(tmp_path: Path, monkeypatch) -> None:
    from app.services import heartbeat

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id, segment_id="seg-cold", created_days_ago=90.0)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: type("S", (), {"AGENT_DATA_DIR": str(tmp_path), "MEMORY_RETENTION_ARCHIVE_AFTER_DAYS": 30.0})(),
    )

    report = await heartbeat._run_t2_retention(agent_id)

    assert report is not None
    assert report.archived == (_seg_ref("sess-1", "seg-cold"),)


def test_execute_heartbeat_wires_retention() -> None:
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._execute_heartbeat)
    assert "_run_t2_retention(" in source


def test_retention_threshold_comes_from_settings() -> None:
    from app.config import get_settings

    assert get_settings().MEMORY_RETENTION_ARCHIVE_AFTER_DAYS > 0
