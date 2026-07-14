"""Heartbeat exposes facts; it never authors a Skill opportunity."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


def _tool_activity(tool_name: str, *, summary: str | None = None):
    return SimpleNamespace(
        action_type="tool_call",
        summary=summary or f"called {tool_name}",
        detail_json={"tool": tool_name, "full_summary": summary or f"called {tool_name}"},
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / str(uuid.uuid4())
    (ws / "evolution").mkdir(parents=True)
    (ws / "skills").mkdir(parents=True)
    return ws


@pytest.mark.asyncio
async def test_single_tool_observation_is_visible_without_count_threshold(monkeypatch, workspace):
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    output = await hb._build_evolution_context(uuid.uuid4(), [_tool_activity("rare_decisive_tool")], tick_count=1)

    assert "Complete Tool Usage Evidence" in output
    assert "rare_decisive_tool (x1)" in output
    assert "model decides" in output.lower()
    assert "Skill Candidate Opportunity" not in output


@pytest.mark.asyncio
async def test_tick_count_and_frequency_never_change_tool_evidence_visibility(monkeypatch, workspace):
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    activities = [_tool_activity("web_search") for _ in range(9)] + [_tool_activity("tail_tool")]

    first = await hb._build_evolution_context(uuid.uuid4(), activities, tick_count=1)
    later = await hb._build_evolution_context(uuid.uuid4(), activities, tick_count=999)

    for output in (first, later):
        assert "web_search (x9)" in output
        assert "tail_tool (x1)" in output
        assert "Skill Candidate Opportunity" not in output
    assert not (workspace / "evolution" / "skill_opportunity_cooldown.json").exists()


@pytest.mark.asyncio
async def test_existing_skill_never_suppresses_complete_tool_facts(monkeypatch, workspace):
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    skill_dir = workspace / "skills" / "web-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Web Research\ndescription: Existing workflow\n---\n# Web Research\n",
        encoding="utf-8",
    )

    output = await hb._build_evolution_context(
        uuid.uuid4(),
        [_tool_activity("web_search"), _tool_activity("web_fetch")],
    )

    assert "web_search (x1)" in output
    assert "web_fetch (x1)" in output


@pytest.mark.asyncio
async def test_full_activity_summary_tail_is_preserved(monkeypatch, workspace):
    from app.services import heartbeat as hb

    monkeypatch.setattr(hb, "_get_canonical_workspace", lambda _aid: workspace)
    tail = "HEARTBEAT_ACTIVITY_DECISIVE_TAIL"
    summary = ("evidence " * 200) + tail
    activity = SimpleNamespace(
        action_type="error",
        summary="database preview",
        detail_json={"full_summary": summary, "error": summary},
    )

    output = await hb._build_evolution_context(uuid.uuid4(), [activity])

    assert tail in output
