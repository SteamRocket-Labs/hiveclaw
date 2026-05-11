from __future__ import annotations

from pathlib import Path
import importlib.util
import re

import yaml


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
SKILL_ROOTS = (
    BACKEND_ROOT / "app" / "templates" / "skills",
    BACKEND_ROOT / "app" / "templates" / "system_skills",
    BACKEND_ROOT / "packs",
    REPO_ROOT / "packs",
    BACKEND_ROOT / "hr_agent_template" / "skills",
)

HIVE_FRONTMATTER_EXTENSIONS = {
    "allowed-tools",
    "compatibility",
    "is_default",
    "is_system",
    "license",
    "metadata",
    "packs",
    "tools",
}
CORE_FRONTMATTER = {"name", "description"}
RESOURCE_AUX_DOC_NAMES = {"README.md", "INSTALLATION_GUIDE.md", "QUICK_REFERENCE.md", "CHANGELOG.md"}
WORKFLOW_MARKERS = (
    "## Workflow",
    "## End-to-End Workflow",
    "## Mode Selection",
    "## Routing",
    "## Operating Procedure",
    "## Steps",
    "## Hiring Workflow",
)


def _skill_files() -> list[Path]:
    paths: list[Path] = []
    for root in SKILL_ROOTS:
        if not root.exists():
            continue
        if root.name == "skills":
            paths.extend(sorted(root.glob("*/SKILL.md")))
            paths.extend(sorted(root.glob("*.md")))
            continue
        paths.extend(sorted(root.glob("*/skills/*/SKILL.md")))
    return sorted(set(paths))


def _split_frontmatter(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    assert match, f"{path.relative_to(REPO_ROOT)} missing YAML frontmatter"
    frontmatter = yaml.safe_load(match.group(1)) or {}
    assert isinstance(frontmatter, dict), f"{path.relative_to(REPO_ROOT)} frontmatter must be a mapping"
    return frontmatter, match.group(2)


def test_all_skill_templates_meet_skill_creator_quality_bar() -> None:
    failures: list[str] = []

    for skill_path in _skill_files():
        rel = skill_path.relative_to(REPO_ROOT)
        skill_dir = skill_path.parent
        try:
            frontmatter, body = _split_frontmatter(skill_path)
        except AssertionError as exc:
            failures.append(str(exc))
            continue

        allowed_keys = CORE_FRONTMATTER | HIVE_FRONTMATTER_EXTENSIONS
        unexpected = sorted(set(frontmatter) - allowed_keys)
        if unexpected:
            failures.append(f"{rel}: unexpected frontmatter keys {unexpected}")

        name = str(frontmatter.get("name") or "").strip()
        description = str(frontmatter.get("description") or "").strip()
        if not name:
            failures.append(f"{rel}: missing name")
        if not description:
            failures.append(f"{rel}: missing description")
        elif len(description.split()) < 18:
            failures.append(f"{rel}: description is too thin for reliable triggering")
        elif not re.search(r"\b(use when|when|for|asks?|needs?|create|generate|audit|research|manage)\b", description, re.I):
            failures.append(f"{rel}: description lacks trigger context")

        if len(body.splitlines()) < 30:
            failures.append(f"{rel}: body is too thin to guide execution")
        if not any(marker in body for marker in WORKFLOW_MARKERS):
            failures.append(f"{rel}: missing executable workflow section")

        references = sorted((skill_dir / "references").glob("*")) if (skill_dir / "references").is_dir() else []
        templates = sorted((skill_dir / "templates").glob("*")) if (skill_dir / "templates").is_dir() else []
        scripts = sorted((skill_dir / "scripts").glob("*")) if (skill_dir / "scripts").is_dir() else []
        resources = [p for p in references + templates + scripts if p.is_file()]
        aux_docs = [p.name for p in resources if p.name in RESOURCE_AUX_DOC_NAMES]
        if aux_docs:
            failures.append(f"{rel}: remove auxiliary resource docs {aux_docs}")

        if references and "references/" not in body and not any(p.name in body for p in references if p.is_file()):
            failures.append(f"{rel}: references are not discoverable from SKILL.md")
        if templates and "templates/" not in body and not any(p.name in body for p in templates if p.is_file()):
            failures.append(f"{rel}: templates are not discoverable from SKILL.md")
        if scripts and "scripts/" not in body and not any(p.name in body for p in scripts if p.is_file()):
            failures.append(f"{rel}: scripts are not discoverable from SKILL.md")
        if scripts and not re.search(r"\b(test|validate|run)\b", body, re.I):
            failures.append(f"{rel}: scripts are not paired with validation instructions")

        eval_path = skill_dir / "evals" / "eval.yaml"
        if skill_path.name != "SKILL.md":
            eval_path = skill_path.with_suffix("") / "evals" / "eval.yaml"
        if eval_path.exists():
            eval_doc = yaml.safe_load(eval_path.read_text(encoding="utf-8")) or {}
            cases = eval_doc.get("cases") or []
            if len(cases) < 2:
                failures.append(f"{eval_path.relative_to(REPO_ROOT)}: needs at least two eval cases")
            for case in cases:
                if not (
                    case.get("expected_artifacts")
                    or case.get("expected_behavior")
                    or case.get("assertions")
                    or case.get("expected_output")
                ):
                    failures.append(
                        f"{eval_path.relative_to(REPO_ROOT)}:{case.get('name', '<unnamed>')} lacks artifact/assertion expectation"
                    )
        else:
            failures.append(f"{rel}: missing evals/eval.yaml")

    assert not failures, "\n".join(failures)


def test_all_skill_directories_pass_hive_quick_validate() -> None:
    validator_path = BACKEND_ROOT / "app" / "services" / "skill_creator_files" / "scripts__quick_validate.py"
    spec = importlib.util.spec_from_file_location("hive_skill_quick_validate", validator_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    failures: list[str] = []
    for skill_path in _skill_files():
        if skill_path.name != "SKILL.md":
            continue
        ok, message = module.validate_skill(skill_path.parent)
        if not ok:
            failures.append(f"{skill_path.parent.relative_to(REPO_ROOT)}: {message}")

    assert not failures, "\n".join(failures)
