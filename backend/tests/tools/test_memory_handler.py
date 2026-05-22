from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_save_memory_writes_t3_file_and_index(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )

        result = save_memory(
            agent_id,
            {
                "content": "User prefers concise answers",
                "category": "feedback",
            },
        )

    feedback_path = tmp_path / str(agent_id) / "memory" / "feedback.md"
    index_path = tmp_path / str(agent_id) / "memory" / "INDEX.md"

    assert "Saved to long-term memory [feedback]" in result
    assert feedback_path.exists()
    assert "User prefers concise answers" in feedback_path.read_text(encoding="utf-8")
    assert index_path.exists()
    assert "feedback.md" in index_path.read_text(encoding="utf-8")


def test_save_memory_persists_control_plane_metadata(tmp_path: Path) -> None:
    from app.memory.md_store import parse_entry_record
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )

        result = save_memory(
            agent_id,
            {
                "content": "Owner Alice email is alice@example.com for vendor escalation.",
                "category": "user",
            },
        )

    user_path = tmp_path / str(agent_id) / "memory" / "user.md"
    body = user_path.read_text(encoding="utf-8")
    entry_line = next(line for line in body.splitlines() if line.startswith("- ["))
    record = parse_entry_record(entry_line)

    assert result.startswith("Saved to long-term memory [user]")
    assert "alice@example.com" not in body
    assert "<Email_1>" in body
    assert record.metadata["sensitivity"] == "PL2_pii"
    assert record.metadata["status"] == "active"
    assert record.metadata["version"] == "1"
    assert record.metadata["access_count"] == "0"
    assert record.metadata["last_accessed"] == "never"
    assert record.metadata["entry_id"]


def test_save_memory_maps_project_to_knowledge(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        save_memory(
            agent_id,
            {
                "content": "Project deadline is 2026-04-15",
                "category": "project",
            },
        )

    knowledge_path = tmp_path / str(agent_id) / "memory" / "knowledge.md"
    assert knowledge_path.exists()
    assert "Project deadline is 2026-04-15" in knowledge_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_search_memory_reads_saved_t3_shadow_index(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory, search_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        save_memory(
            agent_id,
            {
                "content": "Use snake_case for Python variable names",
                "category": "feedback",
            },
        )
        result = await search_memory(
            agent_id,
            {
                "query": "snake_case",
                "scope": "facts",
            },
        )

    assert "## Semantic Memory" in result
    assert "snake_case" in result


@pytest.mark.asyncio
async def test_search_memory_reads_t3_markdown_without_shadow_index(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    (mem_dir / "feedback.md").write_text(
        "# Feedback\n\n- [2026-04-09] 用户要求所有回答都先给结论再展开\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        result = await search_memory(
            agent_id,
            {
                "query": "先给结论",
                "scope": "facts",
            },
        )

    assert "## Semantic Memory" in result
    assert "先给结论再展开" in result


@pytest.mark.asyncio
async def test_search_memory_session_scope_formats_recalled_sessions() -> None:
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()

    fake_hits = [
        {
            "session_id": "sess-1",
            "source": "web",
            "started_at": "2026-04-09",
            "headline": "讨论了 memory-system-redesign",
            "summary": "用户强调 t0 md 是基石，并要求三个蒸馏器职责严格分离。",
            "transcript_window": "User: 你强调 t0 md 是整个系统的基石\nAssistant: 我们讨论了三个蒸馏器的职责边界",
            "snippets": [
                "你强调 t0 md 是整个系统的基石",
                "我们讨论了三个蒸馏器的职责边界",
            ],
        },
        {
            "session_id": "sess-2",
            "source": "feishu",
            "started_at": "2026-04-08",
            "headline": "确认要做 md-first 架构收敛",
            "summary": "用户要求彻底收口 legacy memory path，只保留 md 真源。",
            "snippets": [
                "用户要求彻底收口 legacy memory path",
            ],
        },
    ]

    async def fake_search(*_args, **_kwargs):
        return fake_hits

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.tools.handlers.memory.search_session_history",
            fake_search,
        )
        result = await search_memory(
            agent_id,
            {
                "query": "md-first",
                "scope": "sessions",
                "limit": 5,
            },
        )

    assert "## Session Recall" in result
    assert "(2026-04-09 [web]) 讨论了 memory-system-redesign" in result
    assert "Summary: 用户强调 t0 md 是基石" in result
    assert "Context:" in result
    assert "User: 你强调 t0 md 是整个系统的基石" in result
    assert "t0 md 是整个系统的基石" in result
    assert "(2026-04-08 [feishu]) 确认要做 md-first 架构收敛" in result
