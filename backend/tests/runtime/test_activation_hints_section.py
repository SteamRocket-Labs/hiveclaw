"""Activation hint prompt section tests."""

from __future__ import annotations


def _candidate(kind: str, *, value_pointer: dict, preview: str, key_features: dict | None = None):
    from app.runtime.activation_candidates import ActivationCandidate, ActivationSurface

    return ActivationCandidate(
        candidate_kind=kind,
        candidate_ref={"candidate_id": f"{kind}:item:v1/hash", "kind": kind},
        key_features=key_features or {},
        value_pointer=value_pointer,
        surface=ActivationSurface(surface_kind="hint", preview=preview, token_estimate=3),
    )


def test_activation_hints_render_skill_tool_and_subagent_actions_without_memory_body() -> None:
    from app.runtime.prompt_sections.activation_hints import build_activation_hints_section

    skill = _candidate(
        "skill",
        value_pointer={"loader": "load_skill", "name": "python-api"},
        preview="Python API boundary guide",
        key_features={"name": ["python-api"]},
    )
    tool = _candidate(
        "tool",
        value_pointer={"loader": "tool_search", "selector": "select:team_create", "tool_name": "team_create"},
        preview="Team creation tool",
        key_features={"name": ["team_create"]},
    )
    subagent = _candidate(
        "subagent",
        value_pointer={"loader": "spawn_subagent", "subagent_type": "critic", "definition_name": "code-critic"},
        preview="Code critic worker",
        key_features={"name": ["code-critic"]},
    )
    memory = _candidate(
        "agent_memory",
        value_pointer={"loader": "knowledge_page", "source": "memory/knowledge/private.md"},
        preview="This is only a pointer, not loaded body",
    )

    section = build_activation_hints_section(
        {
            "top_activation_candidates": [
                skill.to_manifest(),
                tool.to_manifest(),
                subagent.to_manifest(),
                memory.to_manifest(),
            ]
        }
    )

    assert "## Activation Hints" in section
    assert "`load_skill` with `python-api`" in section
    assert "`tool_search` with `select:team_create`" in section
    assert "`spawn_subagent` type `critic` definition `code-critic`" in section
    assert "private.md" not in section
    assert "loaded body" not in section


def test_activation_hints_empty_without_actionable_candidates() -> None:
    from app.runtime.prompt_sections.activation_hints import build_activation_hints_section

    assert build_activation_hints_section({"top_activation_candidates": []}) == ""
