"""Handler-level Phase 0 + Tier 3-4 tests.

Phase 0 / Tier 1-1 + 1-2: when synthesis fails, deep_research_check must still
surface source_notes/lane_summaries paths, ledger counts, gaps explaining the
failure, and a failure-notice partial_report — never the legacy pasted fallback.

Tier 3-4: stream_deep_research_artifacts yields incremental events keyed by
filename, terminates on `final.json`, supports cursor resumption.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


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


# ─────────── Tier 3-4: streaming generator ───────────


@pytest.mark.asyncio
async def test_stream_deep_research_artifacts_emits_typed_events_until_final(tmp_path):
    """T3-4: every artifact line surfaces as a typed event; final.json terminates the stream."""
    from app.tools.handlers.deep_research import _deep_research_dir, stream_deep_research_artifacts

    task_id = "fedcba9876543210fedcba9876543210"
    artifact_dir = _deep_research_dir(tmp_path, task_id)
    _seed_failed_run(artifact_dir)

    events: list[dict] = []
    async for event in stream_deep_research_artifacts(
        tmp_path, task_id, poll_interval_seconds=0.0
    ):
        events.append(event)
        if event["event"] == "final":
            break

    event_types = [e["event"] for e in events]
    # The seeded artifact has source_notes, lane_summaries, claims, and a report; no steps file.
    assert "claim" in event_types
    assert "source_note" in event_types
    assert "lane_summary" in event_types
    assert "report" in event_types
    assert event_types[-1] == "final", "final event must terminate the stream"

    # task_id and timestamp are echoed on every event
    assert all(e["task_id"] == task_id for e in events)
    assert all(e["timestamp"] for e in events)

    # Final payload matches the seeded final.json
    final_event = events[-1]
    assert final_event["payload"]["status"] == "failed"
    assert final_event["payload"]["source_count"] == 1


@pytest.mark.asyncio
async def test_stream_deep_research_artifacts_supports_cursor_resume(tmp_path):
    """T3-4: callers can resume past previously-emitted records via cursors."""
    from app.tools.handlers.deep_research import _deep_research_dir, stream_deep_research_artifacts

    task_id = "01234567890123456789012345678901"
    artifact_dir = _deep_research_dir(tmp_path, task_id)
    _seed_failed_run(artifact_dir)

    # Pre-seed cursors so the stream skips the first claim/source_note line.
    cursors = {"claim": 1, "source_note": 1, "lane_summary": 1}

    events: list[dict] = []
    async for event in stream_deep_research_artifacts(
        tmp_path, task_id, poll_interval_seconds=0.0, cursors=cursors
    ):
        events.append(event)
        if event["event"] == "final":
            break

    event_types = [e["event"] for e in events]
    # Already-consumed line types should be skipped
    assert "claim" not in event_types
    assert "source_note" not in event_types
    assert "lane_summary" not in event_types
    # But report + final still surface
    assert "report" in event_types
    assert event_types[-1] == "final"
