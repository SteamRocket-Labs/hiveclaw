from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = BACKEND_ROOT / "app" / "templates" / "skills"
SYSTEM_SKILL_ROOT = BACKEND_ROOT / "app" / "templates" / "system_skills"


def _assert_full_skill_package(skill_dir: Path) -> None:
    assert (skill_dir / "SKILL.md").is_file(), f"{skill_dir.name} missing SKILL.md"
    assert (skill_dir / "references").is_dir(), f"{skill_dir.name} missing references/"
    assert (skill_dir / "templates").is_dir(), f"{skill_dir.name} missing templates/"
    assert (skill_dir / "evals" / "eval.yaml").is_file(), f"{skill_dir.name} missing evals/eval.yaml"


def test_core_office_skills_have_package_resources():
    for skill_name in ("docx-generator", "xlsx-processor", "pptx-generator", "pdf-generator"):
        _assert_full_skill_package(SKILL_ROOT / skill_name)


def test_office_sop_skills_exist_as_packages():
    for skill_name in ("weekly-report-generator", "meeting-minutes", "pitch-deck-generator"):
        _assert_full_skill_package(SKILL_ROOT / skill_name)


def test_all_builtin_template_skills_are_full_packages():
    for skill_dir in sorted(SKILL_ROOT.iterdir()):
        if skill_dir.is_dir():
            _assert_full_skill_package(skill_dir)


def test_all_system_template_skills_are_full_packages():
    for skill_dir in sorted(SYSTEM_SKILL_ROOT.iterdir()):
        if skill_dir.is_dir():
            _assert_full_skill_package(skill_dir)
