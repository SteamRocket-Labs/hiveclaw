from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        if rows and isinstance(rows[0], tuple):
            self._rows = [rows]
        else:
            self._rows = list(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        if self._rows:
            return _FakeResult(self._rows.pop(0))
        return _FakeResult([])


@pytest.mark.asyncio
async def test_search_session_history_groups_matches_into_recall_hits(monkeypatch) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    session_one = uuid.uuid4()
    session_two = uuid.uuid4()
    rows = [
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc),
            "讨论 memory-system-redesign",
            "用户强调 t0 md 是整个体系的基石",
            datetime(2026, 4, 9, 8, 1, tzinfo=timezone.utc),
        ),
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 8, 0, tzinfo=timezone.utc),
            "讨论 memory-system-redesign",
            "我们还确认了三个蒸馏器不能职责混合",
            datetime(2026, 4, 9, 8, 2, tzinfo=timezone.utc),
        ),
        (
            session_two,
            "feishu",
            datetime(2026, 4, 8, 14, 0, tzinfo=timezone.utc),
            "",
            "用户要求彻底收口 legacy memory path",
            datetime(2026, 4, 8, 14, 5, tzinfo=timezone.utc),
        ),
    ]

    monkeypatch.setattr("app.services.session_recall.async_session", lambda: _FakeSession(rows))

    hits = await search_session_history(agent_id, "memory", limit=5, snippet_limit=2)

    assert len(hits) == 2
    assert hits[0]["session_id"] == str(session_one)
    assert hits[0]["source"] == "web"
    assert hits[0]["started_at"] == "2026-04-09"
    assert hits[0]["headline"] == "讨论 memory-system-redesign"
    assert hits[0]["snippets"] == [
        "用户强调 t0 md 是整个体系的基石",
        "我们还确认了三个蒸馏器不能职责混合",
    ]
    assert hits[1]["session_id"] == str(session_two)
    assert hits[1]["headline"] == "回顾到与查询相关的历史会话"
    assert hits[1]["snippets"] == ["用户要求彻底收口 legacy memory path"]


@pytest.mark.asyncio
async def test_search_session_history_returns_empty_when_no_match(monkeypatch) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    monkeypatch.setattr("app.services.session_recall.async_session", lambda: _FakeSession([]))

    hits = await search_session_history(agent_id, "memory", limit=5)

    assert hits == []


@pytest.mark.asyncio
async def test_search_session_history_prefers_t0_chat_logs(monkeypatch, tmp_path: Path) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    logs_dir = tmp_path / str(agent_id) / "logs" / "2026-04-09"
    logs_dir.mkdir(parents=True)
    (logs_dir / "chat-0900-abcd.md").write_text(
        """---
type: chat
session_id: sess-1
source: web
user: Rocky
started: 2026-04-09T09:00:00+00:00
turns: 2
tools: []
---

## Turn 1
**User**: 我希望记忆系统完全以 md 为核心，不要继续依赖 sqlite 真源。
**Agent**: 我会把长期事实读取切换到 T3 md，并保留 sqlite 只做影子索引。

## Turn 2
**User**: session recall 也应该优先从 t0 chat md 回忆。
**Agent**: 我会优先搜索 t0 chat md，并仅在缺失时退回数据库。
""",
        encoding="utf-8",
    )

    def _unexpected_session():
        raise AssertionError("T0 chat logs should satisfy recall without touching DB")

    monkeypatch.setattr("app.services.session_recall.async_session", _unexpected_session)
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "t0 chat md", limit=5, snippet_limit=2)

    assert len(hits) == 1
    assert hits[0]["session_id"] == "sess-1"
    assert hits[0]["source"] == "web"
    assert "t0 chat md" in hits[0]["headline"].lower()
    assert "优先搜索 t0 chat md" in hits[0]["summary"]
    assert any("session recall 也应该优先从 t0 chat md 回忆" in snippet for snippet in hits[0]["snippets"])


@pytest.mark.asyncio
async def test_search_session_history_reads_agent_message_t0_logs(monkeypatch, tmp_path: Path) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    logs_dir = tmp_path / str(agent_id) / "logs" / "2026-04-09"
    logs_dir.mkdir(parents=True)
    (logs_dir / "agent_message-0930-abcd.md").write_text(
        """---
type: agent_message
session_id: agent-msg-1
source: agent
from: source-agent-id
to: Target Agent
started: 2026-04-09T09:30:00+00:00
trace_id: trace-123
---

## Request
请回忆我们上次 agent 之间关于 release checklist 的协作结论。

## Execution
**user**: 请回忆我们上次 agent 之间关于 release checklist 的协作结论。

**assistant**: 我们把发布流程定成 preflight、deploy、post-verify 三段，并写入 docs/release-checklist.md。

## Result
已确认协作结论，并同步到文档草案。
""",
        encoding="utf-8",
    )

    def _unexpected_session():
        raise AssertionError("T0 agent_message logs should satisfy recall without touching DB")

    monkeypatch.setattr("app.services.session_recall.async_session", _unexpected_session)
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "release checklist", limit=5, snippet_limit=2)

    assert len(hits) == 1
    assert hits[0]["session_id"] == "agent-msg-1"
    assert hits[0]["source"] == "agent_message"
    assert "release checklist" in hits[0]["headline"].lower()
    assert "preflight、deploy、post-verify" in hits[0]["summary"]


@pytest.mark.asyncio
async def test_search_session_history_t0_summary_keeps_adjacent_resolution(monkeypatch, tmp_path: Path) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    logs_dir = tmp_path / str(agent_id) / "logs" / "2026-04-09"
    logs_dir.mkdir(parents=True)
    (logs_dir / "chat-1030-efgh.md").write_text(
        """---
type: chat
session_id: sess-2
source: web
user: Rocky
started: 2026-04-09T10:30:00+00:00
turns: 1
tools: []
---

## Turn 1
**User**: 请回忆我们上次关于 priority matrix 的讨论。
**Agent**: 我会整理成一页决策摘要，并写入 workspace/decision-brief.md。
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "priority matrix", limit=3, snippet_limit=2)

    assert len(hits) == 1
    assert "priority matrix" in hits[0]["summary"].lower()
    assert "决策摘要" in hits[0]["summary"]
    assert "User: 请回忆我们上次关于 priority matrix 的讨论。" in hits[0]["transcript_window"]
    assert "Assistant: 我会整理成一页决策摘要，并写入 workspace/decision-brief.md。" in hits[0]["transcript_window"]


@pytest.mark.asyncio
async def test_search_session_history_db_summary_keeps_adjacent_resolution(monkeypatch) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    session_one = uuid.uuid4()
    rows = [
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "priority matrix 决策讨论",
            "user",
            "请回忆我们上次关于 priority matrix 的讨论。",
            datetime(2026, 4, 9, 11, 1, tzinfo=timezone.utc),
        ),
    ]
    transcript_rows = [
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "priority matrix 决策讨论",
            "user",
            "请回忆我们上次关于 priority matrix 的讨论。",
            datetime(2026, 4, 9, 11, 1, tzinfo=timezone.utc),
        ),
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "priority matrix 决策讨论",
            "assistant",
            "我会整理成一页决策摘要，并写入 workspace/decision-brief.md。",
            datetime(2026, 4, 9, 11, 2, tzinfo=timezone.utc),
        ),
    ]

    monkeypatch.setattr("app.services.session_recall.async_session", lambda: _FakeSession([rows, transcript_rows]))
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR="/tmp/nonexistent-session-recall"),
    )

    hits = await search_session_history(agent_id, "priority matrix", limit=3, snippet_limit=2)

    assert len(hits) == 1
    assert "priority matrix" in hits[0]["summary"].lower()
    assert "决策摘要" in hits[0]["summary"]
    assert any("决策摘要" in snippet for snippet in hits[0].get("context_snippets", []))
    assert "User: 请回忆我们上次关于 priority matrix 的讨论。" in hits[0]["transcript_window"]
    assert "Assistant: 我会整理成一页决策摘要，并写入 workspace/decision-brief.md。" in hits[0]["transcript_window"]


@pytest.mark.asyncio
async def test_search_session_history_db_fallback_includes_peer_agent_sessions(monkeypatch, tmp_path: Path) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    search_rows = [
        (
            session_id,
            "agent",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "协作 release checklist",
            "assistant",
            "我们确认发布流程分成 preflight、deploy、post-verify 三段。",
            datetime(2026, 4, 9, 11, 1, tzinfo=timezone.utc),
        ),
    ]
    transcript_rows = [
        (
            str(session_id),
            "assistant",
            "我们确认发布流程分成 preflight、deploy、post-verify 三段。",
            datetime(2026, 4, 9, 11, 1, tzinfo=timezone.utc),
        ),
    ]

    class _PeerAwareSession:
        def __init__(self):
            self._call_index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, stmt):
            self._call_index += 1
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            if self._call_index == 1:
                assert "chat_sessions.peer_agent_id" in sql
                assert "chat_messages.agent_id" not in sql
                excluded_channels = sql.split("chat_sessions.source_channel NOT IN (", 1)[1].split(")", 1)[0]
                assert "'agent'" not in excluded_channels
                return _FakeResult(search_rows)
            if self._call_index == 2:
                assert "chat_messages.agent_id" not in sql
                return _FakeResult(transcript_rows)
            raise AssertionError(f"Unexpected execute call #{self._call_index}: {sql}")

    monkeypatch.setattr("app.services.session_recall.async_session", lambda: _PeerAwareSession())
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "release checklist", limit=5, snippet_limit=2)

    assert len(hits) == 1
    assert hits[0]["session_id"] == str(session_id)
    assert hits[0]["source"] == "agent_message"
    assert "release checklist" in hits[0]["headline"].lower()


@pytest.mark.asyncio
async def test_search_session_history_builds_transcript_focused_recap(monkeypatch, tmp_path: Path) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    logs_dir = tmp_path / str(agent_id) / "logs" / "2026-04-09"
    logs_dir.mkdir(parents=True)
    (logs_dir / "chat-1500-ijkl.md").write_text(
        """---
type: chat
session_id: sess-3
source: web
user: Rocky
started: 2026-04-09T15:00:00+00:00
turns: 2
tools: []
---

## Turn 1
**User**: 上次关于 release checklist 我们最后怎么定的？
**Agent**: 我们决定把发布核对表固定成三个阶段：preflight、deploy、post-verify。

## Turn 2
**User**: 需要落到哪里？
**Agent**: 我会把最终版本写到 docs/release-checklist.md，并在 PR 模板里链接它。
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "release checklist", limit=3, snippet_limit=2)

    assert len(hits) == 1
    assert hits[0]["focused_recap"].startswith("Decision:")
    assert "preflight、deploy、post-verify" in hits[0]["focused_recap"]
    assert "docs/release-checklist.md" in hits[0]["focused_recap"]
    assert hits[0]["evidence_lines"] == [
        "Assistant: 我们决定把发布核对表固定成三个阶段：preflight、deploy、post-verify。",
        "Assistant: 我会把最终版本写到 docs/release-checklist.md，并在 PR 模板里链接它。",
    ]


@pytest.mark.asyncio
async def test_search_session_history_uses_tenant_aware_summary_enrichment(monkeypatch) -> None:
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    session_one = uuid.uuid4()
    rows = [
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "priority matrix 决策讨论",
            "user",
            "请回忆我们上次关于 priority matrix 的讨论。",
            datetime(2026, 4, 9, 11, 2, tzinfo=timezone.utc),
        ),
    ]
    transcript_rows = [
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "priority matrix 决策讨论",
            "user",
            "请回忆我们上次关于 priority matrix 的讨论。",
            datetime(2026, 4, 9, 11, 1, tzinfo=timezone.utc),
        ),
        (
            session_one,
            "web",
            datetime(2026, 4, 9, 11, 0, tzinfo=timezone.utc),
            "priority matrix 决策讨论",
            "assistant",
            "我会整理成一页决策摘要，并写入 workspace/decision-brief.md。",
            datetime(2026, 4, 9, 11, 2, tzinfo=timezone.utc),
        ),
    ]

    async def _fake_summarize(query, hits, tenant):
        assert query == "priority matrix"
        assert tenant == tenant_id
        assert hits[0]["summary"]
        hits[0]["summary"] = "模型聚焦摘要：我们把 priority matrix 整理成了决策摘要。"
        return hits

    monkeypatch.setattr("app.services.session_recall.async_session", lambda: _FakeSession([rows, transcript_rows]))
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR="/tmp/nonexistent-session-recall"),
    )
    monkeypatch.setattr("app.services.session_recall._summarize_recall_hits", _fake_summarize)

    hits = await search_session_history(
        agent_id,
        "priority matrix",
        limit=3,
        snippet_limit=2,
        tenant_id=tenant_id,
    )

    assert len(hits) == 1
    assert hits[0]["summary"] == "模型聚焦摘要：我们把 priority matrix 整理成了决策摘要。"
