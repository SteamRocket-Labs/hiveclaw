"""D5 (purity debt): save_memory lane gate — episodic scan/observation logs
are runtime evidence (the extractor's `artifact_only` lane), not durable T3.

Production symptom: an agent's accepted T3 memory grew with
`2026-06-04 17:00 midday_scan: 14 expos in window, no change` ledger lines —
episodic observations the agent dumped into a durable lane via save_memory,
which bypasses the extractor's LLM lane judge. The compatibility adapter now
writes explicit overlay instead of accepted T3, but D5 still adds a mechanical
backstop on the `agent_tool` lane only (extractor/heartbeat/dream already judged the lane),
deliberately conservative: it needs BOTH a routine-observation verb AND a
null/count observation, so a real reusable strategy is never blocked.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.t3_store import append_t3_memory_candidate
from app.tools.handlers.memory import save_memory


@pytest.mark.asyncio
async def test_agent_tool_episodic_scan_log_is_refused(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="2026-06-04 17:00 midday_scan: 窗口内 14 个展会无变化",
        proposed_by="agent_tool",
        data_root=tmp_path,
    )

    assert result.status == "episodic"
    assert "workspace" in result.reason.lower() or "session log" in result.reason.lower()
    overlay = tmp_path / str(agent_id) / "memory" / "explicit" / "MEMORY.md"
    assert (not overlay.exists()) or "midday_scan" not in overlay.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_agent_tool_english_no_change_log_is_refused(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="2026-06-04 evening_scan complete: polled 14 expo sites, no change in window",
        proposed_by="agent_tool",
        data_root=tmp_path,
    )

    assert result.status == "episodic"


@pytest.mark.asyncio
async def test_agent_tool_durable_strategy_passes_lane_gate(tmp_path: Path) -> None:
    """Positive guard: a real reusable strategy must NOT be blocked (no null/count obs)."""
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="竞品展会监控用每天三次轮询的节奏效果最好，漏掉新展会的概率最低",
        proposed_by="agent_tool",
        data_root=tmp_path,
    )

    assert result.status == "overlay"


@pytest.mark.asyncio
async def test_single_scan_verb_is_not_a_false_positive(tmp_path: Path) -> None:
    """Conservative: a scan verb alone (no null/count observation) is durable, not blocked."""
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="部署前用 ruff check 扫描整个 app 目录能提前抓到格式问题",
        proposed_by="agent_tool",
        data_root=tmp_path,
    )

    assert result.status == "overlay"


@pytest.mark.asyncio
async def test_extractor_lane_is_exempt_from_episodic_gate(tmp_path: Path) -> None:
    """The gate guards only agent_tool; extractor already ran the LLM lane judge upstream."""
    agent_id = uuid.uuid4()

    result = await append_t3_memory_candidate(
        agent_id,
        category="strategy",
        content="2026-06-04 17:00 midday_scan: 窗口内 14 个展会无变化",
        proposed_by="extractor",
        data_root=tmp_path,
    )

    assert result.status == "overlay"


@pytest.mark.asyncio
async def test_save_memory_surfaces_episodic_as_friendly_skip(tmp_path: Path) -> None:
    """Handler renders the episodic refusal as a guiding [Skipped], not a hard [Rejected]."""
    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {
                "content": "2026-06-04 17:00 evening_scan: 窗口内 14 个展会无变化",
                "category": "strategy",
            },
        )

    assert result.startswith("[Skipped]")
    assert "workspace" in result.lower() or "session log" in result.lower()
