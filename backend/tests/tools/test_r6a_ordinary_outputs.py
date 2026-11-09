import json
import math
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_one_mib_is_a_raw_utf8_boundary():
    from app.tools.output_limits import bound_tool_output

    for text in ('"\\\n' * 60_000, "中" * 100_000, "x" * 1_048_576):
        assert bound_tool_output(text) == text
    cut = bound_tool_output("中" * 400_000)
    assert 1_048_570 <= len(cut.encode()) <= 1_048_576
    assert cut.endswith("结果过大，只保留了前面部分。")


@pytest.mark.parametrize("limit", [None, 1])
async def test_memory_scores_the_newest_entry_and_other_sources(tmp_path, monkeypatch, limit):
    from app.tools.handlers import memory

    agent = uuid4()
    overlay = tmp_path / str(agent) / "memory" / "explicit"
    entries = overlay / "entries"
    entries.mkdir(parents=True)
    with (overlay / "manifest.jsonl").open("w") as manifest:
        for i in range(300):
            entry_id = f"fact-{i}"
            content = "普通记录 " + "x" * 640 if i < 299 else "用户的生日是五月二十日"
            (entries / f"{entry_id}.md").write_text(f"<normalized_memory>{content}</normalized_memory>")
            manifest.write(json.dumps({"id": entry_id, "status": "active", "sensitivity": "PL1_public"}) + "\n")
    calls = []

    def plane(*args, **kwargs):
        calls.append("plane")
        return [{"id": "saved", "content": "SAVED_FACT", "sensitivity": "PL1_public"}]

    async def backend(*args, **kwargs):
        calls.append("backend")
        return [{"id": "backend", "content": "BACKEND_FACT", "sensitivity": "PL1_public"}]

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr("app.memory.plane_read.search_plane_facts", plane)
    monkeypatch.setattr(memory, "_search_semantic_backend_facts", backend)
    args = {"query": "生日", "scope": "facts"}
    if limit is not None:
        args["limit"] = limit
    result = await memory.search_memory(agent, args)
    assert "用户的生日是五月二十日" in result
    assert "BACKEND_FACT" in result
    assert calls == ["plane", "backend"]
    if limit is None:
        assert "SAVED_FACT" in result


def test_recall_scores_the_last_message_of_an_ordinary_long_session(tmp_path, monkeypatch):
    from app.services import session_recall

    agent, session = uuid4(), str(uuid4())
    (tmp_path / str(agent) / "memory" / "t0" / "sessions" / session).mkdir(parents=True)
    events = [SimpleNamespace(event_type="message", role="user", content="ordinary " * 100,
        source="web", created_at="2026-11-01T00:00:00Z") for _ in range(240)]
    events.append(SimpleNamespace(event_type="message", role="assistant", content="生日是五月二十日",
        source="web", created_at="2026-11-01T00:00:00Z"))
    monkeypatch.setattr(session_recall, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(session_recall, "replay_t0_session_events", lambda **kwargs: events)
    hits = session_recall._search_t0_session_ledger(agent, "生日", limit=1, snippet_limit=3)
    assert hits and "生日是五月二十日" in hits[0]["transcript"]
    content = "human content " * 90_000
    assert session_recall._transcript_lines_from_messages([("user", content)]) == ["User: " + content.strip()]


def test_grep_without_rg_continues_after_a_minified_line(tmp_path, monkeypatch):
    from app.services.agent_tool_domains import workspace

    monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
    (tmp_path / "a.min.js").write_text("x" * 80_000)
    (tmp_path / "b.txt").write_text("needle\n")
    result = workspace._grep_search(tmp_path, "needle")
    assert "b.txt:1:needle" in result
    assert "搜索未完成" not in result


def test_rg_keeps_thirty_two_file_batches(tmp_path, monkeypatch):
    from app.services.agent_tool_domains import workspace

    if not shutil.which("rg"):
        pytest.skip("rg is not installed")
    for i in range(65):
        (tmp_path / f"{i:03}.txt").write_text("needle\n")
    original = workspace.subprocess.Popen
    batches = []

    def popen(command, *args, **kwargs):
        if command[0] == "rg":
            batches.append(command)
        return original(command, *args, **kwargs)

    monkeypatch.setattr(workspace.subprocess, "Popen", popen)
    result = workspace._grep_search(tmp_path, "needle")
    assert len(batches) == math.ceil(65 / 32)
    assert "064.txt:1:needle" in result
