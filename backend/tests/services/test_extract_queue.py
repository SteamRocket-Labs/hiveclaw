"""Tests for the durable extraction queue (P0-2a).

Covers the contract that hot-path extractions can survive process death:
enqueue → mark_done removes the file; failures leave the file for replay
by P0-2b. Filesystem is redirected via AGENT_DATA_DIR override per test.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def queue_root(tmp_path, monkeypatch):
    """Redirect AGENT_DATA_DIR to a temp dir so tests don't touch real volume."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    qroot = tmp_path / ".failed_extractions"
    qroot.mkdir(parents=True, exist_ok=True)  # ensure tests can plant files directly
    yield qroot


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_enqueue_persists_payload_with_all_fields(queue_root):
    from app.services import extract_queue

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]

    entry_id = extract_queue.enqueue(
        agent_id=agent_id,
        messages=messages,
        source="websocket",
        tenant_id=tenant_id,
        agent_name="Researcher",
    )

    assert entry_id.startswith(str(agent_id))
    files = list(queue_root.glob("*.json"))
    assert len(files) == 1
    payload = _read_json(files[0])
    assert payload["entry_id"] == entry_id
    assert payload["agent_id"] == str(agent_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["source"] == "websocket"
    assert payload["agent_name"] == "Researcher"
    assert payload["messages"] == messages
    assert isinstance(payload["scheduled_at_ms"], int)


def test_enqueue_reuses_entry_for_stable_message_ids(queue_root):
    from app.services import extract_queue

    agent_id = uuid.uuid4()
    messages = [
        {"role": "user", "id": "msg-1", "content": "remember this"},
        {"role": "tool", "tool_call_id": "tool-1", "content": "done"},
    ]

    first = extract_queue.enqueue(
        agent_id=agent_id,
        messages=messages,
        source="web",
        tenant_id=None,
        agent_name="Agent",
    )
    second = extract_queue.enqueue(
        agent_id=agent_id,
        messages=messages,
        source="web",
        tenant_id=None,
        agent_name="Agent",
    )

    files = list(queue_root.glob("*.json"))
    payload = _read_json(files[0])

    assert first == second
    assert len(files) == 1
    assert payload["idempotency_key"]


def test_enqueue_handles_none_tenant_and_empty_messages(queue_root):
    from app.services import extract_queue

    entry_id = extract_queue.enqueue(
        agent_id=uuid.uuid4(),
        messages=None,
        source="trigger",
        tenant_id=None,
        agent_name="Bot",
    )

    payload = _read_json(queue_root / f"{entry_id}.json")
    assert payload["tenant_id"] is None
    assert payload["messages"] == []


def test_mark_done_deletes_entry(queue_root):
    from app.services import extract_queue

    entry_id = extract_queue.enqueue(
        agent_id=uuid.uuid4(),
        messages=[{"role": "user", "content": "x"}],
        source="web",
        tenant_id=None,
        agent_name="Agent",
    )
    path = queue_root / f"{entry_id}.json"
    assert path.exists()

    extract_queue.mark_done(entry_id)
    assert not path.exists()


def test_mark_done_is_idempotent_when_entry_missing(queue_root):
    """No exception when the entry was already cleared by another worker."""
    from app.services import extract_queue

    extract_queue.mark_done("nonexistent-entry-id")  # must not raise
    extract_queue.mark_done("nonexistent-entry-id")  # second call also fine


def test_list_pending_yields_all_unfinished(queue_root):
    from app.services import extract_queue

    ids = [
        extract_queue.enqueue(
            agent_id=uuid.uuid4(),
            messages=[{"role": "user", "content": str(i)}],
            source="web",
            tenant_id=None,
            agent_name=f"Agent{i}",
        )
        for i in range(3)
    ]

    pending = list(extract_queue.list_pending())
    assert {e.entry_id for e in pending} == set(ids)
    assert all(e.path.exists() for e in pending)
    assert all(e.scheduled_at_ms > 0 for e in pending)


def test_list_pending_skips_done_entries(queue_root):
    from app.services import extract_queue

    keep = extract_queue.enqueue(agent_id=uuid.uuid4(), messages=[], source="x", tenant_id=None, agent_name="A")
    drop = extract_queue.enqueue(agent_id=uuid.uuid4(), messages=[], source="x", tenant_id=None, agent_name="A")
    extract_queue.mark_done(drop)

    pending = list(extract_queue.list_pending())
    assert {e.entry_id for e in pending} == {keep}


def test_list_pending_skips_unreadable_entries(queue_root, caplog):
    from app.services import extract_queue

    good = extract_queue.enqueue(agent_id=uuid.uuid4(), messages=[], source="x", tenant_id=None, agent_name="A")

    # Plant a corrupt file alongside the good one.
    bad_path = queue_root / "broken-entry.json"
    bad_path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        pending = list(extract_queue.list_pending())

    assert {e.entry_id for e in pending} == {good}
    assert any("Skipping unreadable entry" in r.message for r in caplog.records)


def test_list_pending_max_age_filters_old_entries(queue_root):
    from app.services import extract_queue

    fresh = extract_queue.enqueue(agent_id=uuid.uuid4(), messages=[], source="x", tenant_id=None, agent_name="A")
    # Plant a stale entry by editing scheduled_at_ms to past.
    stale_path = queue_root / "stale-entry.json"
    stale_path.write_text(
        json.dumps(
            {
                "entry_id": "stale-entry",
                "agent_id": str(uuid.uuid4()),
                "messages": [],
                "source": "x",
                "tenant_id": None,
                "agent_name": "A",
                "scheduled_at_ms": int((time.time() - 3600) * 1000),  # 1h ago
            }
        ),
        encoding="utf-8",
    )

    pending = list(extract_queue.list_pending(max_age_seconds=60))  # only last minute
    assert {e.entry_id for e in pending} == {fresh}


def test_purge_older_than_removes_stale(queue_root):
    from app.services import extract_queue

    fresh = extract_queue.enqueue(agent_id=uuid.uuid4(), messages=[], source="x", tenant_id=None, agent_name="A")
    stale_path = queue_root / "stale-entry.json"
    stale_path.write_text(
        json.dumps(
            {
                "entry_id": "stale-entry",
                "agent_id": str(uuid.uuid4()),
                "messages": [],
                "source": "x",
                "tenant_id": None,
                "agent_name": "A",
                "scheduled_at_ms": int((time.time() - 3600) * 1000),
            }
        ),
        encoding="utf-8",
    )

    purged = extract_queue.purge_older_than(60)
    assert purged == 1
    assert not stale_path.exists()
    assert (queue_root / f"{fresh}.json").exists()


def test_purge_older_than_drops_unreadable_entries(queue_root):
    from app.services import extract_queue

    bad_path = queue_root / "broken.json"
    bad_path.write_text("{garbage", encoding="utf-8")

    purged = extract_queue.purge_older_than(60)
    assert purged == 1
    assert not bad_path.exists()


def test_queue_root_is_created_on_first_enqueue(tmp_path, monkeypatch):
    """Lazy mkdir so initial deployments don't need pre-provisioning."""
    from app.config import get_settings
    from app.services import extract_queue

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    queue_dir = tmp_path / ".failed_extractions"
    assert not queue_dir.exists()

    extract_queue.enqueue(agent_id=uuid.uuid4(), messages=[], source="x", tenant_id=None, agent_name="A")
    assert queue_dir.exists()
