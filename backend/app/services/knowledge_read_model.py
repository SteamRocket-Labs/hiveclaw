"""Knowledge read model — structured views over the agent's memory engine.

Spec §11 / §12 P7: the frontend stops parsing raw file layout; this service
assembles stable, structured read models from the MD truth source and its
sidecars. Pure read side — zero writes, zero LLM calls.

Sources: soul.md, T3 entry manifest, generated memory/indexes/wiki_map.md,
memory/control/lifecycle.json, memory/distillation_audit.jsonl, derived/compat
memory/wiki/ and memory/scenes/ pages, memory/control/auto_dream_state.json,
canonical T0/T2/T3 freshness anchors, and skill/workflow candidate markers. Raw Markdown
stays available through the existing workspace file APIs as the advanced view.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path

from app.services.principal_context import PrincipalStack

logger = logging.getLogger(__name__)

_SLUG_SAFE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_EVENT_LIMIT = 100


def _agent_root(data_root: Path, agent_id: uuid.UUID) -> Path:
    return Path(data_root) / str(agent_id)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
    except OSError:
        return []
    return records[-limit:]


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_soul_candidate_manifests(root: Path) -> list[dict]:
    manifests: list[dict] = []
    candidate_root = root / "memory" / ".staging" / "soul_candidates"
    if not candidate_root.exists():
        return manifests
    for manifest_path in sorted(candidate_root.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        if not manifest:
            continue
        manifest.setdefault("candidate_id", manifest_path.parent.name)
        manifests.append(manifest)
    return manifests


def _soul_candidate_audit_by_id(root: Path) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for record in _read_jsonl(root / "memory" / "distillation_audit.jsonl", 500):
        if record.get("stage") != "soul_candidate":
            continue
        detail = record.get("detail") if isinstance(record.get("detail"), dict) else {}
        candidate_id = str(detail.get("candidate_id") or record.get("candidate_id") or "")
        if candidate_id:
            by_id[candidate_id] = record
    return by_id


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _file_mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return ""


def _mtime_iso(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


_STALE_MULTIPLIER = 3  # state more than 3× its pipeline cadence behind the newest input → stale
_EXTRACTOR_GRACE_SECONDS = 24 * 3600  # per-response extraction has no cadence; a day behind fresh behavior = stale


def _newest_mtime(paths) -> float | None:
    newest: float | None = None
    for path in paths:
        try:
            ts = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or ts > newest:
            newest = ts
    return newest


def _distiller_status(
    state_path: Path,
    *,
    label: str,
    stale_seconds: float | None = None,
    input_anchor: float | None = None,
) -> dict:
    """Distiller pipeline status for the knowledge panel (closure plan A1: exists ≠ fresh).

    ``stale`` means the pipeline's newest *input* mtime is more than
    ``stale_seconds`` ahead of the state file — input keeps arriving but the
    pipeline is not keeping up. An idle agent (no input newer than the state)
    is never stale: a lying "stale" would be the same defect as the lying
    "active" this replaces. Missing state stays ``never_ran``.
    """
    if not state_path.exists():
        return {"name": label, "state": "never_ran", "last_run_at": ""}
    state = "active"
    if stale_seconds is not None and input_anchor is not None:
        try:
            mtime = state_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and input_anchor - mtime > stale_seconds:
            state = "stale"
    return {"name": label, "state": state, "last_run_at": _file_mtime_iso(state_path)}


def _distiller_status_from_anchor(
    output_anchor: float | None,
    *,
    label: str,
    stale_seconds: float | None = None,
    input_anchor: float | None = None,
) -> dict:
    """Status for pipelines whose canonical state is a committed output anchor.

    This avoids resurrecting legacy cursor files as truth. If canonical output
    never landed, the pipeline has never run. If newer canonical input outruns
    the output by the stale window, it is stale; otherwise it is active.
    """
    if output_anchor is None:
        return {"name": label, "state": "never_ran", "last_run_at": ""}
    state = "active"
    if stale_seconds is not None and input_anchor is not None and input_anchor - output_anchor > stale_seconds:
        state = "stale"
    return {"name": label, "state": state, "last_run_at": _mtime_iso(output_anchor)}


def _newest_t0_input_anchor(mem_dir: Path) -> float | None:
    t0_sessions = mem_dir / "t0" / "sessions"
    return _newest_mtime(
        chain(
            t0_sessions.glob("*/index.json"),
            t0_sessions.glob("*/segments/*/events.jsonl"),
            t0_sessions.glob("*/segments/*/source.md"),
        )
    )


def _newest_t2_segment_output_anchor(mem_dir: Path) -> float | None:
    t2_sessions = mem_dir / "t2" / "sessions"
    return _newest_mtime(
        chain(
            t2_sessions.glob("*/segments/*/manifest.json"),
            t2_sessions.glob("*/segments/*/summary.md"),
            t2_sessions.glob("*/segments/*/labels.md"),
            t2_sessions.glob("*/segments/*/review.md"),
        )
    )


def _newest_t3_input_anchor(mem_dir: Path) -> float | None:
    t2_sessions = mem_dir / "t2" / "sessions"
    return _newest_mtime(
        chain(
            t2_sessions.glob("*/segments/*/manifest.json"),
            t2_sessions.glob("*/episodes/*/manifest.json"),
            [mem_dir / "explicit" / "manifest.jsonl"],
        )
    )


def _newest_t3_output_anchor(mem_dir: Path) -> float | None:
    candidate_paths: list[Path] = []
    for rel in ("self/self.md", "profiles/owner.md", "profiles/collaborators.md", "profiles/domain.md"):
        path = mem_dir / rel
        if path.exists():
            candidate_paths.append(path)
    for subdir in ("knowledge", "milestones"):
        directory = mem_dir / subdir
        if directory.exists():
            candidate_paths.extend(directory.glob("*.md"))
    return _newest_mtime(candidate_paths)


def _dream_state_read_path(root: Path) -> Path:
    canonical = root / "memory" / "control" / "auto_dream_state.json"
    if canonical.exists():
        return canonical
    legacy = root / "memory" / "auto_dream_state.json"
    if legacy.exists():
        return legacy
    return canonical


def _build_distiller_statuses(root: Path) -> dict:
    """Per-pipeline freshness, each judged against its own input side.

    extractor consumes the append-only T0 session ledger, the T3 consolidator
    consumes reviewed T2/explicit packages, and Dream consumes active T3 files.
    skill_distiller keeps the two-state
    contract: its real input is skill/workflow *candidates* inside T2, so a
    T2-mtime anchor would false-positive on agents that learn without
    producing candidates — never mis-report stale.
    """
    from app.config import get_settings
    from app.services.auto_dream import MIN_HOURS_BETWEEN_DREAMS

    mem_dir = root / "memory"
    t0_input_anchor = _newest_t0_input_anchor(mem_dir)
    t2_output_anchor = _newest_t2_segment_output_anchor(mem_dir)
    t3_input_anchor = _newest_t3_input_anchor(mem_dir)
    t3_output_anchor = _newest_t3_output_anchor(mem_dir)
    dream_state_path = _dream_state_read_path(root)

    heartbeat_window = _STALE_MULTIPLIER * get_settings().HEARTBEAT_DEFAULT_INTERVAL_MINUTES * 60
    dream_window = _STALE_MULTIPLIER * MIN_HOURS_BETWEEN_DREAMS * 3600

    return {
        "t2_pipeline": _distiller_status_from_anchor(
            t2_output_anchor,
            label="t2_pipeline",
            stale_seconds=_EXTRACTOR_GRACE_SECONDS,
            input_anchor=t0_input_anchor,
        ),
        "heartbeat": _distiller_status_from_anchor(
            t3_output_anchor,
            label="heartbeat",
            stale_seconds=heartbeat_window,
            input_anchor=t3_input_anchor,
        ),
        "dream": _distiller_status(
            dream_state_path,
            label="dream",
            stale_seconds=dream_window,
            input_anchor=t3_output_anchor,
        ),
        "skillDistiller": _distiller_status(root / "evolution" / "skill_distiller_state.json", label="skill_distiller"),
    }


# ── Overview ──


_FAILURE_MODE_STATUS_KEYS = {"active": "active", "规避中": "mitigating", "已根除": "resolved"}


def build_knowledge_overview(data_root: Path, agent_id: uuid.UUID) -> dict:
    """Two-plane overview: per-plane counts (with the self failure-mode
    lifecycle), pipeline health, growth freshness, distiller states. The
    retired flat-T3 lifecycle counters do not resurface here."""
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.memory.plane_read import list_knowledge_pages, list_profile_entries

    root = _agent_root(data_root, agent_id)
    soul_text = _read_text(root / "soul.md")
    soul_blocks = [line.strip() for line in soul_text.splitlines() if line.strip().lower().startswith("<soul_")]
    soul_sections = len(soul_blocks) or sum(1 for line in soul_text.splitlines() if line.startswith("## "))
    frozen_sections = sum(1 for line in soul_blocks if 'frozen="true"' in line.lower())

    plane_entries = list_profile_entries(data_root, agent_id)
    self_entries = [entry for entry in plane_entries if str(entry.get("source") or "").endswith("self/self.md")]
    profile_entries = [entry for entry in plane_entries if entry not in self_entries]
    failure_modes = {"active": 0, "mitigating": 0, "resolved": 0}
    for entry in self_entries:
        if not str(entry.get("id") or "").startswith("fm-"):
            continue
        status = ""
        for line in str(entry.get("content") or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("- 状态:"):
                status = stripped.removeprefix("- 状态:").strip()
                break
        key = _FAILURE_MODE_STATUS_KEYS.get(status, "active")
        failure_modes[key] += 1

    plane_pages = list_knowledge_pages(data_root, agent_id)
    knowledge_pages = sum(1 for page in plane_pages if page.get("kind") == "knowledge")
    milestone_pages = sum(1 for page in plane_pages if page.get("kind") == "milestone")
    explicit_active = sum(
        1 for entry in load_explicit_overlay_entries(Path(data_root), agent_id) if entry.status == "active"
    )

    pending_soul = sum(
        1
        for manifest in _load_soul_candidate_manifests(root)
        if str(manifest.get("status") or "").lower() not in {"committed", "rejected", "archived"}
    )

    debt = _read_json(root / "memory" / "control" / "consolidation_debt.json")
    pipeline = (
        {
            "pendingPackages": debt.get("pending_packages"),
            "heldJobs": debt.get("held_jobs"),
            "stalled": bool(debt.get("stalled")),
            "lastAssessedAt": str(debt.get("generated_at") or ""),
        }
        if debt
        else {}
    )
    growth_history = _read_jsonl(root / "memory" / "control" / "growth_metrics_history.jsonl", limit=1)
    growth = (
        {
            "generatedAt": str(growth_history[-1].get("generated_at") or ""),
            "reportPath": "memory/control/growth_report.md",
        }
        if growth_history
        else {}
    )

    skills_dir = root / "skills"
    skills_count = len(list(skills_dir.glob("*/SKILL.md"))) if skills_dir.exists() else 0

    from app.services.skill_distiller import load_memory_skill_candidates, load_memory_workflow_candidates

    skill_candidates = load_memory_skill_candidates(data_root, agent_id)
    workflow_candidates = load_memory_workflow_candidates(data_root, agent_id)

    return {
        "identity": {
            "sections": soul_sections,
            "frozenSections": frozen_sections,
            "pendingSoulCandidates": pending_soul,
            "lastUpdated": _file_mtime_iso(root / "soul.md"),
        },
        "planes": {
            "self": {"entries": len(self_entries), "failureModes": failure_modes},
            "profiles": {"entries": len(profile_entries)},
            "knowledge": {"pages": knowledge_pages},
            "milestones": {"pages": milestone_pages},
            "explicit": {"active": explicit_active},
        },
        "pipeline": pipeline,
        "growth": growth,
        "distillers": _build_distiller_statuses(root),
        "linkedCapabilities": {
            "skillsReferenced": skills_count,
            "workflowsReferenced": len(workflow_candidates),
            "mcpToolsReferenced": 0,
            "skillCandidates": len(skill_candidates),
        },
    }


def attach_dream_runtime_status(overview: dict, task) -> dict:
    """Overlay DB execution truth without replacing file-backed Dream output truth."""

    if task is None:
        return overview
    result = {**overview, "distillers": dict(overview.get("distillers") or {})}
    dream = dict(result["distillers"].get("dream") or {})
    metadata = dict(getattr(task, "metadata_json", None) or {})
    runtime_outcome = dict(metadata.get("last_attempt_outcome") or metadata.get("outcome") or {})
    coverage = dict(runtime_outcome.get("coverage") or {})
    dream.update(
        {
            "runtime_status": str(getattr(task, "status", "") or ""),
            "runtime_task_id": str(getattr(task, "id", "") or ""),
            "runtime_phase": str(metadata.get("phase") or ""),
            "runtime_mode": str(metadata.get("dream_mode") or ""),
            "runtime_result": str(getattr(task, "result_summary", None) or ""),
            "runtime_created_at": (task.created_at.isoformat() if getattr(task, "created_at", None) else None),
            "coverage_total": int(coverage.get("total") or 0),
            "coverage_reviewed": int(coverage.get("reviewed") or 0),
            "coverage_complete": bool(coverage.get("complete")),
            "coverage_state": (
                "complete" if coverage.get("complete") else "incomplete" if coverage else "legacy_unknown"
            ),
        }
    )
    result["distillers"]["dream"] = dream
    return result


# ── Pages (wiki + scenes) ──


def list_knowledge_pages(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    principal_stack: PrincipalStack | None = None,
) -> list[dict]:
    from app.memory.visibility import can_access_sensitivity, classify_text_sensitivity

    root = _agent_root(data_root, agent_id) / "memory"
    pages: list[dict] = []
    for kind, subdir in (("knowledge", "knowledge"), ("milestone", "milestones")):
        directory = root / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            text = _read_text(path)
            frontmatter = _parse_frontmatter(text)
            sensitivity = frontmatter.get("sensitivity") or classify_text_sensitivity(text)
            if not can_access_sensitivity(sensitivity, principal_stack):
                continue
            pages.append(
                {
                    "id": f"{subdir}/{path.stem}",
                    "kind": kind,
                    "slug": path.stem,
                    "title": frontmatter.get("title") or path.stem.replace("-", " ").title(),
                    "tags": frontmatter.get("tags", ""),
                    "status": frontmatter.get("status", "active"),
                    "updatedAt": _file_mtime_iso(path),
                }
            )
    return pages


def get_knowledge_page(
    data_root: Path,
    agent_id: uuid.UUID,
    page_id: str,
    *,
    principal_stack: PrincipalStack | None = None,
) -> dict | None:
    from app.memory.visibility import classify_and_redact_text

    subdir, _, slug = page_id.partition("/")
    if subdir not in {"knowledge", "milestones"} or not _SLUG_SAFE_RE.match(slug or ""):
        return None
    path = _agent_root(data_root, agent_id) / "memory" / subdir / f"{slug}.md"
    if not path.exists():
        return None
    text = _read_text(path)
    frontmatter = _parse_frontmatter(text)
    visible_text, _sensitivity = classify_and_redact_text(
        text,
        principal_stack,
        sensitivity=frontmatter.get("sensitivity"),
    )

    # P9 wikilink navigation: outgoing/incoming edges from the derived
    # relation graph (rebuilt from Markdown — never persisted).
    links: dict = {"outgoing": [], "incoming": []}
    if visible_text == text:
        try:
            from app.memory.relation_graph import build_relation_graph

            links = build_relation_graph(data_root, agent_id).links_for(page_id)
        except Exception as exc:  # noqa: BLE001 — navigation is an accelerator, never blocks the page read
            logger.debug("[KnowledgeReadModel] relation graph failed for %s: %s", agent_id, exc)

    return {
        "id": page_id,
        "kind": "wiki" if subdir == "wiki" else "scene",
        "slug": slug,
        "frontmatter": _parse_frontmatter(visible_text),
        "markdown": visible_text,
        "updatedAt": _file_mtime_iso(path),
        "links": links,
    }


# ── Entries ──


def list_knowledge_entries(
    data_root: Path,
    agent_id: uuid.UUID,
    *,
    principal_stack: PrincipalStack | None = None,
) -> list[dict]:
    from app.memory.plane_read import list_knowledge_pages, list_profile_entries

    entries: list[dict] = []
    plane_rows = [
        {**row, "category": "profile_plane", "timestamp": "", "metadata": {}}
        for row in list_profile_entries(data_root, agent_id)
    ] + [
        {**row, "category": row["kind"], "timestamp": "", "metadata": {}}
        for row in list_knowledge_pages(data_root, agent_id)
    ]
    del principal_stack  # two-plane rows are PL1; sensitive claims stay in governed evidence
    for entry in plane_rows:
        preview = entry.get("preview") or " ".join(str(entry.get("content", "")).split())[:160]
        entries.append(
            {
                "id": entry.get("id", ""),
                "file": entry.get("source", ""),
                "category": entry.get("category", ""),
                "content": entry.get("content", ""),
                "preview": preview,
                "timestamp": entry.get("timestamp", ""),
                "heat": 0.0,
                "recallCount": 0,
                "lastRecalledAt": "never",
                "sensitivity": "PL1_public",
                "status": entry.get("status", "active"),
                "containerCandidate": "",
                "promotedTo": "",
                "load": "P0 resident" if entry.get("category") == "profile_plane" else "query retrieval",
            }
        )
    return entries


# ── Events (timeline) ──


def list_knowledge_events(data_root: Path, agent_id: uuid.UUID, *, limit: int = _EVENT_LIMIT) -> list[dict]:
    root = _agent_root(data_root, agent_id)
    events: list[dict] = []

    for record in _read_jsonl(root / "memory" / "distillation_audit.jsonl", limit):
        events.append(
            {
                "at": str(record.get("at", "")),
                "kind": f"curation:{record.get('stage', 'unknown')}",
                "outcome": str(record.get("outcome", "")),
                "summary": str(record.get("reason", "")),
                "detail": record.get("detail") or {},
            }
        )

    dream_state_path = _dream_state_read_path(root)
    if dream_state_path.exists():
        try:
            payload = json.loads(dream_state_path.read_text(encoding="utf-8"))
            for item in payload.get("history") or []:
                if isinstance(item, dict):
                    events.append(
                        {
                            "at": str(item.get("timestamp", "")),
                            "kind": "dream:consolidation",
                            "outcome": str(item.get("strategy", "")),
                            "summary": (
                                f"facts {item.get('facts_before', 0)} → {item.get('facts_after', 0)} "
                                f"(v{item.get('version', '?')})"
                            ),
                            "detail": item,
                        }
                    )
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("[KnowledgeReadModel] dream state unreadable for %s: %s", agent_id, exc)

    events.sort(key=lambda item: item["at"], reverse=True)
    return events[:limit]


# ── Candidates ──


def list_knowledge_candidates(data_root: Path, agent_id: uuid.UUID) -> dict:
    from app.services.skill_distiller import load_memory_skill_candidates, load_memory_workflow_candidates

    root = _agent_root(data_root, agent_id)

    held: list[dict] = []
    for record in _read_jsonl(root / "memory" / "distillation_audit.jsonl", 200):
        if record.get("outcome") == "held" and record.get("stage") != "soul_candidate":
            held.append(
                {
                    "at": str(record.get("at", "")),
                    "stage": str(record.get("stage", "")),
                    "reason": str(record.get("reason", "")),
                    "detail": record.get("detail") or {},
                }
            )

    soul_candidates: list[dict] = []
    audit_by_id = _soul_candidate_audit_by_id(root)
    for manifest in _load_soul_candidate_manifests(root):
        status = str(manifest.get("status") or "").lower()
        if status in {"committed", "rejected", "archived"}:
            continue
        candidate_id = str(manifest.get("candidate_id") or "")
        audit = audit_by_id.get(candidate_id) or {}
        soul_candidates.append(
            {
                "candidateId": candidate_id,
                "reason": str(manifest.get("reason") or audit.get("reason") or ""),
                "at": str(manifest.get("created_at") or audit.get("at") or ""),
                "status": status or "candidate",
                "targetPath": str(manifest.get("target_path") or "soul.md"),
            }
        )

    return {
        "skillCandidates": load_memory_skill_candidates(data_root, agent_id),
        "workflowCandidates": load_memory_workflow_candidates(data_root, agent_id),
        "soulCandidates": soul_candidates,
        "heldCurations": held[-50:],
    }


def build_memory_observability(data_root: Path, agent_id: uuid.UUID) -> dict:
    """C8 observability read model over the derived index tables: latest debt
    state (control sidecar), debt trajectory, and per-axis label aggregates.
    Derived data only — absence reads as empty, never as an error."""
    import sqlite3

    from app.memory.reference_index import index_db_path

    root = _agent_root(data_root, agent_id)
    debt = _read_json(root / "memory" / "control" / "consolidation_debt.json")
    debt_payload = (
        {
            "assessed_at": str(debt.get("generated_at") or ""),
            "pending_packages": debt.get("pending_packages"),
            "pending_stitch_packages": debt.get("pending_stitch_packages"),
            "oldest_pending_age_hours": debt.get("oldest_pending_age_hours"),
            "held_jobs": debt.get("held_jobs"),
            "exhausted_jobs": debt.get("exhausted_jobs"),
            "active_explicit_entries": debt.get("active_explicit_entries"),
            "oldest_explicit_age_hours": debt.get("oldest_explicit_age_hours"),
            "stalled": bool(debt.get("stalled")),
            "stall_reasons": debt.get("stall_reasons") or [],
        }
        if debt
        else {}
    )

    debt_history: list[dict] = []
    label_axes: dict[str, dict[str, int]] = {}
    db_path = index_db_path(data_root, agent_id)
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if "consolidation_debt_history" in tables:
                    debt_history = [
                        {
                            "assessed_at": row[0],
                            "pending_packages": row[1],
                            "pending_stitch_packages": row[2],
                            "oldest_pending_age_hours": row[3],
                            "held_jobs": row[4],
                            "exhausted_jobs": row[5],
                            "active_explicit_entries": row[6],
                            "oldest_explicit_age_hours": row[7],
                            "stalled": bool(row[8]),
                            "stall_reasons": json.loads(row[9] or "[]"),
                        }
                        for row in conn.execute(
                            "SELECT * FROM consolidation_debt_history ORDER BY assessed_at DESC LIMIT 100"
                        )
                    ]
                if "t2_label_axes" in tables:
                    for axis, value, count in conn.execute(
                        "SELECT axis, value, COUNT(*) FROM t2_label_axes GROUP BY axis, value"
                    ):
                        label_axes.setdefault(str(axis), {})[str(value)] = int(count)
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("Memory observability index read failed for %s: %s", agent_id, exc)

    growth: dict = {}
    growth_history = _read_jsonl(root / "memory" / "control" / "growth_metrics_history.jsonl", limit=1)
    if growth_history:
        latest = growth_history[-1]
        growth = {
            "generated_at": str(latest.get("generated_at") or ""),
            "metrics": latest,
            "report_path": "memory/control/growth_report.md",
        }

    return {"debt": debt_payload, "debt_history": debt_history, "label_axes": label_axes, "growth": growth}
