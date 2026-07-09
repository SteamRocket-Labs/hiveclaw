from __future__ import annotations


def test_unbacked_tool_result_claim_is_replaced_with_honesty_notice() -> None:
    from app.kernel.final_answer_evidence import verify_final_answer_tool_evidence

    repaired = verify_final_answer_tool_evidence(
        "send_message_to_agent 超时 120s，feishu_wiki_list 返回 Feishu/Lark is not configured",
        available_tool_names={"send_message_to_agent", "feishu_wiki_list"},
        tool_evidence_summary={"schema": "hive.ccplus.tool_evidence_ledger.v1", "has_tool_evidence": False},
    )

    assert "本轮没有实际工具调用记录" in repaired
    assert "send_message_to_agent" in repaired
    assert "feishu_wiki_list" in repaired


def test_tool_result_claim_is_preserved_when_tool_evidence_exists() -> None:
    from app.kernel.final_answer_evidence import verify_final_answer_tool_evidence

    content = "feishu_wiki_list 返回 3 个页面"

    assert (
        verify_final_answer_tool_evidence(
            content,
            available_tool_names={"feishu_wiki_list"},
            tool_evidence_summary={
                "schema": "hive.ccplus.tool_evidence_ledger.v1",
                "has_tool_evidence": True,
                "tool_names": ["feishu_wiki_list"],
            },
        )
        == content
    )


def test_prompt_export_with_tool_names_is_not_replaced_by_honesty_notice() -> None:
    from app.kernel.final_answer_evidence import verify_final_answer_tool_evidence

    content = (
        "## Prompt 导出任务最高优先级\n"
        "当用户要求完整 prompt 时，这是纯文本导出任务。\n"
        "不得列出 `web_fetch` 已返回、失败或超时，也不得要求用户重试。"
    )

    assert (
        verify_final_answer_tool_evidence(
            content,
            available_tool_names={"web_fetch", "read_file"},
            tool_evidence_summary={"schema": "hive.ccplus.tool_evidence_ledger.v1", "has_tool_evidence": False},
        )
        == content
    )


def test_mode_explanation_with_tool_names_is_not_replaced_by_honesty_notice() -> None:
    from app.kernel.final_answer_evidence import verify_final_answer_tool_evidence

    content = "A 模式是不调用工具的问答模式；B 模式才会在需要时使用 web_fetch 返回事实证据。"

    assert (
        verify_final_answer_tool_evidence(
            content,
            available_tool_names={"web_fetch"},
            tool_evidence_summary={"schema": "hive.ccplus.tool_evidence_ledger.v1", "has_tool_evidence": False},
        )
        == content
    )


def test_explicit_tool_result_question_still_gets_honesty_notice() -> None:
    from app.kernel.final_answer_evidence import verify_final_answer_tool_evidence

    repaired = verify_final_answer_tool_evidence(
        "web_fetch 返回了最新公告内容。",
        available_tool_names={"web_fetch"},
        tool_evidence_summary={"schema": "hive.ccplus.tool_evidence_ledger.v1", "has_tool_evidence": False},
    )

    assert "本轮没有实际工具调用记录" in repaired
    assert "web_fetch" in repaired


def test_final_answer_uses_structured_tool_ledger_summary() -> None:
    from app.kernel.final_answer_evidence import verify_final_answer_tool_evidence
    from app.runtime.tool_evidence_ledger import ToolEvidenceLedger

    ledger = ToolEvidenceLedger.from_parts(
        [{"type": "tool_call", "name": "web_fetch", "status": "done", "result": "ok"}]
    )
    content = "web_fetch 返回了最新公告内容。"

    assert (
        verify_final_answer_tool_evidence(
            content,
            available_tool_names={"web_fetch"},
            tool_evidence_summary=ledger.to_summary(),
        )
        == content
    )
