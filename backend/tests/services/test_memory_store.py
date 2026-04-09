from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_memory_store_builds_compatible_context(tmp_path):
    from app.memory.store import FileBackedMemoryStore

    agent_id = uuid4()
    session_id = "session-1"
    agent_root = tmp_path / str(agent_id)
    (agent_root / "memory").mkdir(parents=True)
    (agent_root / "memory" / "memory.json").write_text(
        '[{"subject":"user","content":"Prefers weekly summaries"}]',
        encoding="utf-8",
    )

    async def load_current(_agent_id, _session_id):
        assert _agent_id == agent_id
        assert _session_id == session_id
        return "Current session summary"

    async def load_previous(_agent_id, _session_id):
        raise AssertionError("previous summary should not be used when current summary exists")

    store = FileBackedMemoryStore(
        data_root=tmp_path,
        load_session_summary=load_current,
        load_previous_session_summary=load_previous,
    )

    context = await store.build_context(agent_id=agent_id, tenant_id=uuid4(), session_id=session_id)

    assert "[Previous conversation summary]" in context
    assert "Current session summary" in context
    assert "[Agent memory]" in context
    assert "Prefers weekly summaries" in context


def test_persistent_memory_store_round_trips_semantic_facts(tmp_path):
    from app.memory.store import PersistentMemoryStore

    agent_id = uuid4()
    store = PersistentMemoryStore(data_root=tmp_path)

    store.replace_semantic_facts(
        agent_id,
        [
            {
                "content": "Alice prefers coffee",
                "subject": "preference:drink",
                "timestamp": "2026-03-21T10:00:00+00:00",
            },
            {
                "content": "Works on Project X",
                "timestamp": "2026-03-21T10:05:00+00:00",
            },
        ],
    )

    facts = store.load_semantic_facts(agent_id)

    assert [fact["content"] for fact in facts] == [
        "Alice prefers coffee",
        "Works on Project X",
    ]
    assert facts[0]["subject"] == "preference:drink"
    assert (tmp_path / str(agent_id) / "memory" / "memory.sqlite3").exists()


def test_persistent_memory_store_imports_legacy_memory_json(tmp_path):
    from app.memory.store import PersistentMemoryStore

    agent_id = uuid4()
    memory_dir = tmp_path / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "memory.json").write_text(
        '[{"content":"User prefers weekly reviews","timestamp":"2026-03-20T08:00:00+00:00"}]',
        encoding="utf-8",
    )

    store = PersistentMemoryStore(data_root=tmp_path)

    facts = store.load_semantic_facts(agent_id)

    assert [fact["content"] for fact in facts] == ["User prefers weekly reviews"]
    assert (memory_dir / "memory.sqlite3").exists()
