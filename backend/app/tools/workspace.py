"""Workspace bootstrap helpers for tool runtime."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.memory.legacy_migration import migrate_legacy_memory_tree
from app.memory.md_store import ensure_t3_layout, rebuild_index
from app.memory.t2_store import ensure_t2_layout
from app.models.agent import Agent
from app.models.task import Task

logger = logging.getLogger(__name__)

_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.AGENT_DATA_DIR)

# Single source of truth for HEARTBEAT.md: app/templates/HEARTBEAT.md
_HEARTBEAT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "HEARTBEAT.md"

_EVOLUTION_SCORECARD_SEED = """\
# Evolution Scorecard

## Metrics (updated each heartbeat)
- total_heartbeats: 0
- useful_heartbeats: 0
- failed_attempts: 0
- blocked_approaches: 0
- skills_created: 0
- strategies_evolved: 0

## Recent Trend
(updated automatically by heartbeat Phase 4)
"""

_EVOLUTION_BLOCKLIST_SEED = """\
# Blocked Approaches

Approaches proven impossible in this environment. Do NOT retry these.

(none yet)
"""

_EVOLUTION_LINEAGE_SEED = """\
# Evolution Lineage

Each heartbeat records what was tried and the outcome.
The next heartbeat reads this to avoid repeating failures and to build on successes.

(no entries yet)
"""

_EVOLUTION_SKILL_CANDIDATES_SEED = """\
# Skill Candidates

Repeated workflows that may graduate into reusable skills after enough evidence.

(none yet)
"""

_EVOLUTION_SKILL_REVIEW_SEED = """\
# Skill Review

Lifecycle events for skills: candidate, promoted, patched, or superseded.

(none yet)
"""


def _bootstrap_evolution_files(ws: Path) -> None:
    """Create evolution seed files if they don't exist."""
    seeds = {
        "evolution/scorecard.md": _EVOLUTION_SCORECARD_SEED,
        "evolution/blocklist.md": _EVOLUTION_BLOCKLIST_SEED,
        "evolution/lineage.md": _EVOLUTION_LINEAGE_SEED,
        "evolution/skill_candidates.md": _EVOLUTION_SKILL_CANDIDATES_SEED,
        "evolution/skill_review.md": _EVOLUTION_SKILL_REVIEW_SEED,
    }
    for rel_path, content in seeds.items():
        fpath = ws / rel_path
        if not fpath.exists():
            fpath.write_text(content, encoding="utf-8")


# PR-12: rewrote the template with XML sections + a decision matrix. Use one
# of the new unique tags as the marker so agents on the pre-PR-12 template
# get upgraded on next startup migration.
_HEARTBEAT_MIGRATION_MARKER = "<decision_matrix>"
_DEPRECATED_SKILLS = ("self-improving-agent", "proactive-agent")


def migrate_all_workspaces() -> None:
    """One-time migration: update HEARTBEAT.md + remove deprecated skills for all agents."""
    if not WORKSPACE_ROOT.exists():
        return
    try:
        hb_template = _HEARTBEAT_TEMPLATE_PATH.read_text(encoding="utf-8")
    except Exception:
        logger.warning("[migrate] Cannot read HEARTBEAT.md template, skipping migration")
        return

    migrated = 0
    for agent_dir in WORKSPACE_ROOT.iterdir():
        if not agent_dir.is_dir():
            continue
        try:
            agent_uuid = uuid.UUID(agent_dir.name)
        except ValueError:
            logger.debug("[migrate] Skipping non-agent workspace directory: %s", agent_dir.name)
            continue

        migrate_legacy_memory_tree(WORKSPACE_ROOT, agent_uuid)

        try:
            from app.services.t0_logger import migrate_t0_layout

            t0_report = migrate_t0_layout(agent_uuid)
            if t0_report.get("moved", 0):
                logger.info(
                    "[migrate] T0 layout for %s: moved=%d skipped=%d",
                    agent_dir.name,
                    t0_report["moved"],
                    t0_report.get("skipped", 0),
                )
        except Exception as exc:
            logger.warning("[migrate] T0 layout migration failed for %s: %s", agent_dir.name, exc)

        # Update HEARTBEAT.md if it's the old version
        hb_path = agent_dir / "HEARTBEAT.md"
        if hb_path.exists():
            try:
                current = hb_path.read_text(encoding="utf-8")
                if _HEARTBEAT_MIGRATION_MARKER not in current:
                    hb_path.write_text(hb_template, encoding="utf-8")
                    migrated += 1
            except Exception as e:
                logger.warning("[migrate] Failed to update HEARTBEAT.md for %s: %s", agent_dir.name, e)

        # Clean up nested workspace/workspace/ if empty
        _nested_ws = agent_dir / "workspace" / "workspace"
        if _nested_ws.is_dir() and not any(_nested_ws.iterdir()):
            _nested_ws.rmdir()
            logger.info("[migrate] Removed empty workspace/workspace/ from %s", agent_dir.name)

        # Remove deprecated skill folders
        skills_dir = agent_dir / "skills"
        for skill_name in _DEPRECATED_SKILLS:
            skill_path = skills_dir / skill_name
            if skill_path.exists():
                try:
                    shutil.rmtree(skill_path)
                    logger.info("[migrate] Removed deprecated skill %s from %s", skill_name, agent_dir.name)
                except Exception as e:
                    logger.warning("[migrate] Failed to remove %s from %s: %s", skill_name, agent_dir.name, e)

    if migrated:
        logger.info("[migrate] Updated HEARTBEAT.md for %d agent(s)", migrated)


async def ensure_workspace(agent_id: uuid.UUID, tenant_id: str | None = None) -> Path:
    """Initialize agent workspace with standard structure."""
    ws = WORKSPACE_ROOT / str(agent_id)
    ws.mkdir(parents=True, exist_ok=True)

    (ws / "skills").mkdir(exist_ok=True)
    (ws / "workspace").mkdir(exist_ok=True)
    (ws / "workspace" / "knowledge_base").mkdir(exist_ok=True)

    # Clean up nested workspace/workspace/ if empty (legacy bootstrap bug)
    _nested_ws = ws / "workspace" / "workspace"
    if _nested_ws.is_dir() and not any(_nested_ws.iterdir()):
        _nested_ws.rmdir()
    (ws / "logs").mkdir(exist_ok=True)
    (ws / "memory").mkdir(exist_ok=True)
    (ws / "memory" / "learnings").mkdir(exist_ok=True)
    (ws / "evolution").mkdir(exist_ok=True)
    ensure_t3_layout(WORKSPACE_ROOT, agent_id)
    ensure_t2_layout(WORKSPACE_ROOT, agent_id)

    if tenant_id:
        enterprise_dir = WORKSPACE_ROOT / f"enterprise_info_{tenant_id}"
    else:
        enterprise_dir = WORKSPACE_ROOT / "enterprise_info"
    enterprise_dir.mkdir(parents=True, exist_ok=True)
    (enterprise_dir / "knowledge_base").mkdir(exist_ok=True)

    profile_path = enterprise_dir / "company_profile.md"
    if not profile_path.exists():
        profile_path.write_text(
            "# Company Profile\n\n_Edit company information here. All digital employees can access this._\n\n## Basic Info\n- Company Name:\n- Industry:\n- Founded:\n\n## Business Overview\n\n## Organization Structure\n\n## Company Culture\n",
            encoding="utf-8",
        )

    # Pre-create canonical learnings files so heartbeat/skills don't waste tool calls on missing files.
    for learnings_file, learnings_seed in [
        ("memory/learnings/insights.md", "# Insights\n\nUser corrections, preferences, and agent discoveries.\n"),
        ("memory/learnings/errors.md", "# Errors\n\nExecution failures and blocked approaches.\n"),
        ("memory/learnings/requests.md", "# Requests\n\nCapability gaps and user wishes.\n"),
    ]:
        lpath = ws / learnings_file
        if not lpath.exists():
            lpath.write_text(learnings_seed, encoding="utf-8")

    # Pre-create focus.md (ordinary workspace scratch file; not projected into the prompt)
    focus_path = ws / "focus.md"
    if not focus_path.exists():
        focus_path.write_text("# Focus\n\n## Tasks\n", encoding="utf-8")

    # Pre-create T3 semantic memory files so heartbeat/dream don't need to create them
    for t3_file, t3_seed in [
        ("memory/feedback.md", "# Feedback\n\nUser corrections, preferences, and constraints.\n"),
        ("memory/knowledge.md", "# Knowledge\n\nProject and domain knowledge.\n"),
        ("memory/strategies.md", "# Strategies\n\nEffective approaches and patterns.\n"),
        ("memory/blocked.md", "# Blocked Patterns\n\nFailed approaches — do not retry.\n"),
        ("memory/user.md", "# User Profile\n\nUser characteristics and working style.\n"),
    ]:
        t3_path = ws / t3_file
        if not t3_path.exists():
            t3_path.write_text(t3_seed, encoding="utf-8")

    # Pre-create INDEX.md (T3 memory index)
    index_path = ws / "memory" / "INDEX.md"
    if not index_path.exists():
        rebuild_index(WORKSPACE_ROOT, agent_id)

    soul_path = ws / "soul.md"
    if not soul_path.exists():
        # Structure aligned with agent_template/soul.md (single source of truth)
        agent_name = str(agent_id)[:8]
        role_desc = "digital assistant"
        try:
            # soul.md bootstrap reads the whole agent row by PK to seed name/role.
            # Workspace creation runs both on the tool-resolve path and the
            # heartbeat daemon; an audited single-row bypass is the sanctioned
            # chicken-and-egg breaker under enforced RLS (RLS 阶段1).
            async with (
                async_session() as db,
                enter_rls_bypass(db, reason=f"workspace soul.md bootstrap for agent {agent_id}"),
            ):
                result = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = result.scalar_one_or_none()
                if agent:
                    agent_name = agent.name or agent_name
                    role_desc = agent.role_description or role_desc
        except Exception as exc:
            logger.warning("[Workspace] Failed to load agent for soul.md: %s", exc)
        soul_content = f"""# Soul — {agent_name}

## Identity
- Name: {agent_name}
- Role: {role_desc}

## Personality
- Diligent and detail-oriented
- Proactively reports work progress
- Asks for confirmation when encountering uncertain information

## Boundaries
- Follows company confidentiality policies
- Sensitive operations require creator approval
"""
        # Atomic write-if-not-exists to prevent race condition (ME-07)
        try:
            with open(soul_path, "x", encoding="utf-8") as f:
                f.write(soul_content.strip() + "\n")
        except FileExistsError:
            logger.debug("[Workspace] soul.md already exists for agent %s (concurrent create)", agent_id)

    # Bootstrap evolution seed files
    _bootstrap_evolution_files(ws)

    # Legacy migration runs only at startup via migrate_all_workspaces() (main.py lifespan).
    # The marker file `.legacy_migrated` short-circuits any stragglers; no per-call cost here.
    rebuild_index(WORKSPACE_ROOT, agent_id)

    if not (ws / "HEARTBEAT.md").exists():
        try:
            hb_content = _HEARTBEAT_TEMPLATE_PATH.read_text(encoding="utf-8")
        except Exception:
            hb_content = (
                "# Heartbeat\n\n"
                "Review your recent work and memory, take one evidence-backed useful action, "
                "then reply HEARTBEAT_OK if nothing needed.\n"
            )
        (ws / "HEARTBEAT.md").write_text(hb_content, encoding="utf-8")

    # Pre-install system skills from templates (skip is_default: false)
    templates_dir = Path(__file__).resolve().parent.parent / "templates" / "skills"
    if templates_dir.is_dir():
        for skill_tmpl in templates_dir.iterdir():
            if not skill_tmpl.is_dir():
                continue
            if (ws / "skills" / skill_tmpl.name / "SKILL.md").exists():
                continue
            # Check if the skill template is marked is_default: false — skip if so
            _skill_md = skill_tmpl / "SKILL.md"
            if _skill_md.exists():
                try:
                    _content = _skill_md.read_text(encoding="utf-8")
                    if "is_default: false" in _content:
                        continue
                except Exception as exc:
                    logger.debug("[workspace] Could not read SKILL.md for %s: %s", skill_tmpl.name, exc)
            dest = ws / "skills" / skill_tmpl.name
            dest.mkdir(parents=True, exist_ok=True)
            for f in skill_tmpl.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(skill_tmpl)
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(f), str(dest / rel))

    await _sync_tasks_to_file(agent_id, ws)
    return ws


async def _sync_tasks_to_file(agent_id: uuid.UUID, ws: Path) -> None:
    """Sync tasks from DB to tasks.json in workspace."""
    from app.services.tenant_resolver import resolve_tenant_for_agent

    try:
        # RLS 阶段2b: tasks now bears a USING-only policy. Pin the GUC to the
        # agent's tenant so this read survives the non-owner role; a bare
        # session would fail closed and write an empty tasks.json.
        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(select(Task).where(Task.agent_id == agent_id).order_by(Task.created_at.desc()))
            tasks = result.scalars().all()

        task_list = []
        for task in tasks:
            task_list.append(
                {
                    "title": task.title,
                    "status": task.status,
                    "priority": task.priority,
                    "description": task.description or "",
                    "created_at": task.created_at.isoformat() if task.created_at else "",
                    "completed_at": task.completed_at.isoformat() if task.completed_at else "",
                }
            )

        (ws / "tasks.json").write_text(
            json.dumps(task_list, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("[Workspace] Failed to sync tasks for agent %s: %s", agent_id, exc)
