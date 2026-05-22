"""Phase 11: Understanding store + relationship memory tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.memory.understanding_store import (
    UnderstandingEntry,
    UnderstandingStore,
)


@pytest.fixture
def store(tmp_path: Path) -> UnderstandingStore:
    return UnderstandingStore(tmp_path / "memory")


class TestRecord:
    def test_record_creates_understanding_with_metadata(self, store: UnderstandingStore) -> None:
        entry = store.record(
            subject="agent_a",
            object_="agent_b",
            relation_type="collaborator",
            current_understanding="agent_b reliably delivers research drafts with cited sources",
            evidence_refs=["t0/2026-05-15/delegation-1430-abcd.md"],
            confidence=0.8,
        )
        assert isinstance(entry, UnderstandingEntry)
        assert entry.entry_id
        assert entry.subject == "agent_a"
        assert entry.object_ == "agent_b"
        assert entry.relation_type == "collaborator"
        assert entry.confidence == pytest.approx(0.8)
        assert entry.evidence_refs == ["t0/2026-05-15/delegation-1430-abcd.md"]
        assert entry.contradiction_history == []
        assert entry.last_confirmed_at is not None

    def test_record_persists_to_markdown(self, store: UnderstandingStore, tmp_path: Path) -> None:
        store.record(
            subject="alice",
            object_="acme",
            relation_type="owner_of",
            current_understanding="alice is the founder of acme and is the agent's direct owner",
            evidence_refs=[],
            confidence=0.95,
        )
        path = tmp_path / "memory" / "understandings.md"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "alice" in content
        assert "owner_of" in content


class TestContradict:
    def test_contradiction_creates_version_link(self, store: UnderstandingStore) -> None:
        original = store.record(
            subject="agent_a",
            object_="agent_b",
            relation_type="collaborator",
            current_understanding="agent_b delivers research drafts with cited sources",
            evidence_refs=["t0/old.md"],
            confidence=0.7,
        )
        contradicting = store.contradict(
            entry_id=original.entry_id,
            new_understanding="agent_b's last 2 drafts had fabricated citations on 2026-05-20",
            evidence_refs=["t0/2026-05-20/delegation-0900-xyz.md"],
            confidence=0.85,
        )
        assert contradicting.entry_id != original.entry_id
        assert contradicting.contradiction_history == [original.entry_id]

        refreshed_original = store.get(original.entry_id)
        assert refreshed_original is not None
        assert contradicting.entry_id in refreshed_original.contradiction_history
        assert refreshed_original.confidence < 0.7


class TestQuery:
    def test_query_filters_by_subject_and_relation(self, store: UnderstandingStore) -> None:
        store.record(
            subject="agent_a",
            object_="agent_b",
            relation_type="collaborator",
            current_understanding="agent_b drafts research summaries",
            evidence_refs=[],
            confidence=0.7,
        )
        store.record(
            subject="agent_a",
            object_="acme",
            relation_type="employer_context",
            current_understanding="acme requires legal review for external statements",
            evidence_refs=[],
            confidence=0.9,
        )

        collaborators = store.query(subject="agent_a", relation_type="collaborator")
        assert len(collaborators) == 1
        assert collaborators[0].object_ == "agent_b"

        all_agent_a = store.query(subject="agent_a")
        assert len(all_agent_a) == 2


class TestConfidenceDecay:
    def test_decay_after_threshold_days(self, store: UnderstandingStore) -> None:
        entry = store.record(
            subject="alice",
            object_="bob",
            relation_type="reports_to",
            current_understanding="bob reports to alice on the platform team",
            evidence_refs=[],
            confidence=1.0,
        )
        now = entry.last_confirmed_at + timedelta(days=90)
        decayed = store.decayed_confidence(entry.entry_id, now=now)
        assert decayed < 1.0
        assert decayed > 0.0

    def test_decay_returns_original_within_window(self, store: UnderstandingStore) -> None:
        entry = store.record(
            subject="alice",
            object_="bob",
            relation_type="reports_to",
            current_understanding="bob reports to alice on the platform team",
            evidence_refs=[],
            confidence=0.8,
        )
        decayed = store.decayed_confidence(entry.entry_id, now=entry.last_confirmed_at + timedelta(days=5))
        assert decayed == pytest.approx(0.8)


class TestReload:
    def test_store_reloads_from_markdown(self, tmp_path: Path) -> None:
        first = UnderstandingStore(tmp_path / "memory")
        first.record(
            subject="alice",
            object_="acme",
            relation_type="owner_of",
            current_understanding="alice is founder of acme",
            evidence_refs=["t0/x.md"],
            confidence=0.9,
        )

        second = UnderstandingStore(tmp_path / "memory")
        entries = second.query(subject="alice")
        assert len(entries) == 1
        assert entries[0].object_ == "acme"
        assert entries[0].confidence == pytest.approx(0.9)


class TestNonExistent:
    def test_get_unknown_returns_none(self, store: UnderstandingStore) -> None:
        assert store.get("does-not-exist") is None

    def test_contradict_unknown_raises(self, store: UnderstandingStore) -> None:
        with pytest.raises(KeyError):
            store.contradict(
                entry_id="missing",
                new_understanding="never mind",
                evidence_refs=[],
                confidence=0.5,
            )

    def test_decayed_confidence_unknown_returns_zero(self, store: UnderstandingStore) -> None:
        now = datetime.now(timezone.utc)
        assert store.decayed_confidence("missing", now=now) == 0.0
