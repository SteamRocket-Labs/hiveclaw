from __future__ import annotations


def test_skill_creator_prompt_preserves_agent_authoring_boundary() -> None:
    from app.services.skill_creator_content import SKILL_CREATOR_MD, get_skill_creator_files

    assert "Final `SKILL.md` content must be authored by the Agent" in SKILL_CREATOR_MD
    assert "Do not ask the platform to template-generate the semantic body" in SKILL_CREATOR_MD
    assert "`save_skill` submits an inactive Skill Candidate Package" in SKILL_CREATOR_MD
    assert "candidate_signal.md is evidence only" in SKILL_CREATOR_MD
    assert "configured Agent/model with access to the skill" in SKILL_CREATOR_MD
    assert "claude-with-access-to-the-skill" not in SKILL_CREATOR_MD
    assert "current Agent must inspect the complete" in SKILL_CREATOR_MD
    assert "scripts/improve_description.py" not in SKILL_CREATOR_MD
    assert "scripts/improve_description.py" not in {item["path"] for item in get_skill_creator_files()}
