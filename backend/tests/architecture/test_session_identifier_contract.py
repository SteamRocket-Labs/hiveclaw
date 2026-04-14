from __future__ import annotations

from pathlib import Path


def test_session_identifier_contract_is_centralized() -> None:
    project_root = Path(__file__).resolve().parents[3]

    shared_source = (project_root / "backend/app/session_identifiers.py").read_text(encoding="utf-8")
    session_service_source = (project_root / "backend/app/services/session_service.py").read_text(encoding="utf-8")
    gateway_db_source = (project_root / "backend/app/db_legacy_gateway_conversation_migration.py").read_text(
        encoding="utf-8"
    )
    feishu_service_source = (project_root / "backend/app/services/feishu_identity_maintenance.py").read_text(
        encoding="utf-8"
    )
    feishu_db_source = (project_root / "backend/app/db_legacy_feishu_session_migration.py").read_text(
        encoding="utf-8"
    )
    pending_reply_source = (project_root / "backend/app/services/pending_reply_service.py").read_text(encoding="utf-8")
    channel_user_source = (project_root / "backend/app/services/channel_user_service.py").read_text(encoding="utf-8")
    activity_api_source = (project_root / "backend/app/api/activity.py").read_text(encoding="utf-8")
    trigger_daemon_source = (project_root / "backend/app/services/trigger_daemon.py").read_text(encoding="utf-8")
    feishu_api_source = (project_root / "backend/app/api/feishu.py").read_text(encoding="utf-8")

    assert "def build_legacy_gateway_conversation_ids(" in shared_source
    assert "def canonicalize_agent_pair_ids(" in shared_source
    assert "def parse_legacy_gateway_conversation_id(" in shared_source
    assert "def build_feishu_session_lookup_ids(" in shared_source
    assert "def build_feishu_p2p_conv_id(" in shared_source
    assert "def parse_feishu_p2p_conv_id(" in shared_source

    assert "def _legacy_gateway_conversation_ids(" not in session_service_source
    assert "from app.session_identifiers import (" in session_service_source
    assert "build_legacy_gateway_conversation_ids" in session_service_source
    assert "min(source_agent_id, target_agent_id, key=str)" not in session_service_source
    assert "max(source_agent_id, target_agent_id, key=str)" not in session_service_source
    assert "canonicalize_agent_pair_ids" in session_service_source

    assert "def _legacy_gateway_conversation_ids(" not in gateway_db_source
    assert "def _parse_legacy_gateway_conversation_id(" not in gateway_db_source
    assert "from app.session_identifiers import (" in gateway_db_source
    assert "tuple(sorted(parsed_pair, key=str))" not in gateway_db_source

    assert "def build_feishu_p2p_conv_id(" not in feishu_service_source
    assert "def build_feishu_session_lookup_ids(" not in feishu_service_source
    assert "from app.session_identifiers import build_feishu_session_lookup_ids" in feishu_service_source

    assert "def build_feishu_p2p_conv_id(" not in feishu_db_source
    assert "from app.session_identifiers import build_feishu_p2p_conv_id" in feishu_db_source

    assert 'startswith("feishu_p2p_")' not in pending_reply_source
    assert "from app.session_identifiers import parse_feishu_p2p_conv_id" in pending_reply_source

    assert 'startswith("feishu_p2p_")' not in channel_user_source
    assert "from app.session_identifiers import parse_feishu_p2p_conv_id" in channel_user_source

    assert 'startswith("feishu_p2p_")' not in activity_api_source
    assert "from app.session_identifiers import parse_feishu_p2p_conv_id" in activity_api_source

    assert "sender_identity_from_session" in trigger_daemon_source
    assert 'sender_identity_from_external_conv_id(getattr(session_obj, "external_conv_id", "") or "")' not in trigger_daemon_source

    assert "sender_identity_from_session" in feishu_api_source
    assert 'sender_identity_from_external_conv_id(_sess_obj.external_conv_id or "")' not in feishu_api_source
