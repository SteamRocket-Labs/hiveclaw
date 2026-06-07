"""T2 guidance surface (docs/execution-mode-spectrum.md §5 / §7 / §8 T2).

Small-cut guidance alignment, pinned in one place:

* the seven-primitive decision sequence lives in ``executing_actions`` —
  default to direct work, ``track_todo`` for multi-step working memory,
  ``load_skill``/``tool_search`` for missing method/capability,
  ``spawn_subagent`` vs ``delegate_to_agent`` for who-does-it,
  workflow ONLY when the step order itself is a requirement,
  ``set_trigger`` for when-to-wake, ``save_skill`` vs workflow promotion
  for consolidation (§7 criteria);
* tool descriptions cross-reference each other (CC discipline: selection
  philosophy lives in tool descriptions);
* ``set_trigger`` documents ``config.workflow_ref`` with its criterion;
* ``system.py`` states the small-cut exposure reality: source capabilities
  are core-resident, integration packs still activate via skills;
* ``tool_search`` discovers deferred tools and makes matching schemas callable;
  ``load_skill`` remains for explicit method/instruction loading.
"""

from __future__ import annotations


def _tool_description(name: str) -> str:
    from app.services.agent_tools import get_combined_openai_tools

    return next(t["function"]["description"] for t in get_combined_openai_tools() if t["function"]["name"] == name)


def _tool_parameters(name: str) -> dict:
    from app.services.agent_tools import get_combined_openai_tools

    return next(t["function"]["parameters"] for t in get_combined_openai_tools() if t["function"]["name"] == name)


# ── §5 decision sequence in executing_actions ───────────────────────


def test_executing_actions_carries_seven_primitive_sequence():
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section

    text = build_executing_actions_section()
    for token in (
        "track_todo",
        "load_skill",
        "tool_search",
        "spawn_subagent",
        "delegate_to_agent",
        "preview_workflow",
        "start_workflow",
        "set_trigger",
        "save_skill",
    ):
        assert token in text, token


def test_executing_actions_states_workflow_is_for_required_order():
    """The workflow escalation criterion (§6 S3/S4): step order itself is the
    requirement — not a default for any multi-step task."""
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section

    text = build_executing_actions_section()
    assert "step order itself is a requirement" in text


def test_executing_actions_carries_consolidation_criteria():
    """§7 one-liner: repeated successful know-how → skill; never-drift process
    → workflow promotion (reviewed, never self-approved); one-off → ledger."""
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section

    text = build_executing_actions_section()
    assert "save_skill" in text
    assert "never self-approved" in text


def test_executing_actions_skills_line_excludes_source_capabilities():
    """Platform Integration must not imply source capabilities need a skill."""
    from app.runtime.prompt_sections.executing_actions import build_executing_actions_section

    text = build_executing_actions_section()
    assert "never need a skill" in text


# ── tool description cross-references (CC discipline) ───────────────


def test_start_workflow_description_points_back_to_spawn():
    desc = _tool_description("start_workflow")
    assert "spawn_subagent" in desc


def test_spawn_description_points_to_workflow():
    desc = _tool_description("spawn_subagent")
    assert "start_workflow" in desc
    # the existing peer-delegation cross-reference must survive
    assert "delegate_to_agent" in desc


def test_save_skill_description_separates_skill_from_workflow_promotion():
    desc = _tool_description("save_skill")
    assert "workflow" in desc.lower()
    assert "track_todo" in desc


# ── set_trigger workflow_ref criterion ──────────────────────────────


def test_set_trigger_config_documents_workflow_ref():
    params = _tool_parameters("set_trigger")
    config_desc = params["properties"]["config"]["description"]
    assert "workflow_ref" in config_desc
    assert "definition_name" in config_desc


# ── system.py small-cut exposure wording ────────────────────────────


def test_system_section_states_source_capabilities_resident():
    from app.runtime.prompt_sections.system import build_system_section

    text = build_system_section()
    assert "spawn_subagent" in text
    assert "no skill needed" in text
    # integration packs keep the skill-activation path (small cut, not T3)
    assert "load a matching skill" in text


# ── T3a deferred discovery: tool_search loads matching schemas ──────


def test_tool_search_loads_deferred_tool_schema():
    desc = _tool_description("tool_search")
    assert "deferred" in desc.lower()
    assert "callable" in desc.lower()
    assert "does not auto-load" not in desc
