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


def test_prefix_message_with_sender_label_formats_sender_header() -> None:
    from app.channel_message_contracts import prefix_message_with_sender_label

    assert prefix_message_with_sender_label("你好", sender_name="张三", sender_id="u_123") == "[发送者: 张三 (ID: u_123)] 你好"
    assert prefix_message_with_sender_label("你好", sender_name="张三", sender_id=None) == "[发送者: 张三] 你好"
    assert prefix_message_with_sender_label("你好", sender_name="", sender_id="u_123") == "你好"
