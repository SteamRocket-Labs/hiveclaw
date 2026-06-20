from __future__ import annotations

import json
from pathlib import Path


def test_skill_registry_normalizes_sources_and_evolvable_boundary(tmp_path: Path) -> None:
    from app.services.skill_evolution_registry import (
        can_self_evolve_skill,
        load_skill_evolution_registry,
        upsert_skill_evolution_entry,
    )

    upsert_skill_evolution_entry(
        tmp_path,
        skill_name="web-search",
        target_path="skills/web-search/SKILL.md",
        skill_origin="system_builtin",
        evolvable=True,
    )
    upsert_skill_evolution_entry(
        tmp_path,
        skill_name="incident-response",
        target_path="skills/incident-response/SKILL.md",
        skill_origin="user_skill_creator",
    )
    upsert_skill_evolution_entry(
        tmp_path,
        skill_name="launch-checklist",
        target_path="skills/launch-checklist/SKILL.md",
        skill_origin="t3_auto_created",
    )

    registry = load_skill_evolution_registry(tmp_path)

    assert registry["skills"]["web-search"]["evolvable"] is False
    assert registry["skills"]["web-search"]["skill_origin"] == "system_builtin"
    assert can_self_evolve_skill(tmp_path, "web-search") is False
    assert registry["skills"]["incident-response"]["evolvable"] is True
    assert can_self_evolve_skill(tmp_path, "incident-response") is True
    assert registry["skills"]["launch-checklist"]["evolvable"] is True
    assert can_self_evolve_skill(tmp_path, "launch-checklist") is True


def test_system_builtin_runtime_failure_does_not_create_patch_candidate(tmp_path: Path) -> None:
    from app.services.skill_evolution_registry import upsert_skill_evolution_entry
    from app.services.skill_lifecycle import record_skill_runtime_usage

    upsert_skill_evolution_entry(
        tmp_path,
        skill_name="web-search",
        target_path="skills/web-search/SKILL.md",
        skill_origin="system_builtin",
    )

    first = record_skill_runtime_usage(
        tmp_path,
        skill_name="web-search",
        loaded_skill_names=["web-search"],
        tool_names=["load_skill", "web_search"],
        status="failed",
        note="Search result was not useful.",
        source="web_chat",
        occurred_at="2026-06-20T10:00:00Z",
    )
    second = record_skill_runtime_usage(
        tmp_path,
        skill_name="web-search",
        loaded_skill_names=["web-search"],
        tool_names=["load_skill", "web_search"],
        status="workaround",
        note="Manual query rewrite fixed it once.",
        source="web_chat",
        occurred_at="2026-06-20T10:05:00Z",
    )

    usage_log = (tmp_path / "evolution" / "skill_usage.jsonl").read_text(encoding="utf-8")

    assert first["decision"] == "ignored_non_evolvable"
    assert second["decision"] == "ignored_non_evolvable"
    assert "Search result was not useful" in usage_log
    assert not (tmp_path / "evolution" / "skill_candidates").exists()


def test_skill_candidate_package_records_origin_and_agent_authoring_contract(tmp_path: Path) -> None:
    from app.services.skill_candidate_package import write_skill_candidate_package

    manifest = write_skill_candidate_package(
        workspace=tmp_path,
        candidate_id="save-skill-1",
        rendered_markdown="---\nname: incident-response\ndescription: Handle incidents.\n---\n# Incident Response\n",
        skill_name="incident-response",
        package_type="save_skill",
        target_path="skills/incident-response/SKILL.md",
        source_refs=["chat_session:session-1"],
        reason="User asked the agent to create this skill.",
        declared_tools=[],
        declared_packs=[],
    )

    manifest_path = tmp_path / "evolution" / "skill_candidates" / "save-skill-1" / "manifest.json"
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["skill_origin"] == "user_skill_creator"
    assert manifest["evolvable"] is True
    assert persisted["authoring_contract"]["final_skill_body"] == "agent_llm_authored"
    assert persisted["authoring_contract"]["platform_role"] == "scaffold_validate_govern_commit_exact"
