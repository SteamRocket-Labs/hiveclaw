from __future__ import annotations

from pathlib import Path


_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"
_APP_DIR = Path(__file__).resolve().parents[2] / "app"


def test_heartbeat_template_is_t3_consolidator_not_save_memory_loop() -> None:
    text = (_TEMPLATE_DIR / "HEARTBEAT.md").read_text(encoding="utf-8")

    assert "T3 Consolidator" in text
    assert "source_bundle.json" in text
    assert "t3_neighborhood.md" in text
    assert "consolidation_pitch.md" in text
    assert "revised_patch.md" in text
    assert "memory/t3/episodes.md" in text
    assert "memory/t3/user.md" in text
    assert "memory/t3/worker.md" in text
    assert "memory/t3/capabilities.md" in text
    assert "Do not call `save_memory`" in text or "do not call `save_memory`" in text.lower()
    assert "raw T0" in text
    assert "memory/feedback.md" not in text
    assert "memory/strategies.md" not in text


def test_t3_consolidator_and_memory_gate_templates_exist_with_rubrics() -> None:
    consolidator = (_TEMPLATE_DIR / "T3_CONSOLIDATOR.md").read_text(encoding="utf-8")
    gate = (_TEMPLATE_DIR / "T3_MEMORY_GATE.md").read_text(encoding="utf-8")

    for required in (
        "Segment Packages",
        "T3 neighborhood",
        "merge_required",
        "preserve unique deltas",
        "XML blocks",
        "submit_t3_consolidation_pitch",
        "submit_t3_revised_patch",
    ):
        assert required in consolidator

    for required in (
        "Memory Gate Agent",
        "evidence_strength",
        "scope_clarity",
        "stability",
        "future_utility",
        "conflict_safety",
        "0-4",
        "merge_directives",
    ):
        assert required in gate


def test_t3_memory_gate_rubric_defines_every_score_level_and_dimension() -> None:
    gate = (_TEMPLATE_DIR / "T3_MEMORY_GATE.md").read_text(encoding="utf-8")

    for level in ("0 =", "1 =", "2 =", "3 =", "4 ="):
        assert level in gate
    for dimension in (
        "evidence_strength",
        "scope_clarity",
        "stability",
        "future_utility",
        "conflict_safety",
    ):
        assert f"### {dimension}" in gate
        assert f"{dimension}`" in gate
    assert "Scores without these anchors are invalid." in gate


def test_t3_prompt_surfaces_use_one_reinforcement_vocabulary() -> None:
    heartbeat = (_TEMPLATE_DIR / "HEARTBEAT.md").read_text(encoding="utf-8")
    consolidator = (_TEMPLATE_DIR / "T3_CONSOLIDATOR.md").read_text(encoding="utf-8")
    gate = (_TEMPLATE_DIR / "T3_MEMORY_GATE.md").read_text(encoding="utf-8")
    joined = "\n".join([heartbeat, consolidator, gate])

    assert "reinforce_existing" not in joined
    assert "`reinforced`" in joined
    assert "consolidation_mode=reinforce" in joined
    assert "`create`, `merge`, `supersede`, `reinforce`, `contradict`, `retract`, `noop`" in joined


def test_live_tool_guidance_does_not_route_t3_candidates_through_save_memory() -> None:
    from app.tools.handlers.memory import save_memory

    meta = save_memory.tool_meta
    container_description = meta.parameters["properties"]["container_candidate"]["description"]
    workspace_tool_source = (_APP_DIR / "services" / "agent_tool_domains" / "workspace.py").read_text(
        encoding="utf-8"
    )

    assert "Explicit Memory Overlay" in meta.description
    assert "accepted T3 files are updated later" in meta.description
    assert "pass container_candidate" not in meta.description
    assert "Use skill_candidate / workflow_candidate" not in container_description
    assert "Deprecated compatibility hint" in container_description
    assert "do not use for new skill/workflow" in container_description

    assert "optional container_candidate" not in workspace_tool_source
    assert "submit_t3_consolidation_pitch" in workspace_tool_source
    assert "Use save_memory only for explicit user-commanded memory" in workspace_tool_source
