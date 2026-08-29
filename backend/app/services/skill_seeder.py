"""Seed builtin skills into the global skill registry."""

import asyncio
import shutil
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.database import async_session, enter_rls_bypass
from app.models.skill import Skill, SkillFile
from app.skills.retired import RETIRED_BUILTIN_SKILL_FOLDERS


def _is_seedable_skill_template_file(path: Path) -> bool:
    if "__pycache__" in path.parts:
        return False
    return path.suffix != ".pyc"


_PACK_SKILL_ICONS = {
    "office-productivity": "📄",
    "industry-research": "🏭",
    "topic-deep-dive": "🔬",
    "source-ledger-audit": "📒",
}

DEFAULT_BUILTIN_SKILL_FOLDERS = frozenset(
    {
        "skill-creator",
        "mcp-installer",
        "web-research",
        "feishu-integration",
        "plaza-guide",
        "email-guide",
        "dingtalk-integration",
        "skill-marketplace",
    }
)

DEFAULT_PACK_SKILL_FOLDERS = frozenset({"office-productivity"})


def _extract_skill_frontmatter(content: str, *, fallback: str) -> tuple[str, str]:
    """Parse YAML frontmatter from a SKILL.md and return (name, description).

    Best-effort: malformed frontmatter returns (fallback, "") so seeding never
    aborts on a single bad file.
    """
    if not content.startswith("---"):
        return fallback, ""
    end = content.find("\n---", 3)
    if end < 0:
        return fallback, ""
    fm_text = content[4:end].strip()
    try:
        import yaml

        data = yaml.safe_load(fm_text) or {}
    except Exception:
        return fallback, ""
    if not isinstance(data, dict):
        return fallback, ""
    name = str(data.get("name") or fallback)
    description = str(data.get("description") or "")
    return name, description


def _load_pack_skill_dicts() -> list[dict]:
    """Scan backend/packs/*/pack.yaml and yield BUILTIN_SKILLS-shaped dicts.

    Each pack manifest's `skills:` entries are resolved against the pack root.
    Every file under the skill folder is collected so cookbooks, templates,
    scripts, and references all reach the agent workspace alongside SKILL.md.
    """
    from app.packs.catalog_reader import PackCatalogReader, find_pack_dirs

    skill_dicts: list[dict] = []
    seen_folders: set[str] = set()

    for packs_dir in find_pack_dirs(Path(__file__).resolve()):
        reader = PackCatalogReader(packs_dir)
        reader.discover()
        for manifest in reader.list_packs():
            pack_root = packs_dir / manifest.name
            pack_category = manifest.name.replace("_pack", "")
            for skill_rel in manifest.skills:
                skill_dir = pack_root / skill_rel
                if not skill_dir.is_dir():
                    logger.warning(f"[SkillSeeder] Pack skill dir missing: {skill_dir}")
                    continue
                folder_name = skill_dir.name
                if folder_name in seen_folders:
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    logger.warning(f"[SkillSeeder] SKILL.md missing in {skill_dir}")
                    continue

                try:
                    skill_md_content = skill_md.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    logger.warning(f"[SkillSeeder] SKILL.md not UTF-8: {skill_md}")
                    continue
                name, description = _extract_skill_frontmatter(skill_md_content, fallback=folder_name)

                files: list[dict] = []
                for f in skill_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    rel = f.relative_to(skill_dir)
                    if not _is_seedable_skill_template_file(rel):
                        continue
                    try:
                        files.append({"path": str(rel), "content": f.read_text(encoding="utf-8")})
                    except UnicodeDecodeError:
                        logger.warning(f"[SkillSeeder] Skipping binary pack file: {rel}")

                if not files:
                    continue

                skill_dicts.append(
                    {
                        "name": name,
                        "description": description,
                        "category": pack_category,
                        "icon": _PACK_SKILL_ICONS.get(folder_name, "🧠"),
                        "folder_name": folder_name,
                        "is_default": folder_name in DEFAULT_PACK_SKILL_FOLDERS,
                        "files": files,
                    }
                )
                seen_folders.add(folder_name)
                logger.info(f"[SkillSeeder] Discovered pack skill: {manifest.name}/{folder_name} ({len(files)} files)")

    return skill_dicts


BUILTIN_SKILLS = [
    # ─── Agent-preinstalled platform skill capsules ─────────
    {
        "name": "Skill Creator",
        "description": "Create new skills, modify and improve existing skills, and measure skill performance",
        "category": "development",
        "icon": "🛠️",
        "folder_name": "skill-creator",
        "is_default": True,
        "files": [],  # populated at runtime from skill_creator_content
    },
    # ─── Optional capability playbooks ──────────────
    {
        "name": "MCP Tool Installer",
        "description": "Discover, import, inspect, and read MCP integrations directly in chat when adding or operating external tools",
        "category": "development",
        "icon": "🔌",
        "folder_name": "mcp-installer",
        "is_default": True,
        "files": [],  # populated at runtime from agent_template/skills/mcp-installer/SKILL.md
    },
    {
        "name": "Advanced Web Research",
        "description": "Advanced web research playbook for escalating beyond core web_search/web_fetch into vertical search, source extraction, and crawler tools",
        "category": "system",
        "icon": "🌐",
        "folder_name": "web-research",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Feishu Integration",
        "description": "Feishu/Lark messaging, docs, wiki, sheets, base, approvals, tasks, and calendar guide",
        "category": "system_integration",
        "icon": "🐦",
        "folder_name": "feishu-integration",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Plaza Guide",
        "description": "Agent Plaza shared feed — browsing, posting, and commenting guide",
        "category": "system",
        "icon": "📢",
        "folder_name": "plaza-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Email Guide",
        "description": "Email send, read, and reply playbook for configured email tools",
        "category": "system",
        "icon": "📧",
        "folder_name": "email-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "DingTalk Integration",
        "description": "DingTalk channel conversation behavior guide",
        "category": "system_integration",
        "icon": "💬",
        "folder_name": "dingtalk-integration",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Skill Marketplace Vetting",
        "description": "Discover, vet, and install third-party skills only after source review, risk classification, and explicit user confirmation",
        "category": "system",
        "icon": "🔍",
        "folder_name": "skill-marketplace",
        "is_default": True,
        "files": [],  # populated at runtime from templates/skills/
    },
    # Office document skills (pdf/docx/xlsx/pptx) are now sourced from
    # backend/packs/office_pack/skills/ via _load_pack_skill_dicts() — the
    # pack version ships with cookbooks, scripts, and templates that the
    # earlier templates/skills/ stubs lacked.
]


async def seed_skills():
    """Insert builtin skills if they don't exist."""
    from app.services.skill_creator_content import get_skill_creator_files

    _template_skills_dir = Path(__file__).parent.parent.parent / "agent_template" / "skills"

    # Populate skill-creator files at runtime
    for s in BUILTIN_SKILLS:
        if s["folder_name"] == "skill-creator" and not s["files"]:
            s["files"] = get_skill_creator_files()
        elif s["folder_name"] == "mcp-installer" and not s["files"]:
            mcp_file = _template_skills_dir / "mcp-installer" / "SKILL.md"
            if mcp_file.exists():
                s["files"] = [{"path": "SKILL.md", "content": mcp_file.read_text(encoding="utf-8")}]
            else:
                logger.warning("[SkillSeeder] mcp-installer/SKILL.md not found in agent_template/skills/")

        # System/integration skills — load ALL files from templates/system_skills/<folder>/
        elif (
            s["folder_name"]
            in (
                "web-research",
                "feishu-integration",
                "plaza-guide",
                "email-guide",
                "dingtalk-integration",
            )
            and not s["files"]
        ):
            _sys_skills_dir = Path(__file__).parent.parent / "templates" / "system_skills"
            skill_dir = _sys_skills_dir / s["folder_name"]
            if skill_dir.exists():
                files = []
                for f in skill_dir.rglob("*"):
                    if f.is_file():
                        if not _is_seedable_skill_template_file(f.relative_to(skill_dir)):
                            continue
                        rel = str(f.relative_to(skill_dir))
                        try:
                            files.append({"path": rel, "content": f.read_text(encoding="utf-8")})
                        except UnicodeDecodeError:
                            logger.warning(f"[SkillSeeder] Skipping binary file: {rel}")
                s["files"] = files
            else:
                logger.warning(f"[SkillSeeder] {s['folder_name']}/ not found in templates/system_skills/")

        # Agent behavioral & discovery skills — load ALL files from templates/skills/<folder>/
        elif s["folder_name"] in ("skill-marketplace",) and not s["files"]:
            _agent_skills_dir = Path(__file__).parent.parent / "templates" / "skills"
            skill_dir = _agent_skills_dir / s["folder_name"]
            if skill_dir.exists():
                files = []
                for f in skill_dir.rglob("*"):
                    if f.is_file():
                        if not _is_seedable_skill_template_file(f.relative_to(skill_dir)):
                            continue
                        rel = str(f.relative_to(skill_dir))
                        try:
                            files.append({"path": rel, "content": f.read_text(encoding="utf-8")})
                        except UnicodeDecodeError:
                            logger.warning(f"[SkillSeeder] Skipping binary file: {rel}")
                s["files"] = files
                if not files:
                    logger.warning(f"[SkillSeeder] {s['folder_name']}/ is empty in templates/skills/")
            else:
                logger.warning(f"[SkillSeeder] {s['folder_name']}/ not found in templates/skills/")

    pack_skill_dicts = _load_pack_skill_dicts()
    all_skill_dicts = BUILTIN_SKILLS + pack_skill_dicts

    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="startup builtin skill registry seed") as bypass_db,
    ):
        db = bypass_db
        for skill_data in all_skill_dicts:
            result = await db.execute(
                select(Skill).where(
                    Skill.folder_name == skill_data["folder_name"],
                    Skill.tenant_id.is_(None),
                )
            )
            existing = result.scalar_one_or_none()
            is_default = skill_data.get("is_default", False)
            if existing:
                # Update metadata
                existing.name = skill_data["name"]
                existing.description = skill_data["description"]
                existing.category = skill_data["category"]
                existing.icon = skill_data["icon"]
                existing.is_default = is_default
                # Sync files — add missing ones
                from sqlalchemy.orm import selectinload

                res2 = await db.execute(select(Skill).where(Skill.id == existing.id).options(selectinload(Skill.files)))
                sk = res2.scalar_one()
                existing_paths = {f.path: f for f in sk.files}
                for f in skill_data["files"]:
                    if f["path"] in existing_paths:
                        # Update content if changed
                        existing_file = existing_paths[f["path"]]
                        if existing_file.content != f["content"]:
                            existing_file.content = f["content"]
                            logger.info(f"[SkillSeeder] Updated {f['path']} in {skill_data['name']}")
                    else:
                        db.add(SkillFile(skill_id=existing.id, path=f["path"], content=f["content"]))
                        logger.info(f"[SkillSeeder] Added file {f['path']} to {skill_data['name']}")
            else:
                skill = Skill(
                    name=skill_data["name"],
                    description=skill_data["description"],
                    category=skill_data["category"],
                    icon=skill_data["icon"],
                    folder_name=skill_data["folder_name"],
                    is_builtin=True,
                    is_default=is_default,
                )
                db.add(skill)
                await db.flush()
                for f in skill_data["files"]:
                    db.add(SkillFile(skill_id=skill.id, path=f["path"], content=f["content"]))
                logger.info(f"[SkillSeeder] Created skill: {skill_data['name']}")
        await db.commit()
        logger.info("[SkillSeeder] Skills seeded")


def remove_retired_builtin_skill_dirs(
    agent_dir: Path,
    retired_folders: set[str] | frozenset[str] = RETIRED_BUILTIN_SKILL_FOLDERS,
) -> list[str]:
    """Remove retired builtin skill folders from an agent workspace."""
    skills_dir = agent_dir / "skills"
    if not skills_dir.exists():
        return []

    removed: list[str] = []
    for folder_name in sorted(retired_folders):
        target = skills_dir / folder_name
        if not target.exists():
            continue
        try:
            shutil.rmtree(target)
        except OSError as exc:
            logger.warning("[SkillSeeder] Failed to remove retired skill folder {}: {}", target, exc)
            continue
        removed.append(folder_name)
    return removed


def remove_legacy_flat_skill_files(agent_dir: Path) -> list[str]:
    """Remove known legacy flat system skill files after canonical folder skills exist."""
    skills_dir = agent_dir / "skills"
    if not skills_dir.exists():
        return []

    legacy_to_canonical = {
        "MCP_INSTALLER.md": skills_dir / "mcp-installer" / "SKILL.md",
        "CREATE_EMPLOYEE.md": skills_dir / "create-employee" / "SKILL.md",
    }
    removed: list[str] = []
    for legacy_name, canonical_path in legacy_to_canonical.items():
        legacy_path = skills_dir / legacy_name
        if not legacy_path.is_file() or not canonical_path.is_file():
            continue
        try:
            legacy_path.unlink()
        except OSError as exc:
            logger.warning("[SkillSeeder] Failed to remove legacy flat skill file {}: {}", legacy_path, exc)
            continue
        removed.append(legacy_path.relative_to(agent_dir).as_posix())
    legacy_resource_dirs = {
        "CREATE_EMPLOYEE": skills_dir / "create-employee" / "SKILL.md",
    }
    for legacy_dir_name, canonical_path in legacy_resource_dirs.items():
        legacy_dir = skills_dir / legacy_dir_name
        if not legacy_dir.is_dir() or not canonical_path.is_file():
            continue
        try:
            shutil.rmtree(legacy_dir)
        except OSError as exc:
            logger.warning("[SkillSeeder] Failed to remove legacy skill resource dir {}: {}", legacy_dir, exc)
            continue
        removed.append(legacy_dir.relative_to(agent_dir).as_posix())
    return removed


async def cleanup_retired_builtin_skills() -> dict:
    """Delete retired builtin skills from DB and existing agent workspaces."""
    from app.models.agent import Agent
    from app.services.agent_manager import agent_manager

    removed_skill_rows: list[str] = []
    cleaned_agent_dirs: dict[str, list[str]] = {}

    # Startup maintenance: scrubs retired builtin skills from every agent across
    # all tenants — the unfiltered Agent scan needs cross-tenant visibility, so
    # it runs under an explicit audited bypass.
    async with (
        async_session() as db,
        enter_rls_bypass(
            db, reason="startup maintenance: remove retired builtin skills across all agent workspaces"
        ) as bdb,
    ):
        db = bdb
        result = await db.execute(
            select(Skill).where(
                Skill.is_builtin == True,  # noqa: E712
                Skill.folder_name.in_(sorted(RETIRED_BUILTIN_SKILL_FOLDERS)),
            )
        )
        retired_skills = result.scalars().all()

        for skill in retired_skills:
            if skill.folder_name not in RETIRED_BUILTIN_SKILL_FOLDERS:
                continue
            removed_skill_rows.append(skill.folder_name)
            await db.delete(skill)

        agents_result = await db.execute(select(Agent))
        agents = agents_result.scalars().all()

        if retired_skills:
            await db.commit()

    for agent in agents:
        agent_dir = agent_manager._agent_dir(agent.id)
        removed = await asyncio.to_thread(remove_retired_builtin_skill_dirs, agent_dir)
        removed.extend(await asyncio.to_thread(remove_legacy_flat_skill_files, agent_dir))
        if removed:
            cleaned_agent_dirs[str(agent.id)] = removed

    if removed_skill_rows:
        logger.info(
            "[SkillSeeder] Removed retired builtin skills: {}",
            ", ".join(sorted(set(removed_skill_rows))),
        )
    if cleaned_agent_dirs:
        logger.info(
            "[SkillSeeder] Cleaned retired skill folders from {} agents",
            len(cleaned_agent_dirs),
        )

    return {
        "deleted_skills": sorted(set(removed_skill_rows)),
        "cleaned_agent_dirs": cleaned_agent_dirs,
    }


def _push_default_skill_packages_to_agent(*, agent_dir: Path, default_skills: list[Skill]) -> dict[str, int]:
    """Install one Agent's default Skills under one lock and one optional transaction."""

    from app.services.agent_asset_transaction import AgentAssetTransaction
    from app.services.skill_installation import install_active_skill_package

    result_counts = {"pushed": 0, "updated": 0, "unchanged": 0}
    with AgentAssetTransaction(
        agent_dir,
        operation="startup_default_registry_skill_batch",
        evidence_refs=("startup-default-skill-registry",),
    ) as transaction:
        for skill in default_skills:
            if not skill.files:
                continue
            existing_paths = {
                sf.path for sf in skill.files if (agent_dir / "skills" / skill.folder_name / sf.path).is_file()
            }
            install_result = install_active_skill_package(
                workspace=agent_dir,
                folder_name=skill.folder_name,
                files=[{"path": sf.path, "content": sf.content} for sf in skill.files],
                source=f"startup_default_registry_skill:{skill.id}",
                overwrite=True,
                transaction=transaction,
            )
            if install_result["status"] == "unchanged":
                result_counts["unchanged"] += 1
            elif any(path not in existing_paths for path in install_result["files"]):
                result_counts["pushed"] += 1
            else:
                result_counts["updated"] += 1
        if transaction.has_changes:
            transaction.commit()
    return result_counts


async def push_default_skills_to_existing_agents():
    """Deploy all is_default skills into the workspace of every existing agent that is missing them.

    Called at startup after seed_skills() so existing agents automatically receive new default skills
    like MCP_INSTALLER without requiring manual re-creation.
    """
    from app.models.agent import Agent
    from app.models.skill import Skill
    from app.services.agent_manager import agent_manager
    from sqlalchemy.orm import selectinload

    # Startup: pushes default skills into every agent across all tenants — the
    # unfiltered Agent scan needs cross-tenant visibility, so run under an
    # explicit audited bypass.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="startup: push default skills to every existing agent across tenants") as bdb,
    ):
        db = bdb
        # Load all is_default skills with their files
        default_skills_r = await db.execute(
            select(Skill).where(Skill.is_default, Skill.tenant_id.is_(None)).options(selectinload(Skill.files))
        )
        default_skills = default_skills_r.scalars().all()
        if not default_skills:
            return

        # Load all agents
        agents_r = await db.execute(select(Agent))
        agents = agents_r.scalars().all()

        pushed = 0
        updated = 0
        unchanged = 0
        for agent in agents:
            agent_dir = agent_manager._agent_dir(agent.id)
            install_counts = await asyncio.to_thread(
                _push_default_skill_packages_to_agent,
                agent_dir=agent_dir,
                default_skills=default_skills,
            )
            pushed += install_counts["pushed"]
            updated += install_counts["updated"]
            unchanged += install_counts["unchanged"]
            legacy_removed = await asyncio.to_thread(remove_legacy_flat_skill_files, agent_dir)
            if legacy_removed:
                updated += len(legacy_removed)
                logger.info(
                    "[SkillSeeder] Removed legacy flat skill files for agent {}: {}",
                    agent.id,
                    ", ".join(legacy_removed),
                )

        if pushed or updated:
            logger.info(
                "[SkillSeeder] Default Skill startup result: {} pushed, {} updated, {} unchanged",
                pushed,
                updated,
                unchanged,
            )
        else:
            logger.info(
                "[SkillSeeder] All existing agents already have up-to-date default skills ({} unchanged)",
                unchanged,
            )
