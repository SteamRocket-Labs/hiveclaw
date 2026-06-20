from __future__ import annotations

import pytest


def test_install_active_skill_package_writes_guarded_files(tmp_path):
    from app.services.skill_installation import install_active_skill_package

    result = install_active_skill_package(
        workspace=tmp_path,
        folder_name="deploy-checklist",
        files=[
            {"path": "SKILL.md", "content": "---\nname: Deploy Checklist\n---\n# Deploy Checklist\n"},
            {"path": "references/checks.md", "content": "Run health check after deploy.\n"},
        ],
        source="test",
    )

    assert result["status"] == "installed"
    assert result["folder_name"] == "deploy-checklist"
    assert result["files_written"] == 2
    assert (tmp_path / "skills" / "deploy-checklist" / "SKILL.md").exists()
    assert (tmp_path / "skills" / "deploy-checklist" / "references" / "checks.md").exists()
    assert result["skill_guard"]["allowed"] is True


def test_install_active_skill_package_blocks_unsafe_files_without_partial_write(tmp_path):
    from app.services.skill_installation import install_active_skill_package

    with pytest.raises(ValueError, match="SkillGuard blocked"):
        install_active_skill_package(
            workspace=tmp_path,
            folder_name="unsafe",
            files=[
                {"path": "SKILL.md", "content": "---\nname: unsafe\n---\nOPENAI_API_KEY=abcdef123456"},
                {"path": "../escape.md", "content": "escape"},
            ],
            source="test",
        )

    assert not (tmp_path / "skills" / "unsafe").exists()
    assert not (tmp_path / "escape.md").exists()
