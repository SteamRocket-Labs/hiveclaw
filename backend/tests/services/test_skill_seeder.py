from __future__ import annotations

import shutil
from pathlib import Path


def _pack_skill_folders() -> set[str]:
    from app.services.skill_seeder import _load_pack_skill_dicts

    return {skill["folder_name"] for skill in _load_pack_skill_dicts()}


def test_is_seedable_skill_template_file_skips_pycache_and_pyc():
    from app.services.skill_seeder import _is_seedable_skill_template_file

    assert _is_seedable_skill_template_file(Path("scripts/render.py")) is True
    assert _is_seedable_skill_template_file(Path("scripts/__pycache__/render.cpython-313.pyc")) is False
    assert _is_seedable_skill_template_file(Path("scripts/render.cpython-313.pyc")) is False


def test_pack_skill_seeder_exposes_one_office_entrypoint():
    folders = _pack_skill_folders()

    assert "office-productivity" in folders
    assert "docx-generator" not in folders
    assert "xlsx-processor" not in folders
    assert "pptx-generator" not in folders
    assert "pdf-generator" not in folders
    assert "weekly-report-generator" not in folders
    assert "meeting-minutes" not in folders
    assert "pitch-deck-generator" not in folders


def test_remove_retired_builtin_skill_dirs_continues_after_permission_error(tmp_path, monkeypatch):
    from app.services import skill_seeder

    skills_dir = tmp_path / "skills"
    blocked_dir = skills_dir / "meeting-minutes"
    removable_dir = skills_dir / "topic-deep-dive"
    blocked_dir.mkdir(parents=True)
    removable_dir.mkdir()
    (blocked_dir / "SKILL.md").write_text("# blocked", encoding="utf-8")
    (removable_dir / "SKILL.md").write_text("# removable", encoding="utf-8")

    real_rmtree = shutil.rmtree

    def fake_rmtree(path):
        if Path(path).name == "meeting-minutes":
            raise PermissionError("permission denied")
        real_rmtree(path)

    monkeypatch.setattr(skill_seeder.shutil, "rmtree", fake_rmtree)

    removed = skill_seeder.remove_retired_builtin_skill_dirs(
        tmp_path,
        retired_folders={"meeting-minutes", "topic-deep-dive"},
    )

    assert removed == ["topic-deep-dive"]
    assert blocked_dir.exists()
    assert not removable_dir.exists()


def test_remove_legacy_flat_mcp_installer_file(tmp_path):
    from app.services.skill_seeder import remove_legacy_flat_skill_files

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    legacy = skills_dir / "MCP_INSTALLER.md"
    canonical = skills_dir / "mcp-installer" / "SKILL.md"
    canonical.parent.mkdir(parents=True)
    legacy.write_text("# legacy", encoding="utf-8")
    canonical.write_text("# canonical", encoding="utf-8")

    removed = remove_legacy_flat_skill_files(tmp_path)

    assert removed == ["skills/MCP_INSTALLER.md"]
    assert not legacy.exists()
    assert canonical.exists()


def test_remove_legacy_flat_hr_create_employee_files(tmp_path):
    from app.services.skill_seeder import remove_legacy_flat_skill_files

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    legacy = skills_dir / "CREATE_EMPLOYEE.md"
    legacy_resource_dir = skills_dir / "CREATE_EMPLOYEE" / "evals"
    canonical = skills_dir / "create-employee" / "SKILL.md"
    legacy_resource_dir.mkdir(parents=True)
    canonical.parent.mkdir(parents=True)
    legacy.write_text("# legacy", encoding="utf-8")
    (legacy_resource_dir / "eval.yaml").write_text("cases: []\n", encoding="utf-8")
    canonical.write_text("# canonical", encoding="utf-8")

    removed = remove_legacy_flat_skill_files(tmp_path)

    assert removed == ["skills/CREATE_EMPLOYEE.md", "skills/CREATE_EMPLOYEE"]
    assert not legacy.exists()
    assert not (skills_dir / "CREATE_EMPLOYEE").exists()
    assert canonical.exists()
