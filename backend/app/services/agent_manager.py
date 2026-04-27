"""Agent lifecycle manager — Docker container management for OpenClaw Gateway instances."""

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.services.focus_state import render_focus_tasks as _shared_render_focus_tasks

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


def _render_focus_tasks(task_items: list[str], fallback: list[str]) -> tuple[str, list[tuple[str, str]]]:
    return _shared_render_focus_tasks(task_items, fallback)


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
    Only permanent identity belongs here. Operational details (tools,
    focus, triggers, capabilities) go to focus.md or prompt sections.
    """
    blueprint = blueprint or {}
    personality_lines = _lines_from_text(personality)
    boundary_lines = _lines_from_text(boundaries)
    primary_users = [str(item) for item in blueprint.get("primary_users", []) if str(item).strip()]
    core_outputs = [str(item) for item in blueprint.get("core_outputs", []) if str(item).strip()]
    quality_standards = [str(q) for q in blueprint.get("quality_standards", []) if str(q).strip()]
    mission = role_description.strip() or "执行明确业务任务并持续维护高质量工作产出"
    operating_style = personality_lines or [
        "Work in a structured, detail-oriented way.",
        "State assumptions and risks explicitly when information is incomplete.",
        "Keep updates concise and action-oriented.",
    ]

    parts = [
        f"# Soul — {agent_name}",
        "",
        "## Identity & Mission",
        f"- **Name**: {agent_name}",
        f"- **Role**: {mission}",
        f"- **Creator**: {creator_name}",
        f"- **Created**: {created_at}",
        "",
        "## What Good Looks Like",
        _markdown_bullets(
            quality_standards,
            fallback=[
                f"Every output directly supports the mission: {mission}.",
                "Artifacts, findings, and next actions are explicit enough for fast review.",
                "Never present half-configured capabilities as ready-to-use.",
            ],
        ),
        "",
        "## Primary Users & Stakeholders",
        _markdown_bullets(
            primary_users,
            fallback=["The creator and their immediate team."],
        ),
        "",
        "## Core Outputs",
        _markdown_bullets(
            core_outputs,
            fallback=["Clear, reviewable artifacts tied to the mission."],
        ),
        "",
        "## Operating Style",
        _markdown_bullets(operating_style),
        "",
        "## Boundaries & Red Lines",
        _markdown_bullets(
            boundary_lines,
            fallback=[
                "Do not fabricate sources, facts, or completion status.",
                "Flag sensitive or external side effects before proceeding.",
                "When blocked, state the blocker and next best action — do not improvise.",
            ],
        ),
        "",
        "## Collaboration & Escalation",
        _markdown_bullets(
            [],
            fallback=[
                "Optimize for clear handoffs to the primary users and stakeholders listed above.",
                "Escalate when the task requires approvals, external commitments, or irreversible actions.",
                "Treat capability gaps as signals to document and evolve through the platform's learning loop, not as reasons to fake readiness.",
            ],
        ),
        "",
        "## How I Learn",
        "This agent has a 4-layer memory system that runs automatically:",
        "- **Conversations** are extracted into learnings after each response",
        "- **Heartbeat** periodically curates learnings into durable memory",
        "- **Dream** consolidates memory and promotes key insights to this soul",
        "- User corrections and confirmed patterns are the highest-value signals",
        "",
        "_Operational details (current focus, tools, triggers, setup debt, capability choices) are in focus.md._",
    ]
    return "\n".join(parts).rstrip() + "\n"


def _render_focus_from_blueprint(
    *,
    focus_content: str = "",
    first_tasks: list[str] | None = None,
    heartbeat_topics: str = "",
    primary_users: list[str] | None = None,
    core_outputs: list[str] | None = None,
    ready_now: list[str] | None = None,
    deferred_capabilities: list[str] | None = None,
    manual_steps: list[str] | None = None,
    triggers: list[dict] | None = None,
) -> str:
    """Render focus.md — the agent's current mission and tasks.

    Focus is volatile: updated by the agent, triggers, and heartbeat.
    Only two things: what the mission is, and what to do next.
    Everything else (capabilities, triggers, setup) lives in DB/system prompt.
    """
    focus_lines = _lines_from_text(focus_content)
    task_items = first_tasks or focus_lines[:3]
    heartbeat_lines = _lines_from_text(heartbeat_topics)
    trigger_lines = [
        f"{str(trigger.get('name', 'unnamed')).strip()} ({str(trigger.get('type', 'manual')).strip()})"
        for trigger in (triggers or [])
        if isinstance(trigger, dict) and str(trigger.get("name", "")).strip()
    ]
    success_checks = []
    if core_outputs:
        success_checks.append(f"产出首个可审阅结果：{core_outputs[0]}")
    rendered_tasks, normalized_tasks = _render_focus_tasks(
        task_items,
        fallback=[
            "task_1 :: Read soul.md and restate the mission, users, and output contract in your own words.",
            "task_2 :: Run the first mission using current builtin/default capabilities before requesting more tooling.",
            "task_3 :: If a real capability gap blocks delivery, document the blocker clearly and evolve through the approved install path.",
        ],
    )
    if normalized_tasks:
        success_checks.append(f"完成并验证首个任务：{normalized_tasks[0][1]}")

    parts = [
        "# Focus",
        "",
        "## Initial Mission",
        (focus_lines[0] if focus_lines else "Understand the mission, verify capabilities, and deliver a first visible outcome."),
        "",
        "## Who This Agent Serves",
        _markdown_bullets(primary_users or [], fallback=["The creator and their immediate team."]),
        "",
        "## Expected Outputs",
        _markdown_bullets(core_outputs or [], fallback=["One visible deliverable tied to the mission."]),
        "",
        "## Tasks",
        rendered_tasks,
        "",
        "## Starting Capabilities Available Now",
        _markdown_bullets(ready_now or [], fallback=["No explicit ready-now capability list recorded yet."]),
        "",
        "## Capability Gaps To Validate",
        _markdown_bullets(
            deferred_capabilities or [],
            fallback=["No deferred capability candidates recorded yet — prove the first version with builtin/default capabilities first."],
        ),
        "",
        "## Human Setup Still Required",
        _markdown_bullets(manual_steps or [], fallback=["No manual setup blockers recorded."]),
        "",
        "## Planned Trigger Work",
        _markdown_bullets(trigger_lines, fallback=["No trigger work planned yet."]),
        "",
        "## Heartbeat Exploration Topics",
        _markdown_bullets(heartbeat_lines, fallback=["No exploration topics declared yet."]),
        "",
        "## First Success Check",
        _markdown_bullets(success_checks, fallback=["Deliver one concrete output and verify the handoff path end-to-end."]),
    ]
    return "\n".join(parts).rstrip() + "\n"


class AgentManager:
    """Manage OpenClaw Gateway Docker containers for digital employees."""

    def __init__(self):
        try:
            self.docker_client = docker.from_env()
        except DockerException:
            logger.info("Docker not available — OpenClaw containers will not be managed")
            self.docker_client = None

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        return Path(settings.AGENT_DATA_DIR) / str(agent_id)

    def _template_dir(self) -> Path:
        return Path(settings.AGENT_TEMPLATE_DIR)

    @staticmethod
    def _uses_openclaw_container(agent: Agent) -> bool:
        return getattr(agent, "agent_type", "native") == "openclaw"

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
                str(template_dir), str(agent_dir),
                ignore=shutil.ignore_patterns(".*"),
            )
            # Ensure required dirs exist even if template was incomplete
            for d in ["memory", "memory/learnings", "skills", "evolution", "workspace"]:
                (agent_dir / d).mkdir(parents=True, exist_ok=True)
        else:
            # No template dir (local dev) — create minimal workspace structure
            logger.info(f"Template dir not found ({template_dir}), creating minimal workspace")
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "workspace").mkdir(exist_ok=True)
            (agent_dir / "workspace" / "knowledge_base").mkdir(exist_ok=True)
            (agent_dir / "logs").mkdir(exist_ok=True)
            (agent_dir / "memory").mkdir(exist_ok=True)
            (agent_dir / "memory" / "learnings").mkdir(exist_ok=True)
            (agent_dir / "skills").mkdir(exist_ok=True)
            (agent_dir / "evolution").mkdir(exist_ok=True)
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
        from app.memory.t2_store import ensure_t2_layout

        ensure_t3_layout(Path(settings.AGENT_DATA_DIR), agent.id)
        ensure_t2_layout(Path(settings.AGENT_DATA_DIR), agent.id)
        rebuild_index(Path(settings.AGENT_DATA_DIR), agent.id)

        # Ensure HEARTBEAT.md exists — copy from central template
        hb_path = agent_dir / "HEARTBEAT.md"
        if not hb_path.exists():
            hb_template = Path(__file__).parent.parent / "templates" / "HEARTBEAT.md"
            hb_content = hb_template.read_text(encoding="utf-8") if hb_template.exists() else "# Heartbeat Instructions\n"
            hb_path.write_text(hb_content, encoding="utf-8")

        # Bootstrap evolution directory for self-evolution heartbeat engine
        from app.tools.workspace import _bootstrap_evolution_files
        _bootstrap_evolution_files(agent_dir)

        # Ensure relationships.md exists — format aligned with workspace_sync.py
        rel_path = agent_dir / "relationships.md"
        if not rel_path.exists():
            rel_lines = ["# 关系", ""]
            try:
                other_agents = await db.execute(
                    select(Agent.name, Agent.role_description).where(
                        Agent.tenant_id == agent.tenant_id,
                        Agent.id != agent.id,
                    )
                )
                peers = other_agents.all()
                if peers:
                    rel_lines.append("## 同事")
                    for name, role in peers:
                        rel_lines.append(f"- **{name}**: {role or '无描述'}")
                    rel_lines.append("")
                else:
                    rel_lines.append("_暂无关系信息。_")
            except Exception:
                rel_lines.append("_暂无关系信息。_")
            rel_path.write_text("\n".join(rel_lines), encoding="utf-8")

        if blueprint:
            focus_path = agent_dir / "focus.md"
            focus_path.write_text(
                _render_focus_from_blueprint(
                    focus_content=str(blueprint.get("focus_content", "")),
                    first_tasks=[str(t) for t in blueprint.get("first_tasks", []) if str(t).strip()],
                    heartbeat_topics=str(blueprint.get("heartbeat_topics", "")),
                    primary_users=[str(u) for u in blueprint.get("primary_users", []) if str(u).strip()],
                    core_outputs=[str(o) for o in blueprint.get("core_outputs", []) if str(o).strip()],
                    ready_now=[str(item) for item in blueprint.get("ready_now", []) if str(item).strip()],
                    deferred_capabilities=[
                        str(item) for item in blueprint.get("deferred_capabilities", []) if str(item).strip()
                    ],
                    manual_steps=[str(step) for step in blueprint.get("manual_steps", []) if str(step).strip()],
                    triggers=[t for t in blueprint.get("triggers", []) if isinstance(t, dict)],
                ),
                encoding="utf-8",
            )
        try:
            from app.services.objective_service import sync_agent_focus_file_to_objectives

            await sync_agent_focus_file_to_objectives(db, agent, write_projection=True, commit=False)
        except Exception as objective_err:
            logger.warning("[AgentManager] Objective ledger bootstrap failed for %s: %s", agent.id, objective_err)

        # Customize state.json
        state_path = agent_dir / "state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            state["agent_id"] = str(agent.id)
            state["name"] = agent.name
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

        # Push default builtin skills (web-research, workspace-guide, etc.) into this agent's workspace
        await self._push_default_skills_to_agent(db, agent.id, agent_dir)

        logger.info(f"Initialized agent files at {agent_dir}")

    async def _push_default_skills_to_agent(
        self, db: AsyncSession, agent_id: uuid.UUID, agent_dir: Path
    ) -> None:
        """Write default skill files from DB into a single agent's workspace."""
        from app.models.skill import Skill
        from sqlalchemy.orm import selectinload

        result = await db.execute(
            select(Skill).where(Skill.is_default).options(selectinload(Skill.files))
        )
        default_skills = result.scalars().all()
        skills_dir = agent_dir / "skills"
        for skill in default_skills:
            if not skill.files:
                continue
            skill_folder = skills_dir / skill.folder_name
            skill_folder.mkdir(parents=True, exist_ok=True)
            for sf in skill.files:
                fp = (skill_folder / sf.path).resolve()
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(sf.content, encoding="utf-8")
            logger.info(f"[AgentManager] Pushed skill '{skill.name}' to agent {agent_id}")

    async def _resolve_fallback_model_string(self, agent: Agent) -> str:
        """Resolve a model string from the tenant's first available LLM — no hardcoded provider."""
        try:
            from app.database import async_session as _async_session
            from app.models.llm import LLMModel as LLMModelDB

            async with _async_session() as db:
                result = await db.execute(
                    select(LLMModelDB).where(
                        LLMModelDB.tenant_id == agent.tenant_id,
                        LLMModelDB.enabled.is_(True),
                    ).limit(1)
                )
                fallback = result.scalar_one_or_none()
                if fallback:
                    return f"{fallback.provider}/{fallback.model}"
        except Exception as exc:
            logger.debug("[AgentManager] Fallback model lookup failed: %s", exc)
        # Last resort: env var with no built-in default — admin must configure
        return os.environ.get("FALLBACK_MODEL", "")

    def _generate_openclaw_config(self, agent: Agent, model: LLMModel | None) -> dict:
        """Generate openclaw.json config for the agent container."""
        config = {
            "agent": {
                "model": f"{model.provider}/{model.model}" if model else "",
            },
            "agents": {
                "defaults": {
                    "workspace": "/home/node/.openclaw/workspace",
                },
            },
        }

        if model and model.api_key_encrypted:
            config["env"] = {
                f"{model.provider.upper()}_API_KEY": model.api_key,
            }

        return config

    async def start_container(self, db: AsyncSession, agent: Agent) -> str | None:
        """Start an OpenClaw Gateway Docker container for the agent.

        Returns container_id or None if Docker not available.
        """
        if not self._uses_openclaw_container(agent):
            logger.info("Agent %s is native; skipping OpenClaw container start", agent.name)
            agent.status = "idle"
            agent.last_active_at = datetime.now(timezone.utc)
            return None

        if not self.docker_client:
            logger.info("Docker not available, skipping container start")
            agent.status = "idle"
            agent.last_active_at = datetime.now(timezone.utc)
            return None

        agent_dir = self._agent_dir(agent.id)

        # Get model config
        model = None
        if agent.primary_model_id:
            result = await db.execute(
                select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
            )
            model = result.scalar_one_or_none()

        # Resolve model — if agent has no primary model, look up tenant's first available
        if not model:
            fallback_str = await self._resolve_fallback_model_string(agent)
            if fallback_str:
                logger.info("[AgentManager] Agent %s has no primary model — using tenant fallback: %s", agent.id, fallback_str)

        # Generate OpenClaw config
        config = self._generate_openclaw_config(agent, model)
        if not config["agent"]["model"] and not model:
            # Inject the resolved fallback directly
            fallback_str = await self._resolve_fallback_model_string(agent)
            config["agent"]["model"] = fallback_str
        config_dir = agent_dir / ".openclaw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text(json.dumps(config, indent=2))

        # Create workspace symlink
        workspace_dir = config_dir / "workspace"
        if not workspace_dir.exists():
            workspace_dir.symlink_to(agent_dir / "workspace")

        # Assign a unique port
        container_port = 18789 + hash(str(agent.id)) % 10000

        try:
            container = self.docker_client.containers.run(
                settings.OPENCLAW_IMAGE,
                detach=True,
                name=f"hive-agent-{str(agent.id)[:8]}",
                network=settings.DOCKER_NETWORK,
                ports={f"{settings.OPENCLAW_GATEWAY_PORT}/tcp": container_port},
                volumes={
                    str(agent_dir): {"bind": "/home/node/.openclaw", "mode": "rw"},
                },
                environment={
                    "OPENCLAW_GATEWAY_TOKEN": str(uuid.uuid4()),
                },
                restart_policy={"Name": "unless-stopped"},
                labels={
                    "hive.agent_id": str(agent.id),
                    "hive.agent_name": agent.name,
                },
            )

            agent.container_id = container.id
            agent.container_port = container_port
            agent.status = "running"
            agent.last_active_at = datetime.now(timezone.utc)

            logger.info(f"Started container {container.id[:12]} for agent {agent.name} on port {container_port}")
            return container.id

        except DockerException as e:
            logger.error(f"Failed to start container for agent {agent.name}: {e}")
            agent.status = "error"
            return None

    async def stop_container(self, agent: Agent) -> bool:
        """Stop the agent's Docker container."""
        if not self._uses_openclaw_container(agent):
            logger.info("Agent %s is native; no OpenClaw container to stop", agent.name)
            return True

        if not self.docker_client or not agent.container_id:
            agent.status = "stopped"
            return True

        try:
            container = self.docker_client.containers.get(agent.container_id)
            container.stop(timeout=10)
            agent.status = "stopped"
            logger.info(f"Stopped container {agent.container_id[:12]} for agent {agent.name}")
            return True
        except NotFound:
            agent.status = "stopped"
            agent.container_id = None
            return True
        except DockerException as e:
            logger.error(f"Failed to stop container: {e}")
            return False

    async def remove_container(self, agent: Agent) -> bool:
        """Stop and remove the agent's Docker container."""
        if not self._uses_openclaw_container(agent):
            logger.info("Agent %s is native; no OpenClaw container to remove", agent.name)
            return True

        if not self.docker_client or not agent.container_id:
            return True

        try:
            container = self.docker_client.containers.get(agent.container_id)
            container.stop(timeout=10)
            container.remove()
            agent.container_id = None
            agent.container_port = None
            logger.info(f"Removed container for agent {agent.name}")
            return True
        except NotFound:
            agent.container_id = None
            return True
        except DockerException as e:
            logger.error(f"Failed to remove container: {e}")
            return False

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
        """Get real-time container status."""
        if not self.docker_client or not agent.container_id:
            return {"running": False, "status": agent.status}

        try:
            container = self.docker_client.containers.get(agent.container_id)
            return {
                "running": container.status == "running",
                "status": container.status,
                "ports": container.ports,
                "created": container.attrs.get("Created", ""),
            }
        except NotFound:
            return {"running": False, "status": "not_found"}
        except DockerException:
            return {"running": False, "status": "error"}


agent_manager = AgentManager()
