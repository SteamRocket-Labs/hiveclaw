from __future__ import annotations

import uuid
from pathlib import Path

import pytest


def test_index_shadow_keeps_p0_and_reports_p1_p2_overlap(tmp_path: Path) -> None:
    from app.memory.md_store import append_t3_entry
    from app.memory.retriever import MemoryRetriever

    agent_id = uuid.uuid4()
    append_t3_entry(tmp_path, agent_id, category="feedback", content="User requires Chinese responses")
    append_t3_entry(tmp_path, agent_id, category="blocked_pattern", content="Do not repeat failing web_search calls")
    append_t3_entry(tmp_path, agent_id, category="general", content="Railway deploys require healthcheck verification")
    append_t3_entry(tmp_path, agent_id, category="strategy", content="Use shadow reports before switching memory retrieval")
    append_t3_entry(tmp_path, agent_id, category="user", content="User works on Hive agent architecture")

    retriever = MemoryRetriever(data_root=tmp_path)
    report = retriever.retrieve_t3_index_shadow(agent_id, query="Railway shadow deploy")

    assert report["p0_preserved"] is True
    assert report["direct_count"] >= 5
    assert report["index_count"] >= 5
    assert report["p1_p2_overlap"] >= 1


@pytest.mark.asyncio
async def test_index_first_switch_is_opt_in(tmp_path: Path) -> None:
    from app.memory.md_store import append_t3_entry
    from app.memory.retriever import MemoryRetriever

    agent_id = uuid.uuid4()
    append_t3_entry(tmp_path, agent_id, category="feedback", content="User requires Chinese responses")
    append_t3_entry(tmp_path, agent_id, category="general", content="Railway deploys require healthcheck verification")

    direct = await MemoryRetriever(data_root=tmp_path).retrieve(agent_id, "Railway", None, None)
    switched = await MemoryRetriever(data_root=tmp_path, use_t3_index_first=True).retrieve(
        agent_id, "Railway", None, None
    )

    assert any(item.metadata.get("source_type") == "t3_direct" for item in direct)
    assert any(item.metadata.get("source_type") in {"t3_full_entry", "t3_index_entry"} for item in switched)
