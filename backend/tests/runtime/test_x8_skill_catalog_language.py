"""Model-facing skills use plain language while preserving execution boundaries."""

import json
from uuid import uuid4

from app.services import agent_context
from app.tools.handlers.skills import load_skill, save_skill


def test_real_agent_skill_catalog_and_tool_schemas_use_plain_language(tmp_path, monkeypatch):
    agent_id = uuid4()
    skill = tmp_path / str(agent_id) / "skills" / "weekly-report" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: Weekly report\ndescription: Prepare a weekly report\n---\n# Report\nUse verified facts.")
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path / "tool-workspaces")
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)

    section = agent_context.build_skill_catalog_section_for_agent(agent_id)
    assert "## Skills" in section and "Weekly report" in section
    assert "Prepare a weekly report" in section and "weekly-report/SKILL.md" in section
    assert "Skills provide methods and reusable task instructions." in section
    assert "references, templates, scripts, workflow definitions, and subagent definitions" in section
    assert "loading it adds context and guidance only" in section
    assert "Executable components still run through their governed runtime" in section
    assert "Do NOT load a skill before simple direct tool calls" in section
    assert "do NOT guess what a skill contains" in section
    assert "capsule" not in section.lower()

    for handler in (load_skill, save_skill):
        meta = handler.tool_meta
        assert "capsule" not in json.dumps([meta.description, meta.parameters]).lower()
        assert "reusable task instructions" in meta.description
        assert "governed runtime" in meta.description
