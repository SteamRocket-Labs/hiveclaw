"""Tests for memory session snapshot — frozen at session start."""

import uuid

import pytest

from app.memory.store import FileBackedMemoryStore
from app.runtime.session import SessionContext


@pytest.fixture
def tmp_agent_dir(tmp_path):
    agent_id = uuid.uuid4()
    agent_dir = tmp_path / str(agent_id)
    (agent_dir / "memory").mkdir(parents=True)
    return agent_id, agent_dir, tmp_path


@pytest.fixture
def store(tmp_agent_dir):
    _, _, data_root = tmp_agent_dir

    async def no_summary(agent_id, session_id):
        return None

    return FileBackedMemoryStore(
        data_root=data_root,
        load_session_summary=no_summary,
        load_previous_session_summary=no_summary,
    )


@pytest.mark.asyncio
async def test_snapshot_returns_memory_context(tmp_agent_dir, store):
    agent_id, agent_dir, _ = tmp_agent_dir
    import json

    facts = [{"content": "User likes Python"}, {"content": "Prefers concise answers"}]
    (agent_dir / "memory" / "memory.json").write_text(json.dumps(facts))

    snapshot = await store.build_session_snapshot(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
        session_id="test-session",
    )
    assert "User likes Python" in snapshot
    assert "Prefers concise answers" in snapshot


@pytest.mark.asyncio
async def test_snapshot_frozen_after_write(tmp_agent_dir, store):
    """Mid-session writes should not affect the already-built snapshot."""
    agent_id, agent_dir, _ = tmp_agent_dir
    import json

    facts = [{"content": "Original fact"}]
    memory_file = agent_dir / "memory" / "memory.json"
    memory_file.write_text(json.dumps(facts))

    # Build snapshot (simulates session start)
    snapshot = await store.build_session_snapshot(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
    )
    assert "Original fact" in snapshot

    # Simulate mid-session write
    facts.append({"content": "New fact added mid-session"})
    memory_file.write_text(json.dumps(facts))

    # The snapshot is a string — it's already frozen, not a live reference
    assert "New fact added mid-session" not in snapshot
    assert "Original fact" in snapshot


@pytest.mark.asyncio
async def test_snapshot_stored_on_session_context(tmp_agent_dir, store):
    agent_id, agent_dir, _ = tmp_agent_dir
    import json

    (agent_dir / "memory" / "memory.json").write_text(
        json.dumps([{"content": "Test fact"}])
    )

    session = SessionContext(session_id="s-1")
    assert session.prompt_prefix is None

    snapshot = await store.build_session_snapshot(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
        session_id="s-1",
    )
    session.prompt_prefix = f"AGENT_CONTEXT\n\n{snapshot}"

    assert session.prompt_prefix is not None
    assert "Test fact" in session.prompt_prefix


@pytest.mark.asyncio
async def test_empty_memory_returns_empty_snapshot(tmp_agent_dir, store):
    agent_id, _, _ = tmp_agent_dir

    snapshot = await store.build_session_snapshot(
        agent_id=agent_id,
        tenant_id=uuid.uuid4(),
    )
    assert snapshot == ""
