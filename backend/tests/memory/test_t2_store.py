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
    assert "- [2026-04-08][w=1.00][src=web][cat=feedback]" in content
    assert "[sensitivity=PL1_public][status=active][version=1]" in content
    assert "User prefers concise output" in content


def test_parse_t2_entry_line_reads_metadata() -> None:
    from app.memory.t2_store import parse_t2_entry_line

    entry = parse_t2_entry_line("- [2026-04-08][w=0.70][src=trigger][cat=error] tool call failed after timeout")

    assert entry is not None
    assert entry["timestamp"] == "2026-04-08"
    assert entry["weight"] == 0.70
    assert entry["source"] == "trigger"
    assert entry["category"] == "error"
    assert entry["content"] == "tool call failed after timeout"


def test_t2_entry_supports_evidence_envelope_roundtrip() -> None:
    from app.memory.t2_store import format_t2_entry, parse_t2_entry_line

    line = format_t2_entry(
        category="feedback",
        content="User requires evidence-tagged memory writes",
        source="web",
        timestamp="2026-05-02",
        evidence="user_stated",
        confidence=0.91,
        volatility="stable",
        source_refs=["t0:behavior/chat-2026-05-02.md#L12-L18", "trace:rt-1"],
        novelty=0.72,
        reusability=0.83,
        concept="how-it-works",
        discovery_tokens=128,
    )
    entry = parse_t2_entry_line(line)

    assert "[ev=user_stated]" in line
    assert "[conf=0.91]" in line
    assert "[vol=stable]" in line
    assert "[refs=t0:behavior/chat-2026-05-02.md#L12-L18,trace:rt-1]" in line
    assert "[nov=0.72]" in line
    assert "[reuse=0.83]" in line
    assert "[concept=how-it-works]" in line
    assert "[discovery_tokens=128]" in line
    assert entry is not None
    assert entry["evidence"] == "user_stated"
    assert entry["confidence"] == 0.91
    assert entry["volatility"] == "stable"
    assert entry["source_refs"] == ["t0:behavior/chat-2026-05-02.md#L12-L18", "trace:rt-1"]
    assert entry["novelty"] == 0.72
    assert entry["reusability"] == 0.83
    assert entry["concept"] == "how-it-works"
    assert entry["discovery_tokens"] == 128


def test_append_t2_entries_defaults_evidence_envelope(tmp_path: Path) -> None:
    from app.memory.t2_store import append_t2_entries, load_t2_entries

    agent_id = uuid.uuid4()
    written = append_t2_entries(
        tmp_path,
        agent_id,
        extractions=[
            {
                "category": "blocked_pattern",
                "content": "Repeated list_files calls with identical args should stop",
                "evidence": "system_observed",
                "source_refs": ["trace:loop-1"],
                "volatility": "stable",
                "concept": "gotcha",
                "discovery_tokens": "64",
            }
        ],
        source="system",
        timestamp="2026-05-02",
    )

    entries, _mtimes = load_t2_entries(tmp_path, agent_id)

    assert written == 1
    assert entries[0]["evidence"] == "system_observed"
    assert entries[0]["source_refs"] == ["trace:loop-1"]
    assert entries[0]["volatility"] == "stable"
    assert entries[0]["concept"] == "gotcha"
    assert entries[0]["discovery_tokens"] == 64


def test_append_t2_entries_applies_write_gate_before_persisting(tmp_path: Path) -> None:
    from app.memory.lifecycle_store import MemoryLifecycleStore
    from app.memory.t2_store import append_t2_entries, load_t2_entries

    agent_id = uuid.uuid4()
    written = append_t2_entries(
        tmp_path,
        agent_id,
        extractions=[
            {
                "category": "user",
                "content": "Owner Alice email is alice@example.com for vendor escalation.",
                "evidence": "user_stated",
                "source_refs": ["t0:behavior/chat-2.md#L8"],
            },
            {
                "category": "reference",
                "content": "Owner Alice shared api_key=sk-1234567890abcdefghijklmnop for setup.",
                "evidence": "user_stated",
                "source_refs": ["t0:behavior/chat-3.md#L2"],
            },
        ],
        source="web",
        timestamp="2026-05-22",
    )

    body = (tmp_path / str(agent_id) / "memory" / "learnings" / "insights.md").read_text(encoding="utf-8")
    entries, _mtimes = load_t2_entries(tmp_path, agent_id)

    assert written == 1
    assert "alice@example.com" not in body
    assert "sk-1234567890abcdefghijklmnop" not in body
    assert "<Email_1>" in body
    assert "[sensitivity=PL2_pii]" in body
    assert "[status=active]" in body
    assert "[version=1]" in body
    assert "[access_count=0]" in body
    assert "[last_accessed=never]" in body
    assert entries[0]["sensitivity"] == "PL2_pii"
    assert entries[0]["status"] == "active"
    assert entries[0]["version"] == "1"
    assert entries[0]["access_count"] == "0"
    assert entries[0]["last_accessed"] == "never"
    lifecycle = MemoryLifecycleStore(tmp_path / str(agent_id) / "memory" / "lifecycle.json")
    lifecycle_entry = lifecycle.get(entries[0]["entry_id"])
    assert lifecycle_entry.content == "Owner Alice email is <Email_1> for vendor escalation."
    assert lifecycle_entry.metadata["sensitivity"] == "PL2_pii"


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
    assert "[concept=user-preference]" in render_t2_snapshot(
        [
            {
                "timestamp": "2026-04-08",
                "weight": 1.0,
                "source": "web",
                "category": "feedback",
                "concept": "user-preference",
                "discovery_tokens": 80,
                "content": "User prefers concise output",
                "repeat": 1,
                "file": "insights.md",
            }
        ]
    )
    assert "[w=0.50][repeat=2][src=trigger][cat=project]" in snapshot
    assert "[w=0.30][repeat=1][src=web][cat=request]" in snapshot


def test_t0_backfill_source_uses_human_bucket_weight() -> None:
    """PR-7: backfilled sessions share the original human conversation's
    provenance, so weight must match web/slack/etc. — not get demoted to
    the system bucket (0.85 for feedback vs 1.00 for human).
    """
    from app.memory.t2_store import compute_t2_weight

    assert compute_t2_weight("feedback", "t0_backfill") == compute_t2_weight("feedback", "web")
    assert compute_t2_weight("constraint", "t0_backfill") == compute_t2_weight("constraint", "slack")
    assert compute_t2_weight("feedback", "t0_backfill") == 1.00
