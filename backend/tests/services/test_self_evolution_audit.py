from __future__ import annotations

import uuid
from pathlib import Path


def test_self_evolution_audit_reports_memory_evidence_gaps(tmp_path: Path) -> None:
    from app.services.self_evolution_audit import run_self_evolution_audit

    agent_id = uuid.uuid4()
    workspace = tmp_path / str(agent_id)
    learnings = workspace / "memory" / "learnings"
    learnings.mkdir(parents=True)
    (learnings / "insights.md").write_text(
        "# Insights\n"
        "- [2026-05-02][w=1.00][src=web][cat=feedback] Legacy entry without evidence\n"
        "- [2026-05-02][w=1.00][src=web][cat=feedback][ev=user_stated][refs=t0:chat.md#L1-L2] Tagged entry\n",
        encoding="utf-8",
    )
    memory_dir = workspace / "memory"
    (memory_dir / "t3").mkdir(parents=True, exist_ok=True)
    (memory_dir / "t3" / "user.md").write_text(
        "# T3 User\n\n"
        '<t3_user_memory id="missing-source" status="active" created_at="2026-05-02">'
        "<claim>T3 entry without source ref</claim>"
        "</t3_user_memory>\n",
        encoding="utf-8",
    )
    (workspace / "soul.md").write_text(
        "# Soul\n\n## Learned Behaviors\n- I prefer evidence-tagged memory writes\n",
        encoding="utf-8",
    )

    report = run_self_evolution_audit(data_root=tmp_path, agent_id=agent_id, write_report=True)

    assert report["agent_id"] == str(agent_id)
    assert report["t2_entries_without_evidence"] == 1
    assert report["t3_entries_without_source_ref"] == 1
    assert report["soul_lines_without_promotion_record"] == 1
    assert Path(report["report_path"]).exists()
