"""Unified read model for the Agent Evolution page.

The page is an audit/read surface. It must not become the writer for memory,
Skill semantics, or self-evolution state. This projection stitches together
the new Memory Learning Vault paths and the Skill ecosystem sidecars so the UI
can answer "where did evolution write?" without reviving legacy scorecard
semantics.
"""

from __future__ import annotations

import hashlib
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
_LANES = ("memory", "soul", "skill_ecosystem", "skill_tuning", "legacy_audit")


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
            "t0_raw_evidence": "memory/t0/sessions/<session_id>/segments/<segment_id>/events.jsonl",
            "t0_readable_projection": "memory/t0/sessions/<session_id>/segments/<segment_id>/source.md",
            "t2_segment_packages": "memory/t2/sessions/<session_id>/segments/<segment_id>/{summary,labels,review,manifest}",
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
        "lanes": {lane: [] for lane in _LANES},
        "timeline": [],
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
        "t0_segments": _load_t0_segment_index_events(workspace),
        "t2_segment_packages": _load_t2_package_events(workspace),
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


def _build_event(
    *,
    lane: str,
    stage: str,
    status: str,
    title: str,
    path: str | None = None,
    source_refs: list[str] | None = None,
    created_at: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seed = "|".join([lane, stage, status, title, path or "", created_at or ""])
    event_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return {
        "id": f"{lane}:{stage}:{event_id}",
        "lane": lane,
        "stage": stage,
        "status": status,
        "title": title,
        "path": path,
        "source_refs": source_refs or [],
        "created_at": created_at,
        "details": details or {},
    }


def _load_t0_segment_index_events(workspace: Path) -> list[dict[str, Any]]:
    sessions_dir = workspace / "memory" / "t0" / "sessions"
    if not sessions_dir.exists():
        return []
    events: list[dict[str, Any]] = []
    for index_path in sorted(sessions_dir.glob("*/index.json")):
        index = _read_json(index_path)
        if not index:
            continue
        session_id = str(index.get("session_id") or index_path.parent.name)
        for segment in index.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            segment_id = str(segment.get("segment_id") or "")
            if not segment_id:
                continue
            source_path = f"memory/t0/sessions/{session_id}/segments/{segment_id}/source.md"
            events.append(
                _build_event(
                    lane="memory",
                    stage="t0_segment",
                    status=str(segment.get("state") or segment.get("status") or "open"),
                    title=f"T0 {session_id}/{segment_id}",
                    path=source_path,
                    source_refs=[f"t0://session/{session_id}/segment/{segment_id}"],
                    created_at=str(segment.get("created_at") or segment.get("sealed_at") or "") or None,
                    details={
                        "session_id": session_id,
                        "segment_id": segment_id,
                        "seal_reason": segment.get("seal_reason"),
                    },
                )
            )
    return events


def _load_t2_package_events(workspace: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for packages_dir in (workspace / "memory" / "t2" / "sessions", workspace / "memory" / "sessions"):
        if not packages_dir.exists():
            continue
        for manifest_path in sorted(packages_dir.glob("*/segments/*/manifest.json")):
            manifest = _read_json(manifest_path)
            if not manifest:
                continue
            package_id = str(manifest.get("package_id") or "").strip()
            dedupe_key = package_id or _relative_manifest_path(workspace, manifest_path.parent)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            segment_id = manifest_path.parent.name
            session_id = manifest_path.parents[2].name
            rel_path = _relative_manifest_path(workspace, manifest_path.parent)
            events.append(
                _build_event(
                    lane="memory",
                    stage="t2_package",
                    status=str(manifest.get("package_status") or manifest.get("status") or "reviewed"),
                    title=package_id or f"T2 {session_id}/{segment_id}",
                    path=rel_path,
                    source_refs=[str(ref) for ref in manifest.get("source_refs") or [] if str(ref).strip()],
                    created_at=str(manifest.get("created_at") or "") or None,
                    details={"session_id": session_id, "segment_id": segment_id},
                )
            )
    return events


def _events_from_manifest_list(
    manifests: list[dict[str, Any]],
    *,
    lane: str,
    stage: str,
    id_key: str,
    default_title: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for manifest in manifests:
        title = str(manifest.get(id_key) or manifest.get("job_id") or manifest.get("candidate_id") or default_title)
        events.append(
            _build_event(
                lane=lane,
                stage=stage,
                status=str(manifest.get("status") or manifest.get("package_status") or "pending"),
                title=title,
                path=str(manifest.get("manifest_path") or manifest.get("target_path") or "") or None,
                source_refs=[str(ref) for ref in manifest.get("source_refs") or [] if str(ref).strip()],
                created_at=str(manifest.get("created_at") or manifest.get("updated_at") or "") or None,
                details=manifest,
            )
        )
    return events


def _build_lanes(view: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    lanes: dict[str, list[dict[str, Any]]] = {lane: [] for lane in _LANES}
    memory = view["memory_learning"]
    lanes["memory"].extend(memory.get("t0_segments") or [])
    lanes["memory"].extend(memory.get("t2_segment_packages") or [])
    lanes["memory"].extend(
        _events_from_manifest_list(
            memory.get("pending_t3_jobs") or [],
            lane="memory",
            stage="t3_job",
            id_key="job_id",
            default_title="T3 job",
        )
    )
    for key, target in (memory.get("t3_targets") or {}).items():
        if int(target.get("line_count") or 0) <= 0:
            continue
        lanes["memory"].append(
            _build_event(
                lane="memory",
                stage="t3_target",
                status="active",
                title=f"T3 {key}",
                path=str(target.get("path") or ""),
                source_refs=[],
                details={"line_count": target.get("line_count")},
            )
        )
    lanes["soul"].extend(
        _events_from_manifest_list(
            view["soul"].get("pending_candidates") or [],
            lane="soul",
            stage="soul_candidate",
            id_key="candidate_id",
            default_title="Soul candidate",
        )
    )
    for skill in view["skill_ecosystem"].get("skills") or []:
        lanes["skill_ecosystem"].append(
            _build_event(
                lane="skill_ecosystem",
                stage="skill_registry",
                status=str(skill.get("state") or "active"),
                title=str(skill.get("skill_name") or "skill"),
                path=str(skill.get("target_path") or "") or None,
                source_refs=[],
                created_at=str(skill.get("last_used_at") or "") or None,
                details=skill,
            )
        )
    lanes["skill_tuning"].extend(
        _events_from_manifest_list(
            view["skill_tuning"].get("candidates") or [],
            lane="skill_tuning",
            stage="skill_candidate",
            id_key="candidate_id",
            default_title="Skill candidate",
        )
    )
    for rel in view["legacy_audit"].get("detected_legacy_files") or []:
        lanes["legacy_audit"].append(
            _build_event(
                lane="legacy_audit",
                stage="legacy_file",
                status="detected",
                title=str(rel),
                path=str(rel),
            )
        )
    return lanes


def _sort_event(event: dict[str, Any]) -> tuple[str, str, str]:
    return (str(event.get("created_at") or ""), str(event.get("lane") or ""), str(event.get("title") or ""))


def build_agent_evolution_view(workspace: Path) -> dict[str, Any]:
    view = _empty_view()
    if not workspace.exists():
        return view

    view["memory_learning"] = _build_memory_learning(workspace)
    view["soul"] = _build_soul(workspace)
    view["skill_ecosystem"] = _build_skill_ecosystem(workspace)
    view["skill_tuning"] = _build_skill_tuning(workspace)
    view["legacy_audit"] = _build_legacy_audit(workspace)
    view["lanes"] = _build_lanes(view)
    view["timeline"] = sorted(
        [event for lane_events in view["lanes"].values() for event in lane_events],
        key=_sort_event,
        reverse=True,
    )
    return view
