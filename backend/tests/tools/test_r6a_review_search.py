import json
import asyncio
import shutil
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest


async def test_grep_batch_io_does_not_starve_the_registered_event_loop(tmp_path, monkeypatch):
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers.filesystem import grep_search
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    release = threading.Event()

    def scan(*args, **kwargs):
        release.wait(.5)
        return "complete ordinary output"

    monkeypatch.setattr("app.services.agent_tool_domains.workspace._grep_search", scan)
    request = ToolExecutionRequest(tool_name="grep_search", arguments={"pattern": "x"},
        context=ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=None, workspace=tmp_path))
    started = time.monotonic()
    task = asyncio.create_task(adapt_and_call(SimpleNamespace(adapter="workspace_args"), grep_search, request))
    try:
        await asyncio.sleep(.02)
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert await task == "complete ordinary output"
    assert elapsed < .25


@pytest.mark.parametrize("many_files", [False, True])
def test_rg_preserves_sorted_complete_output_below_one_mib(tmp_path, many_files):
    from app.services.agent_tool_domains.workspace import _grep_search

    if not shutil.which("rg"):
        pytest.skip("rg is not installed")
    count = 5000 if many_files else 80_000
    line = "x" * 100 if many_files else "x"
    names = [f"f{i:02}.txt" for i in range(32)] if many_files else ["a"]
    for name in names:
        (tmp_path / name).write_text((line + "\n") * count)
    expected = f"🔎 Grep results for 'x' ({count} match(es)):\n" + "\n".join(
        f"- {names[0]}:{i}:{line}" for i in range(1, count + 1))
    assert len(expected.encode()) < 1024 * 1024
    actual = _grep_search(tmp_path, "x", max_results=count if many_files else None)
    if actual != expected:
        pytest.fail(f"Grep output differs: expected {len(expected.encode())} bytes, got {len(actual.encode())} bytes", pytrace=False)


@pytest.mark.parametrize("raw", [b"x\rrest\n", b"x\x0crest\n", "x\x85rest\n".encode(), "x\u2028rest\n".encode(), b"x\xe9rest\n"])
def test_rg_keeps_whole_lines_and_replaces_invalid_utf8(tmp_path, raw):
    from app.services.agent_tool_domains.workspace import _grep_search

    if not shutil.which("rg"):
        pytest.skip("rg is not installed")
    (tmp_path / "a").write_bytes(raw)
    assert _grep_search(tmp_path, "x") == "🔎 Grep results for 'x' (1 match(es)):\n- a:1:" + raw.decode("utf-8", errors="replace").rstrip("\r\n")


async def test_long_recall_keeps_the_matching_tail_and_renders_window_first(tmp_path, monkeypatch):
    from app.services import session_recall
    from app.tools.handlers import memory

    agent, session = uuid4(), str(uuid4())
    (tmp_path / str(agent) / "memory/t0/sessions" / session).mkdir(parents=True)
    events = [SimpleNamespace(event_type="tool_result", role="tool", content="ordinary " * 6100,
        source="web", created_at="2026-11-01T00:00:00Z") for _ in range(25)]
    events.append(SimpleNamespace(event_type="message", role="user", content="生日是五月二十日",
        source="web", created_at="2026-11-01T00:00:00Z"))
    monkeypatch.setattr(session_recall, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(session_recall, "replay_t0_session_events", lambda **kwargs: events)
    hits = session_recall._search_t0_session_ledger(agent, "生日", limit=1, snippet_limit=3)
    assert "生日是五月二十日" in hits[0]["transcript"]
    assert "生日是五月二十日" in hits[0]["transcript_window"]

    async def recall(*args, **kwargs):
        return hits

    monkeypatch.setattr(memory, "search_session_history", recall)
    result = await memory.search_memory(agent, {"query": "生日", "scope": "sessions"})
    assert "生日是五月二十日" in result
    assert len(result.encode()) <= 1024 * 1024


async def test_non_active_entries_still_reach_the_existing_authority_reader(tmp_path, monkeypatch):
    from app.tools.handlers import memory

    agent = uuid4()
    root = tmp_path / str(agent) / "memory/explicit"
    (root / "entries").mkdir(parents=True)
    (root / "manifest.jsonl").write_text(json.dumps({"id": "held", "status": "held"}) + "\n")
    (root / "entries/held.md").write_text("<normalized_memory>held fact</normalized_memory>")
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    seen = []

    async def authority(**kwargs):
        seen.extend(entry.entry_id for entry in kwargs["entries"])
        kwargs["issue_observer"].append({"status": "denied", "code": "inactive_entry"})
        return []

    monkeypatch.setattr("app.services.explicit_memory_authority.authorized_explicit_entries", authority)
    issues = []
    await memory._scoped_explicit_entries(agent, SimpleNamespace(tenant_id=str(uuid4())),
        issue_observer=issues, output_byte_limit=1024 * 1024)
    assert seen == ["held"]
    assert issues == [{"status": "denied", "code": "inactive_entry"}]
