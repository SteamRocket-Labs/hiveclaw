from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEM_SKILLS_DIR = REPO_ROOT / "backend" / "app" / "templates" / "system_skills"
VALIDATOR_PATH = REPO_ROOT / "backend" / "app" / "services" / "skill_creator_files" / "scripts__quick_validate.py"


def _load_validator():
    spec = spec_from_file_location("quick_validate", VALIDATOR_PATH)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module.validate_skill


def test_system_skill_templates_pass_quick_validation():
    validate_skill = _load_validator()

    failures: list[str] = []
    for skill_dir in sorted(path for path in SYSTEM_SKILLS_DIR.iterdir() if path.is_dir()):
        ok, message = validate_skill(skill_dir)
        if not ok:
            failures.append(f"{skill_dir.name}: {message}")

    assert not failures, "\n".join(failures)


def test_system_skill_templates_reference_supported_runtime_contracts():
    workspace_skill = (SYSTEM_SKILLS_DIR / "workspace-guide" / "SKILL.md").read_text(encoding="utf-8")
    dingtalk_skill = (SYSTEM_SKILLS_DIR / "dingtalk-integration" / "SKILL.md").read_text(encoding="utf-8")
    feishu_skill = (SYSTEM_SKILLS_DIR / "feishu-integration" / "SKILL.md").read_text(encoding="utf-8")
    atlassian_skill = (SYSTEM_SKILLS_DIR / "atlassian-rovo" / "SKILL.md").read_text(encoding="utf-8")

    assert "send_dingtalk_message" not in workspace_skill
    assert "send_dingtalk_message" not in dingtalk_skill
    assert "dingtalk_user_search" not in dingtalk_skill

    assert "atlassian_list_available_tools" not in atlassian_skill
    assert "atlassian_jira_" not in atlassian_skill
    assert "atlassian_confluence_" not in atlassian_skill
    assert "atlassian_compass_" not in atlassian_skill
    assert "atlassian_rovo_" in atlassian_skill

    send_feishu_row = next(
        line for line in feishu_skill.splitlines() if "| `send_feishu_message` |" in line
    )
    assert "`member_name`" in send_feishu_row
    assert "`user_id`" in send_feishu_row
    assert "`open_id`" in send_feishu_row
    assert "`message`" in send_feishu_row
    assert "`email`" not in send_feishu_row
    assert "`content`" not in send_feishu_row
    assert "feishu_base_table_list" in feishu_skill
    assert "feishu_base_record_list" in feishu_skill
    assert "feishu_base_field_list" in feishu_skill
    assert "feishu_base_record_upsert" in feishu_skill
    assert "feishu_base_record_upload_attachment" in feishu_skill
    assert "feishu_sheet_info" in feishu_skill
    assert "feishu_sheet_read" in feishu_skill
    assert "feishu_task_comment" in feishu_skill
    assert "feishu_task_complete" in feishu_skill
    assert "feishu_task_list" in feishu_skill
    assert "feishu_task_create" in feishu_skill
