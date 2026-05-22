"""Phase 14: durable coordination store backed by sqlite."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents.coordination_store import SqliteCoordinationStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "coordination.db"


class _FixedClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class TestLeaseCrossInstance:
    def test_acquire_then_other_process_sees_lease(self, db_path: Path) -> None:
        store_a = SqliteCoordinationStore(db_path)
        store_b = SqliteCoordinationStore(db_path)
        result_a = store_a.acquire_lease(task_key="task-1", agent_id="agent_a", ttl_seconds=600)
        assert result_a.acquired is True
        assert result_a.lease is not None

        result_b = store_b.acquire_lease(task_key="task-1", agent_id="agent_b", ttl_seconds=600)
        assert result_b.acquired is False
        assert result_b.existing_lease_id == result_a.lease.id

    def test_lease_releases_after_expiry(self, db_path: Path) -> None:
        clock = _FixedClock(datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
        store = SqliteCoordinationStore(db_path, now=clock)

        result_a = store.acquire_lease(task_key="task-2", agent_id="agent_a", ttl_seconds=60)
        assert result_a.acquired is True

        clock.advance(120)
        result_b = store.acquire_lease(task_key="task-2", agent_id="agent_b", ttl_seconds=60)
        assert result_b.acquired is True
        assert result_b.lease is not None
        assert result_b.lease.agent_id == "agent_b"


class TestSignalCrossInstance:
    def test_signal_visible_from_other_instance(self, db_path: Path) -> None:
        store_a = SqliteCoordinationStore(db_path)
        store_b = SqliteCoordinationStore(db_path)
        signal = store_a.send_signal(
            from_agent_id="agent_a",
            to_agent_id="agent_b",
            content="started",
            signal_type="delegation_started",
            thread_id="t-1",
        )
        signals = store_b.read_signals("agent_b", thread_id="t-1")
        assert len(signals) == 1
        assert signals[0].id == signal.id
        assert signals[0].content == "started"


class TestCheckpointCrossInstance:
    def test_create_then_other_instance_gets_it(self, db_path: Path) -> None:
        store_a = SqliteCoordinationStore(db_path)
        store_b = SqliteCoordinationStore(db_path)
        deadline = datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc)
        checkpoint = store_a.create_checkpoint(
            action="send_feishu_message",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=deadline,
            metadata={"reason": "external_visible"},
        )
        roundtrip = store_b.get_checkpoint(checkpoint.id)
        assert roundtrip is not None
        assert roundtrip.id == checkpoint.id
        assert roundtrip.approver_id == "alice"
        assert roundtrip.escalation_chain == ["company_admin"]
        assert roundtrip.metadata["reason"] == "external_visible"

    def test_expired_checkpoint_escalates_for_all_observers(self, db_path: Path) -> None:
        clock = _FixedClock(datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc))
        store = SqliteCoordinationStore(db_path, now=clock)
        deadline = clock.current + timedelta(seconds=300)
        checkpoint = store.create_checkpoint(
            action="approve refund",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=deadline,
        )
        clock.advance(600)
        store.escalate_expired_checkpoints()

        other = SqliteCoordinationStore(db_path, now=clock)
        refreshed = other.get_checkpoint(checkpoint.id)
        assert refreshed is not None
        assert refreshed.current_approver_id == "company_admin"
        assert "escalated_at" in refreshed.metadata
