from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4


def test_false_notice_match_is_exact_not_fuzzy() -> None:
    from app.services.false_tool_evidence_repair import is_false_tool_evidence_notice

    exact = (
        "我不能确认刚才的工具状态：本轮没有实际工具调用记录，因此不能声称 "
        "`read_file` 已返回、失败或超时。请重试该操作，我会先调用对应工具并基于工具结果给出结论。"
    )

    assert is_false_tool_evidence_notice(exact) is True
    assert is_false_tool_evidence_notice("用户说：本轮没有实际工具调用记录") is False
    assert is_false_tool_evidence_notice("我不能确认工具状态，请稍后重试") is False


def test_reconstruct_original_final_requires_same_run_and_generation_window() -> None:
    from app.services.false_tool_evidence_repair import reconstruct_original_final

    run_id = uuid4()
    other_run = uuid4()
    started = datetime(2026, 7, 9, 10, 0, tzinfo=timezone.utc)
    terminal = SimpleNamespace(run_id=run_id, sequence=20)
    events = [
        SimpleNamespace(
            event_type="chunk", content="old", run_id=run_id, sequence=3, created_at=started - timedelta(seconds=1)
        ),
        SimpleNamespace(event_type="thinking", content="hidden", run_id=run_id, sequence=10, created_at=started),
        SimpleNamespace(event_type="chunk", content="正确", run_id=run_id, sequence=11, created_at=started),
        SimpleNamespace(
            event_type="chunk",
            content="答案",
            run_id=run_id,
            sequence=12,
            created_at=started + timedelta(milliseconds=10),
        ),
        SimpleNamespace(event_type="chunk", content="wrong-run", run_id=other_run, sequence=13, created_at=started),
    ]

    assert reconstruct_original_final(events, terminal_event=terminal, generation_started_at=started) == "正确答案"
    assert reconstruct_original_final(events, terminal_event=terminal, generation_started_at=None) is None
