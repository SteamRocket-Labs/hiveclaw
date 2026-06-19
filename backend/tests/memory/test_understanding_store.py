"""Read-only legacy understanding projection tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memory.understanding_store import UnderstandingStore


def _legacy_understanding_file(memory_dir: Path) -> Path:
    memory_dir.mkdir(parents=True, exist_ok=True)
    path = memory_dir / "understandings.md"
    path.write_text(
        "<!-- understanding\n"
        "entry_id: legacy-1\n"
        "subject: alice\n"
        "object: acme\n"
        "relation_type: owner_of\n"
        "evidence_refs: t3:memory/t3/user.md#block-a\n"
        "confidence: 0.9\n"
        f"last_confirmed_at: {datetime.now(timezone.utc).isoformat()}\n"
        "-->\n"
        "alice is founder of acme\n",
        encoding="utf-8",
    )
    return path


def test_record_is_disabled(tmp_path: Path) -> None:
    store = UnderstandingStore(tmp_path / "memory")

    with pytest.raises(RuntimeError, match="writes are disabled"):
        store.record(
            subject="agent_a",
            object_="agent_b",
            relation_type="collaborator",
            current_understanding="agent_b drafts research summaries",
            evidence_refs=[],
            confidence=0.7,
        )

    assert not (tmp_path / "memory" / "understandings.md").exists()


def test_contradict_is_disabled_even_for_existing_projection(tmp_path: Path) -> None:
    _legacy_understanding_file(tmp_path / "memory")
    store = UnderstandingStore(tmp_path / "memory")

    with pytest.raises(RuntimeError, match="writes are disabled"):
        store.contradict(
            entry_id="legacy-1",
            new_understanding="alice is no longer founder",
            evidence_refs=[],
            confidence=0.5,
        )


def test_query_reads_legacy_projection_without_becoming_truth_source(tmp_path: Path) -> None:
    _legacy_understanding_file(tmp_path / "memory")
    store = UnderstandingStore(tmp_path / "memory")

    entries = store.query(subject="alice", relation_type="owner_of")

    assert len(entries) == 1
    assert entries[0].entry_id == "legacy-1"
    assert entries[0].object_ == "acme"
    assert entries[0].current_understanding == "alice is founder of acme"


def test_decayed_confidence_still_works_for_legacy_projection(tmp_path: Path) -> None:
    _legacy_understanding_file(tmp_path / "memory")
    store = UnderstandingStore(tmp_path / "memory")

    decayed = store.decayed_confidence("legacy-1", now=datetime.now(timezone.utc) + timedelta(days=90))

    assert 0.0 < decayed < 0.9


def test_unknown_reads_remain_safe(tmp_path: Path) -> None:
    store = UnderstandingStore(tmp_path / "memory")

    assert store.get("missing") is None
    assert store.decayed_confidence("missing", now=datetime.now(timezone.utc)) == 0.0
