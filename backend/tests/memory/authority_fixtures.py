"""Reference source authority for isolated content/selection unit fixtures."""

from types import SimpleNamespace
from uuid import uuid5


def reference_explicit_memory_authority(monkeypatch):
    # These unit fixtures deliberately have no tenant database or session.
    # Native PostgreSQL integration tests exercise the actual source boundary.
    async def authorized_entries(*, agent_id, entries, **_kwargs):
        return [(entry, SimpleNamespace(id=uuid5(agent_id, entry.entry_id),
            content_sha256=entry.source_sha256)) for entry in entries]

    async def authorized_candidates(_self, items, **_kwargs):
        return items

    monkeypatch.setattr("app.services.explicit_memory_authority.authorized_explicit_entries", authorized_entries)
    monkeypatch.setattr("app.memory.retriever.MemoryRetriever._filter_explicit_sources", authorized_candidates)
