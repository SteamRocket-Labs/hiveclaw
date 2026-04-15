from pathlib import Path


def test_approval_service_source_prefers_provider_delivery_target():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/services/approval_service.py").read_text()

    assert "get_feishu_delivery_target" in source


def test_gateway_source_prefers_provider_backed_org_member_ids():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/api/gateway.py").read_text()

    assert "target_member.external_id or target_member.feishu_user_id" in source
    assert "target_member.open_id or target_member.feishu_open_id" in source
    assert "getattr(r.member, 'external_id', None) or getattr(r.member, 'feishu_user_id', None)" in source
    assert "getattr(r.member, 'open_id', None) or getattr(r.member, 'feishu_open_id', None)" in source


def test_messaging_source_uses_canonical_feishu_conv_ids_for_outbound_history():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/services/agent_tool_domains/messaging.py").read_text()

    assert "find_or_create_feishu_chat_session" in source
    assert "legacy_external_conv_ids" not in source
    assert "OrgMember.external_id == direct_user_id" in source
    assert "OrgMember.open_id == direct_open_id" in source


def test_feishu_api_source_routes_session_alias_logic_through_identity_helper():
    source = Path("/Users/rocky243/vc-saas/hiveclaw/backend/app/api/feishu.py").read_text()

    assert "find_or_create_feishu_chat_session" in source
    assert "legacy_external_conv_ids=" not in source
    assert "build_feishu_session_lookup_ids" not in source
    assert "pre_session_conv_ids" not in source
