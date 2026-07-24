from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


def _allow_overlay_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.memory.write_gate import MemoryWriteDecision

    async def accept_memory(content: str, *, category: str, **_kwargs) -> MemoryWriteDecision:
        return MemoryWriteDecision(
            original_content=content,
            content=content,
            category=category,
            sensitivity="PL1_public",
        )

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", accept_memory)


@pytest.mark.asyncio
async def test_save_memory_writes_explicit_overlay_not_accepted_t3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()
    _allow_overlay_writes(monkeypatch)

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
    revision = json.loads((tmp_path / str(agent_id) / "runtime_artifacts/asset_transactions/revision.json").read_text())
    assert revision["revision"] == 1


@pytest.mark.asyncio
async def test_concurrent_explicit_overlay_writes_share_one_asset_revision_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.explicit_overlay import write_explicit_memory_overlay

    _allow_overlay_writes(monkeypatch)
    agent_id = uuid.uuid4()
    contents = [
        "财务审批必须附发票原件",
        "招聘面试必须保存候选人同意",
        "生产发布必须先完成回滚演练",
        "客户数据导出必须双人复核",
        "供应商付款必须核验银行账户",
        "安全事件必须十五分钟内升级",
        "合同签署必须经过法务审阅",
        "董事会材料必须提前三天发送",
    ]
    results = await asyncio.gather(
        *[
            write_explicit_memory_overlay(
                agent_id,
                category="constraint",
                content=content,
                source_refs=[f"session:concurrent-{index}"],
                data_root=tmp_path,
            )
            for index, content in enumerate(contents)
        ]
    )

    assert all(result.status == "active" for result in results)
    manifest_lines = (
        (tmp_path / str(agent_id) / "memory/explicit/manifest.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(manifest_lines) == len(contents)
    assert {json.loads(line)["source_refs"] for line in manifest_lines} == {
        f"session:concurrent-{index}" for index in range(8)
    }
    index_text = (tmp_path / str(agent_id) / "memory/explicit/MEMORY.md").read_text(encoding="utf-8")
    assert all(result.entry_id in index_text for result in results)
    revision = json.loads((tmp_path / str(agent_id) / "runtime_artifacts/asset_transactions/revision.json").read_text())
    assert revision["revision"] == len(contents)


@pytest.mark.asyncio
async def test_concurrent_duplicate_explicit_candidates_commit_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.explicit_overlay import write_explicit_memory_overlay

    _allow_overlay_writes(monkeypatch)
    agent_id = uuid.uuid4()
    results = await asyncio.gather(
        *[
            write_explicit_memory_overlay(
                agent_id,
                category="constraint",
                content="上线前必须完成原子化全量验收",
                source_refs=["session:duplicate-candidate"],
                data_root=tmp_path,
            )
            for _index in range(10)
        ]
    )

    assert [result.status for result in results].count("active") == 1
    assert [result.status for result in results].count("duplicate") == 9
    manifest_lines = (
        (tmp_path / str(agent_id) / "memory/explicit/manifest.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert len(manifest_lines) == 1
    revision = json.loads((tmp_path / str(agent_id) / "runtime_artifacts/asset_transactions/revision.json").read_text())
    assert revision["revision"] == 1


@pytest.mark.asyncio
async def test_similar_but_distinct_explicit_memories_are_not_mechanically_deduplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import explicit_overlay

    _allow_overlay_writes(monkeypatch)
    agent_id = uuid.uuid4()

    first = await explicit_overlay.write_explicit_memory_overlay(
        agent_id,
        category="constraint",
        content="用户要求架构改造前先讨论并落文档",
        source_refs=["session:first"],
        data_root=tmp_path,
    )
    second = await explicit_overlay.write_explicit_memory_overlay(
        agent_id,
        category="constraint",
        content="用户要求架构改造前先讨论并落文档，同时保留完整验收证据",
        source_refs=["session:second"],
        data_root=tmp_path,
    )

    assert first.status == "active"
    assert second.status == "active"
    assert first.entry_id != second.entry_id
    manifest = (tmp_path / str(agent_id) / "memory/explicit/manifest.jsonl").read_text(encoding="utf-8")
    assert len(manifest.splitlines()) == 2


@pytest.mark.asyncio
async def test_search_and_load_memory_include_active_explicit_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    from app.tools.handlers.memory import load_memory, save_memory, search_memory

    agent_id = uuid.uuid4()
    _allow_overlay_writes(monkeypatch)

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
async def test_explicit_overlay_projects_activation_keys_for_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory.explicit_overlay import search_explicit_overlay_entries
    from app.memory.retriever import MemoryRetriever
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()
    source_ref = "t0://session/s1/segment/seg-1#seq=1..2"
    _allow_overlay_writes(monkeypatch)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        await save_memory(
            agent_id,
            {
                "content": "用户要求所有整改用红灯测试先锁边界，并且先更新文档证据",
                "category": "constraint",
                "source_refs": [source_ref],
            },
        )

    search_hits = search_explicit_overlay_entries(tmp_path, agent_id, "红灯测试", limit=1)

    assert len(search_hits) == 1
    activation_keys = search_hits[0]["metadata"]["activation_keys"]
    assert activation_keys["schema_version"] == "explicit_overlay.activation_keys.20260705"
    assert activation_keys["candidate_kind"] == "agent_memory"
    assert activation_keys["candidate_ref"]["source_type"] == "explicit_overlay"
    assert activation_keys["key_features"]["category"] == ["constraint"]
    assert activation_keys["key_features"]["target_hint"] == ["worker"]
    assert activation_keys["key_features"]["status"] == ["active"]
    assert "红灯" in activation_keys["key_features"]["concepts"]
    assert activation_keys["value_pointer"]["loader"] == "explicit_overlay_entry"
    assert source_ref in activation_keys["source_refs"]

    retrieved = MemoryRetriever(data_root=tmp_path)._retrieve_explicit_overlay(agent_id, query="红灯测试")

    assert retrieved[0].metadata["activation_keys"] == activation_keys


def test_explicit_overlay_activation_keys_preserve_all_semantic_concepts(tmp_path: Path) -> None:
    from app.memory.explicit_overlay import ExplicitMemoryOverlayEntry, build_explicit_overlay_activation_keys

    concepts = [f"concept{index:03d}" for index in range(100)]
    entry = ExplicitMemoryOverlayEntry(
        entry_id="explicit_complete_concepts",
        status="active",
        category="constraint",
        target_hint="worker",
        content=" ".join(concepts),
        source_refs=("t0://session/s1/segment/seg-1#seq=1",),
        sensitivity="PL1",
        path=tmp_path / "entry.md",
        created_at="2026-07-13T00:00:00+00:00",
        metadata={},
    )

    activation_keys = build_explicit_overlay_activation_keys(entry)

    projected = activation_keys["key_features"]["concepts"]
    assert concepts[0] in projected
    assert concepts[-1] in projected
    assert len(projected) >= len(concepts)


@pytest.mark.asyncio
async def test_explicit_overlay_does_not_invent_pl4_from_secret_shaped_prose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()
    _allow_overlay_writes(monkeypatch)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {
                "content": "Production API key is sk-live-abcdef1234567890abcdef",
                "category": "reference",
            },
        )

    assert result.startswith("Saved to explicit memory overlay")
    entry = next((tmp_path / str(agent_id) / "memory" / "explicit" / "entries").glob("*.md"))
    assert "sk-live-abcdef1234567890abcdef" in entry.read_text(encoding="utf-8")
