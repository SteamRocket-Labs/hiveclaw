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


def test_pack_skill_seeder_exposes_one_deep_research_entrypoint():
    folders = _pack_skill_folders()

    assert "deep-research" in folders
    assert "topic-deep-dive" not in folders
    assert "industry-research" not in folders
    assert "source-ledger-audit" not in folders


def test_deep_research_skill_declares_only_dedicated_tools():
    from app.skills.parser import SkillParser

    skill_path = Path(__file__).resolve().parents[2] / "packs" / "deep_research_pack" / "skills" / "deep-research" / "SKILL.md"
    parsed = SkillParser().parse_file(
        skill_path,
        relative_path="skills/deep-research/SKILL.md",
        default_name="deep-research",
    )

    assert set(parsed.metadata.declared_tools) == {
        "deep_research_run",
        "deep_research_start",
        "deep_research_check",
        "deep_research_cancel",
        "deep_research_export",
    }
    assert "web_search" not in parsed.metadata.declared_tools
    assert "web_fetch" not in parsed.metadata.declared_tools


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


def test_finance_skills_are_retired_until_runtime_is_real():
    from app.services.skill_seeder import BUILTIN_SKILLS, RETIRED_BUILTIN_SKILL_FOLDERS

    folders = _pack_skill_folders()
    builtin_folders = {skill["folder_name"] for skill in BUILTIN_SKILLS}

    assert "finance-research" not in builtin_folders
    assert "finance-research" not in folders
    assert "secondary-equity-deep-dive" not in folders
    assert "dcf-valuation" not in folders
    assert "comps-valuation" not in folders
    assert "ipo-pipeline-monitor" not in folders
    assert "primary-market-due-diligence" not in folders
    assert "portfolio-risk-review" not in folders
    assert "ic-memo-generator" not in folders
    assert {
        "finance-research",
        "secondary-equity-deep-dive",
        "dcf-valuation",
        "comps-valuation",
        "ipo-pipeline-monitor",
        "primary-market-due-diligence",
        "portfolio-risk-review",
        "ic-memo-generator",
    } <= RETIRED_BUILTIN_SKILL_FOLDERS


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
