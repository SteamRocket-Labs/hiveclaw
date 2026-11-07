"""A4c: a partial source recheck keeps the valid items and names the withheld count.

Before A4c, one invalid recalled item silently dropped items (or the whole
batch) with only a log line; the model never learned what it did not get.
"""

from types import SimpleNamespace
from uuid import uuid4, uuid5

import pytest

from app.memory.retriever import MemoryRetriever
from app.memory.types import MemoryItem, MemoryKind


def _explicit_item(agent_id: uuid4, entry_id: str, content: str, sha: str) -> MemoryItem:
    return MemoryItem(
        kind=MemoryKind.SEMANTIC,
        content=f"[explicit_overlay][reference] {content}",
        score=0.5,
        source=f"explicit_overlay:{entry_id}",
        metadata={"source_type": "explicit_overlay", "entry_id": entry_id},
        source_refs=({"source_kind": "explicit_memory", "source_id": str(uuid5(agent_id, entry_id)),
                      "source_version": sha},),
    )


@pytest.mark.asyncio
async def test_invalid_explicit_item_is_dropped_and_counted_not_silent(tmp_path, monkeypatch):
    agent_id = uuid4()
    valid = _explicit_item(agent_id, "entry-valid", "valid fact", "sha-valid")
    invalid = _explicit_item(agent_id, "entry-invalid", "invalid fact", "sha-invalid")

    async def authorized_entries(*, entries, **_kwargs):
        # The authority verifies only the valid entry, as a stale receipt would.
        kept = [entry for entry in entries if entry.entry_id == "entry-valid"]
        return [(entry, SimpleNamespace(id=uuid5(agent_id, entry.entry_id),
            content_sha256=entry.source_sha256)) for entry in kept]

    monkeypatch.setattr("app.services.explicit_memory_authority.authorized_explicit_entries", authorized_entries)
    from app.memory.explicit_overlay import ExplicitMemoryOverlayEntry

    monkeypatch.setattr(
        "app.memory.explicit_overlay.load_explicit_overlay_entries_by_ids",
        lambda _root, _agent, ids: [
            ExplicitMemoryOverlayEntry(
                entry_id="entry-valid", status="active", category="reference", target_hint="capabilities",
                content="valid fact", source_refs=(), sensitivity="PL1_public",
                path=tmp_path / "entry-valid.md", created_at="2026-11-06T00:00:00Z",
                metadata={}, source_sha256="sha-valid",
            )
        ],
    )

    retriever = MemoryRetriever(data_root=tmp_path)
    retriever.last_withheld = {}

    class _Activation:
        principal_stack = SimpleNamespace(current_user=SimpleNamespace(id=uuid4()))

    kept_items = await retriever._filter_explicit_sources(
        [valid, invalid], agent_id=agent_id, tenant_id=uuid4(), session_id=str(uuid4()),
        activation_context=_Activation,
    )

    assert [item.metadata["entry_id"] for item in kept_items] == ["entry-valid"]
    assert retriever.last_withheld["explicit_memory"] == {
        "count": 1, "code": "explicit_memory_source_partial",
    }


@pytest.mark.asyncio
async def test_team_authority_outage_is_reported_not_silent(tmp_path, monkeypatch):
    retriever = MemoryRetriever(data_root=tmp_path)
    retriever.last_withheld = {}

    async def broken_read_entries(*_args, **_kwargs):
        raise RuntimeError("authority unavailable")

    monkeypatch.setattr("app.services.team_memory_runtime.read_entries", broken_read_entries)

    class _Activation:
        principal_stack = SimpleNamespace(current_user=SimpleNamespace(id=uuid4()))

    items = await retriever._team_candidates(
        agent_id=uuid4(), tenant_id=uuid4(), session_id=str(uuid4()),
        activation_context=_Activation, runtime_task_id=None,
    )

    assert items == []
    assert retriever.last_withheld["team_memory"]["count"] is None
    assert retriever.last_withheld["team_memory"]["code"] == "team_memory_source_unavailable"


def test_memory_context_names_withheld_count_in_note():
    from app.services.memory_service import _render_withheld_note

    note = _render_withheld_note({"explicit_memory": {"count": 1, "code": "explicit_memory_source_partial"}})
    assert "1" in note and "记忆暂时无法核实" in note
    assert "explicit_memory" not in note and "source_partial" not in note
    assert _render_withheld_note({}) == ""
    assert _render_withheld_note(None) == ""
