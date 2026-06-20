from __future__ import annotations


def test_unbacked_tool_result_claim_is_replaced_with_honesty_notice() -> None:
    from app.kernel.engine import _repair_unbacked_tool_result_claim

    repaired = _repair_unbacked_tool_result_claim(
        "send_message_to_agent 超时 120s，feishu_wiki_list 返回 Feishu/Lark is not configured",
        tool_names={"send_message_to_agent", "feishu_wiki_list"},
        has_tool_evidence=False,
    )

    assert "本轮没有实际工具调用记录" in repaired
    assert "send_message_to_agent" in repaired
    assert "feishu_wiki_list" in repaired


def test_tool_result_claim_is_preserved_when_tool_evidence_exists() -> None:
    from app.kernel.engine import _repair_unbacked_tool_result_claim

    content = "feishu_wiki_list 返回 3 个页面"

    assert (
        _repair_unbacked_tool_result_claim(
            content,
            tool_names={"feishu_wiki_list"},
            has_tool_evidence=True,
        )
        == content
    )
