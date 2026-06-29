from __future__ import annotations

from pathlib import Path
import re

import yaml

from app.skills.parser import SkillParser
from app.tools.collector import collect_tools


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SKILL_ROOT = BACKEND_ROOT / "app" / "templates" / "skills"
SYSTEM_SKILL_ROOT = BACKEND_ROOT / "app" / "templates" / "system_skills"
PACK_ROOTS = (REPO_ROOT / "packs", BACKEND_ROOT / "packs")
OFFICE_PACK_SKILL = BACKEND_ROOT / "packs" / "office_pack" / "skills" / "office-productivity"
RETIRED_OFFICE_TEMPLATE_SKILLS = (
    "docx-generator",
    "xlsx-processor",
    "pptx-generator",
    "pdf-generator",
    "weekly-report-generator",
    "meeting-minutes",
    "pitch-deck-generator",
)


def _assert_full_skill_package(skill_dir: Path) -> None:
    assert (skill_dir / "SKILL.md").is_file(), f"{skill_dir.name} missing SKILL.md"
    assert (skill_dir / "references").is_dir(), f"{skill_dir.name} missing references/"
    assert (skill_dir / "templates").is_dir(), f"{skill_dir.name} missing templates/"
    assert (skill_dir / "evals" / "eval.yaml").is_file(), f"{skill_dir.name} missing evals/eval.yaml"


def test_office_single_purpose_app_templates_are_retired():
    for skill_name in RETIRED_OFFICE_TEMPLATE_SKILLS:
        assert not (SKILL_ROOT / skill_name).exists(), f"{skill_name} should live behind office-productivity pack"


def test_office_productivity_pack_is_the_single_office_skill_entrypoint():
    _assert_full_skill_package(OFFICE_PACK_SKILL)


def test_all_builtin_template_skills_are_full_packages():
    for skill_dir in sorted(SKILL_ROOT.iterdir()):
        if skill_dir.is_dir():
            _assert_full_skill_package(skill_dir)


def test_all_system_template_skills_are_full_packages():
    for skill_dir in sorted(SYSTEM_SKILL_ROOT.iterdir()):
        if skill_dir.is_dir():
            _assert_full_skill_package(skill_dir)


def _all_package_dirs() -> list[Path]:
    roots = [SKILL_ROOT, SYSTEM_SKILL_ROOT]
    for pack_root in PACK_ROOTS:
        if not pack_root.is_dir():
            continue
        roots.extend(
            pack_dir / "skills"
            for pack_dir in sorted(pack_root.iterdir())
            if (pack_dir / "skills").is_dir()
        )
    return [skill_dir for root in roots for skill_dir in sorted(root.iterdir()) if skill_dir.is_dir()]


def _registered_tool_names() -> set[str]:
    return {tool["function"]["name"] for tool in collect_tools().openai_tools}


def test_all_package_declared_and_eval_tools_are_registered():
    parser = SkillParser()
    registered_tools = _registered_tool_names()
    failures: list[str] = []

    for skill_dir in _all_package_dirs():
        skill_path = skill_dir / "SKILL.md"
        parsed = parser.parse_file(skill_path, relative_path=skill_path.relative_to(REPO_ROOT).as_posix())
        for tool_name in parsed.metadata.declared_tools:
            if tool_name not in registered_tools:
                failures.append(f"{skill_path.relative_to(REPO_ROOT)} declares unknown tool {tool_name}")

        eval_path = skill_dir / "evals" / "eval.yaml"
        eval_doc = yaml.safe_load(eval_path.read_text(encoding="utf-8")) or {}
        for case in eval_doc.get("cases", []) or []:
            for tool_name in case.get("expected_tools", []) or []:
                if tool_name not in registered_tools:
                    failures.append(f"{eval_path.relative_to(REPO_ROOT)} expects unknown tool {tool_name}")

    assert not failures, "\n".join(failures)


def test_package_resource_files_do_not_reference_unknown_tool_like_names():
    registered_tools = _registered_tool_names()
    tool_like_prefixes = (
        "feishu_",
        "plaza_",
        "web_",
        "firecrawl_",
        "xcrawl_",
        "send_",
        "read_",
        "write_",
        "edit_",
        "list_",
        "delete_",
        "run_",
        "execute_",
        "save_",
        "search_",
        "set_",
        "update_",
        "cancel_",
        "load_",
        "tool_",
        "upload_",
        "glob_",
        "grep_",
        "delegate_",
        "check_",
        "import_",
        "discover_",
        "create_",
        "preview_",
        "fs_",
    )
    dynamic_prefixes: tuple[str, ...] = ()
    non_tool_identifiers = {
        "allowed_by_tool_config",
        "build_peer_multiple_table",
        "compute_explicit_dcf",
        "create_doc_and_share",
        "current_company_fact",
        "current_thread_reminder",
        "delivery_plan",
        "do_not_recommend",
        "email_draft",
        "expected_artifacts",
        "expected_behavior",
        "expected_tools",
        "feishu_action_plan",
        "generate_ic_memo_from_workflow",
        "inspect_before_duplicate",
        "inspect_then_edit",
        "install_with_warning",
        "known_url_fetch",
        "memory_entry",
        "monitor_cross_market_ipo_pipeline",
        "needs_user_confirmation",
        "paid_optional",
        "plaza_post",
        "public_default",
        "publish_validated_finding",
        "read_jira_issue",
        "read_sheet",
        "recommend",
        "refuse",
        "reject_arbitrary_dm",
        "review_portfolio_holdings",
        "risk_level",
        "route_agent_message_to_delegation",
        "run_listed_company_research",
        "run_private_company_diligence",
        "safe_to_install",
        "save_explicit_preference",
        "schedule_one_time_reminder",
        "send_file_to_current_requester",
        "skill_recommendation",
        "skill_security_review",
        "source_ledger",
        "stop_when_rovo_missing",
        "tool_choice",
        "workspace_artifact_plan",
    }
    backtick_identifier = re.compile(r"`([a-z][a-z0-9_]*[a-z0-9])`")
    failures: list[str] = []

    for skill_dir in _all_package_dirs():
        for path in skill_dir.rglob("*"):
            if not path.is_file() or path.suffix not in {".md", ".yaml", ".yml"}:
                continue
            for name in sorted(set(backtick_identifier.findall(path.read_text(encoding="utf-8")))):
                if name in registered_tools or name in non_tool_identifiers or name.startswith(dynamic_prefixes):
                    continue
                if name.startswith(tool_like_prefixes):
                    failures.append(f"{path.relative_to(REPO_ROOT)} references unknown tool-like `{name}`")

    assert not failures, "\n".join(failures)
