from pathlib import Path


def test_collaboration_service_routes_delegate_and_message_through_runtime_surface() -> None:
    project_root = Path(__file__).resolve().parents[3]
    source = (project_root / "backend/app/services/collaboration.py").read_text(encoding="utf-8")

    assert "_delegate_to_agent_async(" in source
    assert "_send_message_to_agent(" in source
    assert "event_bus.publish(" not in source
    assert "msg_file.write_text(" not in source
