"""Tests for startup replay of pending extractions (P0-2b).

Verifies the contract that entries left on disk by P0-2a are re-scheduled
through the normal hot path on next startup, and that stale entries are
neither re-scheduled nor silently deleted.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def queue_root(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HIVE_ENABLE_LEGACY_EXTRACT_REPLAY", "1")
    qroot = tmp_path / ".failed_extractions"
    qroot.mkdir(parents=True, exist_ok=True)
    yield qroot


def _plant_entry(queue_root: Path, *, entry_id: str, age_seconds: int = 0, **overrides) -> Path:
    payload = {
        "entry_id": entry_id,
        "agent_id": str(uuid.uuid4()),
        "messages": [{"role": "user", "content": "carryover"}],
        "source": "web",
        "tenant_id": str(uuid.uuid4()),
        "agent_name": "ResumedAgent",
        "scheduled_at_ms": int((time.time() - age_seconds) * 1000),
    }
    payload.update(overrides)
    path = queue_root / f"{entry_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_replay_is_disabled_by_default(queue_root, monkeypatch):
    from app.services.extract_queue_replay import replay_pending_extractions

    _plant_entry(queue_root, entry_id="disabled")
    monkeypatch.delenv("HIVE_ENABLE_LEGACY_EXTRACT_REPLAY", raising=False)

    result = await replay_pending_extractions()

    assert result == {"scheduled": 0, "skipped_stale": 0, "failed": 0, "disabled": 1}
    assert (queue_root / "disabled.json").exists()


@pytest.mark.asyncio
async def test_replay_empty_queue_returns_zero_counts(queue_root):
    from app.services.extract_queue_replay import replay_pending_extractions

    result = await replay_pending_extractions()
    assert result == {"scheduled": 0, "skipped_stale": 0, "failed": 0}


@pytest.mark.asyncio
async def test_replay_reschedules_each_entry_and_clears_old(queue_root, monkeypatch):
    """Each in-window entry triggers schedule_extract; old entries are removed."""
    from app.services import extract_queue_replay

    _plant_entry(queue_root, entry_id="entry-1", source="web", agent_name="A1")
    _plant_entry(queue_root, entry_id="entry-2", source="trigger", agent_name="A2")

    captured: list[dict] = []

    def _capture_schedule(**kwargs):
        captured.append(kwargs)
        return True

    fake_extractor = type("F", (), {"schedule_extract": staticmethod(_capture_schedule)})()
    monkeypatch.setattr(
        "app.services.extract_agent.extract_agent",
        fake_extractor,
    )

    result = await extract_queue_replay.replay_pending_extractions()

    assert result["scheduled"] == 2
    assert result["failed"] == 0
    assert {c["agent_name"] for c in captured} == {"A1", "A2"}
    # Source is tagged so downstream metrics can distinguish replays.
    assert all(c["source"].startswith("replay:") for c in captured)
    # Old entries are gone (new schedule_extract would normally re-enqueue,
    # but we mocked it out — so only the originals' deletion matters here).
    assert list(queue_root.glob("*.json")) == []


@pytest.mark.asyncio
async def test_replay_skips_stale_entries_beyond_max_age(queue_root, monkeypatch):
    from app.services import extract_queue_replay

    _plant_entry(queue_root, entry_id="fresh", age_seconds=10)
    _plant_entry(queue_root, entry_id="ancient", age_seconds=10 * 24 * 3600)  # 10 days old

    captured: list[dict] = []

    def _capture_stale(**kw):
        captured.append(kw)
        return True

    monkeypatch.setattr(
        "app.services.extract_agent.extract_agent",
        type("F", (), {"schedule_extract": staticmethod(_capture_stale)})(),
    )

    result = await extract_queue_replay.replay_pending_extractions(max_age_seconds=24 * 3600)

    assert result["scheduled"] == 1
    assert result["skipped_stale"] == 1
    assert len(captured) == 1
    # Ancient entry is still on disk for operator inspection.
    assert (queue_root / "ancient.json").exists()
    # Fresh entry was rescheduled and old file removed.
    assert not (queue_root / "fresh.json").exists()


@pytest.mark.asyncio
async def test_replay_keeps_entry_when_schedule_extract_raises(queue_root, monkeypatch, caplog):
    from app.services import extract_queue_replay

    path = _plant_entry(queue_root, entry_id="boom", source="web")

    def _explode(**_kwargs):
        raise RuntimeError("event loop missing")

    monkeypatch.setattr(
        "app.services.extract_agent.extract_agent",
        type("F", (), {"schedule_extract": staticmethod(_explode)})(),
    )

    with caplog.at_level("ERROR"):
        result = await extract_queue_replay.replay_pending_extractions()

    assert result == {"scheduled": 0, "skipped_stale": 0, "failed": 1}
    # Entry remains for next startup attempt.
    assert path.exists()
    assert any("schedule_extract raised for entry boom" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_replay_keeps_entry_when_reschedule_lacks_durable_enqueue(queue_root, monkeypatch, caplog):
    from app.services import extract_queue_replay

    path = _plant_entry(queue_root, entry_id="durability-gap", source="web")
    captured: list[dict] = []

    def _non_durable_schedule(**kwargs):
        captured.append(kwargs)
        return False

    monkeypatch.setattr(
        "app.services.extract_agent.extract_agent",
        type("F", (), {"schedule_extract": staticmethod(_non_durable_schedule)})(),
    )

    with caplog.at_level("ERROR"):
        result = await extract_queue_replay.replay_pending_extractions()

    assert result == {"scheduled": 0, "skipped_stale": 0, "failed": 1}
    assert captured[0]["require_durable_enqueue"] is True
    assert path.exists()
    assert any("without a fresh durable queue entry" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_replay_skips_entry_with_malformed_uuid(queue_root, monkeypatch, caplog):
    from app.services import extract_queue_replay

    path = _plant_entry(
        queue_root,
        entry_id="bad-uuid",
        agent_id="not-a-uuid",
    )

    captured: list[dict] = []

    def _capture_bad_uuid(**kw):
        captured.append(kw)
        return True

    monkeypatch.setattr(
        "app.services.extract_agent.extract_agent",
        type("F", (), {"schedule_extract": staticmethod(_capture_bad_uuid)})(),
    )

    with caplog.at_level("WARNING"):
        result = await extract_queue_replay.replay_pending_extractions()

    assert result["failed"] == 1
    assert result["scheduled"] == 0
    assert captured == []
    # Malformed entry stays on disk so an operator can investigate.
    assert path.exists()
    assert any("malformed UUID" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_replay_handles_none_tenant_id(queue_root, monkeypatch):
    """Entries with tenant_id=None (allowed in the queue payload) replay fine."""
    from app.services import extract_queue_replay

    _plant_entry(queue_root, entry_id="no-tenant", tenant_id=None)

    captured: list[dict] = []

    def _capture_no_tenant(**kw):
        captured.append(kw)
        return True

    monkeypatch.setattr(
        "app.services.extract_agent.extract_agent",
        type("F", (), {"schedule_extract": staticmethod(_capture_no_tenant)})(),
    )

    result = await extract_queue_replay.replay_pending_extractions()
    assert result["scheduled"] == 1
    assert captured[0]["tenant_id"] is None


@pytest.mark.asyncio
async def test_end_to_end_replay_drives_real_schedule_extract(queue_root, monkeypatch):
    """Plant a real entry, replay → real schedule_extract enqueues a fresh one
    (with replay: source) and the original entry is deleted."""
    from unittest.mock import patch

    from app.services import extract_queue, extract_queue_replay

    _plant_entry(queue_root, entry_id="e2e-original", source="web")

    # Stub _append_to_learnings so the real extract task completes quickly.
    with patch("app.services.extract_agent._append_to_learnings_with_llm", return_value=1):
        result = await extract_queue_replay.replay_pending_extractions()

    # The fresh schedule_extract path will have written its own entry —
    # await its completion so the queue settles before assertions.
    from app.services.extract_agent import extract_agent

    # Drain any in-flight tasks for the agent_id we replanted.
    pending_after_replay = list(extract_queue.list_pending())
    # Replay enqueues one new entry (then mark_done removes it on success).
    # Either way, original entry is gone.
    assert not (queue_root / "e2e-original.json").exists()

    # Drain whichever agent_id ended up in flight.
    if pending_after_replay:
        agent_uuid = uuid.UUID(pending_after_replay[0].agent_id)
        await extract_agent.drain(agent_uuid, timeout_s=5.0)

    # Give the done callback a tick to clear the new entry.
    await asyncio.sleep(0.05)

    assert result["scheduled"] == 1
    assert result["failed"] == 0
