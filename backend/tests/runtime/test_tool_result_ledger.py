from __future__ import annotations


def test_tool_result_ledger_classifies_evidence_and_trace_refs() -> None:
    from app.runtime.tool_result_ledger import build_tool_result_ledger_entry

    entry = build_tool_result_ledger_entry(
        tool_name="web_search",
        tool_args={"query": "market map"},
        result_text="found sources",
        status="ok",
        trace_metadata={"evidence_refs": ["truth://policy/web"]},
    )

    assert entry["result_kind"] == "evidence"
    assert entry["context_effect"] == "external_reference"
    assert entry["source_refs"] == ["truth://policy/web", "query:market map"]
    assert entry["side_effects"] == {}
    assert entry["followup_activation_events"] == []


def test_tool_result_ledger_classifies_block_error_and_context_injection() -> None:
    from app.runtime.tool_result_ledger import build_tool_result_ledger_entry

    blocked = build_tool_result_ledger_entry(
        tool_name="send_email",
        tool_args={"to": "user@example.com"},
        result_text="Blocked by hook: policy",
        status="blocked_by_hook",
    )
    errored = build_tool_result_ledger_entry(
        tool_name="send_email",
        tool_args={"to": "user@example.com"},
        result_text="boom",
        status="error",
    )
    injected = build_tool_result_ledger_entry(
        tool_name="office_document_apply",
        tool_args={},
        result_text="done",
        status="ok",
        side_effects={"new_messages": [{"role": "user", "content": "follow up"}]},
    )

    assert blocked["result_kind"] == "blocked"
    assert errored["result_kind"] == "error"
    assert injected["result_kind"] == "context_injection"
    assert injected["context_effect"] == "conversation_injection"
