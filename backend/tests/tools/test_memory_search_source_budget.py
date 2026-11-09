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


async def test_search_memory_scores_all_entries_before_bounding_output(tmp_path, monkeypatch):
    from app.services import workspace_resource_authority as authority
    from app.tools.handlers.memory import search_memory

    agent = uuid4()
    entries = _manifest(tmp_path, agent, ["first", "second", "beyond-budget"])
    for name in ("first", "second", "beyond-budget"):
        (entries / (name + ".md")).write_text("<normalized_memory>" + "memory match " * 50_000 + "</normalized_memory>")
    original = authority.open_workspace_file
    opened = []

    @contextmanager
    def observe_reads(root, path, *args, **kwargs):
        opened.append(path)
        with original(root, path, *args, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(authority, "open_workspace_file", observe_reads)
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    result = await search_memory(agent, {"query": "match", "scope": "facts"})
    assert any(path.endswith("beyond-budget.md") for path in opened)
    assert len(result.encode()) <= 1024 * 1024
    assert "结果过大，只保留了前面部分。" in result


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
    assert len(result.encode()) <= 1024 * 1024
    assert "结果过大，只保留了前面部分。" in result
    row = load_explicit_overlay_entries(tmp_path, agent, output_byte_limit=1024 * 1024)[0]
    assert row.source_sha256 == digest.hexdigest()


async def test_entry_cut_is_reported_after_ranking_and_limit_without_hiding_other_sources(tmp_path, monkeypatch):
    from app.tools.handlers import memory

    agent = uuid4()
    entries = _manifest(tmp_path, agent, ["huge", "newest"])
    (entries / "huge.md").write_text("<normalized_memory>" + "x" * 1_100_000 + "</normalized_memory>")
    (entries / "newest.md").write_text("<normalized_memory>生日是五月二十日</normalized_memory>")
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr("app.memory.plane_read.search_plane_facts", lambda *a, **kw: [{"id": "saved", "content": "SAVED_FACT"}])

    async def backend(*args, **kwargs):
        return [{"id": "backend", "content": "BACKEND_FACT"}]

    monkeypatch.setattr(memory, "_search_semantic_backend_facts", backend)
    result = await memory.search_memory(agent, {"query": "生日", "scope": "facts", "limit": 1})
    assert "生日是五月二十日" in result
    assert "BACKEND_FACT" in result
    assert result.endswith("结果过大，只保留了前面部分。")
