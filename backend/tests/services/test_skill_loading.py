from __future__ import annotations

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


def test_load_skill_sanitizes_managed_channel_env_guidance(tmp_path):
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

    assert "Managed capability credential boundary" in content
    assert "channel config" in content
    assert "FEISHU_APP_ID" not in content
    assert "FEISHU_APP_SECRET" not in content
    assert "env | grep" not in content


def test_read_file_sanitizes_skill_managed_channel_env_guidance(tmp_path):
    from app.services.agent_tool_domains.workspace import _read_file

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "slack"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Slack\nRun `printenv SLACK_BOT_TOKEN` before using Slack.",
        encoding="utf-8",
    )

    content = _read_file(workspace, "skills/slack/SKILL.md")

    assert "Managed capability credential boundary" in content
    assert "SLACK_BOT_TOKEN" not in content
    assert "printenv" not in content


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


def test_save_skill_creates_folder_layout_and_loadable_body(tmp_path):
    from app.services.agent_tool_domains.workspace import _load_skill, _save_skill

    workspace = tmp_path / "agent"
    workspace.mkdir(parents=True)

    result = _save_skill(
        workspace,
        name="Deployment Review",
        description="Review deployment diffs and verify rollback paths.",
        instructions="Check rollout status, verify logs, and confirm rollback steps.",
        declared_tools=("web_search", "web_fetch"),
        declared_packs=("web_pack",),
    )

    skill_path = workspace / "skills" / "deployment-review" / "SKILL.md"
    assert "skills/deployment-review/SKILL.md" in result
    assert skill_path.exists()

    content = skill_path.read_text(encoding="utf-8")
    assert 'name: "Deployment Review"' in content
    assert 'description: "Review deployment diffs and verify rollback paths."' in content
    assert "tools:" in content
    assert "  - web_search" in content
    assert "packs:" in content
    assert "  - web_pack" in content
    assert _load_skill(workspace, "deployment review").startswith("# Deployment Review")
    review_log = (workspace / "evolution" / "skill_review.md").read_text(encoding="utf-8")
    assert "Deployment Review" in review_log


def test_save_skill_updates_existing_skill_when_overwrite_enabled(tmp_path):
    from app.services.agent_tool_domains.workspace import _save_skill

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "research"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\nname: \"Research\"\ndescription: \"Old\"\n---\n# Research\nOld steps.\n",
        encoding="utf-8",
    )

    rejected = _save_skill(
        workspace,
        name="Research",
        description="Updated workflow.",
        instructions="Use primary sources first.",
    )
    assert "already exists" in rejected

    updated = _save_skill(
        workspace,
        name="Research",
        description="Updated workflow.",
        instructions="Use primary sources first.",
        overwrite=True,
    )
    assert "Updated skill" in updated
    content = skill_path.read_text(encoding="utf-8")
    assert 'description: "Updated workflow."' in content
    assert "Use primary sources first." in content
