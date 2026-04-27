from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def test_runtime_entrypoints_pass_session_context_to_invoker() -> None:
    websocket = (APP_ROOT / "api/websocket.py").read_text(encoding="utf-8")
    heartbeat = (APP_ROOT / "services/heartbeat.py").read_text(encoding="utf-8")
    trigger_daemon = (APP_ROOT / "services/trigger_daemon.py").read_text(encoding="utf-8")
    task_executor = (APP_ROOT / "services/task_executor.py").read_text(encoding="utf-8")

    assert "SessionContext(" in websocket
    assert "session_context=effective_session_context" in websocket
    assert "_get_or_create_heartbeat_session_ctx" in heartbeat
    assert "session_context=_get_or_create_heartbeat_session_ctx" in heartbeat
    assert 'source_channel="trigger"' in trigger_daemon
    assert "external_conv_id=objective_session_key" in trigger_daemon
    assert 'source="task"' in task_executor


def test_internal_execution_channels_are_excluded_from_normal_chat_listing() -> None:
    chat_sessions_api = (APP_ROOT / "api/chat_sessions.py").read_text(encoding="utf-8")
    session_recall = (APP_ROOT / "services/session_recall.py").read_text(encoding="utf-8")

    assert '"trigger", "task", "heartbeat"' in chat_sessions_api
    assert '"trigger"' in session_recall
    assert '"heartbeat"' in session_recall
    assert '"task"' in session_recall
