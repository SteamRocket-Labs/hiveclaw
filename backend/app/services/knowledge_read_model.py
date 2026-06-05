"""Knowledge read model — structured views over the agent's memory engine.

Spec §11 / §12 P7: the frontend stops parsing raw file layout; this service
assembles stable, structured read models from the MD truth source and its
sidecars. Pure read side — zero writes, zero LLM calls.

Sources: soul.md, T3 entry manifest, memory/lifecycle.json,
memory/distillation_audit.jsonl, memory/wiki/, memory/scenes/,
memory/auto_dream_state.json, evolution/ ledgers, learnings cursors, and
skill/workflow candidate markers. Raw Markdown stays available through the
existing workspace file APIs as the advanced view.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


def _distiller_status(state_path: Path, *, label: str) -> dict:
    if not state_path.exists():
        return {"name": label, "state": "never_ran", "last_run_at": ""}
    return {"name": label, "state": "active", "last_run_at": _file_mtime_iso(state_path)}


# ── Overview ──


def build_knowledge_overview(data_root: Path, agent_id: uuid.UUID) -> dict:
    from app.memory.lifecycle_store import MemoryLifecycleStore, lifecycle_path
    from app.memory.md_store import build_t3_entry_manifest

    root = _agent_root(data_root, agent_id)
    soul_text = _read_text(root / "soul.md")
    soul_sections = sum(1 for line in soul_text.splitlines() if line.startswith("## "))

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

    pending_soul = 0
    for record in _read_jsonl(root / "evolution" / "evolution_ledger.jsonl", 500):
        if record.get("event") == "memory_promotion_decision" and record.get("decision") == "hold":
            pending_soul += 1

    skills_dir = root / "skills"
    skills_count = len(list(skills_dir.glob("*/SKILL.md"))) if skills_dir.exists() else 0

    from app.services.skill_distiller import load_memory_skill_candidates, load_memory_workflow_candidates

    skill_candidates = load_memory_skill_candidates(data_root, agent_id)
    workflow_candidates = load_memory_workflow_candidates(data_root, agent_id)

    return {
        "identity": {
            "sections": soul_sections,
            "frozenSections": 0,
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
        "distillers": {
            "extractor": _distiller_status(root / "memory" / "learnings" / ".extract_cursor.json", label="extractor"),
            "heartbeat": _distiller_status(root / "memory" / ".curation_cursor.json", label="heartbeat"),
            "dream": _distiller_status(root / "memory" / "auto_dream_state.json", label="dream"),
            "skillDistiller": _distiller_status(
                root / "evolution" / "skill_distiller_state.json", label="skill_distiller"
            ),
        },
        "linkedCapabilities": {
            "skillsReferenced": skills_count,
            "workflowsReferenced": len(workflow_candidates),
            "mcpToolsReferenced": 0,
            "skillCandidates": len(skill_candidates),
        },
    }


# ── Pages (wiki + scenes) ──


def list_knowledge_pages(data_root: Path, agent_id: uuid.UUID) -> list[dict]:
    root = _agent_root(data_root, agent_id) / "memory"
    pages: list[dict] = []
    for kind, subdir in (("wiki", "wiki"), ("scene", "scenes")):
        directory = root / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            text = _read_text(path)
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


def get_knowledge_page(data_root: Path, agent_id: uuid.UUID, page_id: str) -> dict | None:
    subdir, _, slug = page_id.partition("/")
    if subdir not in {"wiki", "scenes"} or not _SLUG_SAFE_RE.match(slug or ""):
        return None
    path = _agent_root(data_root, agent_id) / "memory" / subdir / f"{slug}.md"
    if not path.exists():
        return None
    text = _read_text(path)

    # P9 wikilink navigation: outgoing/incoming edges from the derived
    # relation graph (rebuilt from Markdown — never persisted).
    links: dict = {"outgoing": [], "incoming": []}
    try:
        from app.memory.relation_graph import build_relation_graph

        links = build_relation_graph(data_root, agent_id).links_for(page_id)
    except Exception as exc:  # noqa: BLE001 — navigation is an accelerator, never blocks the page read
        logger.debug("[KnowledgeReadModel] relation graph failed for %s: %s", agent_id, exc)

    return {
        "id": page_id,
        "kind": "wiki" if subdir == "wiki" else "scene",
        "slug": slug,
        "frontmatter": _parse_frontmatter(text),
        "markdown": text,
        "updatedAt": _file_mtime_iso(path),
        "links": links,
    }


# ── Entries ──


def list_knowledge_entries(data_root: Path, agent_id: uuid.UUID) -> list[dict]:
    from app.memory.md_store import build_t3_entry_manifest, compute_entry_heat

    entries: list[dict] = []
    for entry in build_t3_entry_manifest(data_root, agent_id):
        metadata = entry.metadata
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
        if record.get("outcome") == "held":
            held.append(
                {
                    "at": str(record.get("at", "")),
                    "stage": str(record.get("stage", "")),
                    "reason": str(record.get("reason", "")),
                    "detail": record.get("detail") or {},
                }
            )

    soul_candidates: list[dict] = []
    for record in _read_jsonl(root / "evolution" / "evolution_ledger.jsonl", 500):
        if record.get("event") == "memory_promotion_decision" and record.get("decision") == "hold":
            soul_candidates.append(
                {
                    "candidateId": str(record.get("candidate_id", "")),
                    "reason": str(record.get("reason", "")),
                    "at": str(record.get("at") or record.get("recorded_at") or ""),
                }
            )

    return {
        "skillCandidates": load_memory_skill_candidates(data_root, agent_id),
        "workflowCandidates": load_memory_workflow_candidates(data_root, agent_id),
        "soulCandidates": soul_candidates,
        "heldCurations": held[-50:],
    }
