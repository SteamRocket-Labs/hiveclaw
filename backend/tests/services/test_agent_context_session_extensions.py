from __future__ import annotations

from uuid import uuid4

from app.services import agent_context as agent_context_mod


def test_skill_catalog_includes_session_extension_overlay_only_for_matching_session(tmp_path, monkeypatch):
    agent_id = uuid4()
    session_id = uuid4()
    other_session_id = uuid4()
    monkeypatch.setattr(agent_context_mod, "TOOL_WORKSPACE", tmp_path / "tool")
    monkeypatch.setattr(agent_context_mod, "PERSISTENT_DATA", tmp_path / "data")

    overlay_skill = (
        tmp_path
        / "data"
        / str(agent_id)
        / "session_extensions"
        / str(session_id)
        / "skills"
        / "trial"
        / "SKILL.md"
    )
    overlay_skill.parent.mkdir(parents=True)
    overlay_skill.write_text("---\nname: Trial Session Skill\ndescription: Only for one chat session\n---\n\nUse only here.", encoding="utf-8")

    no_session_catalog = agent_context_mod.build_skill_catalog_section_for_agent(agent_id)
    other_session_catalog = agent_context_mod.build_skill_catalog_section_for_agent(agent_id, session_id=other_session_id)
    matching_session_catalog = agent_context_mod.build_skill_catalog_section_for_agent(agent_id, session_id=session_id)

    assert "Trial Session Skill" not in no_session_catalog
    assert "Trial Session Skill" not in other_session_catalog
    assert "Trial Session Skill" in matching_session_catalog
