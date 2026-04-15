"""Tests for heartbeat skill opportunity cooldown and coverage suppression."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


def _make_tool_activity(tool_name: str):
    return SimpleNamespace(
        action_type="tool_call",
        summary=f"called {tool_name}",
        detail_json={"tool": tool_name},
    )


def _repeat_activities(tool_name: str, count: int) -> list:
    return [_make_tool_activity(tool_name) for _ in range(count)]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / str(uuid.uuid4())
    (ws / "evolution").mkdir(parents=True)
    (ws / "skills").mkdir(parents=True)
    return ws


@pytest.mark.asyncio
async def test_skill_opportunity_pushed_on_first_detection(monkeypatch, workspace):
    """First time a new tool pattern hits the threshold, opportunity must fire and persist state."""
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    agent_id = uuid.uuid4()

    # 6 tool calls, 3 of them web_search, 3 of them web_fetch → both clear threshold
    activities = _repeat_activities("web_search", 3) + _repeat_activities("web_fetch", 3)
    out = await hb._build_evolution_context(agent_id, activities, tick_count=1)

    assert "Skill Creation Opportunity" in out
    assert "web_search" in out
    assert "web_fetch" in out
    # state persisted
    state_path = workspace / "evolution" / "skill_opportunity_cooldown.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["tick"] == 1
    assert sorted(state["tools"]) == ["web_fetch", "web_search"]


@pytest.mark.asyncio
async def test_skill_opportunity_suppressed_within_cooldown(monkeypatch, workspace):
    """Same frequent tool set within the cooldown window must not fire again."""
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    agent_id = uuid.uuid4()

    activities = _repeat_activities("web_search", 3) + _repeat_activities("web_fetch", 3)
    # First tick — fires
    out1 = await hb._build_evolution_context(agent_id, activities, tick_count=1)
    assert "Skill Creation Opportunity" in out1

    # Second tick with same tools, only 2 ticks later (cooldown=5) → suppressed
    out2 = await hb._build_evolution_context(agent_id, activities, tick_count=3)
    assert "Skill Creation Opportunity" not in out2


@pytest.mark.asyncio
async def test_skill_opportunity_fires_after_cooldown(monkeypatch, workspace):
    """After the cooldown window elapses the same tool set may be suggested again."""
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    agent_id = uuid.uuid4()

    activities = _repeat_activities("web_search", 3) + _repeat_activities("web_fetch", 3)
    out1 = await hb._build_evolution_context(agent_id, activities, tick_count=1)
    assert "Skill Creation Opportunity" in out1

    # 7 ticks later (> cooldown=5) → fires again
    out2 = await hb._build_evolution_context(agent_id, activities, tick_count=8)
    assert "Skill Creation Opportunity" in out2

    state = json.loads((workspace / "evolution" / "skill_opportunity_cooldown.json").read_text(encoding="utf-8"))
    assert state["tick"] == 8


@pytest.mark.asyncio
async def test_skill_opportunity_suppressed_when_existing_skill_covers_tools(monkeypatch, workspace):
    """If a workspace skill already declares the frequent tools, no need to suggest creation."""
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    agent_id = uuid.uuid4()

    skill_dir = workspace / "skills" / "web-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: "Web Research"\n'
        'description: "Run web research workflows"\n'
        "tools:\n"
        "  - web_search\n"
        "  - web_fetch\n"
        "---\n"
        "# Web Research\n\nSearch then fetch.\n",
        encoding="utf-8",
    )

    activities = _repeat_activities("web_search", 3) + _repeat_activities("web_fetch", 3)
    out = await hb._build_evolution_context(agent_id, activities, tick_count=1)

    assert "Skill Creation Opportunity" not in out
    # cooldown state should not be written — push was suppressed by coverage, not cooldown
    assert not (workspace / "evolution" / "skill_opportunity_cooldown.json").exists()


@pytest.mark.asyncio
async def test_skill_opportunity_fires_for_new_tool_set(monkeypatch, workspace):
    """Different frequent tool set bypasses cooldown because it's a genuinely new pattern."""
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    agent_id = uuid.uuid4()

    web_activities = _repeat_activities("web_search", 3) + _repeat_activities("web_fetch", 3)
    feishu_activities = _repeat_activities("feishu_doc_create", 3) + _repeat_activities(
        "feishu_doc_append", 3
    )

    out1 = await hb._build_evolution_context(agent_id, web_activities, tick_count=1)
    assert "Skill Creation Opportunity" in out1

    # tick 2: new tool set (different from last fire) → must fire
    out2 = await hb._build_evolution_context(agent_id, feishu_activities, tick_count=2)
    assert "Skill Creation Opportunity" in out2
    assert "feishu_doc_create" in out2
