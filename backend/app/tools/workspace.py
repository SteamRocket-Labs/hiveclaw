"""Workspace bootstrap helpers for tool runtime."""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, tenant_scoped_session
from app.memory.hygiene import repair_agent_memory_hygiene
from app.memory.md_store import ensure_t3_layout, rebuild_index
from app.models.agent import Agent
from app.models.task import Task
from app.services.tenant_resolver import resolve_tenant_for_agent

logger = logging.getLogger(__name__)

_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.AGENT_DATA_DIR)

# Single source of truth for HEARTBEAT.md: app/templates/HEARTBEAT.md
_HEARTBEAT_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "HEARTBEAT.md"
_PLATFORM_ROOT_FILES = {"soul.md", "HEARTBEAT.md", "tasks.json", "DREAM.md"}
_PLATFORM_ROOT_DIRS = {
    "workspace",
    "skills",
    "memory",
    "evolution",
    "runtime_artifacts",
    "logs",
    "enterprise_info",
    "subagents",
}
_LEGACY_ROOT_WORK_FILES_DIR = Path("workspace") / "archived" / "legacy-root-files"

_EVOLUTION_SKILL_REVIEW_SEED = """\
# Skill Review

Lifecycle events for skills: candidate, promoted, patched, or superseded.

(none yet)
"""


def _bootstrap_evolution_files(ws: Path) -> None:
    """Create evolution seed files if they don't exist."""
    seeds = {
        "evolution/skill_review.md": _EVOLUTION_SKILL_REVIEW_SEED,
    }
    for rel_path, content in seeds.items():
        fpath = ws / rel_path
        if not fpath.exists():
            fpath.write_text(content, encoding="utf-8")
    legacy_candidates = ws / "evolution" / "skill_candidates.md"
    if legacy_candidates.exists():
        _move_legacy_file_if_needed(
            legacy_candidates,
            ws / "evolution" / "legacy" / "skill_candidates.md",
        )


# PR-12: rewrote the template with XML sections + a decision matrix. Use one
# of the new unique tags as the marker so agents on the pre-PR-12 template
# get upgraded on next startup migration.
_HEARTBEAT_MIGRATION_MARKER = "<two_plane_curation>"
_DEPRECATED_SKILLS = ("self-improving-agent", "proactive-agent")


def _move_legacy_file_if_needed(source: Path, target: Path) -> bool:
    if not source.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not target.exists():
            source.replace(target)
        else:
            source.unlink()
    except OSError as exc:
        logger.warning("[migrate] Failed to move legacy file %s -> %s: %s", source, target, exc)
        return False
    return True


def _move_legacy_directory_if_needed(source: Path, target: Path) -> bool:
    if not source.is_dir():
        return False
    target.mkdir(parents=True, exist_ok=True)
    moved_any = False
    for item in list(source.iterdir()):
        destination = target / item.name
        try:
            if item.is_dir():
                if destination.exists():
                    for nested in item.rglob("*"):
                        if not nested.is_file():
                            continue
                        rel = nested.relative_to(item)
                        nested_destination = destination / rel
                        nested_destination.parent.mkdir(parents=True, exist_ok=True)
                        if nested_destination.exists():
                            nested_destination.unlink()
                        nested.replace(nested_destination)
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.replace(destination)
            else:
                if destination.exists():
                    if item.name.endswith(".jsonl"):
                        with destination.open("a", encoding="utf-8") as handle:
                            handle.write(item.read_text(encoding="utf-8", errors="replace"))
                    item.unlink()
                else:
                    item.replace(destination)
            moved_any = True
        except OSError as exc:
            logger.warning("[migrate] Failed to move legacy directory entry %s -> %s: %s", item, destination, exc)
    try:
        source.rmdir()
    except OSError:
        pass
    return moved_any


_LEGACY_MEMORY_FILES = ("memory.md", "knowledge.md", "feedback.md", "INDEX.md", "index.md", "wiki_map.md")


def _archive_legacy_memory_files(agent_dir: Path) -> list[str]:
    """Move pre-two-plane single-file memories into the archive (never delete).

    Content reorganization into the two planes is the migration tool's job
    (app.scripts.migrate_memory_two_planes); this step only keeps the root
    memory/ dir canonical.
    """
    moved: list[str] = []
    memory_dir = agent_dir / "memory"
    archive_dir = memory_dir / ".archive" / "legacy_import"
    for name in _LEGACY_MEMORY_FILES:
        if _move_legacy_file_if_needed(memory_dir / name, archive_dir / name):
            moved.append(name)
    learnings = memory_dir / "learnings"
    if learnings.is_dir() and _move_legacy_directory_if_needed(learnings, archive_dir / "learnings"):
        moved.append("learnings/")
    return moved


def _migrate_workspace_runtime_artifacts(agent_dir: Path) -> list[str]:
    moved: list[str] = []
    mappings = {
        "workspace/session_memory.md": "runtime_artifacts/session_memory.md",
        "workspace/compaction_summary.md": "runtime_artifacts/compaction_summary.md",
        "workspace/recovery_manifest.json": "runtime_artifacts/recovery_manifest.json",
        "skills/.usage.json": "evolution/skill_usage.json",
        "state.json": "runtime_artifacts/agent_state.json",
    }
    for source_rel, target_rel in mappings.items():
        if _move_legacy_file_if_needed(agent_dir / source_rel, agent_dir / target_rel):
            moved.append(f"{source_rel}->{target_rel}")
    if _move_legacy_directory_if_needed(agent_dir / "traces", agent_dir / "runtime_artifacts" / "traces"):
        moved.append("traces/->runtime_artifacts/traces/")
    return moved


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Cannot find available archive path for {path}")


def _migrate_legacy_root_work_files(agent_dir: Path) -> list[str]:
    """Move legacy user work files out of the agent root into workspace/archive."""
    moved: list[str] = []
    archive_dir = agent_dir / _LEGACY_ROOT_WORK_FILES_DIR
    for child in sorted(agent_dir.iterdir(), key=lambda item: item.name):
        if child.name.startswith(".") or child.is_symlink():
            continue
        if child.is_dir():
            continue
        if child.name in _PLATFORM_ROOT_FILES:
            continue
        if child.name in _PLATFORM_ROOT_DIRS:
            continue
        target = _unique_destination(archive_dir / child.name)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            child.replace(target)
            moved.append(f"{child.name}->{target.relative_to(agent_dir).as_posix()}")
        except OSError as exc:
            logger.warning("[migrate] Failed to archive legacy root work file %s -> %s: %s", child, target, exc)
    return moved


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

        moved_runtime_artifacts = _migrate_workspace_runtime_artifacts(agent_dir)
        moved_runtime_artifacts.extend(_archive_legacy_memory_files(agent_dir))
        if moved_runtime_artifacts:
            logger.info(
                "[migrate] Runtime artifact paths for %s: %s",
                agent_dir.name,
                ", ".join(moved_runtime_artifacts),
            )
        hygiene_report = repair_agent_memory_hygiene(WORKSPACE_ROOT, agent_uuid, dry_run=False)
        if hygiene_report.get("changed"):
            logger.info(
                "[migrate] Memory hygiene for %s: entries_migrated=%d retired_artifacts=%d dead_stubs=%d",
                agent_dir.name,
                hygiene_report.get("entries_migrated", 0),
                len(hygiene_report.get("retired_artifacts", [])),
                len(hygiene_report.get("dead_stubs", [])),
            )
        moved_root_work_files = _migrate_legacy_root_work_files(agent_dir)
        if moved_root_work_files:
            logger.info(
                "[migrate] Archived legacy root work files for %s: %s",
                agent_dir.name,
                ", ".join(moved_root_work_files),
            )

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
        try:
            from app.services.skill_seeder import remove_legacy_flat_skill_files

            removed_flat = remove_legacy_flat_skill_files(agent_dir)
            if removed_flat:
                logger.info("[migrate] Removed legacy flat skill files from %s: %s", agent_dir.name, removed_flat)
        except Exception as exc:
            logger.warning("[migrate] Legacy flat skill cleanup failed for %s: %s", agent_dir.name, exc)

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
    (ws / "evolution").mkdir(exist_ok=True)
    (ws / "runtime_artifacts").mkdir(exist_ok=True)
    _migrate_workspace_runtime_artifacts(ws)
    _archive_legacy_memory_files(ws)
    ensure_t3_layout(WORKSPACE_ROOT, agent_id)

    if tenant_id:
        enterprise_dir = WORKSPACE_ROOT / f"enterprise_info_{tenant_id}"
    else:
        enterprise_dir = WORKSPACE_ROOT / "enterprise_info"
    enterprise_dir.mkdir(parents=True, exist_ok=True)

    profile_path = enterprise_dir / "company_profile.md"
    if not profile_path.exists():
        profile_path.write_text(
            "# Company Profile\n\n_Edit company information here. All digital employees can access this._\n\n## Basic Info\n- Company Name:\n- Industry:\n- Founded:\n\n## Business Overview\n\n## Organization Structure\n\n## Company Culture\n",
            encoding="utf-8",
        )

    # Pre-create the explicit overlay index. Two-plane memory files
    # (self/profiles/knowledge/milestones) are created by governed writers —
    # ensure_t3_layout above bootstraps the directories only; the legacy
    # memory/t3/ four-file skeleton is retired (C7 cutover).
    for seed_file, seed_content in [
        (
            "memory/explicit/MEMORY.md",
            "# Explicit Memory Overlay\n\nImmediate user-commanded memories awaiting T3 consolidation. Not accepted T3 truth.\n",
        ),
    ]:
        seed_path = ws / seed_file
        if not seed_path.exists():
            seed_path.parent.mkdir(parents=True, exist_ok=True)
            seed_path.write_text(seed_content, encoding="utf-8")

    # Pre-create the single generated Memory Wiki map. Root memory/wiki_map.md,
    # memory/INDEX.md, lower-case memory/index.md, and the old
    # .derived/t3_index.md path are retired.
    index_path = ws / "memory" / "indexes" / "wiki_map.md"
    if not index_path.exists():
        rebuild_index(WORKSPACE_ROOT, agent_id)

    soul_path = ws / "soul.md"
    if not soul_path.exists():
        # Structure aligned with agent_template/soul.md (single source of truth)
        agent_name = str(agent_id)[:8]
        role_desc = "digital assistant"
        try:
            resolved_tenant_id = None
            if tenant_id:
                try:
                    resolved_tenant_id = uuid.UUID(str(tenant_id))
                except ValueError:
                    resolved_tenant_id = None
            if resolved_tenant_id is None:
                resolved_tenant_id = await resolve_tenant_for_agent(
                    agent_id,
                    session_factory=async_session,
                )
            if resolved_tenant_id is not None:
                async with tenant_scoped_session(
                    resolved_tenant_id,
                    session_factory=async_session,
                    require_tenant=True,
                    source="workspace_soul_bootstrap",
                ) as db:
                    result = await db.execute(
                        select(Agent).where(
                            Agent.id == agent_id,
                            Agent.tenant_id == resolved_tenant_id,
                        )
                    )
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
