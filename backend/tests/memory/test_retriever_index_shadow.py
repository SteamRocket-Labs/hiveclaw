from __future__ import annotations

import uuid
from pathlib import Path

import pytest


def test_index_shadow_keeps_p0_and_reports_p1_p2_overlap(tmp_path: Path) -> None:
    from app.memory.retriever import MemoryRetriever

    agent_id = uuid.uuid4()
    memory_dir = tmp_path / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "feedback.md").write_text(
        "# Feedback\n- [2026-05-02] User requires Chinese responses\n", encoding="utf-8"
    )
    (memory_dir / "blocked.md").write_text(
        "# Blocked\n- [2026-05-02] Do not repeat failing web_search calls\n", encoding="utf-8"
    )
    (memory_dir / "knowledge.md").write_text(
        "# Knowledge\n- [2026-05-02] Railway deploys require healthcheck verification\n", encoding="utf-8"
    )
    (memory_dir / "strategies.md").write_text(
        "# Strategies\n- [2026-05-02] Use shadow reports before switching memory retrieval\n", encoding="utf-8"
    )
    (memory_dir / "user.md").write_text(
        "# User\n- [2026-05-02] User works on Hive agent architecture\n", encoding="utf-8"
    )
    (memory_dir / "INDEX.md").write_text(
        "# Memory Index\n"
        "| File | Category | Items | Updated | Load |\n"
        "|------|----------|-------|---------|------|\n"
        "| knowledge.md | knowledge | 1 | 2026-05-02 | Railway deploys |\n"
        "| strategies.md | strategy | 1 | 2026-05-02 | shadow reports |\n",
        encoding="utf-8",
    )

    retriever = MemoryRetriever(data_root=tmp_path)
    report = retriever.retrieve_t3_index_shadow(agent_id, query="Railway shadow deploy")

    assert report["p0_preserved"] is True
    assert report["direct_count"] >= 5
    assert report["index_count"] >= 3
    assert report["p1_p2_overlap"] >= 1


@pytest.mark.asyncio
async def test_index_first_switch_is_opt_in(tmp_path: Path) -> None:
    from app.memory.retriever import MemoryRetriever

    agent_id = uuid.uuid4()
    memory_dir = tmp_path / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "feedback.md").write_text(
        "# Feedback\n- [2026-05-02] User requires Chinese responses\n", encoding="utf-8"
    )
    (memory_dir / "blocked.md").write_text("# Blocked\n", encoding="utf-8")
    (memory_dir / "knowledge.md").write_text(
        "# Knowledge\n- [2026-05-02] Railway deploys require healthcheck verification\n", encoding="utf-8"
    )
    (memory_dir / "strategies.md").write_text("# Strategies\n", encoding="utf-8")
    (memory_dir / "user.md").write_text("# User\n", encoding="utf-8")
    (memory_dir / "INDEX.md").write_text(
        "# Memory Index\n"
        "| File | Category | Items | Updated | Load |\n"
        "| knowledge.md | knowledge | 1 | 2026-05-02 | Railway deploys |\n",
        encoding="utf-8",
    )

    direct = await MemoryRetriever(data_root=tmp_path).retrieve(agent_id, "Railway", None, None)
    switched = await MemoryRetriever(data_root=tmp_path, use_t3_index_first=True).retrieve(
        agent_id, "Railway", None, None
    )

    assert any(item.metadata.get("source_type") == "t3_direct" for item in direct)
    assert any(item.metadata.get("source_type") in {"t3_full_entry", "t3_index_entry"} for item in switched)
