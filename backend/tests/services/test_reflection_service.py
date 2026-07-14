from __future__ import annotations

import json
import uuid
from pathlib import Path


def test_reportable_reflection_writes_artifact_without_legacy_t2_projection(tmp_path: Path) -> None:
    from app.services.reflection_service import create_reportable_reflection

    agent_id = uuid.uuid4()
    report = create_reportable_reflection(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="s-1",
        reason="loop_guard_triggered",
        messages=[
            {"role": "user", "content": "Retry failed search"},
            {"role": "assistant", "content": "Search failed twice"},
        ],
        metadata={"trace_ref": "logs/2026-05-02/traces/s-1.jsonl"},
    )

    report_path = Path(report["report_path"])
    legacy_projection_dir = tmp_path / str(agent_id) / "memory" / "learnings"

    assert report_path.exists()
    assert report["canonical_t2_projected"] is False
    assert report["projection_status"] == "reflection_artifact_only"
    assert "loop_guard_triggered" in report_path.read_text(encoding="utf-8")
    assert "logs/2026-05-02/traces/s-1.jsonl" in report_path.read_text(encoding="utf-8")
    assert not legacy_projection_dir.exists()


def test_reportable_reflection_is_full_mechanical_evidence_not_platform_semantics(tmp_path: Path) -> None:
    from app.services.reflection_service import create_reportable_reflection

    agent_id = uuid.uuid4()
    earliest_tail = "EARLIEST_EVIDENCE_TAIL"
    messages = [
        {"role": "user", "content": "u" * 700 + earliest_tail},
        *({"role": "assistant", "content": f"message-{index}"} for index in range(7)),
    ]
    report = create_reportable_reflection(
        data_root=tmp_path,
        agent_id=agent_id,
        session_id="s-full",
        reason="partial_failure",
        messages=messages,
        metadata={"partial_failure": True, "trace_ref": "trace:full"},
    )

    payload = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert payload["messages"] == messages
    assert earliest_tail in json.dumps(payload, ensure_ascii=False)
    assert payload["semantic_review_status"] == "not_run"
    assert "decision" not in payload
    assert "root_cause" not in payload
    assert "next_policy" not in payload


def test_reportability_uses_structured_runtime_facts_not_text_or_message_count() -> None:
    from app.runtime.hooks_setup import _is_reportable_session

    benign_messages = [{"role": "user", "content": "Discuss the word failed as documentation text."} for _ in range(30)]

    assert _is_reportable_session(benign_messages, {}) is False
    assert _is_reportable_session([], {"partial_failure": True}) is True
