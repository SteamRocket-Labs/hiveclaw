from contextlib import contextmanager
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4


def _manifest(root, agent, ids):
    overlay = root / str(agent) / "memory" / "explicit"
    (overlay / "entries").mkdir(parents=True)
    (overlay / "manifest.jsonl").write_text("".join(json.dumps({
        "id": entry, "status": "active", "sensitivity": "PL1_public", "category": "fact"
    }) + "\n" for entry in ids))
    return overlay / "entries"


async def test_search_memory_stops_opening_entries_after_output_budget(tmp_path, monkeypatch):
    from app.services import workspace_resource_authority as authority
    from app.tools.handlers.memory import search_memory

    agent = uuid4()
    entries = _manifest(tmp_path, agent, ["first", "second", "beyond-budget"])
    for name in ("first", "second", "beyond-budget"):
        (entries / (name + ".md")).write_text("<normalized_memory>" + "memory match " * 3500 + "</normalized_memory>")
    original = authority.open_workspace_file

    @contextmanager
    def stop_at_budget(root, path, *args, **kwargs):
        assert not path.endswith("beyond-budget.md"), "Search kept collecting after its output budget"
        with original(root, path, *args, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(authority, "open_workspace_file", stop_at_budget)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    result = await search_memory(agent, {"query": "match", "scope": "facts"})
    assert len(result.encode()) <= 65_536
    assert "请缩小范围后继续查找" in result


async def test_single_huge_memory_entry_streams_with_original_source_hash(tmp_path, monkeypatch):
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.services import workspace_resource_authority as authority
    from app.tools.handlers.memory import search_memory

    agent = uuid4()
    entries = _manifest(tmp_path, agent, ["huge"])
    digest = hashlib.sha256()
    with (entries / "huge.md").open("wb") as stream:
        stream.write(b"<normalized_memory>")
        digest.update(b"<normalized_memory>")
        chunk = b"memory match " * 4096
        for _ in range(2060):
            stream.write(chunk)
            digest.update(chunk)
        stream.write(b"</normalized_memory>")
        digest.update(b"</normalized_memory>")
    assert (entries / "huge.md").stat().st_size > 100 * 1024 * 1024
    original = authority.open_workspace_file

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            assert 0 < size <= 65_536, "Search attempted an unbounded memory-entry read"
            return self.stream.read(size)

    @contextmanager
    def bounded_open(root, path, *args, **kwargs):
        with original(root, path, *args, **kwargs) as stream:
            yield BoundedReader(stream) if path.endswith("huge.md") else stream

    monkeypatch.setattr(authority, "open_workspace_file", bounded_open)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    result = await search_memory(agent, {"query": "match", "scope": "facts"})
    assert len(result.encode()) <= 65_536
    assert "请缩小范围后继续查找" in result
    row = load_explicit_overlay_entries(tmp_path, agent, output_byte_limit=65_536)[0]
    assert row.source_sha256 == digest.hexdigest()
