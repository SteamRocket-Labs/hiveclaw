"""Tests for pending reply context service — cross-session context propagation."""

from __future__ import annotations

from app.services.pending_reply_service import (
    build_task_context,
    extract_recipient_info,
    format_pending_reply_context,
    normalize_identity,
    sender_identity_from_external_conv_id,
    sender_identity_from_session,
)
from app.services.channel_delivery_service import channel_delivery_target
from datetime import datetime, timezone


# ── normalize_identity ──


class TestNormalizeIdentity:
    def test_feishu(self) -> None:
        assert normalize_identity("feishu", "u_abc123") == "feishu:u_abc123"

    def test_web(self) -> None:
        assert normalize_identity("web", "simon") == "web:simon"

    def test_strips_whitespace(self) -> None:
        assert normalize_identity("slack", "  U123  ") == "slack:U123"


# ── extract_recipient_info ──


class TestExtractRecipientInfo:
    def test_feishu_with_user_id(self) -> None:
        result = extract_recipient_info("send_feishu_message", {
            "member_name": "王天怡",
            "user_id": "u_abc",
            "open_id": "ou_xyz",
            "message": "hello",
        })
        assert result is not None
        assert result["channel"] == "feishu"
        assert result["name"] == "王天怡"
        assert result["identity"] == "feishu:u_abc"

    def test_feishu_fallback_to_open_id(self) -> None:
        result = extract_recipient_info("send_feishu_message", {
            "member_name": "天怡",
            "open_id": "ou_xyz",
            "message": "hi",
        })
        assert result is not None
        assert result["identity"] == "feishu:ou_xyz"

    def test_feishu_fallback_to_name(self) -> None:
        result = extract_recipient_info("send_feishu_message", {
            "member_name": "王天怡",
            "message": "hi",
        })
        assert result is not None
        assert result["identity"] == "feishu:王天怡"

    def test_web(self) -> None:
        result = extract_recipient_info("send_web_message", {
            "username": "simon",
            "message": "hello",
        })
        assert result is not None
        assert result["channel"] == "web"
        assert result["identity"] == "web:simon"

    def test_send_channel_message_uses_current_delivery_target(self) -> None:
        token = channel_delivery_target.set({
            "channel": "telegram",
            "chat_id": 123456,
            "sender_id": 789,
        })
        try:
            result = extract_recipient_info("send_channel_message", {"message": "hello"})
        finally:
            channel_delivery_target.reset(token)

        assert result is not None
        assert result["channel"] == "telegram"
        assert result["identity"] == "telegram:123456:789"

    def test_send_channel_message_supports_wecom_delivery_target(self) -> None:
        token = channel_delivery_target.set({
            "channel": "wecom",
            "user_id": "zhangsan",
            "user_label": "张三",
        })
        try:
            result = extract_recipient_info("send_channel_message", {"message": "hello"})
        finally:
            channel_delivery_target.reset(token)

        assert result is not None
        assert result["channel"] == "wecom"
        assert result["name"] == "张三"
        assert result["identity"] == "wecom:zhangsan"

    def test_unknown_tool(self) -> None:
        assert extract_recipient_info("unknown_tool", {"message": "hi"}) is None

    def test_empty_args(self) -> None:
        assert extract_recipient_info("send_feishu_message", {}) is None


# ── build_task_context ──


class TestBuildTaskContext:
    def test_extracts_from_messages(self) -> None:
        messages = [
            {"role": "user", "content": "帮我约王天怡周二下午开会"},
            {"role": "assistant", "content": "好的，我先给王天怡发消息确认时间"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
        ]
        ctx = build_task_context(messages, {"message": "天怡你好，慕涵想约你开会"})
        assert "王天怡" in ctx["originator_message"]
        assert "王天怡" in ctx["agent_reasoning"]
        assert "天怡你好" in ctx["outbound_message"]

    def test_empty_messages(self) -> None:
        ctx = build_task_context([], {"message": "hello"})
        assert ctx["originator_message"] == ""
        assert ctx["agent_reasoning"] == ""
        assert ctx["outbound_message"] == "hello"

    def test_skips_tool_call_assistant(self) -> None:
        """Assistant messages with tool_calls are skipped for reasoning extraction."""
        messages = [
            {"role": "user", "content": "发消息给Bob"},
            {"role": "assistant", "content": "calling tool", "tool_calls": [{"id": "1"}]},
            {"role": "assistant", "content": "我来帮你联系Bob"},
        ]
        ctx = build_task_context(messages, {"message": "hi bob"})
        assert ctx["agent_reasoning"] == "我来帮你联系Bob"


class TestSenderIdentityFromExternalConvId:
    def test_telegram(self) -> None:
        assert sender_identity_from_external_conv_id("tg_123456_789") == "telegram:123456:789"

    def test_wechat_personal(self) -> None:
        assert sender_identity_from_external_conv_id("wechat_p2p_wxid_abc") == "wechat_personal:wxid_abc"

    def test_wecom_p2p(self) -> None:
        assert sender_identity_from_external_conv_id("wecom_p2p_zhangsan") == "wecom:zhangsan"

    def test_wecom_group(self) -> None:
        assert sender_identity_from_external_conv_id("wecom_group_sales-room_zhangsan") == "wecom:zhangsan"

    def test_web(self) -> None:
        assert sender_identity_from_external_conv_id("web_alice") == "web:alice"


class TestSenderIdentityFromSession:
    def test_prefers_delivery_target_identity(self) -> None:
        session = type(
            "Session",
            (),
            {
                "external_conv_id": "web_legacy",
                "delivery_target_json": {"channel": "web", "username": "alice"},
            },
        )()

        assert sender_identity_from_session(session) == "web:alice"

    def test_falls_back_to_external_conv_id(self) -> None:
        session = type(
            "Session",
            (),
            {
                "external_conv_id": "wecom_p2p_zhangsan",
                "delivery_target_json": None,
            },
        )()

        assert sender_identity_from_session(session) == "wecom:zhangsan"


# ── format_pending_reply_context ──


class TestFormatPendingReplyContext:
    def _make_pending(self, **kwargs) -> object:
        """Create a mock PendingReplyContext-like object."""
        defaults = {
            "originator_name": "慕涵",
            "originator_identity": "feishu:u_muhan",
            "recipient_identity": "feishu:u_tianyi",
            "outbound_message": "天怡你好！慕涵想约你周二14:00开会",
            "task_summary": "帮我约王天怡周二下午开会",
            "expected_action": "确认后创建日程并邀请双方",
            "created_at": datetime(2026, 4, 14, 9, 30, tzinfo=timezone.utc),
        }
        defaults.update(kwargs)

        class _Mock:
            pass

        obj = _Mock()
        for k, v in defaults.items():
            setattr(obj, k, v)
        return obj

    def test_empty_list(self) -> None:
        assert format_pending_reply_context([]) == ""

    def test_single_pending(self) -> None:
        result = format_pending_reply_context([self._make_pending()])
        assert "待回复任务上下文" in result
        assert "慕涵" in result
        assert "feishu:u_muhan" in result
        assert "feishu:u_tianyi" in result
        assert "2026-04-14" in result
        assert "天怡你好" in result
        assert "周二" in result
        assert "不要重复询问" in result

    def test_multiple_pending(self) -> None:
        result = format_pending_reply_context([
            self._make_pending(originator_name="Alice"),
            self._make_pending(originator_name="Bob"),
        ])
        assert "任务 1" in result
        assert "任务 2" in result
        assert "Alice" in result
        assert "Bob" in result

    def test_no_originator(self) -> None:
        result = format_pending_reply_context([self._make_pending(originator_name=None, originator_identity=None)])
        assert "待回复任务上下文" in result
        assert "发起人" not in result
