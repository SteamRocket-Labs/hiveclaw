from __future__ import annotations

import uuid
from pathlib import Path


def test_append_t2_entries_writes_weighted_metadata_and_dedupes(tmp_path: Path) -> None:
    from app.memory.t2_store import append_t2_entries

    agent_id = uuid.uuid4()

    written_first = append_t2_entries(
        tmp_path,
        agent_id,
        extractions=[{"category": "feedback", "content": "User prefers concise output"}],
        source="web",
        timestamp="2026-04-08",
    )
    written_second = append_t2_entries(
        tmp_path,
        agent_id,
        extractions=[{"category": "feedback", "content": "User prefers concise output"}],
        source="web",
        timestamp="2026-04-08",
    )

    content = (tmp_path / str(agent_id) / "memory" / "learnings" / "insights.md").read_text(encoding="utf-8")

    assert written_first == 1
    assert written_second == 0
    assert "- [2026-04-08][w=1.00][src=web][cat=feedback] User prefers concise output" in content


def test_parse_t2_entry_line_reads_metadata() -> None:
    from app.memory.t2_store import parse_t2_entry_line

    entry = parse_t2_entry_line(
        "- [2026-04-08][w=0.70][src=trigger][cat=error] tool call failed after timeout"
    )

    assert entry is not None
    assert entry["timestamp"] == "2026-04-08"
    assert entry["weight"] == 0.70
    assert entry["source"] == "trigger"
    assert entry["category"] == "error"
    assert entry["content"] == "tool call failed after timeout"


def test_render_t2_snapshot_groups_by_priority_and_repetition() -> None:
    from app.memory.t2_store import render_t2_snapshot

    snapshot = render_t2_snapshot(
        [
            {
                "timestamp": "2026-04-08",
                "weight": 1.0,
                "source": "web",
                "category": "feedback",
                "content": "User prefers concise output",
                "repeat": 1,
                "file": "insights.md",
            },
            {
                "timestamp": "2026-04-08",
                "weight": 0.50,
                "source": "trigger",
                "category": "project",
                "content": "Nightly sync completed",
                "repeat": 2,
                "file": "insights.md",
            },
            {
                "timestamp": "2026-04-08",
                "weight": 0.30,
                "source": "web",
                "category": "request",
                "content": "Would be nice to support PDF parsing",
                "repeat": 1,
                "file": "requests.md",
            },
        ]
    )

    assert "## High Priority" in snapshot
    assert "## Medium Priority" in snapshot
    assert "## Low Priority" in snapshot
    assert "[w=1.00][repeat=1][src=web][cat=feedback]" in snapshot
    assert "[w=0.50][repeat=2][src=trigger][cat=project]" in snapshot
    assert "[w=0.30][repeat=1][src=web][cat=request]" in snapshot
