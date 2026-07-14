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

    monkeypatch.setattr("app.services.session_recall.tenant_scoped_session", lambda *a, **k: _FakeSession(rows))

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
    monkeypatch.setattr("app.services.session_recall.tenant_scoped_session", lambda *a, **k: _FakeSession([]))

    hits = await search_session_history(agent_id, "memory", limit=5)

    assert hits == []


@pytest.mark.asyncio
async def test_search_session_history_prefers_t0_session_ledger(monkeypatch, tmp_path: Path) -> None:
    from app.memory.t0.ledger import append_t0_session_event, seal_t0_session_segment
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    append_t0_session_event(
        agent_id=agent_id,
        session_id="sess-ledger-1",
        event_type="user_message",
        role="user",
        content="我们上次确认 session ledger 是 T0 唯一真相源。",
        source="web",
        data_root=tmp_path,
        created_at=datetime(2026, 4, 9, 9, 0, tzinfo=timezone.utc),
    )
    append_t0_session_event(
        agent_id=agent_id,
        session_id="sess-ledger-1",
        event_type="assistant_message",
        role="assistant",
        content="我会用 memory/t0/sessions 下的 source.md 作为 recall 入口。",
        source="web",
        data_root=tmp_path,
        created_at=datetime(2026, 4, 9, 9, 1, tzinfo=timezone.utc),
    )
    seal_t0_session_segment(
        agent_id=agent_id,
        session_id="sess-ledger-1",
        reason="session_close",
        data_root=tmp_path,
        created_at=datetime(2026, 4, 9, 9, 2, tzinfo=timezone.utc),
    )

    def _unexpected_session(*_a, **_k):
        raise AssertionError("T0 session ledger should satisfy recall without touching DB")

    monkeypatch.setattr("app.services.session_recall.tenant_scoped_session", _unexpected_session)
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "session ledger", limit=5, snippet_limit=2)

    assert len(hits) == 1
    assert hits[0]["session_id"] == "sess-ledger-1"
    assert hits[0]["source"] == "web"
    assert hits[0]["started_at"] == "2026-04-09"
    assert "session ledger 是 T0 唯一真相源" in hits[0]["summary"]
    assert any("memory/t0/sessions" in snippet for snippet in hits[0]["snippets"])
    assert not (tmp_path / str(agent_id) / "logs").exists()


@pytest.mark.asyncio
async def test_search_session_history_has_no_hidden_default_result_cap(monkeypatch, tmp_path: Path) -> None:
    from app.memory.t0.ledger import append_t0_session_event
    from app.services.session_recall import search_session_history

    agent_id = uuid.uuid4()
    for index in range(4):
        append_t0_session_event(
            agent_id=agent_id,
            session_id=f"sess-full-{index}",
            event_type="user_message",
            role="user",
            content=f"shared recall marker with distinct evidence {index}",
            source="web",
            data_root=tmp_path,
            created_at=datetime(2026, 4, 9, 9, index, tzinfo=timezone.utc),
        )

    def _unexpected_session(*_a, **_k):
        raise AssertionError("T0 session ledger should satisfy recall without touching DB")

    monkeypatch.setattr("app.services.session_recall.tenant_scoped_session", _unexpected_session)
    monkeypatch.setattr(
        "app.services.session_recall.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    hits = await search_session_history(agent_id, "shared recall marker")

    assert len(hits) == 4
    assert {hit["session_id"] for hit in hits} == {f"sess-full-{index}" for index in range(4)}


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

    def _unexpected_session(*_a, **_k):
        raise AssertionError("legacy per-file chat logs should satisfy recall without touching DB")

    monkeypatch.setattr("app.services.session_recall.tenant_scoped_session", _unexpected_session)
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

    monkeypatch.setattr(
        "app.services.session_recall.tenant_scoped_session", lambda *a, **k: _FakeSession([rows, transcript_rows])
    )
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
    assert hits[0]["focused_recap"].startswith("Evidence passthrough:")
    assert "preflight、deploy、post-verify" in hits[0]["focused_recap"]
    assert "docs/release-checklist.md" in hits[0]["focused_recap"]
    assert hits[0]["evidence_lines"] == [
        "User: 上次关于 release checklist 我们最后怎么定的？",
        "Assistant: 我们决定把发布核对表固定成三个阶段：preflight、deploy、post-verify。",
        "User: 需要落到哪里？",
        "Assistant: 我会把最终版本写到 docs/release-checklist.md，并在 PR 模板里链接它。",
    ]
    assert hits[0]["summary_method"] == "evidence_passthrough"
    assert hits[0]["summary_model_status"] == "no_tenant"
    assert "User: 需要落到哪里？" in hits[0]["transcript"]
    assert "Assistant: 我会把最终版本写到 docs/release-checklist.md，并在 PR 模板里链接它。" in hits[0]["transcript"]


@pytest.mark.asyncio
async def test_summary_model_receives_complete_recalled_transcript(monkeypatch) -> None:
    from app.services import session_recall

    captured: dict[str, object] = {}
    decisive_tail = "SESSION_RECALL_DECISIVE_TAIL"
    transcript = "User: question\nAssistant: " + ("x" * 4_000) + decisive_tail
    hits = [
        {
            "headline": "Past decision",
            "summary": "fallback",
            "focused_recap": "fallback recap",
            "snippets": ["question"],
            "context_snippets": ["question"],
            "transcript_window": "User: question",
            "evidence_lines": ["User: question"],
            "transcript": transcript,
        }
    ]

    async def _get_summary_model_config(_tenant_id):
        return {
            "provider": "openai",
            "model": "test",
            "api_key": "key",
            "max_output_tokens": 32_768,
        }

    class _Client:
        async def stream(self, *, messages, max_tokens, temperature):
            captured["prompt"] = messages[0].content
            captured["max_tokens"] = max_tokens
            captured["temperature"] = temperature
            return SimpleNamespace(content="complete recap")

        async def close(self):
            return None

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", _get_summary_model_config)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _config: _Client())
    monkeypatch.setattr("app.services.llm_client.with_llm_usage_context", lambda config, **_kwargs: config)

    result = await session_recall._summarize_recall_hits("question", hits, uuid.uuid4(), uuid.uuid4())

    assert decisive_tail in str(captured["prompt"])
    assert captured["max_tokens"] == 32_768
    assert result[0]["summary"] == "complete recap"
    assert result[0]["summary_method"] == "model"
    assert result[0]["summary_model_status"] == "completed"


@pytest.mark.asyncio
async def test_summary_model_failure_keeps_full_evidence_passthrough_and_is_observable(monkeypatch) -> None:
    from app.services import session_recall

    decisive_tail = "RECALL_FAILURE_DECISIVE_TAIL"
    transcript = "User: question\nAssistant: " + ("evidence " * 80) + decisive_tail
    hits = [
        {
            "headline": "Past decision",
            "summary": transcript,
            "focused_recap": "Evidence passthrough:\n" + transcript,
            "snippets": ["question"],
            "context_snippets": ["question"],
            "transcript_window": "User: question",
            "evidence_lines": transcript.splitlines(),
            "transcript": transcript,
            "summary_method": "evidence_passthrough",
        }
    ]

    async def _get_summary_model_config(_tenant_id):
        return {"provider": "openai", "model": "test", "api_key": "key"}

    class _Client:
        async def stream(self, **_kwargs):
            raise RuntimeError("model unavailable")

        async def close(self):
            return None

    monkeypatch.setattr("app.services.memory_service._get_summary_model_config", _get_summary_model_config)
    monkeypatch.setattr("app.services.llm_client.create_llm_client_from_config", lambda _config: _Client())
    monkeypatch.setattr("app.services.llm_client.with_llm_usage_context", lambda config, **_kwargs: config)

    result = await session_recall._summarize_recall_hits("question", hits, uuid.uuid4(), uuid.uuid4())

    assert decisive_tail in result[0]["summary"]
    assert decisive_tail in result[0]["focused_recap"]
    assert result[0]["summary_method"] == "evidence_passthrough"
    assert result[0]["summary_model_status"] == "failed"
    assert result[0]["summary_model_error_class"] == "RuntimeError"


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

    async def _fake_summarize(query, hits, tenant, agent=None):
        assert query == "priority matrix"
        assert tenant == tenant_id
        assert agent == agent_id
        assert hits[0]["summary"]
        hits[0]["summary"] = "模型聚焦摘要：我们把 priority matrix 整理成了决策摘要。"
        return hits

    monkeypatch.setattr(
        "app.services.session_recall.tenant_scoped_session", lambda *a, **k: _FakeSession([rows, transcript_rows])
    )
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
