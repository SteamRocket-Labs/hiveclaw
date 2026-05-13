"""Handler-level Phase 0 tests for failed-run diagnostics.

Tier 1-1 + Tier 1-2: when synthesis fails, deep_research_check must still surface
source_notes/lane_summaries paths, ledger counts, gaps explaining the failure, and a
failure-notice partial_report — never the legacy pasted fallback dump.
"""

from __future__ import annotations

import json
from pathlib import Path


def _seed_failed_run(artifact_dir: Path, source_id: str = "src_aaaaaaaaaaaa") -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)

    (artifact_dir / "sources.jsonl").write_text(
        json.dumps(
            {
                "source_id": source_id,
                "url": "https://example.invalid/x",
                "title": "Title",
                "publisher": "Publisher",
                "source_type": "primary",
                "content": "body",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "claims.jsonl").write_text(
        json.dumps(
            {
                "claim_id": "claim_x",
                "text": "Issuer A reports 35% growth.",
                "status": "verified",
                "source_ids": [source_id],
                "evidence": "evidence",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "source_notes.jsonl").write_text(
        json.dumps(
            {
                "source_id": source_id,
                "key_entities": ["Issuer A"],
                "key_numbers": ["35% growth"],
                "source_bound_summary": "Issuer A reports 35% growth.",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "lane_summaries.jsonl").write_text(
        json.dumps(
            {
                "lane_id": "primary",
                "evidence_strength": "moderate",
                "key_findings": ["growth confirmed"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (artifact_dir / "report.md").write_text(
        "# Deep Research — Synthesis Failed\n\n"
        "This is not a completed Deep Research report.\n\n"
        "The synthesis step failed; the evidence ledger is preserved for diagnosis.\n",
        encoding="utf-8",
    )
    (artifact_dir / "final.json").write_text(
        json.dumps(
            {
                "schema": "deep_research_final.v1",
                "status": "failed",
                "summary": "Deep research produced a failed synthesis with preserved ledger.",
                "question": "test question",
                "mode": "topic_deep_dive",
                "source_count": 1,
                "claim_count": 1,
                "quality_gates": {"attribution": "passed", "synthesis": "failed"},
                "gaps": [
                    "Synthesis failed; no user-deliverable report was produced.",
                    "LLM outage during synthesis.",
                ],
                "sources": [{"source_id": source_id}],
                "claims": [{"claim_id": "claim_x"}],
                "source_notes_path": (artifact_dir / "source_notes.jsonl").as_posix(),
                "lane_summaries_path": (artifact_dir / "lane_summaries.jsonl").as_posix(),
                "report_path": (artifact_dir / "report.md").as_posix(),
                "created_at": "2026-05-13T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_deep_research_check_surfaces_artifacts_and_gaps_on_failed_run(tmp_path):
    """Tier 1-1 + Tier 1-2 contract: failed runs are not lost — the check tool must surface
    artifact paths (including source_notes/lane_summaries), counts, gaps, and a partial
    report that contains the failure notice rather than a pasted evidence dump."""
    from app.tools.handlers.deep_research import _deep_research_dir, _read_deep_research_artifact

    task_id = "abcdef0123456789abcdef0123456789"
    artifact_dir = _deep_research_dir(tmp_path, task_id)
    _seed_failed_run(artifact_dir)

    result = _read_deep_research_artifact(tmp_path, task_id)

    assert result["status"] == "failed"
    assert result["source_count"] == 1
    assert result["claim_count"] >= 1
    assert result["gaps"], "failed run must surface gaps for the agent to act on"
    assert any("synth" in gap.lower() for gap in result["gaps"])
    assert result["quality_gates"].get("synthesis") == "failed"

    # Tier 1-1: source_notes and lane_summaries paths must be surfaced for diagnosis
    assert "source_notes_path" in result, (
        "Tier 1-1 contract: _read_deep_research_artifact must include source_notes_path"
    )
    assert "lane_summaries_path" in result, (
        "Tier 1-1 contract: _read_deep_research_artifact must include lane_summaries_path"
    )
    assert result["source_notes_path"], "source_notes_path must point to the artifact file"
    assert result["lane_summaries_path"], "lane_summaries_path must point to the artifact file"

    # Tier 1-2: failure notice must be surfaced via partial_report, not the legacy dump
    assert result["partial_report"]
    text_lower = result["partial_report"].lower()
    assert ("synthesis failed" in text_lower) or ("not a completed" in text_lower)
    assert "Source-Grounded Findings" not in result["partial_report"]
    assert "Evidence coverage spans" not in result["partial_report"]
