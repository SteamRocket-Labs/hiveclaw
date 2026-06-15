from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.runtime.session import SessionContext


def test_record_skill_runtime_usage_for_invocation_records_loaded_skill_and_trace_metadata(monkeypatch, tmp_path):
    from app.services import skill_runtime_telemetry

    agent_id = uuid4()
    calls = []
    session_context = SessionContext(
        session_id="task-session-1",
        source="task",
        channel="task",
        metadata={"runtime_task_id": "runtime-task-1", "trace_id": "trace-1"},
    )

    monkeypatch.setattr(skill_runtime_telemetry, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    def fake_record_skill_runtime_usage(workspace, **kwargs):
        calls.append((workspace, kwargs))
        return {"decision": "candidate", "workflow_signature": "load_skill -> read_file"}

    monkeypatch.setattr(skill_runtime_telemetry, "record_skill_runtime_usage", fake_record_skill_runtime_usage)

    result = skill_runtime_telemetry.record_skill_runtime_usage_for_invocation(
        agent_id=agent_id,
        session_context=session_context,
        tool_events=[
            {"name": "load_skill", "args": {"name": "Incident Response"}, "status": "done"},
            {"name": "read_file", "args": {"path": "workspace/runbook.md"}, "status": "done"},
            {"name": "write_file", "args": {"path": "workspace/draft.md"}, "status": "running"},
        ],
        terminal_status="completed",
        assistant_text="[OUTCOME:action_taken] Updated the runbook.",
        note="Updated the runbook.",
    )

    assert result == {"decision": "candidate", "workflow_signature": "load_skill -> read_file"}
    assert calls == [
        (
            tmp_path / str(agent_id),
            {
                "skill_name": "Incident Response",
                "loaded_skill_names": ["Incident Response"],
                "tool_names": ["load_skill", "read_file"],
                "status": "success",
                "note": "Updated the runbook.",
                "source": "task",
                "session_id": "task-session-1",
                "runtime_task_id": "runtime-task-1",
                "trace_id": "trace-1",
                "blocker": None,
            },
        )
    ]


def test_record_skill_runtime_usage_for_invocation_maps_outcome_failure(monkeypatch, tmp_path):
    from app.services import skill_runtime_telemetry

    agent_id = uuid4()
    calls = []
    session_context = SessionContext(session_id="heartbeat-session-1", source="heartbeat", channel="internal")

    monkeypatch.setattr(skill_runtime_telemetry, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    def fake_record_skill_runtime_usage(workspace, **kwargs):
        calls.append((workspace, kwargs))
        return {"decision": "candidate", "workflow_signature": "load_skill -> send_email"}

    monkeypatch.setattr(skill_runtime_telemetry, "record_skill_runtime_usage", fake_record_skill_runtime_usage)

    skill_runtime_telemetry.record_skill_runtime_usage_for_invocation(
        agent_id=agent_id,
        session_context=session_context,
        tool_events=[
            {"name": "load_skill", "args": {"skill_name": "Daily Brief"}, "status": "done"},
            {"name": "send_email", "args": {"to": "ops@example.com"}, "status": "done"},
        ],
        terminal_status="completed",
        assistant_text="[OUTCOME:failure] Could not send the email.",
        note="Could not send the email.",
    )

    assert calls[0][1]["status"] == "failed"
    assert calls[0][1]["blocker"] == "Could not send the email."


def test_record_skill_runtime_usage_for_invocation_ignores_turn_without_loaded_skill(monkeypatch, tmp_path):
    from app.services import skill_runtime_telemetry

    agent_id = uuid4()
    session_context = SessionContext(session_id="session-no-skill", source="task", channel="task")
    calls = []

    monkeypatch.setattr(skill_runtime_telemetry, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(
        skill_runtime_telemetry,
        "record_skill_runtime_usage",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = skill_runtime_telemetry.record_skill_runtime_usage_for_invocation(
        agent_id=agent_id,
        session_context=session_context,
        tool_events=[{"name": "read_file", "args": {"path": "workspace/runbook.md"}, "status": "done"}],
        terminal_status="completed",
        assistant_text="[OUTCOME:action_taken] Read the file.",
        note="Read the file.",
    )

    assert result is None
    assert calls == []
