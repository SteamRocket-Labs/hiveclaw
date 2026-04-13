from pathlib import Path


def test_schedule_api_depends_on_trigger_trunk_only() -> None:
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/api/schedules.py").read_text(encoding="utf-8")

    assert "from app.models.schedule import AgentSchedule" not in source
    assert "from app.services.scheduler import compute_next_run" not in source
    assert "_execute_schedule" not in source
    assert "from app.services.schedule_surface import (" in source
    assert "from app.services.schedule_compat import (" in source
    assert "from app.services.schedule_compat import (\n    migrate_legacy_schedules,\n)" in source


def test_trigger_daemon_uses_schedule_surface_not_compat_bridge() -> None:
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/services/trigger_daemon.py").read_text(encoding="utf-8")

    assert "from app.services.schedule_compat import schedule_manual_evaluation" not in source
    assert "from app.services.schedule_compat import build_schedule_activity_entries" not in source
    assert "from app.services.schedule_compat import clear_schedule_manual_pending, is_schedule_compat_trigger" not in source
    assert "from app.services.schedule_surface import schedule_manual_evaluation" in source
    assert "from app.services.schedule_surface import build_schedule_activity_entries" in source
    assert "from app.services.schedule_surface import clear_schedule_manual_pending, is_schedule_surface_trigger" in source


def test_schedule_compat_bridge_is_limited_to_schedule_api_migration_path() -> None:
    project_root = Path(__file__).resolve().parents[3]
    app_root = project_root / "backend/app"

    compat_importers = sorted(
        str(path.relative_to(project_root))
        for path in app_root.rglob("*.py")
        if "from app.services.schedule_compat import" in path.read_text(encoding="utf-8")
    )

    assert compat_importers == [
        "backend/app/api/schedules.py",
        "backend/app/main.py",
    ]


def test_agent_schedule_model_is_confined_to_legacy_boundary() -> None:
    project_root = Path(__file__).resolve().parents[3]
    app_root = project_root / "backend/app"

    assert not (app_root / "services/scheduler.py").exists()

    legacy_importers = sorted(
        str(path.relative_to(project_root))
        for path in app_root.rglob("*.py")
        if "from app.models.schedule import AgentSchedule" in path.read_text(encoding="utf-8")
    )

    assert legacy_importers == [
        "backend/app/services/schedule_compat.py",
    ]


def test_agent_schedule_model_repo_imports_are_explicitly_allowlisted() -> None:
    project_root = Path(__file__).resolve().parents[3]
    backend_root = project_root / "backend"

    importers = sorted(
        str(path.relative_to(project_root))
        for path in backend_root.rglob("*.py")
        if "backend/tests/" not in str(path.relative_to(project_root))
        if "from app.models.schedule import AgentSchedule" in path.read_text(encoding="utf-8")
    )

    assert importers == [
        "backend/alembic/env.py",
        "backend/app/services/schedule_compat.py",
    ]


def test_main_only_starts_trigger_daemon_for_autonomy_background_loops() -> None:
    project_root = Path(__file__).resolve().parents[3]
    app_root = project_root / "backend/app"
    source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")

    assert '("trigger_daemon", start_trigger_daemon())' in source
    assert "start_scheduler()" not in source
    assert "start_supervision_reminder()" not in source
    assert not (app_root / "services/supervision_reminder.py").exists()


def test_legacy_schedule_startup_migration_runs_inside_main_not_entrypoint_script() -> None:
    project_root = Path(__file__).resolve().parents[3]
    main_source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")
    entrypoint_source = (project_root / "backend/entrypoint.sh").read_text(encoding="utf-8")

    assert "migrate_all_legacy_schedules" in main_source
    assert "import app.models.schedule" not in main_source
    assert "import app.models.schedule" not in entrypoint_source
    assert "python -m app.scripts.migrate_schedules_to_triggers" not in entrypoint_source
    assert not (project_root / "backend/app/scripts/migrate_schedules_to_triggers.py").exists()
