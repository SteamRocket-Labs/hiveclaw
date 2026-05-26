from __future__ import annotations

import json
from pathlib import Path

from app.services.evolution_view import build_evolution_view


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_workspace(ws: Path) -> None:
    """Write realistic data-source files into a temp agent workspace."""
    usage = {
        "weekly-report": {
            "created_by": "agent",
            "created_at": "2026-01-01T08:00:00+00:00",
            "last_used_at": "2026-05-20T09:00:00+00:00",
            "use_count": 12,
            "view_count": 3,
            "state": "active",
            "pinned": True,
            "archived_at": None,
        },
        "invoice-parse": {
            "created_by": "agent",
            "created_at": "2026-02-01T08:00:00+00:00",
            "last_used_at": "2026-03-01T09:00:00+00:00",
            "use_count": 4,
            "view_count": 1,
            "state": "stale",
            "pinned": False,
            "archived_at": None,
        },
        "old-scraper": {
            "created_by": "agent",
            "created_at": "2025-10-01T08:00:00+00:00",
            "last_used_at": "2025-11-01T09:00:00+00:00",
            "use_count": 2,
            "view_count": 0,
            "state": "archived",
            "pinned": False,
            "archived_at": "2026-02-01T00:00:00+00:00",
        },
    }
    _write(ws / "skills" / ".usage.json", json.dumps(usage))

    _write(
        ws / "evolution" / "skill_review.md",
        "# Skill Review\n\n"
        "- 2026-05-20T09:05:00+00:00 [promote] weekly-report: 3 successes in window\n"
        "- 2026-03-01T09:10:00+00:00 [stale] invoice-parse: curator auto-transition active→stale: unused since 2026-03-01 (> 30d)\n"
        "- 2026-02-01T00:00:00+00:00 [archived] old-scraper: curator auto-transition stale→archived\n",
    )

    ledger_lines = [
        json.dumps(
            {
                "schema": "evolution_candidate.v1",
                "event": "candidate",
                "candidate_id": "abc123",
                "target_type": "skill",
                "target_id": "weekly-report",
                "created_at": "2026-05-19T08:00:00+00:00",
            }
        ),
        json.dumps(
            {
                "schema": "evolution_eval_run.v1",
                "event": "eval_run",
                "candidate_id": "abc123",
                "dataset": "regression",
                "reward": 0.9,
                "baseline_reward": 0.7,
                "passed": True,
                "created_at": "2026-05-20T08:00:00+00:00",
            }
        ),
        json.dumps(
            {
                "schema": "evolution_promotion_decision.v1",
                "event": "promotion_decision",
                "candidate_id": "abc123",
                "decision": "promote",
                "reason": "reward beat baseline with no critical regressions",
                "created_at": "2026-05-21T08:00:00+00:00",
            }
        ),
    ]
    _write(ws / "evolution" / "evolution_ledger.jsonl", "\n".join(ledger_lines) + "\n")


def test_skill_summary_counts_by_state(tmp_path):
    _seed_workspace(tmp_path)
    view = build_evolution_view(tmp_path)
    summary = view["skill_summary"]
    assert summary == {"active": 1, "stale": 1, "archived": 1, "total": 3}


def test_skills_list_carries_state_and_usage_sorted(tmp_path):
    _seed_workspace(tmp_path)
    view = build_evolution_view(tmp_path)
    skills = view["skills"]
    assert len(skills) == 3
    # active first, then by use_count desc — active(weekly-report) leads.
    assert skills[0]["slug"] == "weekly-report"
    assert skills[0]["state"] == "active"
    assert skills[0]["use_count"] == 12
    assert skills[0]["pinned"] is True
    assert skills[0]["last_used_at"] == "2026-05-20T09:00:00+00:00"
    slugs = {s["slug"] for s in skills}
    assert slugs == {"weekly-report", "invoice-parse", "old-scraper"}


def test_timeline_merges_review_and_ledger_reverse_chron(tmp_path):
    _seed_workspace(tmp_path)
    view = build_evolution_view(tmp_path)
    timeline = view["timeline"]
    # 3 review events + 3 ledger events
    assert len(timeline) == 6
    timestamps = [item["at"] for item in timeline]
    assert timestamps == sorted(timestamps, reverse=True)
    # newest event is the promotion decision (2026-05-21)
    assert timeline[0]["kind"] == "promotion"
    assert timeline[0]["at"].startswith("2026-05-21")
    kinds = {item["kind"] for item in timeline}
    assert {"promote", "stale", "archived", "candidate", "eval", "promotion"} == kinds
    for item in timeline:
        assert set(item.keys()) == {"at", "kind", "title", "detail"}


def test_missing_workspace_returns_empty_structure(tmp_path):
    view = build_evolution_view(tmp_path / "does-not-exist")
    assert view == {
        "skill_summary": {"active": 0, "stale": 0, "archived": 0, "total": 0},
        "skills": [],
        "timeline": [],
    }


def test_corrupt_files_do_not_raise(tmp_path):
    _write(tmp_path / "skills" / ".usage.json", "{ not json")
    _write(tmp_path / "evolution" / "evolution_ledger.jsonl", "garbage line\n{bad}\n")
    _write(tmp_path / "evolution" / "skill_review.md", "# Skill Review\n\nnot a bullet line\n")
    view = build_evolution_view(tmp_path)
    assert view["skill_summary"]["total"] == 0
    assert view["skills"] == []
    assert view["timeline"] == []
