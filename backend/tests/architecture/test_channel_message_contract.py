from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_channel_message_contract_is_centralized() -> None:
    contract_source = (APP_ROOT / "channel_message_contracts.py").read_text(encoding="utf-8")
    activity_source = (APP_ROOT / "api" / "activity.py").read_text(encoding="utf-8")
    hooks_source = (APP_ROOT / "runtime" / "hooks_setup.py").read_text(encoding="utf-8")
    feishu_source = (APP_ROOT / "api" / "feishu.py").read_text(encoding="utf-8")

    assert "def extract_sender_label_from_message(" in contract_source
    assert "def strip_sender_label_prefix(" in contract_source
    assert "def prefix_message_with_sender_label(" in contract_source

    assert "from app.channel_message_contracts import" in activity_source
    assert "extract_sender_label_from_message" in activity_source
    assert "strip_sender_label_prefix" in activity_source
    assert 're.search(r"\\[发送者:' not in activity_source
    assert "re.sub(r'^\\[发送者:" not in activity_source

    assert "from app.channel_message_contracts import extract_sender_label_from_message" in hooks_source
    assert 'content.startswith("[发送者:")' not in hooks_source
    assert 'content.startswith("[发送者：")' not in hooks_source

    assert "from app.channel_message_contracts import prefix_message_with_sender_label" in feishu_source
    assert 'llm_user_text = f"[发送者: {sender_name}{id_part}] {user_text}"' not in feishu_source


def test_gateway_session_identifier_contract_is_centralized() -> None:
    gateway_source = (APP_ROOT / "api" / "gateway.py").read_text(encoding="utf-8")
    service_source = (APP_ROOT / "services" / "agent_pair_session.py").read_text(encoding="utf-8")
    gateway_migration_source = (APP_ROOT / "db_legacy_gateway_conversation_migration.py").read_text(encoding="utf-8")
    feishu_migration_source = (APP_ROOT / "db_legacy_feishu_session_migration.py").read_text(encoding="utf-8")

    assert "from app.session_identifiers import" in service_source
    assert "build_agent_pair_session_id" in service_source
    assert "build_legacy_gateway_conversation_ids" in service_source
    assert "from app.services.agent_pair_session import" in gateway_source
    assert "parse_legacy_gateway_conversation_id" in gateway_migration_source
    assert "build_legacy_gateway_conversation_ids" in gateway_migration_source
    assert "build_feishu_p2p_conv_id" in feishu_migration_source
    assert 'f"gw_agent_' not in gateway_source
    assert "uuid5(" not in gateway_source


def test_agent_pair_session_creation_uses_shared_service() -> None:
    service_source = (APP_ROOT / "services" / "agent_pair_session.py").read_text(encoding="utf-8")
    gateway_source = (APP_ROOT / "api" / "gateway.py").read_text(encoding="utf-8")
    messaging_source = (APP_ROOT / "services" / "agent_tool_domains" / "messaging.py").read_text(encoding="utf-8")

    assert "def find_or_create_agent_pair_session(" in service_source
    assert "def get_or_create_agent_participant_id(" in service_source
    assert "def session_conversation_id(" in service_source

    for source in (gateway_source, messaging_source):
        assert "find_or_create_agent_pair_session" in source
        assert "get_or_create_agent_participant_id" in source
        assert "session_agent_id = min(" not in source
        assert "session_peer_id = max(" not in source
        assert 'source_channel="agent"' not in source
        assert "source_channel='agent'" not in source


def test_chat_sessions_use_canonical_channel_names() -> None:
    chat_sessions_source = (APP_ROOT / "api" / "chat_sessions.py").read_text(encoding="utf-8")

    assert '"microsoft_teams"' in chat_sessions_source
    assert '"teams"' not in chat_sessions_source
