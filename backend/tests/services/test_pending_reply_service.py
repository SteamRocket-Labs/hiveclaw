"""Tests for pending reply context service — cross-session context propagation."""

from __future__ import annotations

from app.services.pending_reply_service import (
    build_task_context,
    extract_recipient_info,
    format_pending_reply_context,
    normalize_identity,
)


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


# ── format_pending_reply_context ──


class TestFormatPendingReplyContext:
    def _make_pending(self, **kwargs) -> object:
        """Create a mock PendingReplyContext-like object."""
        defaults = {
            "originator_name": "慕涵",
            "outbound_message": "天怡你好！慕涵想约你周二14:00开会",
            "task_summary": "帮我约王天怡周二下午开会",
            "expected_action": "确认后创建日程并邀请双方",
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
        result = format_pending_reply_context([self._make_pending(originator_name=None)])
        assert "待回复任务上下文" in result
        assert "发起人" not in result
