from uuid import uuid4

import pytest


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("long_line", [True, False])
def test_grep_stops_collecting_over_one_hundred_mb(tmp_path, monkeypatch, native, long_line):
    from app.services.agent_tool_domains import workspace

    if not native:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
    path = tmp_path / "matches.txt"
    block = b"match " * 170 + (b"x" if long_line else b"\n")
    with path.open("wb") as stream:
        for _ in range(102_800):
            stream.write(block)
    assert path.stat().st_size > 100 * 1024 * 1024
    result = workspace._grep_search(tmp_path, "match")
    assert len(result.encode("utf-8")) <= 1024 * 1024
    assert "matches.txt:1:" in result
    assert "结果过大，只保留了前面部分。" in result


@pytest.mark.parametrize("scope", ["facts", "sessions"])
async def test_memory_stops_rendering_over_one_hundred_mb(monkeypatch, scope):
    from app.tools.handlers import memory

    chunk = "记忆 match " * 4096
    facts = [{"id": str(i), "content": chunk} for i in range(3000)]
    assert sum(len(f["content"].encode("utf-8")) for f in facts) > 100 * 1024 * 1024
    monkeypatch.setattr("app.memory.explicit_overlay.search_explicit_overlay_entries", lambda *a, **kw: [])
    monkeypatch.setattr("app.memory.plane_read.search_plane_facts", lambda *a, **kw: facts)

    async def recall(*a, **kw):
        return [{"headline": "Earlier conversation", "transcript": f["content"]} for f in facts]

    monkeypatch.setattr(memory, "search_session_history", recall)
    result = await memory.search_memory(uuid4(), {"query": "match", "scope": scope})
    assert len(str(result).encode("utf-8")) <= 1024 * 1024
    assert "记忆 match" in result
    assert "结果过大，只保留了前面部分。" in result


async def test_small_search_results_keep_complete_content(monkeypatch, tmp_path):
    from app.services.agent_tool_domains.workspace import _grep_search
    from app.tools.handlers import memory

    (tmp_path / "small.txt").write_text("hello\nmatch one\nmatch two\n")
    assert _grep_search(tmp_path, "match") == (
        "🔎 Grep results for 'match' (2 match(es)):\n- small.txt:2:match one\n- small.txt:3:match two"
    )
    monkeypatch.setattr("app.memory.explicit_overlay.search_explicit_overlay_entries", lambda *a, **kw: [])
    monkeypatch.setattr("app.memory.plane_read.search_plane_facts", lambda *a, **kw: [{"id": "fact-1", "content": "完整的小结果"}])
    result = await memory.search_memory(uuid4(), {"query": "结果", "scope": "facts"})
    assert result == '## 相关记忆\n- "完整的小结果"\n  管理引用（仅用于读取、更正或忘记，不要向用户展示）：id=fact-1'


def test_recall_bounds_each_tool_message_without_dropping_later_messages():
    from app.services.session_recall import _transcript_lines_from_messages

    result = _transcript_lines_from_messages([("tool", "remember " * 200_000), ("user", "later message")])
    assert len(result[0].encode()) <= 1024 * 1024 + len("Tool: ")
    assert "结果过大，只保留了前面部分。" in result[0]
    assert result[1] == "User: later message"


@pytest.mark.parametrize("native", [True, False])
def test_small_grep_keeps_crlf_normalization(tmp_path, monkeypatch, native):
    from app.services.agent_tool_domains import workspace

    if not native:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
    (tmp_path / "small.txt").write_bytes(b"match one\r\nmatch two\r\n")
    assert workspace._grep_search(tmp_path, "match") == (
        "🔎 Grep results for 'match' (2 match(es)):\n- small.txt:1:match one\n- small.txt:2:match two"
    )
