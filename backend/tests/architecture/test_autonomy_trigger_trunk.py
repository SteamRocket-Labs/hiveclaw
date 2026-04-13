from pathlib import Path


def test_schedule_api_depends_on_trigger_trunk_only() -> None:
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/api/schedules.py").read_text(encoding="utf-8")

    assert "from app.models.schedule import AgentSchedule" not in source
    assert "from app.services.scheduler import compute_next_run" not in source
    assert "_execute_schedule" not in source


def test_main_only_starts_trigger_daemon_for_autonomy_background_loops() -> None:
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")

    assert '("trigger_daemon", start_trigger_daemon())' in source
    assert "start_scheduler()" not in source
    assert "start_supervision_reminder()" not in source
