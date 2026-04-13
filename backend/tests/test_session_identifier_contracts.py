from __future__ import annotations

from uuid import uuid4


def test_build_legacy_gateway_conversation_ids_is_order_preserving_pair() -> None:
    from app.session_identifiers import build_legacy_gateway_conversation_ids

    source_agent_id = uuid4()
    target_agent_id = uuid4()

    assert build_legacy_gateway_conversation_ids(source_agent_id, target_agent_id) == (
        f"gw_agent_{source_agent_id}_{target_agent_id}",
        f"gw_agent_{target_agent_id}_{source_agent_id}",
    )


def test_parse_legacy_gateway_conversation_id_returns_original_pair() -> None:
    from app.session_identifiers import parse_legacy_gateway_conversation_id

    source_agent_id = uuid4()
    target_agent_id = uuid4()

    assert parse_legacy_gateway_conversation_id(f"gw_agent_{source_agent_id}_{target_agent_id}") == (
        str(source_agent_id),
        str(target_agent_id),
    )
    assert parse_legacy_gateway_conversation_id("gw_agent_not-a-uuid") is None


def test_build_feishu_session_lookup_ids_prefers_user_id_and_keeps_open_id_alias() -> None:
    from app.session_identifiers import build_feishu_session_lookup_ids

    conv_id, legacy_ids = build_feishu_session_lookup_ids(
        provider_user_id="u_123",
        provider_open_id="ou_456",
    )

    assert conv_id == "feishu_p2p_u_123"
    assert legacy_ids == ["feishu_p2p_ou_456"]


def test_build_feishu_session_lookup_ids_for_group_chat_uses_chat_id_only() -> None:
    from app.session_identifiers import build_feishu_session_lookup_ids

    conv_id, legacy_ids = build_feishu_session_lookup_ids(
        provider_user_id="u_123",
        provider_open_id="ou_456",
        chat_type="group",
        chat_id="oc_group_123",
    )

    assert conv_id == "feishu_group_oc_group_123"
    assert legacy_ids == []


def test_parse_feishu_p2p_conv_id_returns_identifier_only_for_p2p_sessions() -> None:
    from app.session_identifiers import parse_feishu_p2p_conv_id

    assert parse_feishu_p2p_conv_id("feishu_p2p_u_123") == "u_123"
    assert parse_feishu_p2p_conv_id("feishu_group_oc_group_123") is None
    assert parse_feishu_p2p_conv_id("") is None
