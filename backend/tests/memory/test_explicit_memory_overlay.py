from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_save_memory_writes_explicit_overlay_not_accepted_t3(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {
                "content": "用户要求架构改造前先讨论并落文档",
                "category": "feedback",
                "source_refs": ["t0://session/s1/segment/seg-1#seq=1..2"],
            },
        )

    mem_dir = tmp_path / str(agent_id) / "memory"
    assert "Saved to explicit memory overlay [feedback]" in result
    assert (mem_dir / "explicit" / "MEMORY.md").exists()
    assert (mem_dir / "explicit" / "manifest.jsonl").exists()

    entries = list((mem_dir / "explicit" / "entries").glob("*.md"))
    assert len(entries) == 1
    body = entries[0].read_text(encoding="utf-8")
    assert "<explicit_memory" in body
    assert "<normalized_memory>用户要求架构改造前先讨论并落文档</normalized_memory>" in body
    assert "target_hint: user" in body
    assert "status: active" in body
    assert "t0://session/s1/segment/seg-1#seq=1..2" in body

    accepted_t3 = mem_dir / "t3" / "user.md"
    assert not accepted_t3.exists() or "用户要求架构改造前先讨论并落文档" not in accepted_t3.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_search_and_load_memory_include_active_explicit_overlay(tmp_path: Path) -> None:
    import re

    from app.tools.handlers.memory import load_memory, save_memory, search_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        await save_memory(
            agent_id,
            {
                "content": "用户要求所有整改用红灯测试先锁边界",
                "category": "constraint",
            },
        )
        search_result = await search_memory(agent_id, {"query": "红灯测试", "scope": "facts"})

        assert "## Explicit Memory Overlay" in search_result
        assert "explicit_overlay" in search_result
        assert "红灯测试先锁边界" in search_result
        match = re.search(r"id=(explicit_[a-zA-Z0-9_-]+)", search_result)
        assert match

        loaded = load_memory(agent_id, {"ids": [match.group(1)]})

    assert "## Loaded Explicit Memory Overlay" in loaded
    assert "用户要求所有整改用红灯测试先锁边界" in loaded
    assert "source=memory/explicit/" in loaded


@pytest.mark.asyncio
async def test_explicit_overlay_uses_write_gate_and_refuses_pl4(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {
                "content": "Production API key is sk-live-abcdef1234567890abcdef",
                "category": "reference",
            },
        )

    assert result.startswith("[Rejected]")
    assert not (tmp_path / str(agent_id) / "memory" / "explicit" / "entries").exists()
