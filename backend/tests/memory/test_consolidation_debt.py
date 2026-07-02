"""C9-2 red tests: consolidation-debt ledger + stall alerting (spec §6.2.2).

T2→T3 consolidation currently has no debt accounting: if the heartbeat stops
or consolidation keeps failing, reviewed packages pile up silently. Contract:

- ``assess_consolidation_debt`` — pure measurement: pending t3_intake
  packages (segments + episode stitch packages, absorbed excluded), segments
  waiting on episode stitching, held/exhausted T2 jobs, active explicit
  overlay entries, oldest ages.
- ``refresh_consolidation_debt`` — assess + persist
  ``memory/control/consolidation_debt.json`` + one-shot
  ``memory_consolidation_stalled`` audit alert while stalled; the alert marker
  clears on recovery so a new stall alerts again.
- Thresholds come from platform config (heartbeat wrapper threads settings);
  the module itself is parameterized.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest


NOW = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)


def _iso_hours_ago(hours: float) -> str:
    return (NOW - timedelta(hours=hours)).isoformat()


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _write_segment_package(
    tmp_path: Path,
    agent_id,
    *,
    session_id: str = "sess-1",
    segment_id: str = "seg-1",
    package_status: str = "reviewed",
    allowed_next: str = "t3_intake",
    created_hours_ago: float = 1.0,
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
                "created_at": _iso_hours_ago(created_hours_ago),
            }
        ),
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


def _write_episode_package(
    tmp_path: Path,
    agent_id,
    *,
    session_id: str = "sess-1",
    episode_id: str = "ep-1",
    trigger_package_id: str = "t2pkg-seg-1",
    package_status: str = "reviewed",
    allowed_next: str = "t3_intake",
    created_hours_ago: float = 1.0,
) -> Path:
    episode_dir = _mem_dir(tmp_path, agent_id) / "t2" / "sessions" / session_id / "episodes" / episode_id
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.episode-stitch.manifest.v1",
                "episode_id": episode_id,
                "trigger_package_id": trigger_package_id,
                "package_status": package_status,
                "session_id": session_id,
                "created_at": _iso_hours_ago(created_hours_ago),
            }
        ),
        encoding="utf-8",
    )
    (episode_dir / "review.md").write_text(
        f"""<episode_review schema_version="t2.episode_review.v1" episode_id="{episode_id}">
  <decision>approved</decision>
  <allowed_next>{allowed_next}</allowed_next>
</episode_review>""",
        encoding="utf-8",
    )
    return episode_dir


def _write_job_manifest(tmp_path: Path, agent_id, *, job_id: str, status: str, retry_count: int = 0) -> Path:
    job_dir = _mem_dir(tmp_path, agent_id) / ".staging" / "t2_jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "job_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package-job.v1",
                "job_id": job_id,
                "agent_id": str(agent_id),
                "session_id": "sess-1",
                "t0_segment_id": "seg-1",
                "status": status,
                "retry_count": retry_count,
                "created_at": _iso_hours_ago(2.0),
                "updated_at": _iso_hours_ago(1.0),
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path


def _write_explicit_entry(tmp_path: Path, agent_id, *, entry_id: str, status: str, created_hours_ago: float) -> None:
    overlay_dir = _mem_dir(tmp_path, agent_id) / "explicit"
    (overlay_dir / "entries").mkdir(parents=True, exist_ok=True)
    with (overlay_dir / "manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": entry_id,
                    "status": status,
                    "category": "general",
                    "created_at": _iso_hours_ago(created_hours_ago),
                }
            )
            + "\n"
        )
    (overlay_dir / "entries" / f"{entry_id}.md").write_text(
        "<normalized_memory>记住这个工作偏好。</normalized_memory>",
        encoding="utf-8",
    )


def _capture_audit_alerts(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_write_audit_log(action: str, payload: dict | None = None, **kwargs):
        calls.append({"action": action, "payload": payload or {}, **kwargs})

    monkeypatch.setattr("app.services.audit_logger.write_audit_log", fake_write_audit_log)
    return calls


# --- measurement ---


def test_debt_empty_memory_reports_zero(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    report = assess_consolidation_debt(agent_id=uuid4(), data_root=tmp_path, now=NOW)

    assert report.pending_packages == 0
    assert report.pending_stitch_packages == 0
    assert report.oldest_pending_age_hours is None
    assert report.held_jobs == 0
    assert report.exhausted_jobs == 0
    assert report.active_explicit_entries == 0
    assert report.stalled is False
    assert report.stall_reasons == ()


def test_debt_counts_pending_t3_intake_packages_with_age(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id, segment_id="seg-old", created_hours_ago=72.0)
    _write_segment_package(tmp_path, agent_id, segment_id="seg-new", created_hours_ago=2.0)
    _write_episode_package(tmp_path, agent_id, episode_id="ep-pending", created_hours_ago=10.0)

    report = assess_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.pending_packages == 3
    assert report.oldest_pending_age_hours == pytest.approx(72.0, abs=0.01)


def test_debt_excludes_absorbed_and_terminal_packages(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    agent_id = uuid4()
    for status in ["absorbed", "reinforced", "contested", "retired"]:
        _write_segment_package(tmp_path, agent_id, segment_id=f"seg-{status}", package_status=status)

    report = assess_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.pending_packages == 0
    assert report.oldest_pending_age_hours is None


def test_debt_excludes_reviewed_packages_not_routed_to_t3(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id, segment_id="seg-none", allowed_next="none")
    _write_segment_package(tmp_path, agent_id, segment_id="seg-archive", allowed_next="archive_recall_only")

    report = assess_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.pending_packages == 0
    assert report.pending_stitch_packages == 0


def test_debt_counts_segments_waiting_for_stitching(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    agent_id = uuid4()
    _write_segment_package(
        tmp_path, agent_id, segment_id="seg-waiting", allowed_next="episode_stitching", package_id="t2pkg-waiting"
    )
    _write_segment_package(
        tmp_path, agent_id, segment_id="seg-stitched", allowed_next="episode_stitching", package_id="t2pkg-stitched"
    )
    _write_episode_package(tmp_path, agent_id, episode_id="ep-done", trigger_package_id="t2pkg-stitched")

    report = assess_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.pending_stitch_packages == 1


def test_debt_counts_held_and_exhausted_jobs(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    agent_id = uuid4()
    _write_job_manifest(tmp_path, agent_id, job_id="t2job-held", status="held", retry_count=1)
    _write_job_manifest(tmp_path, agent_id, job_id="t2job-exhausted", status="held", retry_count=3)
    _write_job_manifest(tmp_path, agent_id, job_id="t2job-done", status="committed")

    report = assess_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.held_jobs == 2
    assert report.exhausted_jobs == 1


def test_debt_counts_active_explicit_entries_with_age(tmp_path: Path) -> None:
    from app.memory.consolidation_debt import assess_consolidation_debt

    agent_id = uuid4()
    _write_explicit_entry(tmp_path, agent_id, entry_id="ex-active", status="active", created_hours_ago=30.0)
    _write_explicit_entry(tmp_path, agent_id, entry_id="ex-absorbed", status="absorbed", created_hours_ago=90.0)

    report = assess_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.active_explicit_entries == 1
    assert report.oldest_explicit_age_hours == pytest.approx(30.0, abs=0.01)


# --- stall detection + one-shot alerting ---


@pytest.mark.asyncio
async def test_debt_stalls_and_alerts_once_on_pending_overdue(tmp_path: Path, monkeypatch) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt

    agent_id = uuid4()
    package_dir = _write_segment_package(tmp_path, agent_id, segment_id="seg-overdue", created_hours_ago=100.0)
    alerts = _capture_audit_alerts(monkeypatch)

    first = await refresh_consolidation_debt(
        agent_id=agent_id, data_root=tmp_path, now=NOW, pending_age_alert_hours=48.0
    )

    assert first.stalled is True
    assert "pending_package_overdue" in first.stall_reasons
    assert first.alerted is True
    assert len(alerts) == 1
    assert alerts[0]["action"] == "memory_consolidation_stalled"
    assert alerts[0]["payload"]["pending_packages"] == 1

    second = await refresh_consolidation_debt(
        agent_id=agent_id, data_root=tmp_path, now=NOW, pending_age_alert_hours=48.0
    )

    assert second.stalled is True
    assert second.alerted is False
    assert len(alerts) == 1

    # debt cleared → recovery clears the alert marker, so a new stall re-alerts
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package_status"] = "absorbed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    recovered = await refresh_consolidation_debt(
        agent_id=agent_id, data_root=tmp_path, now=NOW, pending_age_alert_hours=48.0
    )

    assert recovered.stalled is False
    assert recovered.alerted is False

    _write_segment_package(tmp_path, agent_id, segment_id="seg-overdue-2", created_hours_ago=200.0)
    restalled = await refresh_consolidation_debt(
        agent_id=agent_id, data_root=tmp_path, now=NOW, pending_age_alert_hours=48.0
    )

    assert restalled.stalled is True
    assert restalled.alerted is True
    assert len(alerts) == 2


@pytest.mark.asyncio
async def test_debt_stalls_on_exhausted_jobs(tmp_path: Path, monkeypatch) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt

    agent_id = uuid4()
    _write_job_manifest(tmp_path, agent_id, job_id="t2job-dead", status="held", retry_count=3)
    alerts = _capture_audit_alerts(monkeypatch)

    report = await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    assert report.stalled is True
    assert "t2_jobs_retry_exhausted" in report.stall_reasons
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_debt_stalls_on_explicit_entry_overdue(tmp_path: Path, monkeypatch) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt

    agent_id = uuid4()
    _write_explicit_entry(tmp_path, agent_id, entry_id="ex-stuck", status="active", created_hours_ago=100.0)
    _capture_audit_alerts(monkeypatch)

    report = await refresh_consolidation_debt(
        agent_id=agent_id, data_root=tmp_path, now=NOW, explicit_age_alert_hours=72.0
    )

    assert report.stalled is True
    assert "explicit_entry_overdue" in report.stall_reasons


@pytest.mark.asyncio
async def test_refresh_writes_control_report(tmp_path: Path, monkeypatch) -> None:
    from app.memory.consolidation_debt import refresh_consolidation_debt

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id, segment_id="seg-1", created_hours_ago=5.0)
    _capture_audit_alerts(monkeypatch)

    await refresh_consolidation_debt(agent_id=agent_id, data_root=tmp_path, now=NOW)

    report_path = _mem_dir(tmp_path, agent_id) / "control" / "consolidation_debt.json"
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "consolidation_debt.v1"
    assert payload["agent_id"] == str(agent_id)
    assert payload["pending_packages"] == 1
    assert payload["stalled"] is False
    assert payload["generated_at"]


# --- production wiring ---


@pytest.mark.asyncio
async def test_heartbeat_maintenance_refreshes_consolidation_debt(tmp_path: Path, monkeypatch) -> None:
    from app.services import heartbeat

    agent_id = uuid4()
    _write_segment_package(tmp_path, agent_id, segment_id="seg-live", created_hours_ago=1.0)
    monkeypatch.setattr("app.config.get_settings", lambda: type("S", (), {"AGENT_DATA_DIR": str(tmp_path)})())
    _capture_audit_alerts(monkeypatch)

    report = await heartbeat._run_consolidation_debt_refresh(agent_id)

    assert report is not None
    assert report.pending_packages == 1
    report_path = _mem_dir(tmp_path, agent_id) / "control" / "consolidation_debt.json"
    assert report_path.exists()


def test_execute_heartbeat_wires_debt_refresh() -> None:
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._execute_heartbeat)
    assert "_run_consolidation_debt_refresh(" in source


def test_debt_thresholds_come_from_settings() -> None:
    from app.config import get_settings

    settings = get_settings()
    assert settings.MEMORY_DEBT_PENDING_AGE_ALERT_HOURS > 0
    assert settings.MEMORY_DEBT_EXPLICIT_AGE_ALERT_HOURS > 0
