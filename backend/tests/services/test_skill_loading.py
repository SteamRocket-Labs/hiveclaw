from __future__ import annotations

import json

from uuid import uuid4


def test_load_skill_reads_folder_and_flat_file(tmp_path):
    from app.services.agent_tools import _load_skill

    workspace = tmp_path / "agent"
    folder_skill = workspace / "skills" / "web-research"
    folder_skill.mkdir(parents=True)
    (folder_skill / "SKILL.md").write_text("folder skill body", encoding="utf-8")

    flat_skill = workspace / "skills" / "data-analysis.md"
    flat_skill.write_text("flat skill body", encoding="utf-8")

    assert _load_skill(workspace, "web research") == "folder skill body"
    assert _load_skill(workspace, "data analysis") == "flat skill body"


def test_load_skill_reads_nested_scoped_skill_by_name_and_explicit_path(tmp_path):
    from app.services.agent_tools import _load_skill

    workspace = tmp_path / "agent"
    nested_skill = workspace / "projects" / "api" / "skills" / "python"
    nested_skill.mkdir(parents=True)
    (nested_skill / "SKILL.md").write_text(
        "---\nname: Python\ndescription: API-local Python guidance\n---\n# Python\nUse typed boundaries.\n",
        encoding="utf-8",
    )

    assert _load_skill(workspace, "Python") == "# Python\nUse typed boundaries."
    explicit = _load_skill(workspace, "projects/api/skills/python/SKILL.md")
    assert "API-local Python guidance" in explicit
    assert "# Python\nUse typed boundaries." in explicit


def test_load_skill_preserves_managed_channel_guidance_for_model_judgment(tmp_path):
    from app.services.agent_tools import _load_skill

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "feishu-calendar-event"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "# Feishu Calendar",
                "Use the calendar API.",
                "export FEISHU_APP_ID=cli_xxxxxxxxxxxx",
                "export FEISHU_APP_SECRET=xxxxxxxxxxxxx",
                "To debug credentials, run `env | grep -E '^FEISHU_'`.",
            ]
        ),
        encoding="utf-8",
    )

    content = _load_skill(workspace, "feishu calendar event")

    assert "FEISHU_APP_ID" in content
    assert "FEISHU_APP_SECRET" in content
    assert "env | grep" in content


def test_read_file_preserves_nested_scoped_skill_instruction_file(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_file

    workspace = tmp_path / "agent"
    skill_dir = workspace / "projects" / "api" / "skills" / "slack"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Slack\nRun `printenv SLACK_BOT_TOKEN` before using Slack.",
        encoding="utf-8",
    )

    content = _read_file(workspace, "projects/api/skills/slack/SKILL.md")

    assert "SLACK_BOT_TOKEN" in content
    assert "printenv" in content


def test_load_skill_explicit_path_preserves_full_instruction_body(tmp_path):
    from app.services.agent_tools import _load_skill

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "long-skill"
    skill_dir.mkdir(parents=True)
    sentinel = "FINAL_SENTINEL_FULL_BODY_VISIBLE"
    body = "# Long Skill\n" + ("A" * 17000) + f"\n{sentinel}\n"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")

    content = _load_skill(workspace, "skills/long-skill/SKILL.md")

    assert sentinel in content
    assert "[truncated" not in content


def test_read_file_preserves_skill_managed_channel_env_guidance(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_file

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "slack"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Slack\nRun `printenv SLACK_BOT_TOKEN` before using Slack.",
        encoding="utf-8",
    )

    content = _read_file(workspace, "skills/slack/SKILL.md")

    assert "SLACK_BOT_TOKEN" in content
    assert "printenv" in content


def test_load_skills_index_instructs_load_skill(monkeypatch, tmp_path):
    from app.services.agent_context import _load_skills_index

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "skills" / "writing"
    workspace.mkdir(parents=True)
    (workspace / "SKILL.md").write_text(
        "---\nname: Writing\ndescription: Draft polished content\n---\n# Writing\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)

    skills_index = _load_skills_index(agent_id)

    assert "`load_skill`" in skills_index
    assert "call `read_file`" not in skills_index


def test_save_skill_submits_candidate_package_without_active_skill(tmp_path):
    from app.services.agent_tool_domains.workspace import _load_skill, _submit_skill_activation_candidate

    workspace = tmp_path / "agent"
    workspace.mkdir(parents=True)

    result = _submit_skill_activation_candidate(
        workspace,
        agent_id=None,
        name="Deployment Review",
        description="Review deployment diffs and verify rollback paths.",
        instructions="Check rollout status, verify logs, and confirm rollback steps.",
        declared_tools=("web_search", "web_fetch"),
        declared_packs=("web_pack",),
    )

    skill_path = workspace / "skills" / "deployment-review" / "SKILL.md"
    assert "submitted for review" in result
    assert "skills/deployment-review/SKILL.md" in result
    assert not skill_path.exists()
    assert not (workspace / "evolution" / "skill_activation_candidates.md").exists()

    packages = sorted((workspace / "evolution" / "skill_candidates").iterdir())
    assert len(packages) == 1
    package = packages[0]
    draft = package / "SKILL.md.draft"
    manifest = package / "manifest.json"
    assert draft.exists()
    assert manifest.exists()

    content = draft.read_text(encoding="utf-8")
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert 'name: "Deployment Review"' in content
    assert 'description: "Review deployment diffs and verify rollback paths."' in content
    assert "tools:" not in content
    assert "packs:" not in content
    assert manifest_data["declared_tools"] == ["web_search", "web_fetch"]
    assert manifest_data["declared_packs"] == ["web_pack"]
    assert "not_found" in _load_skill(workspace, "deployment review")
    review_log = (workspace / "evolution" / "skill_review.md").read_text(encoding="utf-8")
    assert "Deployment Review" in review_log


def test_save_skill_similarity_is_observation_not_candidate_rejection(tmp_path):
    from app.services.agent_tool_domains.workspace import _submit_skill_activation_candidate

    workspace = tmp_path / "agent"
    existing_dir = workspace / "skills" / "deployment-review"
    existing_dir.mkdir(parents=True)
    (existing_dir / "SKILL.md").write_text(
        "---\n"
        'name: "Deployment Review"\n'
        'description: "Review deployment diffs and verify rollback paths."\n'
        "---\n"
        "# Deployment Review\n\nCheck deployment evidence.\n",
        encoding="utf-8",
    )

    result = _submit_skill_activation_candidate(
        workspace,
        agent_id=None,
        name="Deployment Diff Review",
        description="Review deployment diffs and verify rollback paths.",
        instructions="Compare deployment evidence and rollback readiness.",
    )

    assert "submitted for review" in result
    manifests = list((workspace / "evolution" / "skill_candidates").glob("*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    observations = manifest["metadata"]["semantic_observations"]
    assert observations[0]["kind"] == "similar_existing_skill"
    assert observations[0]["skill_name"] == "Deployment Review"


def test_save_skill_requests_patch_package_without_overwriting_active_skill(tmp_path):
    from app.services.agent_tool_domains.workspace import _submit_skill_activation_candidate

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "research"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        '---\nname: "Research"\ndescription: "Old"\n---\n# Research\nOld steps.\n',
        encoding="utf-8",
    )

    rejected = _submit_skill_activation_candidate(
        workspace,
        agent_id=None,
        name="Research",
        description="Updated workflow.",
        instructions="Use primary sources first.",
    )
    assert "already exists" in rejected

    updated = _submit_skill_activation_candidate(
        workspace,
        agent_id=None,
        name="Research",
        description="Updated workflow.",
        instructions="Use primary sources first.",
        overwrite=True,
    )
    assert "submitted for review" in updated
    content = skill_path.read_text(encoding="utf-8")
    assert 'description: "Old"' in content
    assert "Old steps." in content
    packages = sorted((workspace / "evolution" / "skill_candidates").iterdir())
    assert len(packages) == 1
    draft = (packages[0] / "SKILL.md.draft").read_text(encoding="utf-8")
    assert 'description: "Updated workflow."' in draft
    assert "Use primary sources first." in draft


def test_save_skill_marks_candidate_review_record(tmp_path):
    """Saving a skill records an inactive candidate, not an active curator entry."""
    from app.services.agent_tool_domains.workspace import _submit_skill_activation_candidate

    workspace = tmp_path / "agent"
    workspace.mkdir(parents=True)

    _submit_skill_activation_candidate(
        workspace,
        agent_id=None,
        name="Deployment Review",
        description="Review deployment diffs and verify rollback paths.",
        instructions="Check rollout status, verify logs, and confirm rollback steps.",
    )

    review_log = (workspace / "evolution" / "skill_review.md").read_text(encoding="utf-8")
    assert "[candidate] Deployment Review:" in review_log
    assert not (workspace / "skills" / "deployment-review" / "SKILL.md").exists()


def test_load_skill_bumps_curator_use_count(tmp_path):
    """Loading a skill increments its curator use_count and refreshes last_used_at."""
    from app.services.agent_tool_domains.workspace import _load_skill
    from app.services.skill_curator import load_skill_usage

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "deployment-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        'name: "Deployment Review"\n'
        'description: "Review deployment diffs and verify rollback paths."\n'
        "---\n"
        "# Deployment Review\n\n"
        "Check rollout status, verify logs, and confirm rollback steps.\n",
        encoding="utf-8",
    )

    _load_skill(workspace, "deployment review")
    _load_skill(workspace, "deployment review")

    rec = load_skill_usage(workspace)["deployment-review"]
    assert rec["use_count"] == 2
    assert rec["last_used_at"] is not None


def test_load_skill_surfaces_allowed_tools_scope_guidance(tmp_path):
    """Step 9: a skill's allowed-tools is re-surfaced as scoped tool guidance on
    the registry path (which strips frontmatter). Guidance, not a hard filter."""
    from app.services.agent_tools import _load_skill

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "market-research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: Market Research",
                "description: Research a market.",
                "allowed-tools: web_search, web_fetch, write_file",
                "---",
                "# Market Research",
                "Search, fetch, summarize.",
            ]
        ),
        encoding="utf-8",
    )

    content = _load_skill(workspace, "market research")

    assert "# Market Research" in content
    assert "Tool scope (skill guidance)" in content
    assert "web_search" in content
    assert "web_fetch" in content
    assert "guidance, not a hard limit" in content


def test_load_skill_without_allowed_tools_has_no_scope_guidance(tmp_path):
    from app.services.agent_tools import _load_skill

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "plain-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Plain Skill\ndescription: No tool scope.\n---\n# Plain\nDo the thing.\n",
        encoding="utf-8",
    )

    content = _load_skill(workspace, "plain skill")

    assert "# Plain" in content
    assert "Tool scope (skill guidance)" not in content


def test_load_skill_allows_provisional_with_trial_notice_and_blocks_terminal_states(tmp_path):
    from app.services.agent_tools import _load_skill
    from app.services.skill_evolution_registry import ORIGIN_T3_AUTO_CREATED, upsert_skill_evolution_entry

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "trial-skill"
    skill_dir.mkdir(parents=True)
    target_path = "skills/trial-skill/SKILL.md"
    (workspace / target_path).write_text(
        "---\nname: Trial Skill\ndescription: Trial guidance.\n---\n# Trial Skill\nUse it.\n",
        encoding="utf-8",
    )
    upsert_skill_evolution_entry(
        workspace,
        skill_name="Trial Skill",
        target_path=target_path,
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        state="provisional",
        last_candidate_id="cand-trial",
    )

    provisional = _load_skill(workspace, target_path)
    assert "provisional trial" in provisional.lower()
    assert "# Trial Skill" in provisional

    upsert_skill_evolution_entry(
        workspace,
        skill_name="Trial Skill",
        target_path=target_path,
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        state="rolled_back",
        last_candidate_id="cand-trial",
    )
    blocked = _load_skill(workspace, target_path)
    assert "skill_state_blocked" in blocked
    assert "rolled_back" in blocked
    assert "# Trial Skill" not in blocked


def test_workspace_skill_loader_filters_non_loadable_registry_states(tmp_path):
    from app.services.skill_evolution_registry import ORIGIN_T3_AUTO_CREATED, upsert_skill_evolution_entry
    from app.skills.loader import WorkspaceSkillLoader

    for slug in ("active-skill", "trial-skill", "review-skill"):
        path = tmp_path / "skills" / slug / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nname: {slug}\ndescription: test\n---\n# {slug}\n",
            encoding="utf-8",
        )
    review_script = tmp_path / "skills" / "review-skill" / "scripts" / "run.py"
    review_script.parent.mkdir(parents=True)
    review_script.write_text("print('must remain blocked')\n", encoding="utf-8")
    for slug, skill_name, state in (
        ("active-skill", "active-skill", "active"),
        ("trial-skill", "trial-skill", "provisional"),
        ("review-skill", "Review Display Name", "needs_review"),
    ):
        upsert_skill_evolution_entry(
            tmp_path,
            skill_name=skill_name,
            target_path=f"skills/{slug}/SKILL.md",
            skill_origin=ORIGIN_T3_AUTO_CREATED,
            state=state,
        )

    names = [skill.metadata.name for skill in WorkspaceSkillLoader().load_from_workspace(tmp_path)]
    assert names == ["active-skill", "trial-skill"]
    assert WorkspaceSkillLoader().list_resources(tmp_path, "review-skill") == ()


def test_skill_catalog_surfaces_provisional_trial_without_raw_control_paths(monkeypatch, tmp_path):
    from app.services.agent_context import _load_skills_index
    from app.services.skill_evolution_registry import ORIGIN_T3_AUTO_CREATED, upsert_skill_evolution_entry

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    skill_path = workspace / "skills" / "trial-skill" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: Trial Skill\ndescription: monitored guidance\n---\n# Trial\n",
        encoding="utf-8",
    )
    upsert_skill_evolution_entry(
        workspace,
        skill_name="Trial Skill",
        target_path="skills/trial-skill/SKILL.md",
        skill_origin=ORIGIN_T3_AUTO_CREATED,
        state="provisional",
        last_candidate_id="cand-trial",
        metadata={"trial_path": "evolution/skill_trials/cand-trial/trial.json"},
    )
    monkeypatch.setattr("app.services.agent_context.TOOL_WORKSPACE", tmp_path)
    monkeypatch.setattr("app.services.agent_context.PERSISTENT_DATA", tmp_path)

    catalog = _load_skills_index(agent_id)

    assert "Skills on provisional trial" in catalog
    assert "Trial Skill" in catalog
    assert "evolution/skill_trials" not in catalog
