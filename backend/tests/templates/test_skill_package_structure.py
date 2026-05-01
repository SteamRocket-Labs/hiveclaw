from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = BACKEND_ROOT / "app" / "templates" / "skills"


def test_core_office_skills_have_package_resources():
    for skill_name in ("docx-generator", "xlsx-processor", "pptx-generator", "pdf-generator"):
        skill_dir = SKILL_ROOT / skill_name
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "references").is_dir(), f"{skill_name} missing references/"
        assert (skill_dir / "templates").is_dir(), f"{skill_name} missing templates/"
        assert (skill_dir / "evals" / "eval.yaml").is_file(), f"{skill_name} missing evals/eval.yaml"


def test_office_sop_skills_exist_as_packages():
    for skill_name in ("weekly-report-generator", "meeting-minutes", "pitch-deck-generator"):
        skill_dir = SKILL_ROOT / skill_name
        assert (skill_dir / "SKILL.md").is_file(), f"{skill_name} missing SKILL.md"
        assert (skill_dir / "references").is_dir(), f"{skill_name} missing references/"
        assert (skill_dir / "templates").is_dir(), f"{skill_name} missing templates/"
        assert (skill_dir / "evals" / "eval.yaml").is_file(), f"{skill_name} missing evals/eval.yaml"
