from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_save_memory_writes_t3_file_and_index(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )

        result = await save_memory(
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


@pytest.mark.asyncio
async def test_save_memory_passes_tenant_id_to_hindsight_sync(tmp_path: Path) -> None:
    """Closure A3: agent-tool writes must carry tenant_id end-to-end.

    hindsight_sync returns early on tenant_id=None, so a save_memory that
    drops it silently skips the immediate read-side sync for every
    Hindsight-enabled tenant. The agent_args adapter passes tenant_id as the
    third positional argument once the handler signature accepts it.
    """
    from app.memory import hindsight_sync
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    seen: dict = {}

    # Test Double rationale: Hindsight is the optional external read-side
    # accelerator boundary; the durable MD write chain below it runs for real.
    async def _capture(aid, tid, *, data_root=None):
        seen["agent_id"] = aid
        seen["tenant_id"] = tid
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        mp.setattr(hindsight_sync, "sync_t3_to_hindsight", _capture)

        result = await save_memory(
            agent_id,
            {"content": "Tenant-scoped fact for sync", "category": "feedback"},
            tenant_id,
        )

    assert "Saved to long-term memory" in result
    assert seen["agent_id"] == agent_id
    assert seen["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_save_memory_adapter_string_tenant_reaches_hindsight_as_uuid(tmp_path: Path) -> None:
    """The production agent_args adapter carries tenant_id as a string.

    Hindsight backend resolution uses tenant_id.hex, so save_memory must
    normalize the adapter value before calling append_t3_memory_candidate.
    """
    from app.memory import hindsight_sync
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers.memory import save_memory
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    seen: dict = {}

    async def _capture(aid, tid, *, data_root=None):
        seen["agent_id"] = aid
        seen["tenant_id"] = tid
        seen["tenant_type"] = type(tid).__name__
        return 0

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        mp.setattr(hindsight_sync, "sync_t3_to_hindsight", _capture)

        result = await adapt_and_call(
            save_memory.meta,
            save_memory,
            ToolExecutionRequest(
                tool_name="save_memory",
                arguments={"content": "Adapter tenant string is normalized", "category": "feedback"},
                context=ToolExecutionContext(
                    agent_id=agent_id,
                    user_id=user_id,
                    tenant_id=str(tenant_id),
                    workspace=tmp_path,
                ),
            ),
        )

    assert "Saved to long-term memory" in result
    assert seen["agent_id"] == agent_id
    assert seen["tenant_id"] == tenant_id
    assert seen["tenant_type"] == "UUID"


@pytest.mark.asyncio
async def test_save_memory_persists_control_plane_metadata(tmp_path: Path) -> None:
    from app.memory.lifecycle_store import MemoryLifecycleStore
    from app.memory.md_store import parse_entry_record
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )

        result = await save_memory(
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
    # D2: prose carries only [date][entry_id]; sensitivity/status/version + the
    # D1 telemetry all live in the lifecycle sidecar, never inlined into prose.
    assert "[sensitivity=" not in entry_line
    assert "[status=" not in entry_line
    assert "[version=" not in entry_line
    assert "[access_count" not in entry_line
    assert "[last_accessed" not in entry_line
    assert record.metadata["entry_id"]
    lifecycle = MemoryLifecycleStore(tmp_path / str(agent_id) / "memory" / "lifecycle.json")
    lifecycle_entry = lifecycle.get(record.metadata["entry_id"])
    assert lifecycle_entry.metadata.get("sensitivity") == "PL2_pii"
    assert lifecycle_entry.content == "Owner Alice email is <Email_1> for vendor escalation."
    assert lifecycle_entry.status == "active"
    assert lifecycle_entry.access_count == 0
    assert lifecycle_entry.last_accessed is None


@pytest.mark.asyncio
async def test_save_memory_maps_project_to_knowledge(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        await save_memory(
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
    import re

    from app.tools.handlers.memory import load_memory, save_memory, search_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        await save_memory(
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
        assert "load_memory" in result
        match = re.search(r"id=([a-zA-Z0-9_-]+)", result)
        assert match

        loaded = load_memory(agent_id, {"ids": [match.group(1)]})

        assert "## Loaded Memory" in loaded
        assert "Use snake_case for Python variable names" in loaded
        assert "source=memory/feedback.md" in loaded


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
    assert "id=" in result
    assert "先给结论再展开" in result


def test_load_memory_reports_missing_ids(tmp_path: Path) -> None:
    from app.tools.handlers.memory import load_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        result = load_memory(agent_id, {"ids": ["missing-id"]})

    assert "No memory entries found" in result
    assert "missing-id" in result


def test_load_memory_suppresses_pl3_by_default(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.tools.handlers.memory import load_memory

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    (mem_dir / "knowledge.md").write_text(
        "# Knowledge\n\n"
        "- [2026-06-05][entry_id=mem_public][sensitivity=PL1_public] public deployment note\n"
        "- [2026-06-05][entry_id=mem_salary][sensitivity=PL3_sensitive] salary planning is confidential\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        result = load_memory(agent_id, {"ids": ["mem_public", "mem_salary"]})

    assert "public deployment note" in result
    assert "mem_salary" not in result
    assert "salary planning is confidential" not in result
    assert "Suppressed entries: 1" in result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        suppressed_only = load_memory(agent_id, {"ids": ["mem_salary"]})

    assert "mem_salary" not in suppressed_only
    assert "No visible memory entries found." in suppressed_only
    assert "Suppressed entries: 1" in suppressed_only


@pytest.mark.asyncio
async def test_search_memory_suppresses_pl3_by_default(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    (mem_dir / "knowledge.md").write_text(
        "# Knowledge\n\n"
        "- [2026-06-05][entry_id=mem_salary][sensitivity=PL3_sensitive] salary planning is confidential\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        result = await search_memory(agent_id, {"query": "salary", "scope": "facts"})

    assert "salary planning is confidential" not in result
    assert "mem_salary" not in result


@pytest.mark.asyncio
async def test_search_memory_suppresses_sensitive_wiki_page_even_when_preview_is_safe(tmp_path: Path) -> None:
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()
    wiki_dir = tmp_path / str(agent_id) / "memory" / "wiki"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "comp-plan.md").write_text(
        "---\ntitle: Comp Plan\ntype: concept\nstatus: active\n---\n\n"
        "## Current Claim\n\n"
        f"{'public context ' * 20}\n\nsalary planning is confidential\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        result = await search_memory(agent_id, {"query": "public context", "scope": "facts"})

    assert "Comp Plan" not in result
    assert "salary planning is confidential" not in result


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
