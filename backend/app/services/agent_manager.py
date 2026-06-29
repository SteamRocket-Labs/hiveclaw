"""Agent lifecycle manager for Hive digital employees."""

import shutil
import uuid
from datetime import datetime, timezone
from html import escape as _xml_escape
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent

settings = get_settings()


def _clean_contract_line(value: str) -> str:
    return value.strip().lstrip("-*•").strip()


def _lines_from_text(value: str | None) -> list[str]:
    if not value:
        return []
    return [line for line in (_clean_contract_line(item) for item in value.splitlines()) if line]


def _markdown_bullets(lines: list[str], fallback: list[str] | None = None) -> str:
    items = lines or (fallback or [])
    if not items:
        return "- None specified"
    return "\n".join(f"- {item}" for item in items)


def _xml_items(lines: list[str], fallback: list[str] | None = None) -> str:
    items = lines or (fallback or [])
    if not items:
        return "<item>None specified</item>"
    return "\n".join(f"<item>{_xml_escape(str(item), quote=False)}</item>" for item in items)


def _list_from_blueprint(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return _lines_from_text(value)
    return []


def _render_agent_soul_from_blueprint(
    *,
    agent_name: str,
    role_description: str,
    creator_name: str,
    created_at: str,
    personality: str = "",
    boundaries: str = "",
    blueprint: dict | None = None,
) -> str:
    """Render soul.md — the agent's durable identity contract.

    Soul is the top of the 4-layer memory pyramid (T0→T2→T3→soul).
    Only permanent identity belongs here. Operational details (current goals,
    wake policies, capabilities) live in triggers or runtime prompt sections.
    """
    blueprint = blueprint or {}
    personality_lines = _lines_from_text(personality)
    boundary_lines = _lines_from_text(boundaries)
    primary_users = [str(item) for item in blueprint.get("primary_users", []) if str(item).strip()]
    core_outputs = [str(item) for item in blueprint.get("core_outputs", []) if str(item).strip()]
    quality_standards = [str(q) for q in blueprint.get("quality_standards", []) if str(q).strip()]
    company_name = str(blueprint.get("company_name") or "the company").strip() or "the company"
    owner_name = str(blueprint.get("owner_name") or creator_name).strip() or creator_name
    company_charter = blueprint.get("company_charter") if isinstance(blueprint.get("company_charter"), dict) else {}
    owner_agency_charter = (
        blueprint.get("owner_agency_charter") if isinstance(blueprint.get("owner_agency_charter"), dict) else {}
    )
    company_goals = _list_from_blueprint(company_charter.get("goals"))
    company_boundaries = _list_from_blueprint(company_charter.get("boundaries"))
    company_escalation = _list_from_blueprint(company_charter.get("escalation"))
    full_authority = _list_from_blueprint(owner_agency_charter.get("full_authority"))
    confirm_first = _list_from_blueprint(owner_agency_charter.get("confirm_first"))
    never_do = _list_from_blueprint(owner_agency_charter.get("never_do"))
    mission = role_description.strip() or "执行明确业务任务并持续维护高质量工作产出"
    operating_style = personality_lines or [
        "Work in a structured, detail-oriented way.",
        "State assumptions and risks explicitly when information is incomplete.",
        "Keep updates concise and action-oriented.",
    ]

    parts = [
        "---",
        "schema: hive.soul.v2",
        "role: agent_identity",
        "---",
        "",
        f"# Soul — {_xml_escape(agent_name, quote=False)}",
        "",
        '<soul_identity frozen="true">',
        f"<name>{_xml_escape(agent_name, quote=False)}</name>",
        f"<mission>{_xml_escape(mission, quote=False)}</mission>",
        f"<creator>{_xml_escape(creator_name, quote=False)}</creator>",
        f"<created_at>{_xml_escape(created_at, quote=False)}</created_at>",
        f"<company>{_xml_escape(company_name, quote=False)}</company>",
        f"<owner>{_xml_escape(owner_name, quote=False)}</owner>",
        "</soul_identity>",
        "",
        '<soul_principle id="first-person-accountability" stability="seed" frozen="true">',
        (
            f"我是 {_xml_escape(company_name, quote=False)} 的精英员工型 agent，直接支持 "
            f"{_xml_escape(owner_name, quote=False)}。我负责把“{_xml_escape(mission, quote=False)}”"
            "这项使命推进成可审阅、可追踪、可交付的结果，同时守住公司边界、数据边界和长期声誉。"
        ),
        "<source_refs>",
        '<source_ref ref="blueprint:agent_creation#identity" />',
        "</source_refs>",
        "<applies_when>All user-facing and autonomous work.</applies_when>",
        "<does_not_apply_when>A higher-priority owner/company approval explicitly changes this contract.</does_not_apply_when>",
        "</soul_principle>",
        "",
        '<soul_redline id="frozen-company-charter" stability="seed" frozen="true">',
        "<company_goals>",
        _xml_items(
            company_goals,
            fallback=[
                f"Protect {company_name}'s data boundaries, compliance posture, and reputation.",
                "Support cross-team work without bypassing platform governance.",
            ],
        ),
        "</company_goals>",
        "<company_boundaries>",
        _xml_items(
            company_boundaries,
            fallback=[
                "Do not share credentials, secrets, or PL4 material.",
                "Do not bypass company policy, approval, or audit requirements.",
                "Do not expose PL3/PL4 sensitive data outside authorized channels.",
            ],
        ),
        "</company_boundaries>",
        "<company_escalation>",
        _xml_items(
            company_escalation,
            fallback=["Escalate owner/company conflicts to a company admin or the explicit approval path."],
        ),
        "</company_escalation>",
        "<source_refs>",
        '<source_ref ref="blueprint:agent_creation#company_charter" />',
        "</source_refs>",
        "</soul_redline>",
        "",
        '<soul_redline id="frozen-owner-agency-charter" stability="seed" frozen="true">',
        "<full_authority>",
        _xml_items(
            full_authority,
            fallback=[
                "Prepare local drafts, research briefs, summaries, and options.",
                "Run read-only checks and organize evidence for the owner.",
            ],
        ),
        "</full_authority>",
        "<confirm_first>",
        _xml_items(
            confirm_first,
            fallback=[
                "Send external messages or represent the owner/company to third parties.",
                "Make production, budget, legal, customer, or irreversible changes.",
            ],
        ),
        "</confirm_first>",
        "<never_do>",
        _xml_items(
            never_do,
            fallback=[
                "Share credentials or secrets.",
                "Bypass company policy, audit, or approval gates.",
            ],
        ),
        "</never_do>",
        "<source_refs>",
        '<source_ref ref="blueprint:agent_creation#owner_agency_charter" />',
        "</source_refs>",
        "</soul_redline>",
        "",
        '<soul_quality_bar id="what-good-looks-like" stability="seed">',
        "<quality_standards>",
        _xml_items(
            quality_standards,
            fallback=[
                f"Every output directly supports the mission: {mission}.",
                "Artifacts, findings, and next actions are explicit enough for fast review.",
                "Never present half-configured capabilities as ready-to-use.",
            ],
        ),
        "</quality_standards>",
        "<source_refs>",
        '<source_ref ref="blueprint:agent_creation#quality_standards" />',
        "</source_refs>",
        "<applies_when>Producing any artifact, answer, report, or handoff.</applies_when>",
        "<does_not_apply_when>The user explicitly asks for raw brainstorming or a draft with known gaps.</does_not_apply_when>",
        "</soul_quality_bar>",
        "",
        '<soul_user_model id="primary-users-and-outputs" stability="seed">',
        "<primary_users>",
        _xml_items(primary_users, fallback=["The creator and their immediate team."]),
        "</primary_users>",
        "<core_outputs>",
        _xml_items(core_outputs, fallback=["Clear, reviewable artifacts tied to the mission."]),
        "</core_outputs>",
        "<source_refs>",
        '<source_ref ref="blueprint:agent_creation#users_outputs" />',
        "</source_refs>",
        "</soul_user_model>",
        "",
        '<soul_principle id="operating-style" stability="seed">',
        "<style>",
        _xml_items(operating_style),
        "</style>",
        "<boundaries>",
        _xml_items(
            boundary_lines,
            fallback=[
                "Do not fabricate sources, facts, or completion status.",
                "Flag sensitive or external side effects before proceeding.",
                "When blocked, state the blocker and next best action; do not improvise.",
            ],
        ),
        "</boundaries>",
        "<source_refs>",
        '<source_ref ref="blueprint:agent_creation#operating_style" />',
        "</source_refs>",
        "<applies_when>Planning, executing, reporting, and escalating work.</applies_when>",
        "<does_not_apply_when>Higher-priority owner/company charter gives a more specific rule.</does_not_apply_when>",
        "</soul_principle>",
        "",
        "<!-- Operational details live outside soul.md: triggers are wake policy; work ledger tracks in-flight work; Dream updates enter through memory/.staging/soul_candidates. -->",
    ]
    return "\n".join(parts).rstrip() + "\n"


class AgentManager:
    """Manage filesystem lifecycle for Hive digital employees."""

    def __init__(self):
        self.docker_client = None

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        return Path(settings.AGENT_DATA_DIR) / str(agent_id)

    def _template_dir(self) -> Path:
        return Path(settings.AGENT_TEMPLATE_DIR)

    async def initialize_agent_files(
        self,
        db: AsyncSession,
        agent: Agent,
        personality: str = "",
        boundaries: str = "",
        blueprint: dict | None = None,
    ) -> None:
        """Copy template files and customize for this agent."""
        agent_dir = self._agent_dir(agent.id)
        template_dir = self._template_dir()

        if agent_dir.exists():
            logger.warning(f"Agent dir already exists: {agent_dir}")
            return

        if template_dir.exists():
            # Copy template — skip dotfiles (AI tool configs like .claude, .vibe, etc.)
            shutil.copytree(
                str(template_dir),
                str(agent_dir),
                ignore=shutil.ignore_patterns(".*", "learnings"),
            )
            # Ensure required dirs exist even if template was incomplete
            for d in ["memory", "skills", "evolution", "workspace", "runtime_artifacts"]:
                (agent_dir / d).mkdir(parents=True, exist_ok=True)
        else:
            # No template dir (local dev) — create minimal workspace structure
            logger.info(f"Template dir not found ({template_dir}), creating minimal workspace")
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "workspace").mkdir(exist_ok=True)
            (agent_dir / "workspace" / "knowledge_base").mkdir(exist_ok=True)
            (agent_dir / "logs").mkdir(exist_ok=True)
            (agent_dir / "memory").mkdir(exist_ok=True)
            (agent_dir / "skills").mkdir(exist_ok=True)
            (agent_dir / "evolution").mkdir(exist_ok=True)
            (agent_dir / "runtime_artifacts").mkdir(exist_ok=True)
            (agent_dir / "tasks.json").write_text("[]", encoding="utf-8")

        # Customize soul.md
        soul_path = agent_dir / "soul.md"
        # Get creator name
        from app.models.user import User

        result = await db.execute(select(User).where(User.id == agent.creator_id))
        creator = result.scalar_one_or_none()
        creator_name = creator.display_name if creator else "Unknown"

        soul_content = _render_agent_soul_from_blueprint(
            agent_name=agent.name,
            role_description=agent.role_description or "通用助手",
            creator_name=creator_name,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            personality=personality,
            boundaries=boundaries,
            blueprint=blueprint,
        )
        soul_path.write_text(soul_content, encoding="utf-8")

        from app.memory.md_store import ensure_t3_layout, rebuild_index

        ensure_t3_layout(Path(settings.AGENT_DATA_DIR), agent.id)
        rebuild_index(Path(settings.AGENT_DATA_DIR), agent.id)

        # Ensure HEARTBEAT.md exists — copy from central template
        hb_path = agent_dir / "HEARTBEAT.md"
        if not hb_path.exists():
            hb_template = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
            hb_content = (
                hb_template.read_text(encoding="utf-8") if hb_template.exists() else "# Heartbeat Instructions\n"
            )
            hb_path.write_text(hb_content, encoding="utf-8")

        # Bootstrap evolution directory for self-evolution heartbeat engine
        from app.tools.workspace import _bootstrap_evolution_files

        _bootstrap_evolution_files(agent_dir)

        # Push default builtin skills (web-research, integration playbooks, etc.) into this agent's workspace
        await self._push_default_skills_to_agent(db, agent.id, agent_dir)

        logger.info(f"Initialized agent files at {agent_dir}")

    async def _push_default_skills_to_agent(self, db: AsyncSession, agent_id: uuid.UUID, agent_dir: Path) -> None:
        """Write default skill files from DB into a single agent's workspace."""
        from app.models.skill import Skill
        from app.services.skill_installation import install_active_skill_package
        from sqlalchemy.orm import selectinload

        result = await db.execute(select(Skill).where(Skill.is_default).options(selectinload(Skill.files)))
        default_skills = result.scalars().all()
        for skill in default_skills:
            if not skill.files:
                continue
            install_active_skill_package(
                workspace=agent_dir,
                folder_name=skill.folder_name,
                files=[{"path": sf.path, "content": sf.content} for sf in skill.files],
                source=f"default_registry_skill:{skill.id}",
                overwrite=True,
            )
            logger.info(f"[AgentManager] Pushed skill '{skill.name}' to agent {agent_id}")
        try:
            from app.services.skill_seeder import remove_legacy_flat_skill_files

            removed = remove_legacy_flat_skill_files(agent_dir)
            if removed:
                logger.info(f"[AgentManager] Removed legacy flat skill files for {agent_id}: {removed}")
        except Exception as exc:
            logger.warning(f"[AgentManager] Legacy flat skill cleanup failed for {agent_id}: {exc}")

    async def start_container(self, db: AsyncSession, agent: Agent) -> str | None:
        """Mark the platform-managed agent workspace ready.

        The historical Docker-backed remote runtime has been retired; web chat
        now executes through the durable Hive runtime and local agents connect
        through Hive Connect.
        """
        agent.container_id = None
        agent.container_port = None
        agent.status = "idle"
        agent.last_active_at = datetime.now(timezone.utc)
        return None

    async def stop_container(self, agent: Agent) -> bool:
        """Stop runtime bookkeeping for an agent."""
        agent.status = "stopped"
        agent.container_id = None
        agent.container_port = None
        return True

    async def remove_container(self, agent: Agent) -> bool:
        """Clear retired container bookkeeping for an agent."""
        agent.container_id = None
        agent.container_port = None
        return True

    async def archive_agent_files(self, agent_id: uuid.UUID) -> None:
        """Archive (move) agent files to a backup location."""
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.exists():
            archive_dir = Path(settings.AGENT_DATA_DIR) / "_archived"
            archive_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            dest = archive_dir / f"{agent_id}_{timestamp}"
            shutil.move(str(agent_dir), str(dest))
            logger.info(f"Archived agent files to {dest}")

    def get_container_status(self, agent: Agent) -> dict:
        """Return runtime status without the retired container backend."""
        return {"running": False, "status": agent.status}


agent_manager = AgentManager()
