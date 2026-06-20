"""Unified read model for the Agent Evolution page.

The page is an audit/read surface. It must not become the writer for memory,
Skill semantics, or self-evolution state. This projection stitches together
the new Memory Learning Vault paths and the Skill ecosystem sidecars so the UI
can answer "where did evolution write?" without reviving legacy scorecard
semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.skill_curator import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, load_skill_usage
from app.services.skill_evolution_registry import registry_rel_path

_LEGACY_EVOLUTION_FILES = (
    "evolution/scorecard.md",
    "evolution/blocklist.md",
    "evolution/lineage.md",
    "evolution/skill_candidates.md",
)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _empty_view() -> dict[str, Any]:
    return {
        "schema": "agent_evolution_view.v2",
        "path_contract": {
            "t0_raw_evidence": "memory/t0/sessions/<session_id>/segments/<segment_id>/source.md",
            "t2_segment_packages": "memory/sessions/<session_id>/segments/<segment_id>/{summary,labels,review,manifest}",
            "t3_episodes": "memory/t3/episodes.md",
            "t3_user": "memory/t3/user.md",
            "t3_worker": "memory/t3/worker.md",
            "t3_capabilities": "memory/t3/capabilities.md",
            "soul": "soul.md",
            "t3_staging": "memory/.staging/t3_jobs/<job_id>/",
            "soul_staging": "memory/.staging/soul_candidates/<candidate_id>/",
            "skill_registry": registry_rel_path(),
            "skill_candidates": "evolution/skill_candidates/<candidate_id>/",
        },
        "memory_learning": {"pending_t3_jobs": [], "t3_targets": {}},
        "soul": {"pending_candidates": [], "active_path": "soul.md"},
        "skill_ecosystem": {
            "summary": {
                "total": 0,
                "active": 0,
                "stale": 0,
                "archived": 0,
                "evolvable": 0,
                "by_origin": {},
            },
            "skills": [],
        },
        "skill_tuning": {"candidates": []},
        "legacy_audit": {"detected_legacy_files": []},
    }


def _relative_manifest_path(workspace: Path, manifest_path: Path) -> str:
    try:
        return manifest_path.relative_to(workspace).as_posix()
    except ValueError:
        return manifest_path.as_posix()


def _load_manifests(workspace: Path, root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        if not manifest:
            continue
        manifest.setdefault("manifest_path", _relative_manifest_path(workspace, manifest_path))
        manifests.append(manifest)
    return manifests


def _count_lines(path: Path) -> int:
    try:
        return len([line for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()])
    except OSError:
        return 0


def _build_memory_learning(workspace: Path) -> dict[str, Any]:
    memory = workspace / "memory"
    targets = {
        "episodes": {"path": "memory/t3/episodes.md", "line_count": _count_lines(memory / "t3" / "episodes.md")},
        "user": {"path": "memory/t3/user.md", "line_count": _count_lines(memory / "t3" / "user.md")},
        "worker": {"path": "memory/t3/worker.md", "line_count": _count_lines(memory / "t3" / "worker.md")},
        "capabilities": {
            "path": "memory/t3/capabilities.md",
            "line_count": _count_lines(memory / "t3" / "capabilities.md"),
        },
    }
    return {
        "pending_t3_jobs": _load_manifests(workspace, memory / ".staging" / "t3_jobs"),
        "t3_targets": targets,
    }


def _build_soul(workspace: Path) -> dict[str, Any]:
    return {
        "active_path": "soul.md",
        "pending_candidates": _load_manifests(workspace, workspace / "memory" / ".staging" / "soul_candidates"),
    }


def _build_skill_ecosystem(workspace: Path) -> dict[str, Any]:
    registry = _read_json(workspace / registry_rel_path()) or {"skills": {}}
    registered = registry.get("skills") if isinstance(registry.get("skills"), dict) else {}
    usage = load_skill_usage(workspace)
    summary = {
        "total": 0,
        "active": 0,
        "stale": 0,
        "archived": 0,
        "evolvable": 0,
        "by_origin": {},
    }
    skills: list[dict[str, Any]] = []
    all_keys = set(registered.keys()) | set(usage.keys())
    for key in sorted(all_keys):
        entry = registered.get(key) if isinstance(registered.get(key), dict) else {}
        usage_record = usage.get(key) if isinstance(usage.get(key), dict) else {}
        state = str(entry.get("state") or usage_record.get("state") or STATE_ACTIVE)
        origin = str(entry.get("skill_origin") or "unknown")
        evolvable = bool(entry.get("evolvable"))
        use_count = int(usage_record.get("use_count") or 0)

        summary["total"] += 1
        if state in {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}:
            summary[state] += 1
        if evolvable:
            summary["evolvable"] += 1
        summary["by_origin"][origin] = int(summary["by_origin"].get(origin) or 0) + 1
        skills.append(
            {
                "skill_name": str(entry.get("skill_name") or key),
                "skill_origin": origin,
                "evolvable": evolvable,
                "state": state,
                "use_count": use_count,
                "last_used_at": usage_record.get("last_used_at"),
                "target_path": entry.get("target_path"),
                "active_version_hash": entry.get("active_version_hash"),
                "last_candidate_id": entry.get("last_candidate_id"),
            }
        )

    state_rank = {STATE_ACTIVE: 0, STATE_STALE: 1, STATE_ARCHIVED: 2}
    skills.sort(key=lambda item: (state_rank.get(str(item["state"]), 9), -int(item["use_count"]), item["skill_name"]))
    return {"summary": summary, "skills": skills}


def _build_skill_tuning(workspace: Path) -> dict[str, Any]:
    candidates = _load_manifests(workspace, workspace / "evolution" / "skill_candidates")
    candidates.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"candidates": candidates}


def _build_legacy_audit(workspace: Path) -> dict[str, Any]:
    detected = [rel for rel in _LEGACY_EVOLUTION_FILES if (workspace / rel).exists()]
    return {"detected_legacy_files": detected}


def build_agent_evolution_view(workspace: Path) -> dict[str, Any]:
    view = _empty_view()
    if not workspace.exists():
        return view

    view["memory_learning"] = _build_memory_learning(workspace)
    view["soul"] = _build_soul(workspace)
    view["skill_ecosystem"] = _build_skill_ecosystem(workspace)
    view["skill_tuning"] = _build_skill_tuning(workspace)
    view["legacy_audit"] = _build_legacy_audit(workspace)
    return view
