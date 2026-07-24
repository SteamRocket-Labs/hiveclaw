from __future__ import annotations

from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def test_background_subagent_completion_has_one_durable_authority() -> None:
    low_level_source = (APP_ROOT / "agents" / "subagent.py").read_text(encoding="utf-8")
    tool_source = (APP_ROOT / "tools" / "handlers" / "subagent.py").read_text(encoding="utf-8")
    run_service_source = (APP_ROOT / "services" / "subagent_run_service.py").read_text(encoding="utf-8")
    daemon_source = (APP_ROOT / "services" / "workflow_daemon.py").read_text(encoding="utf-8")

    assert not (APP_ROOT / "services" / "subagent_wake_consumer.py").exists()
    assert "def consume_subagent_signals" not in low_level_source
    assert "async def _emit_completion_signal" not in low_level_source
    assert "run_in_background: bool" not in low_level_source
    assert "start_subagent_run(" in tool_source
    assert "enqueue_completion_notification(" in run_service_source
    assert "COORDINATION_BACKEND" not in tool_source
    assert "COORDINATION_BACKEND" not in run_service_source
    assert "subagent_wake_consumer" not in daemon_source
