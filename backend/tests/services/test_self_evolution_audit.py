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
        "- [2026-05-02][w=1.00][src=web][cat=feedback] Legacy entry without evidence must be ignored\n",
        encoding="utf-8",
    )
    package_dir = workspace / "memory" / "sessions" / "s1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True)
    (package_dir / "summary.md").write_text("<t2_summary>Canonical package without refs</t2_summary>", encoding="utf-8")
    (package_dir / "labels.md").write_text("<t2_labels/>", encoding="utf-8")
    (package_dir / "review.md").write_text("<t2_review><decision>approved</decision></t2_review>", encoding="utf-8")
    (package_dir / "manifest.json").write_text(
        '{"schema_version":"t2.segment-package.manifest.v1","package_status":"reviewed","source_refs":[]}\n',
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
        "---\n"
        "schema: hive.soul.v2\n"
        "---\n\n"
        '<soul_user_model id="audit-gap" status="active">\n'
        "<claim>I prefer evidence-tagged memory writes</claim>\n"
        "</soul_user_model>\n",
        encoding="utf-8",
    )

    report = run_self_evolution_audit(data_root=tmp_path, agent_id=agent_id, write_report=True)

    assert report["agent_id"] == str(agent_id)
    assert report["t2_entries_without_evidence"] == 1
    assert report["t3_entries_without_source_ref"] == 1
    assert report["soul_lines_without_promotion_record"] == 1
    assert Path(report["report_path"]).exists()
