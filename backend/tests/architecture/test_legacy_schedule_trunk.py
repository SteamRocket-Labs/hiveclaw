from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def test_schedule_api_is_trigger_compat_facade_not_second_runtime() -> None:
    source = (APP_ROOT / "api/schedules.py").read_text(encoding="utf-8")

    assert "from app.models.trigger import AgentTrigger" in source
    assert "from app.models.schedule import AgentSchedule" not in source
    assert "from app.services.scheduler" not in source
    assert "_execute_schedule" not in source
    assert "asyncio.create_task" not in source


def test_legacy_scheduler_runtime_is_removed() -> None:
    assert not (APP_ROOT / "services/scheduler.py").exists()
