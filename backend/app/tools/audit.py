"""Tool coverage audit — warn on tools that no pack/skill/prompt references.

Every tool should be discoverable by the LLM through at least one channel:
  (a) membership in a `RUNTIME_TOOL_GROUPS` entry (activated via load_skill side effects)
  (b) `declared_tools` list of a system_skills or skills template SKILL.md
  (c) explicit mention by name inside a prompt section under
      app/runtime/prompt_sections/

A tool that appears in none of the three is a **functional orphan** — it
can be called but the LLM has no signal telling it when to reach for the
tool. New tools will drift into orphan status whenever someone forgets to
register them, which is exactly what this audit catches.

The audit runs once at startup (lifespan), emits a WARNING log listing
orphans, and exposes its findings via `audit_tool_coverage()` so an admin
endpoint can surface them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolAuditReport:
    total_tools: int = 0
    covered_by_pack: frozenset[str] = field(default_factory=frozenset)
    covered_by_skill: frozenset[str] = field(default_factory=frozenset)
    covered_by_prompt: frozenset[str] = field(default_factory=frozenset)
    orphans: frozenset[str] = field(default_factory=frozenset)
    unregistered_system_skill_dirs: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict:
        return {
            "total_tools": self.total_tools,
            "orphans": sorted(self.orphans),
            "unregistered_system_skill_dirs": sorted(self.unregistered_system_skill_dirs),
            "covered_by_pack_only": sorted(self.covered_by_pack - self.covered_by_skill - self.covered_by_prompt),
            "covered_by_skill_only": sorted(self.covered_by_skill - self.covered_by_pack - self.covered_by_prompt),
            "covered_by_prompt_only": sorted(self.covered_by_prompt - self.covered_by_pack - self.covered_by_skill),
        }


def _pack_tools() -> frozenset[str]:
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    tools = {tool for pack in RUNTIME_TOOL_GROUPS for tool in pack.tools}

    # Manifest-backed packs are catalog-side in v1, but they are still valid
    # discovery paths for tool coverage. Keep this independent from
    # app.services.pack_service so the startup audit does not pull in service
    # wiring just to read pack.yaml files.
    try:
        from app.packs.catalog_reader import PackCatalogReader, find_pack_dirs

        for packs_dir in find_pack_dirs(Path(__file__).resolve()):
            reader = PackCatalogReader(packs_dir)
            reader.discover()
            for manifest in reader.list_packs():
                tools.update(manifest.tool_names)
    except Exception as exc:
        logger.debug("[tool-audit] failed to read manifest pack catalog: %s", exc)

    return frozenset(tools)


def _template_skill_declared_tools(backend_root: Path | None = None) -> frozenset[str]:
    """Aggregate declared_tools across every SKILL.md that ships with the backend.

    Includes templates/ (general / system) AND every `*_agent_template/skills/`
    directory so agent-specific skills (e.g. HR agent's hr-guide) are counted
    as live coverage, not mislabelled as orphans.
    """
    from app.skills import SkillParser

    if backend_root is None:
        backend_root = Path(__file__).resolve().parents[2]

    roots: list[Path] = [backend_root / "app" / "templates"]
    for tpl_dir in backend_root.glob("*_agent_template"):
        skills_dir = tpl_dir / "skills"
        if skills_dir.is_dir():
            roots.append(skills_dir)

    parser = SkillParser()
    declared: set[str] = set()
    for root in roots:
        for skill_md in root.rglob("SKILL.md"):
            try:
                parsed = parser.parse_file(skill_md, relative_path=skill_md.as_posix())
            except Exception as exc:
                logger.debug("[tool-audit] failed to parse %s: %s", skill_md, exc)
                continue
            for tool_name in parsed.metadata.declared_tools or ():
                if tool_name:
                    declared.add(tool_name)
    return frozenset(declared)


def _prompt_section_mentions(tool_names: Iterable[str], backend_root: Path | None = None) -> frozenset[str]:
    if backend_root is None:
        backend_root = Path(__file__).resolve().parents[2]
    sections_root = backend_root / "app" / "runtime" / "prompt_sections"
    texts: list[str] = []
    for py_file in sections_root.glob("*.py"):
        try:
            texts.append(py_file.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            logger.debug("[tool-audit] failed to read %s: %s", py_file, exc)
    joined = "\n".join(texts)
    return frozenset(name for name in tool_names if name in joined)


def _unregistered_system_skill_dirs(backend_root: Path | None = None) -> frozenset[str]:
    """SKILL.md files under templates/system_skills/ whose folder is not in
    BUILTIN_SKILLS — they sit on disk but the seeder will never load them
    into the DB, so agents never see them in their skill catalog."""
    if backend_root is None:
        backend_root = Path(__file__).resolve().parents[2]
    sys_skills_root = backend_root / "app" / "templates" / "system_skills"
    if not sys_skills_root.is_dir():
        return frozenset()

    try:
        from app.services.skill_seeder import BUILTIN_SKILLS, RETIRED_BUILTIN_SKILL_FOLDERS
    except Exception as exc:
        logger.debug("[tool-audit] cannot load BUILTIN_SKILLS: %s", exc)
        return frozenset()

    registered = {s["folder_name"] for s in BUILTIN_SKILLS}
    on_disk = {p.parent.name for p in sys_skills_root.glob("*/SKILL.md")}
    return frozenset(on_disk - registered - RETIRED_BUILTIN_SKILL_FOLDERS)


def audit_tool_coverage(*, backend_root: Path | None = None) -> ToolAuditReport:
    """Compute which tools have at least one discovery path; return the full report."""
    from app.tools.collector import collect_tools

    collected = collect_tools()
    all_tools = frozenset(request["function"]["name"] for request in collected.openai_tools)

    covered_pack = _pack_tools() & all_tools
    covered_skill = _template_skill_declared_tools(backend_root) & all_tools
    covered_prompt = _prompt_section_mentions(all_tools, backend_root)

    covered = covered_pack | covered_skill | covered_prompt
    orphans = all_tools - covered

    return ToolAuditReport(
        total_tools=len(all_tools),
        covered_by_pack=covered_pack,
        covered_by_skill=covered_skill,
        covered_by_prompt=covered_prompt,
        orphans=orphans,
        unregistered_system_skill_dirs=_unregistered_system_skill_dirs(backend_root),
    )


def run_startup_audit() -> ToolAuditReport:
    """Run the audit and log a WARNING listing every orphan tool."""
    try:
        report = audit_tool_coverage()
    except Exception as exc:
        logger.warning("[tool-audit] failed to compute coverage: %s", exc)
        return ToolAuditReport()

    if report.orphans:
        logger.warning(
            "[tool-audit] %d/%d tools are functional orphans (no pack, no declared_tools in a skill, no prompt mention): %s",
            len(report.orphans),
            report.total_tools,
            sorted(report.orphans),
        )
    else:
        logger.info(
            "[tool-audit] All %d tools have at least one discovery path (pack / skill / prompt).",
            report.total_tools,
        )
    if report.unregistered_system_skill_dirs:
        logger.warning(
            "[tool-audit] %d SKILL.md folder(s) under templates/system_skills/ are NOT in BUILTIN_SKILLS — "
            "they will never be seeded to the DB nor pushed to agent workspaces: %s",
            len(report.unregistered_system_skill_dirs),
            sorted(report.unregistered_system_skill_dirs),
        )
    return report
