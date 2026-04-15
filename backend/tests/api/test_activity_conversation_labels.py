from __future__ import annotations


def test_feishu_conversation_partner_name_uses_sender_label_for_p2p() -> None:
    from app.api.activity import _feishu_conversation_partner_name

    assert (
        _feishu_conversation_partner_name(
            "feishu_p2p_u_123",
            "[发送者: 张三 (ID: u_123)] 你好",
        )
        == "📱 张三"
    )


def test_feishu_conversation_partner_name_falls_back_for_p2p_without_sender_label() -> None:
    from app.api.activity import _feishu_conversation_partner_name

    assert _feishu_conversation_partner_name("feishu_p2p_u_123", "普通消息") == "📱 飞书用户"


def test_feishu_conversation_partner_name_prefers_delivery_target_label_when_sender_prefix_missing() -> None:
    from app.api.activity import _feishu_conversation_partner_name

    assert _feishu_conversation_partner_name("feishu_p2p_u_123", "普通消息", "王天怡") == "📱 王天怡"


def test_feishu_conversation_partner_name_returns_group_label_for_group_chat() -> None:
    from app.api.activity import _feishu_conversation_partner_name

    assert _feishu_conversation_partner_name("feishu_group_oc_group_123", "[发送者: 张三] 你好") == "👥 飞书群聊"
