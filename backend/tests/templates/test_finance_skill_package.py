from __future__ import annotations

from pathlib import Path

import yaml

from app.services.skill_seeder import BUILTIN_SKILLS
from app.skills.parser import SkillParser


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
FINANCE_SYSTEM_SKILL = BACKEND_ROOT / "app" / "templates" / "system_skills" / "finance-research"
FINANCE_PACK = REPO_ROOT / "packs" / "finance_pack"


EXPECTED_FINANCE_TOOLS = {
    "finance_get_provider_status",
    "finance_resolve_entity",
    "finance_get_source_ledger",
    "finance_get_price_history",
    "finance_get_financial_statements",
    "finance_search_filings",
    "finance_get_filing",
    "finance_get_ipo_pipeline",
    "finance_get_funding_rounds",
    "finance_get_company_registry",
    "finance_compute_dcf",
    "finance_build_comps",
    "finance_compile_research_packet",
    "finance_run_workflow",
}


def test_finance_system_skill_is_a_full_package_and_declares_runtime_tools() -> None:
    skill_path = FINANCE_SYSTEM_SKILL / "SKILL.md"
    parsed = SkillParser().parse_file(
        skill_path,
        relative_path="app/templates/system_skills/finance-research/SKILL.md",
    )

    assert parsed.metadata.name == "Finance Research"
    assert parsed.metadata.is_system is True
    assert set(parsed.metadata.declared_tools) == EXPECTED_FINANCE_TOOLS
    assert (FINANCE_SYSTEM_SKILL / "references" / "data-source-boundary.md").is_file()
    assert (FINANCE_SYSTEM_SKILL / "references" / "workflow-playbooks.md").is_file()
    assert (FINANCE_SYSTEM_SKILL / "templates" / "equity-deep-dive.md").is_file()
    assert (FINANCE_SYSTEM_SKILL / "templates" / "ic-memo.md").is_file()
    assert (FINANCE_SYSTEM_SKILL / "evals" / "eval.yaml").is_file()


def test_finance_skill_is_seeded_as_default_builtin_skill() -> None:
    finance_skill = next((skill for skill in BUILTIN_SKILLS if skill["folder_name"] == "finance-research"), None)

    assert finance_skill is not None
    assert finance_skill["is_default"] is True
    assert finance_skill["category"] == "finance"


def test_finance_pack_manifest_skill_paths_exist() -> None:
    manifest = yaml.safe_load((FINANCE_PACK / "pack.yaml").read_text(encoding="utf-8"))

    for skill_ref in manifest["skills"]:
        skill_dir = FINANCE_PACK / skill_ref
        assert (skill_dir / "SKILL.md").is_file(), f"{skill_ref} missing SKILL.md"
        assert (skill_dir / "references").is_dir(), f"{skill_ref} missing references/"
        assert (skill_dir / "templates").is_dir(), f"{skill_ref} missing templates/"
        assert (skill_dir / "evals" / "eval.yaml").is_file(), f"{skill_ref} missing evals/eval.yaml"
