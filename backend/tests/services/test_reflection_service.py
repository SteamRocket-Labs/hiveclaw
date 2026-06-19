from __future__ import annotations

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
        messages=[{"role": "user", "content": "Retry failed search"}, {"role": "assistant", "content": "Search failed twice"}],
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
