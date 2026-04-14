from __future__ import annotations


def test_extract_sender_label_from_message_supports_ascii_colon_and_id_suffix() -> None:
    from app.channel_message_contracts import extract_sender_label_from_message

    assert extract_sender_label_from_message("[发送者: 张三 (ID: u_123)] 你好") == "张三"


def test_extract_sender_label_from_message_supports_full_width_colon() -> None:
    from app.channel_message_contracts import extract_sender_label_from_message

    assert extract_sender_label_from_message("[发送者：Alice] hello") == "Alice"


def test_extract_sender_label_from_message_returns_none_for_plain_text() -> None:
    from app.channel_message_contracts import extract_sender_label_from_message

    assert extract_sender_label_from_message("普通消息") is None


def test_strip_sender_label_prefix_removes_sender_block_only() -> None:
    from app.channel_message_contracts import strip_sender_label_prefix

    assert strip_sender_label_prefix("[发送者: 张三 (ID: u_123)] 你好") == "你好"
    assert strip_sender_label_prefix("普通消息") == "普通消息"
