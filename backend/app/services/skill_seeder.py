"""Seed builtin skills into the global skill registry."""

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
    "docx-generator": "📄",
    "xlsx-processor": "📊",
    "pptx-generator": "🎞️",
    "pdf-generator": "📑",
    "weekly-report-generator": "🗓️",
    "meeting-minutes": "🗒️",
    "pitch-deck-generator": "🎯",
    "industry-research": "🏭",
    "topic-deep-dive": "🔬",
    "source-ledger-audit": "📒",
}


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
                        "is_default": True,
                        "files": files,
                    }
                )
                seen_folders.add(folder_name)
                logger.info(f"[SkillSeeder] Discovered pack skill: {manifest.name}/{folder_name} ({len(files)} files)")

    return skill_dicts


BUILTIN_SKILLS = [
    {
        "name": "Complex Task Executor",
        "description": "Structured methodology for decomposing, planning, and executing complex multi-step tasks with progress tracking",
        "category": "productivity",
        "icon": "🎯",
        "folder_name": "complex-task-executor",
        "is_default": True,
        "files": [
            {
                "path": "SKILL.md",
                "content": """---
name: Complex Task Executor
description: Structured methodology for decomposing, planning, and executing complex multi-step tasks with progress tracking
---

# Complex Task Executor

## When to Use This Skill

Use this skill when a task meets ANY of the following criteria:
- Requires more than 3 distinct steps to complete
- Involves multiple tools or information sources
- Has dependencies between steps (step B needs output from step A)
- Requires research before execution
- Could benefit from a documented plan others can review
- The user explicitly asks for a thorough or systematic approach

**DO NOT use this for simple tasks** like answering a question, reading a single file, or performing one tool call.

## Workflow

### Phase 1: Task Analysis (THINK before acting)

Before creating any files, analyze the task:

1. **Understand the goal**: What is the final deliverable? What does "done" look like?
2. **Assess complexity**: How many steps? What tools are needed?
3. **Identify dependencies**: Which steps depend on others?
4. **Identify risks**: What could go wrong? What information is missing?
5. **Estimate scope**: Is the task feasible with available tools/skills?

### Phase 2: Create Task Plan

Create a task folder and plan file in the workspace:

```
workspace/<task-name>/plan.md
```

The plan.md MUST follow this exact format:

```markdown
# Task: <Clear title>

## Objective
<One-sentence description of the desired outcome>

## Steps

- [ ] 1. <First step — verb-noun format>
  - Details: <What specifically to do>
  - Output: <What this step produces>
- [ ] 2. <Second step>
  - Details: <...>
  - Depends on: Step 1
- [ ] 3. <Third step>
  - Details: <...>

## Status
- Created: <timestamp>
- Current Step: Not started
- Progress: 0/<total>

## Notes
<Any assumptions, risks, or open questions>
```

Rules for writing the plan:
- Each step should be completable in 1-3 tool calls
- Use verb-noun format: "Research competitors", "Draft report", "Validate data"
- Mark dependencies explicitly
- Include expected outputs for each step

### Phase 3: Execute Step-by-Step

For EACH step in the plan:

1. **Read the plan** — Call `read_file` on `workspace/<task>/plan.md` to check current state
2. **Mark as in-progress** — Update the checkbox from `[ ]` to `[/]` and update the "Current Step" field
3. **Execute the step** — Do the actual work (tool calls, analysis, writing)
4. **Record output** — Save results to `workspace/<task>/` (e.g., intermediate files, data)
5. **Mark as complete** — Update the checkbox from `[/]` to `[x]` and update "Progress" counter
6. **Proceed to next step** — Move to the next uncompleted step

### Phase 4: Completion

When all steps are done:
1. Update plan.md status to "✅ Completed"
2. Create a `workspace/<task>/summary.md` with:
   - What was accomplished
   - Key results and deliverables
   - Any follow-up items
3. Present the final result to the user

## Adaptive Replanning

If during execution you discover:
- A step is impossible → Mark it `[!]` with a reason, add alternative steps
- New steps are needed → Add them to the plan with `[+]` prefix
- A step produced unexpected results → Add a note and adjust subsequent steps
- The plan needs major changes → Create a new section "## Revised Plan" and follow it

Always update plan.md BEFORE changing course, so the plan stays the source of truth.

## Error Handling

- If a tool call fails, retry once. If it fails again, mark the step as blocked and note the error.
- Never silently skip a step. Always update the plan to reflect what happened.
- If you're stuck, tell the user what's blocking and ask for guidance.

## Example Scenarios

### Example 1: "Research our top 3 competitors and write a comparison report"

Plan would be:
```
- [ ] 1. Identify the user's company/product context
- [ ] 2. Research Competitor A — website, pricing, features
- [ ] 3. Research Competitor B — website, pricing, features
- [ ] 4. Research Competitor C — website, pricing, features
- [ ] 5. Create comparison matrix
- [ ] 6. Write analysis and recommendations
- [ ] 7. Compile final report
```

### Example 2: "Analyze our Q4 sales data and prepare a board presentation"

Plan would be:
```
- [ ] 1. Read and understand the sales data files
- [ ] 2. Calculate key metrics (revenue, growth, trends)
- [ ] 3. Identify top insights and anomalies
- [ ] 4. Create data summary tables
- [ ] 5. Draft presentation outline
- [ ] 6. Write each presentation section
- [ ] 7. Add executive summary
- [ ] 8. Review and polish final document
```

## Key Principles

1. **Plan is the source of truth** — Always update it before moving on
2. **One step at a time** — Don't skip ahead or batch too many steps
3. **Show your work** — Save intermediate results to the task folder
4. **Communicate progress** — The user can read plan.md at any time to see status
5. **Be adaptive** — Plans change; that's OK if you update the plan first
""",
            },
            {
                "path": "examples/plan_template.md",
                "content": """# Task: [Title]

## Objective
[One-sentence description of the desired outcome]

## Steps

- [ ] 1. [First step]
  - Details: [What specifically to do]
  - Output: [What this step produces]
- [ ] 2. [Second step]
  - Details: [...]
  - Depends on: Step 1
- [ ] 3. [Third step]
  - Details: [...]

## Status
- Created: [timestamp]
- Current Step: Not started
- Progress: 0/3

## Notes
- [Any assumptions, risks, or open questions]
""",
            },
        ],
    },
    # ─── Skill Creator (mandatory default) ─────────
    {
        "name": "Skill Creator",
        "description": "Create new skills, modify and improve existing skills, and measure skill performance",
        "category": "development",
        "icon": "🛠️",
        "folder_name": "skill-creator",
        "is_default": True,
        "files": [],  # populated at runtime from skill_creator_content
    },
    # ─── MCP Tool Installer (mandatory default) ──────────────
    {
        "name": "MCP Tool Installer",
        "description": "Discover, import, inspect, and read MCP integrations directly in chat when adding or operating external tools",
        "category": "development",
        "icon": "🔌",
        "folder_name": "mcp-installer",
        "is_default": True,
        "files": [],  # populated at runtime from agent_template/skills/MCP_INSTALLER.md
    },
    # ─── System operational guides (default — auto-assigned to all agents) ───
    {
        "name": "Workspace Guide",
        "description": "Workspace structure, file operations, and artifact handoff guide",
        "category": "system",
        "icon": "📂",
        "folder_name": "workspace-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Trigger Management Guide",
        "description": "Trigger creation and management guide",
        "category": "system",
        "icon": "⏰",
        "folder_name": "trigger-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Web Research",
        "description": "Web research guide — use core web_search/web_fetch first, then tool_search for advanced search/crawl tools",
        "category": "system",
        "icon": "🌐",
        "folder_name": "web-research",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    # ─── Standard capability skills (default — every pack has a matching skill) ──
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
        "description": "Email send, read, and reply guide — activates send_email / read_emails / reply_email",
        "category": "system",
        "icon": "📧",
        "folder_name": "email-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Memory Guide",
        "description": "Authoritative rules for save/update/retire/search memory — categories, T3 routing, correction paths",
        "category": "system",
        "icon": "🧠",
        "folder_name": "memory-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Messaging Guide",
        "description": "Decision tree for delivering messages, files, images to humans across channels — which transport, when",
        "category": "system",
        "icon": "💬",
        "folder_name": "messaging-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Delegation Guide",
        "description": "Multi-agent delegation decisions, instruction quality, and async task lifecycle",
        "category": "system",
        "icon": "🤝",
        "folder_name": "delegation-guide",
        "is_default": True,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "DingTalk Integration",
        "description": "DingTalk channel conversation behavior guide",
        "category": "system_integration",
        "icon": "💬",
        "folder_name": "dingtalk-integration",
        "is_default": False,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    {
        "name": "Atlassian Rovo",
        "description": "Jira / Confluence / Compass tool integration guide",
        "category": "system_integration",
        "icon": "🔷",
        "folder_name": "atlassian-rovo",
        "is_default": False,
        "files": [],  # populated at runtime from templates/system_skills/
    },
    # ─── Agent behavioral & discovery skills (default, from templates/skills/) ──
    {
        "name": "Find Skills",
        "description": "Discover and install skills from skills.sh and ClawHub with ranking and security vetting",
        "category": "system",
        "icon": "🔍",
        "folder_name": "find-skills",
        "is_default": True,
        "files": [],  # populated at runtime from templates/skills/
    },
    {
        "name": "Skill Vetter",
        "description": "Security review protocol for third-party skills — red flag detection, risk classification, vetting report",
        "category": "system",
        "icon": "🔒",
        "folder_name": "skill-vetter",
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
            mcp_file = _template_skills_dir / "MCP_INSTALLER.md"
            if mcp_file.exists():
                s["files"] = [{"path": "SKILL.md", "content": mcp_file.read_text(encoding="utf-8")}]
            else:
                logger.warning("[SkillSeeder] MCP_INSTALLER.md not found in agent_template/skills/")

        # System operational guides + channel integration skills — load ALL files from templates/system_skills/<folder>/
        elif (
            s["folder_name"]
            in (
                "workspace-guide",
                "trigger-guide",
                "web-research",
                "feishu-integration",
                "plaza-guide",
                "email-guide",
                "dingtalk-integration",
                "atlassian-rovo",
                "memory-guide",
                "messaging-guide",
                "delegation-guide",
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
        elif (
            s["folder_name"]
            in (
                "find-skills",
                "skill-vetter",
                "pdf-generator",
                "docx-generator",
                "xlsx-processor",
                "pptx-generator",
            )
            and not s["files"]
        ):
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

    async with async_session() as db:
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
        removed = remove_retired_builtin_skill_dirs(agent_manager._agent_dir(agent.id))
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


async def push_default_skills_to_existing_agents():
    """Deploy all is_default skills into the workspace of every existing agent that is missing them.

    Called at startup after seed_skills() so existing agents automatically receive new default skills
    like MCP_INSTALLER without requiring manual re-creation.
    """
    from app.models.agent import Agent
    from app.models.skill import Skill
    from sqlalchemy.orm import selectinload
    from app.services.agent_manager import agent_manager

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
        for agent in agents:
            agent_dir = agent_manager._agent_dir(agent.id)
            skills_dir = agent_dir / "skills"
            for skill in default_skills:
                if not skill.files:
                    continue
                skill_folder = skills_dir / skill.folder_name
                skill_folder.mkdir(parents=True, exist_ok=True)
                for sf in skill.files:
                    fp = (skill_folder / sf.path).resolve()
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    if fp.exists():
                        existing_content = fp.read_text(encoding="utf-8")
                        if existing_content == sf.content:
                            continue  # already up-to-date
                        fp.write_text(sf.content, encoding="utf-8")
                        updated += 1
                    else:
                        fp.write_text(sf.content, encoding="utf-8")
                        pushed += 1
                        logger.info(f"[SkillSeeder] Pushed '{skill.name}' to agent {agent.id}")

        if pushed or updated:
            logger.info(f"[SkillSeeder] Pushed {pushed} new + {updated} updated skill files to existing agents")
        else:
            logger.info("[SkillSeeder] All existing agents already have up-to-date default skills")
