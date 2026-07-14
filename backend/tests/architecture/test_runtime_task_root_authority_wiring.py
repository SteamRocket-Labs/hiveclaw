from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "app"


def test_all_runtime_control_surfaces_use_root_authority_kernel() -> None:
    files = {
        "async": ROOT / "services" / "agent_tool_domains" / "messaging.py",
        "subagent": ROOT / "services" / "subagent_run_service.py",
        "command": ROOT / "tools" / "handlers" / "command_parity.py",
        "continuation": ROOT / "tools" / "handlers" / "subagent.py",
        "autonomy": ROOT / "services" / "autonomy_overview.py",
        "session_runtime": ROOT / "services" / "web_chat_runtime.py",
    }
    for label, path in files.items():
        source = path.read_text(encoding="utf-8")
        if label == "session_runtime":
            assert "root_user_id=user.id" in source
            assert "root_session_id=" in source
            assert "delegation_chain_json=" in source
        else:
            assert "authorize_runtime_task_record" in source, f"{label} bypasses RuntimeTask root authority"

    messaging = files["async"].read_text(encoding="utf-8")
    assert "list_async_delegations(parent_agent_id=from_agent_id)" not in messaging
    command = files["command"].read_text(encoding="utf-8")
    assert "def _can_access_runtime_task" not in command


def test_runtime_task_model_has_canonical_root_authority_columns() -> None:
    from app.models.runtime_task import RuntimeTask

    columns = RuntimeTask.__table__.columns
    assert {"root_user_id", "root_session_id", "delegation_chain_json"} <= set(columns.keys())


def test_background_subagent_dispatch_and_wake_never_fall_back_to_agent_creator() -> None:
    run_source = (ROOT / "services" / "subagent_run_service.py").read_text(encoding="utf-8")
    wake_source = (ROOT / "services" / "subagent_wake_consumer.py").read_text(encoding="utf-8")

    assert "runtime_task_requester_user_id" in run_source
    assert "runtime_task_requester_user_id" in wake_source
    assert "agent.creator_id" not in run_source
    assert 'getattr(agent, "creator_id"' not in wake_source
    assert "or request.signal_id" not in wake_source
    assert "RuntimeTaskRequesterUnavailable" in wake_source
