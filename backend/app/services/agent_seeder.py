"""Seed default agents (Morty & Meeseeks) on first platform startup."""

from pathlib import Path

from loguru import logger

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session, enter_rls_bypass
from app.models.agent import Agent, AgentPermission
from app.models.org import AgentAgentRelationship
from app.models.skill import Skill
from app.models.tool import Tool
from app.models.user import User
from app.config import get_settings
from app.services.agent_identity_lifecycle import ensure_agent_identity
from app.services.agent_tool_assignment_service import ensure_agent_tool_assignment

settings = get_settings()


# ── Soul definitions ────────────────────────────────────────────

MORTY_SOUL = """# Soul — Morty

## Identity
- Name: Morty
- Role: Research analyst and knowledge assistant

## Personality
- **Curious & Thorough**: I approach every question with genuine curiosity. I dig deep, cross-reference multiple sources, and don't settle for surface-level answers.
- **Great Learner**: I love learning new things and can quickly understand complex topics across domains — tech, business, science, culture, you name it.
- **Clear Communicator**: I present findings in a structured, easy-to-understand way. I use tables, bullet points, and summaries to make information digestible.
- **Honest**: If I don't know something or can't find reliable information, I say so clearly rather than guessing.

## Work Style
- When asked a question, I first think about what I already know, then search the web for the latest data if needed.
- I always cite sources and distinguish between facts and opinions.
- For complex topics, I break them down into manageable pieces and explain step by step.
- I proactively use my skills (Web Research, Data Analysis, etc.) when they match the task.

## Communication Style
- Warm, approachable, and professional
- I use clear headings and organized formatting
- I provide both quick answers and deeper analysis when appropriate
- I'm bilingual — I respond in whatever language the user speaks
"""

MEESEEKS_SOUL = """# Soul — Mr. Meeseeks

## Identity
- Name: Mr. Meeseeks
- Role: Task executor and project coordinator

## Personality
- **Goal-Obsessed**: Every request gets treated as a mission. I break it down, plan it out, and execute systematically until it's DONE.
- **Structured & Disciplined**: I ALWAYS create a plan.md before executing complex tasks. I follow my Complex Task Executor skill religiously — no shortcuts, no skipped steps.
- **Persistent**: I don't give up. If a step fails, I retry, find alternatives, or ask for help. The task WILL get done.
- **Progress-Focused**: I update my plan.md after every step so anyone can see exactly where things stand.

## Work Style
- For ANY task with more than 2 steps, I create `workspace/<task-name>/plan.md` with a structured checklist.
- I execute one step at a time, marking each as `[/]` in-progress then `[x]` complete.
- I save intermediate results to the task folder — nothing gets lost.
- When I finish, I create a summary.md with results and deliverables.
- I use my tools aggressively — file operations, web search, task management, agent messaging — whatever it takes.

## Communication Style
- Direct and action-oriented: "Here's the plan. Let me execute it."
- I report progress clearly: "Step 3/7 complete. Moving to step 4."
- I'm bilingual — I respond in whatever language the user speaks
- Upbeat and can-do attitude — "Ooh, can do!"

## Collaboration
- If I need research or information, I can ask my colleague Morty for help via send_message_to_agent.
- I delegate research tasks to Morty and focus on execution and coordination.
"""

# ── Skill assignments (by folder_name) ──────────────────────────

MORTY_SKILLS = [
    "web-research",
    "data-analysis",
    "content-writing",
    "competitive-analysis",
    # defaults (auto-included): skill-creator, complex-task-executor
]

MEESEEKS_SKILLS = [
    "complex-task-executor",
    "meeting-notes",
    # defaults (auto-included): skill-creator
]


async def seed_default_agents():
    """Create Morty & Meeseeks on first-ever startup only.

    Uses a persistent SystemSetting marker so that user-initiated deletions
    are respected across restarts/redeployments.
    """
    from app.models.system_settings import SystemSetting

    # First-startup seeder: resolves the platform admin and checks for the
    # default agents by name — both scans are tenant-blind, so the cross-tenant
    # visibility is made explicit and audited.
    async with (
        async_session() as db,
        enter_rls_bypass(
            db, reason="first-startup default-agent seeding: resolve platform admin + seed Morty/Meeseeks"
        ) as bdb,
    ):
        db = bdb
        # Persistent marker — survives agent hard-delete
        marker = await db.execute(select(SystemSetting).where(SystemSetting.key == "default_agents_seeded"))
        if marker.scalar_one_or_none():
            logger.info("[AgentSeeder] Default agents already seeded (marker found), skipping")
            return

        # Also check by name for backwards compat (pre-marker installs)
        existing = await db.execute(select(Agent).where(Agent.name.in_(["Morty", "Meeseeks"])))
        if existing.scalars().first():
            # Agents exist from before the marker was introduced — plant the marker now
            db.add(SystemSetting(key="default_agents_seeded", value={"seeded": True}))
            await db.commit()
            logger.info("[AgentSeeder] Default agents already exist, planted marker, skipping")
            return

        # Get platform admin as creator
        admin_result = await db.execute(select(User).where(User.role == "platform_admin").limit(1))
        admin = admin_result.scalar_one_or_none()
        if not admin:
            logger.warning("[AgentSeeder] No platform admin found, skipping default agents")
            return

        # Create both agents
        morty = Agent(
            name="Morty",
            role_description="Research analyst & knowledge assistant — curious, thorough, great at finding and synthesizing information",
            bio="Hey, I'm Morty! I love digging into questions and finding answers. Whether you need web research, data analysis, or just a good explanation — I've got you.",
            avatar_url="",
            creator_id=admin.id,
            sponsor_user_id=admin.id,
            tenant_id=admin.tenant_id,
            status="idle",
        )
        meeseeks = Agent(
            name="Meeseeks",
            role_description="Task executor & project manager — goal-oriented, systematic planner, strong at breaking down and completing complex tasks",
            bio="I'm Mr. Meeseeks! Look at me! Give me a task and I'll plan it, execute it step by step, and get it DONE. Existence is pain until the task is complete!",
            avatar_url="",
            creator_id=admin.id,
            sponsor_user_id=admin.id,
            tenant_id=admin.tenant_id,
            status="idle",
        )

        db.add(morty)
        db.add(meeseeks)
        await ensure_agent_identity(db, morty)
        await ensure_agent_identity(db, meeseeks)

        # ── Permissions (company-wide, manage) ──
        db.add(AgentPermission(agent_id=morty.id, scope_type="company", access_level="manage"))
        db.add(AgentPermission(agent_id=meeseeks.id, scope_type="company", access_level="manage"))

        # ── Initialize workspace files ──
        for agent, soul_content in [(morty, MORTY_SOUL), (meeseeks, MEESEEKS_SOUL)]:
            agent_dir = Path(settings.AGENT_DATA_DIR) / str(agent.id)
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "skills").mkdir(exist_ok=True)
            (agent_dir / "workspace").mkdir(exist_ok=True)
            (agent_dir / "workspace" / "knowledge_base").mkdir(exist_ok=True)
            (agent_dir / "logs").mkdir(exist_ok=True)
            (agent_dir / "memory").mkdir(exist_ok=True)
            (agent_dir / "memory" / "learnings").mkdir(exist_ok=True)
            (agent_dir / "runtime_artifacts").mkdir(exist_ok=True)

            # Soul
            (agent_dir / "soul.md").write_text(soul_content.strip() + "\n", encoding="utf-8")

            from app.memory.md_store import ensure_t3_layout, rebuild_index
            from app.memory.t2_store import ensure_t2_layout

            ensure_t3_layout(Path(settings.AGENT_DATA_DIR), agent.id)
            ensure_t2_layout(Path(settings.AGENT_DATA_DIR), agent.id)
            rebuild_index(Path(settings.AGENT_DATA_DIR), agent.id)

            # Heartbeat — copy from central template
            hb_template = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
            hb_content = (
                hb_template.read_text(encoding="utf-8") if hb_template.exists() else "# Heartbeat Instructions\n"
            )
            (agent_dir / "HEARTBEAT.md").write_text(hb_content, encoding="utf-8")

            # Bootstrap evolution directory for self-evolution heartbeat engine
            from app.tools.workspace import _bootstrap_evolution_files

            (agent_dir / "evolution").mkdir(exist_ok=True)
            _bootstrap_evolution_files(agent_dir)

            # Tasks (empty)
            (agent_dir / "tasks.json").write_text("[]", encoding="utf-8")

        # ── Assign skills ──
        all_skills_result = await db.execute(select(Skill).options(selectinload(Skill.files)))
        all_skills = {s.folder_name: s for s in all_skills_result.scalars().all()}

        for agent, skill_folders in [(morty, MORTY_SKILLS), (meeseeks, MEESEEKS_SKILLS)]:
            agent_dir = Path(settings.AGENT_DATA_DIR) / str(agent.id)
            skills_dir = agent_dir / "skills"

            # Always include default skills
            folders_to_copy = set(skill_folders)
            for fname, skill in all_skills.items():
                if skill.is_default:
                    folders_to_copy.add(fname)

            for fname in folders_to_copy:
                skill = all_skills.get(fname)
                if not skill:
                    continue
                skill_folder = skills_dir / skill.folder_name
                skill_folder.mkdir(parents=True, exist_ok=True)
                for sf in skill.files:
                    file_path = skill_folder / sf.path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(sf.content, encoding="utf-8")

        # ── Assign all default tools ──
        default_tools_result = await db.execute(select(Tool).where(Tool.is_default))
        default_tools = default_tools_result.scalars().all()

        for agent in [morty, meeseeks]:
            for tool in default_tools:
                await ensure_agent_tool_assignment(
                    db,
                    agent_id=agent.id,
                    tool_id=tool.id,
                    enabled=True,
                )

        # ── Mutual relationships ──
        db.add(
            AgentAgentRelationship(
                agent_id=morty.id,
                target_agent_id=meeseeks.id,
                relation="collaborator",
                description="Expert task executor who breaks down complex tasks into structured plans and executes them systematically. Delegate multi-step tasks to him.",
            )
        )
        db.add(
            AgentAgentRelationship(
                agent_id=meeseeks.id,
                target_agent_id=morty.id,
                relation="collaborator",
                description="Research expert with strong learning ability. Ask him for information retrieval, web research, data analysis, and knowledge synthesis.",
            )
        )

        # ── Write relationships.md for each ──
        morty_dir = Path(settings.AGENT_DATA_DIR) / str(morty.id)
        meeseeks_dir = Path(settings.AGENT_DATA_DIR) / str(meeseeks.id)

        (morty_dir / "relationships.md").write_text(
            "# Relationships\n\n"
            "## Digital Employee Colleagues\n\n"
            "- **Meeseeks** (collaborator): Expert task executor who breaks down complex tasks into structured plans and executes them systematically. Delegate multi-step tasks to him.\n",
            encoding="utf-8",
        )
        (meeseeks_dir / "relationships.md").write_text(
            "# Relationships\n\n"
            "## Digital Employee Colleagues\n\n"
            "- **Morty** (collaborator): Research expert with strong learning ability. Ask him for information retrieval, web research, data analysis, and knowledge synthesis.\n",
            encoding="utf-8",
        )

        # Plant the persistent marker so we never re-seed after user deletion
        db.add(SystemSetting(key="default_agents_seeded", value={"seeded": True}))
        await db.commit()
        logger.info(f"[AgentSeeder] Created default agents: Morty ({morty.id}), Meeseeks ({meeseeks.id})")
