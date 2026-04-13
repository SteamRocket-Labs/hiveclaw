from pathlib import Path


def test_core_session_entrypoints_use_session_service() -> None:
    project_root = Path(__file__).resolve().parents[3]
    expectations = {
        "backend/app/api/websocket.py": ["find_or_create_web_chat_session("],
        "backend/app/api/gateway.py": ["find_or_create_agent_pair_session("],
        "backend/app/api/chat_sessions.py": ["create_chat_session("],
        "backend/app/services/agent_tool_domains/messaging.py": [
            "find_or_create_web_chat_session(",
            "find_or_create_agent_pair_session(",
        ],
        "backend/app/services/heartbeat.py": ["create_chat_session("],
        "backend/app/services/trigger_daemon.py": ["create_chat_session("],
        "backend/app/services/task_executor.py": ["create_chat_session("],
        "backend/app/services/channel_session.py": ["create_chat_session("],
        "backend/app/services/channel_delivery_service.py": ["find_or_create_web_chat_session("],
    }

    channel_session_source = (project_root / "backend/app/services/channel_session.py").read_text(encoding="utf-8")
    assert "legacy_external_conv_ids" not in channel_session_source

    for relative_path, required_calls in expectations.items():
        source = (project_root / relative_path).read_text(encoding="utf-8")
        assert "ChatSession(" not in source, f"{relative_path} should not instantiate ChatSession directly"
        for needle in required_calls:
            assert needle in source, f"{relative_path} should route session creation through session_service"
