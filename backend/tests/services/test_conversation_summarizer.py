"""Tests for state-first conversation compaction summaries."""

from __future__ import annotations


def test_extract_summary_emits_structured_state_ledgers():
    from app.services.conversation_summarizer import _extract_summary

    messages = [
        {
            "role": "user",
            "content": "请修复 /tmp/auth.py 里的 bug，并记住我更喜欢表格格式输出。",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"/tmp/auth.py"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "write_file", "arguments": '{"path":"/tmp/auth.py"}'},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Loaded file /tmp/auth.py",
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": "Updated /tmp/auth.py and wrote fix result_id=auth_fix_123",
        },
        {
            "role": "assistant",
            "content": "我已经修复 auth.py。下一步需要补测试，并继续验证 API 响应。",
        },
    ]

    summary = _extract_summary(messages)

    assert "**Task Ledger:**" in summary
    assert "**Decision Ledger:**" in summary
    assert "**Artifact Ledger:**" in summary
    assert "**Tool Ledger:**" in summary
    assert "**Preference Ledger:**" in summary
    assert "**Pending Ledger:**" in summary
    assert "/tmp/auth.py" in summary
    assert "read_file" in summary
    assert "write_file" in summary
    assert "表格格式输出" in summary
