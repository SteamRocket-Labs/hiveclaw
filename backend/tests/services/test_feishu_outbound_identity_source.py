from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_approval_service_source_prefers_provider_delivery_target():
    source = (_BACKEND_ROOT / "app/services/approval_service.py").read_text()

    assert "get_feishu_delivery_target" in source


def test_gateway_source_prefers_provider_backed_org_member_ids():
    source = (_BACKEND_ROOT / "app/api/gateway.py").read_text()

    assert "target_member.external_id or target_member.feishu_user_id" in source
    assert "target_member.open_id or target_member.feishu_open_id" in source


def test_messaging_source_uses_canonical_feishu_conv_ids_for_outbound_history():
    messaging_source = (_BACKEND_ROOT / "app/services/agent_tool_domains/messaging.py").read_text()
    identity_source = (_BACKEND_ROOT / "app/services/feishu_identity_maintenance.py").read_text()

    assert "find_or_create_feishu_chat_session" in messaging_source
    assert "build_feishu_p2p_conv_id" in identity_source
    assert "legacy_external_conv_ids" in identity_source
