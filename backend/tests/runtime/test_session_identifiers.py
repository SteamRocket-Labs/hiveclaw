from __future__ import annotations

from uuid import uuid4


def test_agent_pair_ids_are_canonical():
    from app.session_identifiers import (
        build_agent_pair_session_id,
        canonicalize_agent_pair_ids,
    )

    a = uuid4()
    b = uuid4()

    assert canonicalize_agent_pair_ids(a, b) == canonicalize_agent_pair_ids(b, a)
    assert build_agent_pair_session_id(a, b) == build_agent_pair_session_id(b, a)


def test_feishu_lookup_ids_prefer_user_id_and_keep_open_id_legacy_alias():
    from app.session_identifiers import build_feishu_session_lookup_ids

    canonical, legacy = build_feishu_session_lookup_ids(
        provider_user_id="u_123",
        provider_open_id="ou_456",
    )

    assert canonical == "feishu_p2p_u_123"
    assert legacy == ["feishu_p2p_ou_456"]


def test_channel_sender_prefix_contract_round_trip():
    from app.channel_message_contracts import (
        extract_sender_label_from_message,
        prefix_message_with_sender_label,
        strip_sender_label_prefix,
    )

    raw = "请查一下状态"
    prefixed = prefix_message_with_sender_label(raw, sender_name="Alice", sender_id="u_123")

    assert prefixed == "[发送者: Alice (ID: u_123)] 请查一下状态"
    assert extract_sender_label_from_message(prefixed) == "Alice"
    assert strip_sender_label_prefix(prefixed) == raw
