"""Lightweight Recovery Manifest for high-fidelity post-compaction restoration.

Captures structured state about what to restore after context compression,
instead of relying solely on natural language summaries. Built from
SessionContext runtime tracking fields.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.session_memory import load_session_memory

logger = logging.getLogger(__name__)
RECOVERY_MANIFEST_REL_PATH = Path("runtime_artifacts") / "recovery_manifest.json"
LEGACY_RECOVERY_MANIFEST_REL_PATH = Path("workspace") / "recovery_manifest.json"


@dataclass(slots=True)
class RecoveryManifest:
    """Structured record of what to restore after compaction."""

    session_id: str | None = None

    # Files the agent recently read or wrote
    recent_reads: list[str] = field(default_factory=list)
    recent_writes: list[str] = field(default_factory=list)
    file_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Tool execution outcomes worth preserving
    recent_tool_outcomes: list[dict[str, str]] = field(default_factory=list)

    # Skills and runtime tool groups currently active
    active_skills: list[str] = field(default_factory=list)
    active_tool_groups: list[str] = field(default_factory=list)

    # External resources referenced
    recent_external_refs: list[str] = field(default_factory=list)

    # Unfinished work
    pending_items: list[str] = field(default_factory=list)

    # Blocked patterns from evolution (do-not-retry list)
    blocked_patterns: list[str] = field(default_factory=list)

    # Tool-call closure state that must survive compact/restart/fork.
    discovered_tools: list[str] = field(default_factory=list)
    pending_tool_frames: list[dict[str, Any]] = field(default_factory=list)
    permission_checkpoints: list[dict[str, Any]] = field(default_factory=list)
    hook_lifecycle_records: list[dict[str, Any]] = field(default_factory=list)
    compaction_lifecycle_records: list[dict[str, Any]] = field(default_factory=list)
    permission_profile: dict[str, Any] = field(default_factory=dict)
    mcp_assignments: list[dict[str, Any]] = field(default_factory=list)
    truth_evidence_refs: list[str] = field(default_factory=list)
    truth_evidence: list[dict[str, Any]] = field(default_factory=list)
    pending_skill_handoffs: list[dict[str, Any]] = field(default_factory=list)
    executed_skill_handoffs: list[dict[str, Any]] = field(default_factory=list)
    continuation_records: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.recent_reads,
                self.recent_writes,
                self.recent_tool_outcomes,
                self.active_skills,
                self.active_tool_groups,
                self.recent_external_refs,
                self.pending_items,
                self.blocked_patterns,
                self.discovered_tools,
                self.pending_tool_frames,
                self.permission_checkpoints,
                self.hook_lifecycle_records,
                self.compaction_lifecycle_records,
                self.permission_profile,
                self.mcp_assignments,
                self.truth_evidence_refs,
                self.truth_evidence,
                self.pending_skill_handoffs,
                self.executed_skill_handoffs,
                self.continuation_records,
            ]
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "recent_reads": self.recent_reads,
            "recent_writes": self.recent_writes,
            "file_snapshots": self.file_snapshots,
            "recent_tool_outcomes": self.recent_tool_outcomes,
            "active_skills": self.active_skills,
            "active_tool_groups": self.active_tool_groups,
            "recent_external_refs": self.recent_external_refs,
            "pending_items": self.pending_items,
            "blocked_patterns": self.blocked_patterns,
            "discovered_tools": self.discovered_tools,
            "pending_tool_frames": self.pending_tool_frames,
            "permission_checkpoints": self.permission_checkpoints,
            "hook_lifecycle_records": self.hook_lifecycle_records,
            "compaction_lifecycle_records": self.compaction_lifecycle_records,
            "permission_profile": self.permission_profile,
            "mcp_assignments": self.mcp_assignments,
            "truth_evidence_refs": self.truth_evidence_refs,
            "truth_evidence": self.truth_evidence,
            "pending_skill_handoffs": self.pending_skill_handoffs,
            "executed_skill_handoffs": self.executed_skill_handoffs,
            "continuation_records": self.continuation_records,
        }

    def to_restoration_text(self, *, budget_chars: int = 20000) -> str:
        """Render manifest as structured text for prompt injection."""
        sections: list[str] = []
        total = 0

        def _add(title: str, items: list[str]) -> None:
            nonlocal total
            if not items or total >= budget_chars:
                return
            block = f"### {title}\n" + "\n".join(f"- {item}" for item in items)
            if total + len(block) < budget_chars:
                sections.append(block)
                total += len(block)

        def _add_dicts(title: str, items: list[dict[str, Any]]) -> None:
            rendered = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in items if item]
            _add(title, rendered)

        def _format_file_item(path: str) -> str:
            snapshot = self.file_snapshots.get(path, {})
            version = ""
            if snapshot.get("exists") is True:
                version = f" [size={snapshot.get('size')}, mtime_ns={snapshot.get('mtime_ns')}]"
            elif snapshot.get("exists") is False:
                version = " [missing at last snapshot]"
            return f'{path}{version} — reload with read_file("{path}")'

        _add("Recent Reads", [_format_file_item(path) for path in self.recent_reads[-5:]])
        _add("Recent Writes", [_format_file_item(path) for path in self.recent_writes[-5:]])
        _add(
            "Recent Tool Results",
            [f"{o.get('tool', '?')}: {o.get('summary', '')}" for o in self.recent_tool_outcomes[-5:]],
        )
        _add("Active Skills", self.active_skills)
        _add("Active Runtime Tool Groups", self.active_tool_groups)
        _add("External References", self.recent_external_refs[-5:])
        _add("Pending Work", self.pending_items[-5:])
        _add("Blocked Patterns (DO NOT retry)", self.blocked_patterns[-5:])
        _add("Discovered Tools", self.discovered_tools)
        _add_dicts("Pending Tool Frames", self.pending_tool_frames[-5:])
        _add_dicts("Permission Checkpoints", self.permission_checkpoints[-5:])
        _add_dicts("Hook Lifecycle Records", self.hook_lifecycle_records[-10:])
        _add_dicts("Compaction Lifecycle Records", self.compaction_lifecycle_records[-5:])
        _add_dicts("MCP Assignments", self.mcp_assignments[-10:])
        _add("Truth Evidence Refs", self.truth_evidence_refs[-20:])
        _add_dicts("Truth Evidence", self.truth_evidence[-10:])
        _add_dicts("Pending Skill Handoffs", self.pending_skill_handoffs[-10:])
        _add_dicts("Executed Skill Handoffs", self.executed_skill_handoffs[-10:])
        _add_dicts("Continuation Records", self.continuation_records[-10:])
        if self.permission_profile:
            _add("Permission Profile", [json.dumps(self.permission_profile, ensure_ascii=False, sort_keys=True)])

        if not sections:
            return ""
        return "\n\n".join(sections)


def _metadata_dict_list(metadata: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, dict):
            result.append(dict(value))
        elif isinstance(value, list):
            result.extend(dict(item) for item in value if isinstance(item, dict))
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _string_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _truth_evidence_list(value: Any) -> list[dict[str, Any]]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    return _dict_list(raw)


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _continuation_records_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    records = _metadata_dict_list(
        metadata,
        "continuation_record",
        "continuation_records",
        "permission_denial_continuation",
    )
    if metadata.get("source") == "session_permission_denied_resume":
        record: dict[str, Any] = {"source": "session_permission_denied_resume"}
        for key in (
            "resumed_from_permission_request_id",
            "denied_tool_name",
            "denied_tool_call_id",
            "resumed_turn_id",
            "resumed_runtime_task_id",
            "round_state",
            "t0_refs",
        ):
            value = metadata.get(key)
            if value is not None:
                record[key] = value
        records = [item for item in records if item != record]
        records.append(record)
    return records


def recovery_manifest_path(agent_id: Any, *, data_root: str | Path | None = None) -> Path:
    if data_root is None:
        from app.config import get_settings

        data_root = get_settings().AGENT_DATA_DIR
    return Path(data_root) / str(agent_id) / RECOVERY_MANIFEST_REL_PATH


def _manifest_from_payload(payload: dict[str, Any]) -> RecoveryManifest:
    return RecoveryManifest(
        session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
        recent_reads=_string_list(payload.get("recent_reads")),
        recent_writes=_string_list(payload.get("recent_writes")),
        file_snapshots=_dict_value(payload.get("file_snapshots")),
        recent_tool_outcomes=_dict_list(payload.get("recent_tool_outcomes")),
        active_skills=_string_list(payload.get("active_skills")),
        active_tool_groups=_string_list(payload.get("active_tool_groups")),
        recent_external_refs=_string_list(payload.get("recent_external_refs")),
        pending_items=_string_list(payload.get("pending_items")),
        blocked_patterns=_string_list(payload.get("blocked_patterns")),
        discovered_tools=_string_list(payload.get("discovered_tools")),
        pending_tool_frames=_dict_list(payload.get("pending_tool_frames")),
        permission_checkpoints=_dict_list(payload.get("permission_checkpoints")),
        hook_lifecycle_records=_dict_list(payload.get("hook_lifecycle_records")),
        compaction_lifecycle_records=_dict_list(payload.get("compaction_lifecycle_records")),
        permission_profile=_dict_value(payload.get("permission_profile")),
        mcp_assignments=_dict_list(payload.get("mcp_assignments")),
        truth_evidence_refs=_string_refs(payload.get("truth_evidence_refs")),
        truth_evidence=_truth_evidence_list(payload.get("truth_evidence")),
        pending_skill_handoffs=_dict_list(payload.get("pending_skill_handoffs")),
        executed_skill_handoffs=_dict_list(payload.get("executed_skill_handoffs")),
        continuation_records=_dict_list(payload.get("continuation_records")),
    )


def load_recovery_manifest(
    agent_id: Any,
    *,
    data_root: str | Path | None = None,
) -> RecoveryManifest | None:
    """Load the persisted RecoveryManifest from the production runtime artifact path."""
    path = recovery_manifest_path(agent_id, data_root=data_root)
    candidates = [path, path.parent.parent / LEGACY_RECOVERY_MANIFEST_REL_PATH]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load recovery manifest from %s: %s", candidate, exc)
            return None
        if not isinstance(payload, dict):
            logger.warning("Recovery manifest at %s is not a JSON object", candidate)
            return None
        return _manifest_from_payload(payload)
    return None


def _append_unique_strings(target: list[str], values: list[str], *, limit: int | None = None) -> None:
    seen = set(target)
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        target.append(item)
        seen.add(item)
    if limit is not None and len(target) > limit:
        del target[: len(target) - limit]


def _stable_dict_key(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _append_unique_dicts(target: list[dict[str, Any]], values: list[dict[str, Any]], *, limit: int | None = None) -> None:
    seen = {_stable_dict_key(item) for item in target if isinstance(item, dict)}
    for value in values:
        if not isinstance(value, dict) or not value:
            continue
        item = dict(value)
        key = _stable_dict_key(item)
        if key in seen:
            continue
        target.append(item)
        seen.add(key)
    if limit is not None and len(target) > limit:
        del target[: len(target) - limit]


def _merge_metadata_dict_list(metadata: dict[str, Any], key: str, values: list[dict[str, Any]]) -> None:
    existing = metadata.get(key)
    target = [dict(item) for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    if isinstance(existing, dict):
        target.insert(0, dict(existing))
    _append_unique_dicts(target, values)
    if target:
        metadata[key] = target


def _merge_metadata_string_list(metadata: dict[str, Any], key: str, values: list[str]) -> None:
    existing = metadata.get(key)
    target = [str(item) for item in existing if str(item).strip()] if isinstance(existing, list) else []
    if isinstance(existing, str) and existing.strip():
        target.append(existing.strip())
    _append_unique_strings(target, values)
    if target:
        metadata[key] = target


def hydrate_session_context_from_recovery_manifest(session_context: Any, manifest: RecoveryManifest | None) -> bool:
    """Restore machine-readable runtime state from a persisted RecoveryManifest.

    The prompt renderer still includes ``to_restoration_text()`` for model-visible
    continuity, but this function revives the runtime object fields that govern
    tool discovery, permission profile, pending frames, MCP assignments, Truth
    evidence, and skill handoffs. It is intentionally idempotent so a resumed
    session can call it on every prompt assembly without duplicating state.
    """
    if session_context is None or manifest is None or manifest.is_empty():
        return False

    _append_unique_strings(getattr(session_context, "recent_files", []), manifest.recent_reads, limit=10)
    _append_unique_strings(getattr(session_context, "recent_writes", []), manifest.recent_writes, limit=10)
    _append_unique_dicts(getattr(session_context, "recent_tool_outcomes", []), manifest.recent_tool_outcomes, limit=10)
    _append_unique_strings(getattr(session_context, "recent_external_refs", []), manifest.recent_external_refs, limit=5)
    _append_unique_strings(getattr(session_context, "pending_items", []), manifest.pending_items, limit=10)

    file_snapshots = getattr(session_context, "file_snapshots", None)
    if isinstance(file_snapshots, dict):
        file_snapshots.update(dict(manifest.file_snapshots or {}))

    for skill_name in manifest.active_skills:
        if skill_name not in getattr(session_context, "active_skills", []):
            try:
                session_context.track_skill_loaded(skill_name)
            except Exception:
                getattr(session_context, "active_skills", []).append(skill_name)

    active_tool_groups = getattr(session_context, "active_tool_groups", [])
    existing_groups = {
        str(item.get("name") or "").strip()
        for item in active_tool_groups
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    for group_name in manifest.active_tool_groups:
        if group_name and group_name not in existing_groups:
            active_tool_groups.append({"name": group_name, "summary": "", "tools": []})
            existing_groups.add(group_name)

    try:
        session_context.track_discovered_tools(tuple(manifest.discovered_tools))
    except Exception:
        _append_unique_strings(getattr(session_context, "discovered_tools", []), manifest.discovered_tools)

    metadata = getattr(session_context, "metadata", None)
    if not isinstance(metadata, dict):
        return True

    metadata["discovered_tools"] = list(getattr(session_context, "discovered_tools", []))
    for key, values in (
        ("pending_tool_frames", manifest.pending_tool_frames),
        ("permission_checkpoints", manifest.permission_checkpoints),
        ("hook_lifecycle_records", manifest.hook_lifecycle_records),
        ("compaction_lifecycle_records", manifest.compaction_lifecycle_records),
        ("mcp_assignments", manifest.mcp_assignments),
        ("truth_evidence", manifest.truth_evidence),
        ("pending_skill_handoffs", manifest.pending_skill_handoffs),
        ("executed_skill_handoffs", manifest.executed_skill_handoffs),
        ("continuation_records", manifest.continuation_records),
    ):
        _merge_metadata_dict_list(metadata, key, values)
    _merge_metadata_string_list(metadata, "truth_evidence_refs", manifest.truth_evidence_refs)
    _merge_metadata_string_list(metadata, "evidence_refs", manifest.truth_evidence_refs)
    if manifest.permission_profile:
        metadata["permission_profile"] = dict(manifest.permission_profile)
    if manifest.pending_tool_frames:
        metadata["recovered_pending_tool_frames"] = [dict(item) for item in manifest.pending_tool_frames]
    metadata["recovered_from_manifest"] = True
    return True


def build_recovery_manifest(session_context: Any) -> RecoveryManifest:
    """Build a RecoveryManifest from the current SessionContext state."""
    if session_context is None:
        return RecoveryManifest()

    tool_group_names = []
    for p in getattr(session_context, "active_tool_groups", []):
        if isinstance(p, dict):
            tool_group_names.append(p.get("name", "?"))

    metadata = getattr(session_context, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}

    permission_profile = metadata.get("permission_profile")
    truth_evidence = _truth_evidence_list(metadata.get("truth_evidence") or metadata.get("truth_evidence_json"))
    truth_refs = _string_refs(metadata.get("truth_evidence_refs") or metadata.get("evidence_refs"))
    for evidence in truth_evidence:
        truth_refs.extend(_string_refs(evidence.get("evidence_id")))

    return RecoveryManifest(
        session_id=getattr(session_context, "session_id", None),
        recent_reads=list(getattr(session_context, "recent_files", [])),
        recent_writes=list(getattr(session_context, "recent_writes", [])),
        file_snapshots=dict(getattr(session_context, "file_snapshots", {}) or {}),
        recent_tool_outcomes=list(getattr(session_context, "recent_tool_outcomes", [])),
        active_skills=list(getattr(session_context, "active_skills", [])),
        active_tool_groups=tool_group_names,
        recent_external_refs=list(getattr(session_context, "recent_external_refs", [])),
        pending_items=list(getattr(session_context, "pending_items", [])),
        discovered_tools=list(getattr(session_context, "discovered_tools", [])),
        pending_tool_frames=_metadata_dict_list(metadata, "pending_tool_frame", "pending_tool_frames"),
        permission_checkpoints=_metadata_dict_list(metadata, "permission_checkpoint", "permission_checkpoints"),
        hook_lifecycle_records=_metadata_dict_list(metadata, "hook_lifecycle_records"),
        compaction_lifecycle_records=_metadata_dict_list(metadata, "compaction_lifecycle_records"),
        permission_profile=dict(permission_profile) if isinstance(permission_profile, dict) else {},
        mcp_assignments=_metadata_dict_list(
            metadata,
            "mcp_assignment",
            "mcp_assignments",
            "mcp_server_assignments",
        ),
        truth_evidence_refs=list(dict.fromkeys(truth_refs)),
        truth_evidence=truth_evidence,
        pending_skill_handoffs=_metadata_dict_list(metadata, "pending_skill_handoffs"),
        executed_skill_handoffs=_metadata_dict_list(metadata, "executed_skill_handoffs"),
        continuation_records=_continuation_records_from_metadata(metadata),
    )


def merge_session_memory_into_manifest(
    manifest: RecoveryManifest,
    *,
    agent_id: Any,
    data_root: str | Path | None = None,
) -> RecoveryManifest:
    """Merge structured session-memory artifact into the recovery manifest."""
    try:
        payload = load_session_memory(agent_id, session_id=manifest.session_id, data_root=data_root)
    except Exception as exc:
        logger.warning("Failed to load session memory for recovery manifest: %s", exc)
        payload = None
    if payload is None:
        return manifest

    for item in payload.important_files:
        if item not in manifest.recent_reads:
            manifest.recent_reads.append(item)
    for item in payload.pending_work:
        if item not in manifest.pending_items:
            manifest.pending_items.append(item)
    if payload.current_state:
        combined_summary = payload.current_state
        if payload.key_results:
            combined_summary = f"{combined_summary}\nKey Results: {'; '.join(payload.key_results)}"
        manifest.recent_tool_outcomes.append({"tool": "session_memory", "summary": combined_summary})
    for item in payload.key_results:
        manifest.recent_tool_outcomes.append({"tool": "session_memory:key_result", "summary": item})
    if payload.last_successful_step and payload.last_successful_step != payload.current_state:
        manifest.recent_tool_outcomes.append(
            {"tool": "session_memory:last_successful_step", "summary": payload.last_successful_step}
        )
    return manifest


def persist_recovery_manifest(
    agent_id: Any,
    session_context: Any,
    *,
    data_root: str | Path | None = None,
    delete_if_empty: bool = False,
) -> list[Path]:
    """Persist the current recovery manifest to the canonical runtime artifact path.

    This is intentionally shared by compaction and tool-lifecycle checkpoints:
    a process may die before compaction, so killed-process recovery cannot rely
    on the compaction path alone.
    """

    if agent_id is None or session_context is None:
        return []

    if data_root is None:
        from app.config import get_settings

        roots = [
            Path(get_settings().AGENT_DATA_DIR) / str(agent_id),
            Path("/tmp/hive_workspaces") / str(agent_id),
        ]

        def should_write_root(root: Path) -> bool:
            return root.exists() or root == roots[0]

    else:
        roots = [Path(data_root) / str(agent_id)]

        def should_write_root(_root: Path) -> bool:
            return True

    manifest = build_recovery_manifest(session_context)
    manifest = merge_session_memory_into_manifest(manifest, agent_id=agent_id, data_root=data_root)
    written: list[Path] = []
    payload = manifest.to_payload()
    for root in roots:
        if not should_write_root(root):
            continue
        path = root / RECOVERY_MANIFEST_REL_PATH
        legacy_path = root / LEGACY_RECOVERY_MANIFEST_REL_PATH
        if manifest.is_empty():
            if delete_if_empty:
                path.unlink(missing_ok=True)
                legacy_path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        legacy_path.unlink(missing_ok=True)
        written.append(path)
    return written
