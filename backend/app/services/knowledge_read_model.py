"""Knowledge read model — structured views over the agent's memory engine.

Spec §11 / §12 P7: the frontend stops parsing raw file layout; this service
assembles stable, structured read models from the MD truth source and its
sidecars. Pure read side — zero writes, zero LLM calls.

Sources: soul.md, T3 entry manifest, generated memory/wiki_map.md,
memory/lifecycle.json, memory/distillation_audit.jsonl, derived/compat
memory/wiki/ and memory/scenes/ pages, memory/auto_dream_state.json,
legacy learnings cursors, and skill/workflow candidate markers. Raw Markdown
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


def _build_distiller_statuses(root: Path) -> dict:
    """Per-pipeline freshness, each judged against its own input side.

    extractor consumes the append-only T0 session ledger, heartbeat consumes T2 learnings,
    dream consumes active T3 files. skill_distiller keeps the two-state
    contract: its real input is skill/workflow *candidates* inside T2, so a
    learnings-mtime anchor would false-positive on agents that learn without
    producing candidates — never mis-report stale.
    """
    from app.config import get_settings
    from app.memory.md_store import T3_FILE_SPECS
    from app.services.auto_dream import MIN_HOURS_BETWEEN_DREAMS

    mem_dir = root / "memory"
    behavior_anchor = _newest_mtime(
        chain(
            (mem_dir / "t0" / "sessions").glob("*/segments/*/source.md"),
            (root / "logs").glob("*/behavior/*"),
        )
    )
    learnings_anchor = _newest_mtime((mem_dir / "learnings").glob("*.md"))
    t3_anchor = _newest_mtime(mem_dir / spec["filename"] for spec in T3_FILE_SPECS)

    heartbeat_window = _STALE_MULTIPLIER * get_settings().HEARTBEAT_DEFAULT_INTERVAL_MINUTES * 60
    dream_window = _STALE_MULTIPLIER * MIN_HOURS_BETWEEN_DREAMS * 3600

    return {
        "extractor": _distiller_status(
            mem_dir / "learnings" / ".extract_cursor.json",
            label="extractor",
            stale_seconds=_EXTRACTOR_GRACE_SECONDS,
            input_anchor=behavior_anchor,
        ),
        "heartbeat": _distiller_status(
            mem_dir / ".curation_cursor.json",
            label="heartbeat",
            stale_seconds=heartbeat_window,
            input_anchor=learnings_anchor,
        ),
        "dream": _distiller_status(
            mem_dir / "auto_dream_state.json",
            label="dream",
            stale_seconds=dream_window,
            input_anchor=t3_anchor,
        ),
        "skillDistiller": _distiller_status(root / "evolution" / "skill_distiller_state.json", label="skill_distiller"),
    }


# ── Overview ──


def build_knowledge_overview(data_root: Path, agent_id: uuid.UUID) -> dict:
    from app.memory.lifecycle_store import MemoryLifecycleStore, lifecycle_path
    from app.memory.md_store import build_t3_entry_manifest

    root = _agent_root(data_root, agent_id)
    soul_text = _read_text(root / "soul.md")
    soul_blocks = [
        line.strip()
        for line in soul_text.splitlines()
        if line.strip().lower().startswith("<soul_")
    ]
    soul_sections = len(soul_blocks) or sum(1 for line in soul_text.splitlines() if line.startswith("## "))
    frozen_sections = sum(1 for line in soul_blocks if 'frozen="true"' in line.lower())

    manifest = build_t3_entry_manifest(data_root, agent_id)
    sensitive_suppressed = sum(
        1 for entry in manifest if entry.metadata.get("sensitivity", "").startswith(("PL3", "PL4"))
    )

    lifecycle_counts = {"superseded": 0, "archived": 0, "stale": 0}
    store = MemoryLifecycleStore(lifecycle_path(data_root, agent_id))
    for entry in store._entries.values():  # noqa: SLF001 — read model over the same package's store
        status = entry.status.value
        if status in lifecycle_counts:
            lifecycle_counts[status] += 1

    pending_soul = sum(
        1
        for manifest in _load_soul_candidate_manifests(root)
        if str(manifest.get("status") or "").lower() not in {"committed", "rejected", "archived"}
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
        "memory": {
            "active": len(manifest),
            "stale": lifecycle_counts["stale"],
            "superseded": lifecycle_counts["superseded"],
            "archived": lifecycle_counts["archived"],
            "sensitiveSuppressed": sensitive_suppressed,
        },
        "distillers": _build_distiller_statuses(root),
        "linkedCapabilities": {
            "skillsReferenced": skills_count,
            "workflowsReferenced": len(workflow_candidates),
            "mcpToolsReferenced": 0,
            "skillCandidates": len(skill_candidates),
        },
    }


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
    for kind, subdir in (("wiki", "wiki"), ("scene", "scenes")):
        directory = root / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            text = _read_text(path)
            if not can_access_sensitivity(classify_text_sensitivity(text), principal_stack):
                continue
            frontmatter = _parse_frontmatter(text)
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
    if subdir not in {"wiki", "scenes"} or not _SLUG_SAFE_RE.match(slug or ""):
        return None
    path = _agent_root(data_root, agent_id) / "memory" / subdir / f"{slug}.md"
    if not path.exists():
        return None
    text = _read_text(path)
    visible_text, _sensitivity = classify_and_redact_text(text, principal_stack)

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
    from app.memory.md_store import build_t3_entry_manifest, compute_entry_heat
    from app.memory.visibility import can_access_metadata

    entries: list[dict] = []
    for entry in build_t3_entry_manifest(data_root, agent_id):
        metadata = entry.metadata
        if not can_access_metadata(metadata, principal_stack):
            continue
        entries.append(
            {
                "id": entry.entry_id,
                "file": entry.filename,
                "category": entry.category,
                "content": entry.content,
                "preview": entry.preview,
                "timestamp": entry.timestamp,
                "heat": compute_entry_heat(metadata),
                "recallCount": int(metadata.get("access_count", "0") or 0),
                "lastRecalledAt": metadata.get("last_accessed", "never"),
                "sensitivity": metadata.get("sensitivity", "PL1_public"),
                "status": metadata.get("status", "active"),
                "containerCandidate": metadata.get("container", ""),
                "promotedTo": metadata.get("promoted_to", ""),
                "load": entry.load,
            }
        )
    entries.sort(key=lambda item: item["heat"], reverse=True)
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

    dream_state_path = root / "memory" / "auto_dream_state.json"
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
