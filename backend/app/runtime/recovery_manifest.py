"""Lightweight Recovery Manifest for high-fidelity post-compaction restoration.

Captures structured state about what to restore after context compression,
instead of relying solely on natural language summaries. Built from
SessionContext runtime tracking fields.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
import sys
import time
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.session_memory import load_session_memory

logger = logging.getLogger(__name__)
# Read-only compatibility locations. New writers must use the session-scoped
# directory below so concurrent sessions cannot overwrite one another.
RECOVERY_MANIFEST_REL_PATH = Path("runtime_artifacts") / "recovery_manifest.json"
LEGACY_RECOVERY_MANIFEST_REL_PATH = Path("workspace") / "recovery_manifest.json"
RECOVERY_MANIFESTS_REL_DIR = Path("runtime_artifacts") / "recovery_manifests"
LEGACY_RECOVERY_QUARANTINE_REL_DIR = RECOVERY_MANIFESTS_REL_DIR / "legacy_quarantine"
LEGACY_RECOVERY_CUTOVER_FILENAME = "legacy_cutover.json"
MAX_RECOVERY_MANIFEST_BYTES = 2 * 1024 * 1024
RECOVERY_LOCK_TIMEOUT_SECONDS = 0.25
# Legacy migration holds its global lock across durable directory/file fsyncs.
# Fresh-tree contention measures above one second on supported macOS hosts, so
# this lane has a separate bounded SLA while per-session locks stay responsive.
RECOVERY_LEGACY_LOCK_TIMEOUT_SECONDS = 5.0
RECOVERY_LOCK_POLL_SECONDS = 0.01
RECOVERY_TEMP_STALE_SECONDS = 1.0
RECOVERY_CANONICAL_MAX_DEPTH = 32
RECOVERY_CANONICAL_MAX_NODES = 4096
RECOVERY_CANONICAL_MAX_STRING_BYTES = 16 * 1024
RECOVERY_CANONICAL_MAX_OUTPUT_BYTES = 256 * 1024
RECOVERY_CANONICAL_MARKER_RESERVE_BYTES = 256
_BOUNDED_RECOVERY_KEY_PREFIX = "__bounded_key_sha256__:"


@dataclass(slots=True)
class RecoveryManifest:
    """Structured record of what to restore after compaction."""

    session_id: str | None = None
    agent_id: str | None = None
    tenant_id: str | None = None
    runtime_task_id: str | None = None
    claim_version: int | None = None
    claim_worker_id: str | None = None
    checkpoint_seq: int | None = None
    legacy_conflict: dict[str, Any] = field(default_factory=dict)
    recovery_reconciliation_blocked: bool = False
    prior_run_recovery_reconciliations: list[dict[str, Any]] = field(default_factory=list)
    reconciliation_resolution: dict[str, Any] = field(default_factory=dict)

    # Files the agent recently read or wrote
    recent_reads: list[str] = field(default_factory=list)
    recent_writes: list[str] = field(default_factory=list)
    current_turn_writes: list[str] = field(default_factory=list)
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
                self.current_turn_writes,
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
                self.legacy_conflict,
                self.recovery_reconciliation_blocked,
                self.prior_run_recovery_reconciliations,
                self.reconciliation_resolution,
            ]
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "tenant_id": self.tenant_id,
            "runtime_task_id": self.runtime_task_id,
            "claim_version": self.claim_version,
            "claim_worker_id": self.claim_worker_id,
            "checkpoint_seq": self.checkpoint_seq,
            "legacy_conflict": self.legacy_conflict,
            "recovery_reconciliation_blocked": self.recovery_reconciliation_blocked,
            "prior_run_recovery_reconciliations": self.prior_run_recovery_reconciliations,
            "reconciliation_resolution": self.reconciliation_resolution,
            "recent_reads": self.recent_reads,
            "recent_writes": self.recent_writes,
            "current_turn_writes": self.current_turn_writes,
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
        _add_dicts(
            "Pending Tool Frames",
            [_sanitize_pending_tool_frame(item) for item in self.pending_tool_frames[-5:]],
        )
        _add_dicts("Hook Lifecycle Records", self.hook_lifecycle_records[-10:])
        _add_dicts("Compaction Lifecycle Records", self.compaction_lifecycle_records[-5:])
        _add_dicts("MCP Assignments", self.mcp_assignments[-10:])
        _add("Truth Evidence Refs", self.truth_evidence_refs[-20:])
        _add_dicts("Truth Evidence", self.truth_evidence[-10:])
        _add_dicts(
            "Pending Skill Handoffs",
            [_sanitize_recovered_skill_handoff(item) for item in self.pending_skill_handoffs[-10:]],
        )
        _add_dicts("Executed Skill Handoffs", self.executed_skill_handoffs[-10:])
        _add_dicts("Continuation Records", self.continuation_records[-10:])

        if not sections:
            return ""
        return "\n\n".join(sections)


_SENSITIVE_RECOVERY_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "authorization_scopes",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "permission_profile",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_SENSITIVE_RECOVERY_FIELD_TOKENS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "authorizationscopes",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "permissionprofile",
        "privatekey",
        "refreshtoken",
        "secret",
        "token",
    }
)
_SENSITIVE_RECOVERY_FIELD_SUFFIXES = (
    "accesstoken",
    "apikey",
    "authorization",
    "authorizationscopes",
    "clientsecret",
    "credential",
    "credentials",
    "password",
    "permissionprofile",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
)


def _sensitive_recovery_field(key: str) -> bool:
    normalized = key.strip().casefold().replace("-", "_")
    if normalized in _SENSITIVE_RECOVERY_FIELDS or normalized.endswith(
        ("_api_key", "_credential", "_credentials", "_password", "_secret", "_token")
    ):
        return True
    compact = "".join(character for character in unicodedata.normalize("NFKC", key).casefold() if character.isalnum())
    return compact in _SENSITIVE_RECOVERY_FIELD_TOKENS or compact.endswith(_SENSITIVE_RECOVERY_FIELD_SUFFIXES)


@dataclass(slots=True)
class _CanonicalBudget:
    nodes: int = 0
    emitted_bytes: int = 0
    truncation_emitted: bool = False

    def reserve(self, serialized_bytes: int, *, marker: bool = False) -> bool:
        limit = RECOVERY_CANONICAL_MAX_OUTPUT_BYTES
        if not marker:
            limit -= RECOVERY_CANONICAL_MARKER_RESERVE_BYTES
        if serialized_bytes < 0 or self.emitted_bytes + serialized_bytes > limit:
            return False
        self.emitted_bytes += serialized_bytes
        return True


def _compact_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bounded_recovery_marker(budget: _CanonicalBudget, reason: str) -> dict[str, str]:
    marker = {"__bounded__": reason}
    if not budget.reserve(len(_compact_json_bytes(marker)), marker=True):
        raise ValueError("Recovery canonical marker reserve was exhausted")
    if reason in {"node_limit", "output_limit"}:
        budget.truncation_emitted = True
    return marker


def _bounded_recovery_key(key: str) -> str:
    encoded = key.encode("utf-8", errors="replace")
    if len(encoded) <= RECOVERY_CANONICAL_MAX_STRING_BYTES:
        return key
    return f"{_BOUNDED_RECOVERY_KEY_PREFIX}{hashlib.sha256(encoded).hexdigest()}"


def _append_recovery_dict_marker(
    result: dict[str, Any],
    budget: _CanonicalBudget,
    *,
    reason: str,
) -> None:
    if reason in {"node_limit", "output_limit"} and budget.truncation_emitted:
        return
    marker_key = "__bounded__" if "__bounded__" not in result else "__hive_recovery_truncated__"
    serialized_bytes = len(_compact_json_bytes(marker_key)) + 1 + len(_compact_json_bytes(reason))
    if result:
        serialized_bytes += 1
    if not budget.reserve(serialized_bytes, marker=True):
        raise ValueError("Recovery canonical marker reserve was exhausted")
    result[marker_key] = reason
    if reason in {"node_limit", "output_limit"}:
        budget.truncation_emitted = True


def _append_recovery_list_marker(
    result: list[Any],
    budget: _CanonicalBudget,
    *,
    reason: str,
) -> None:
    if reason in {"node_limit", "output_limit"} and budget.truncation_emitted:
        return
    marker = {"__bounded__": reason}
    serialized_bytes = len(_compact_json_bytes(marker)) + (1 if result else 0)
    if not budget.reserve(serialized_bytes, marker=True):
        raise ValueError("Recovery canonical marker reserve was exhausted")
    result.append(marker)
    if reason in {"node_limit", "output_limit"}:
        budget.truncation_emitted = True


def _bounded_recovery_value(
    value: Any,
    *,
    scrub_secrets: bool,
    budget: _CanonicalBudget | None = None,
    ancestors: set[int] | None = None,
    depth: int = 0,
) -> Any:
    """Return a cycle-safe, secret-free and mechanically bounded JSON value."""

    budget = budget or _CanonicalBudget()
    ancestors = ancestors or set()
    if budget.nodes >= RECOVERY_CANONICAL_MAX_NODES:
        return _bounded_recovery_marker(budget, "node_limit")
    budget.nodes += 1
    if depth > RECOVERY_CANONICAL_MAX_DEPTH:
        return _bounded_recovery_marker(budget, "depth_limit")
    if value is None or isinstance(value, (bool, int)):
        if budget.reserve(len(_compact_json_bytes(value))):
            return value
        return _bounded_recovery_marker(budget, "output_limit")
    if isinstance(value, float):
        result = value if value == value and value not in {float("inf"), float("-inf")} else str(value)
        if budget.reserve(len(_compact_json_bytes(result))):
            return result
        return _bounded_recovery_marker(budget, "output_limit")
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= RECOVERY_CANONICAL_MAX_STRING_BYTES and budget.reserve(len(_compact_json_bytes(value))):
            return value
        prefix = encoded[:RECOVERY_CANONICAL_MAX_STRING_BYTES].decode("utf-8", errors="ignore")
        bounded = {
            "__bounded__": "string_limit",
            "prefix": prefix,
            "original_bytes_at_least": len(encoded),
        }
        if budget.reserve(len(_compact_json_bytes(bounded))):
            return bounded
        return _bounded_recovery_marker(budget, "output_limit")
    if isinstance(value, bytes):
        prefix = value[:RECOVERY_CANONICAL_MAX_STRING_BYTES]
        bounded = {
            "__type__": "bytes",
            "bytes": len(value),
            "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
        }
        if budget.reserve(len(_compact_json_bytes(bounded))):
            return bounded
        return _bounded_recovery_marker(budget, "output_limit")

    identity = id(value)
    if identity in ancestors:
        return _bounded_recovery_marker(budget, "cycle")
    if isinstance(value, dict):
        if not budget.reserve(2):
            return _bounded_recovery_marker(budget, "output_limit")
        ancestors.add(identity)
        try:
            result: dict[str, Any] = {}
            string_keys = sorted(key for key in value if isinstance(key, str))
            for index, key in enumerate(string_keys):
                if scrub_secrets and _sensitive_recovery_field(key):
                    continue
                bounded_key = _bounded_recovery_key(key)
                key_bytes = len(_compact_json_bytes(bounded_key)) + 1 + (1 if result else 0)
                if not budget.reserve(key_bytes):
                    _append_recovery_dict_marker(result, budget, reason="output_limit")
                    break
                result[bounded_key] = _bounded_recovery_value(
                    value[key],
                    scrub_secrets=scrub_secrets,
                    budget=budget,
                    ancestors=ancestors,
                    depth=depth + 1,
                )
                if budget.nodes >= RECOVERY_CANONICAL_MAX_NODES:
                    if index < len(string_keys) - 1:
                        _append_recovery_dict_marker(result, budget, reason="node_limit")
                    break
            return result
        finally:
            ancestors.remove(identity)
    if isinstance(value, (list, tuple)):
        if not budget.reserve(2):
            return _bounded_recovery_marker(budget, "output_limit")
        ancestors.add(identity)
        try:
            result = []
            for index, item in enumerate(value):
                if result and not budget.reserve(1):
                    _append_recovery_list_marker(result, budget, reason="output_limit")
                    break
                result.append(
                    _bounded_recovery_value(
                        item,
                        scrub_secrets=scrub_secrets,
                        budget=budget,
                        ancestors=ancestors,
                        depth=depth + 1,
                    )
                )
                if budget.nodes >= RECOVERY_CANONICAL_MAX_NODES:
                    if index < len(value) - 1:
                        _append_recovery_list_marker(result, budget, reason="node_limit")
                    break
            return result
        finally:
            ancestors.remove(identity)
    bounded = {"__type__": "opaque"}
    if budget.reserve(len(_compact_json_bytes(bounded))):
        return bounded
    return _bounded_recovery_marker(budget, "output_limit")


def _bounded_recovery_json(value: Any, *, scrub_secrets: bool) -> bytes:
    canonical = _bounded_recovery_value(value, scrub_secrets=scrub_secrets)
    raw = _compact_json_bytes(canonical)
    if len(raw) <= RECOVERY_CANONICAL_MAX_OUTPUT_BYTES:
        return raw
    return _compact_json_bytes(
        {
            "__bounded__": "output_limit",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    )


def _dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        sanitized = _bounded_recovery_value(value, scrub_secrets=True)
        if not isinstance(sanitized, dict):
            continue
        item = sanitized
        key = _bounded_recovery_json(item, scrub_secrets=True).decode("utf-8", errors="replace")
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _metadata_dict_list(metadata: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, dict):
            values.append(dict(value))
        elif isinstance(value, list):
            values.extend(dict(item) for item in value if isinstance(item, dict))
    return _dedupe_dicts(values)


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
    return _dedupe_dicts([dict(item) for item in value if isinstance(item, dict)])


def _sanitize_recovered_skill_handoff(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = _bounded_recovery_value(value, scrub_secrets=True)
    return sanitized if isinstance(sanitized, dict) else {}


def _sanitize_pending_tool_frame(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = _bounded_recovery_value(value, scrub_secrets=True)
    return sanitized if isinstance(sanitized, dict) else {}


_RECOVERABLE_PENDING_TOOL_FRAME_STATUSES = frozenset(
    {"", "pending", "running", "started", "in_progress", "needs_reconciliation"}
)
_TERMINAL_PENDING_TOOL_FRAME_STATUSES = frozenset(
    {"completed", "done", "failed", "cancelled", "canceled", "skipped", "resolved"}
)


def _raw_recovery_evidence_sha256(value: Any) -> str:
    return hashlib.sha256(_bounded_recovery_json(value, scrub_secrets=False)).hexdigest()


def _registered_recovery_tool_names() -> dict[str, str]:
    """Return one immutable alias->canonical snapshot without per-name reloads."""

    from app.tools.decorator import get_all_registered_tools

    registered = get_all_registered_tools()
    if not registered:
        from app.tools.collector import _import_handler_modules

        _import_handler_modules()
        registered = get_all_registered_tools()
    return {name: meta.name for name, (meta, _handler) in registered.items()}


def _valid_recovered_tool_call_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 512 or any(ord(character) < 0x20 for character in normalized):
        return None
    return normalized


def _unknown_recovered_tool_frame(
    value: Any,
    *,
    reason: str,
    source: str,
    registered_names: dict[str, str],
) -> dict[str, Any]:
    digest = _raw_recovery_evidence_sha256(value)
    raw = value if isinstance(value, dict) else {}
    raw_tool_name = raw.get("tool_name") if isinstance(raw.get("tool_name"), str) else None
    tool_name = registered_names.get(raw_tool_name or "", "unknown_recovered_tool")
    serialized = _bounded_recovery_json(value, scrub_secrets=False)
    return {
        "schema": "hive.unknown_recovered_tool_frame.v1",
        "event_type": "unknown_recovered_tool_frame",
        "tool_call_id": f"unknown-recovery:{digest[:24]}",
        "tool_name": tool_name,
        "status": "needs_reconciliation",
        "reason": f"pending_tool_frame_{reason}",
        "raw_sha256": digest,
        "raw_bytes": len(serialized),
        "raw_type": "object" if isinstance(value, dict) else "container_or_scalar",
        "source": source,
    }


def _decode_pending_tool_frames(
    value: Any,
    *,
    source: str,
    allow_single: bool = False,
) -> list[dict[str, Any]]:
    """Decode pending frames without ever discarding unknown side-effect evidence.

    Only the digest and a small non-secret identity summary survive malformed
    input. Tool arguments and permission material are deliberately excluded from
    synthetic frames.
    """

    if value is None:
        return []
    container_issue: str | None = None
    if isinstance(value, list):
        items = list(value)
    elif allow_single and isinstance(value, dict):
        items = [value]
    else:
        items = [value]
        container_issue = "invalid_container"

    registered_names = _registered_recovery_tool_names()
    decoded: list[dict[str, Any]] = []
    for item in items:
        reason = container_issue
        if not isinstance(item, dict):
            reason = reason or "non_object_frame"
        else:
            raw_tool_name = item.get("tool_name")
            tool_name = raw_tool_name.strip() if isinstance(raw_tool_name, str) else None
            canonical_tool_name = registered_names.get(tool_name or "")
            tool_call_id = _valid_recovered_tool_call_id(item.get("tool_call_id"))
            status = str(item.get("status") or "").strip().lower()
            if tool_name is None:
                reason = reason or "missing_tool_name"
            elif canonical_tool_name is None:
                reason = reason or "unknown_tool_name"
            elif tool_call_id is None:
                reason = reason or "invalid_tool_call_id"
            elif status in _TERMINAL_PENDING_TOOL_FRAME_STATUSES:
                reason = reason or "terminal_status"
            elif status not in _RECOVERABLE_PENDING_TOOL_FRAME_STATUSES:
                reason = reason or "unknown_status"
        if reason:
            decoded.append(
                _unknown_recovered_tool_frame(
                    item,
                    reason=reason,
                    source=source,
                    registered_names=registered_names,
                )
            )
        else:
            sanitized = _sanitize_pending_tool_frame(item)
            sanitized["tool_name"] = canonical_tool_name
            sanitized["tool_call_id"] = tool_call_id
            if "status" in item:
                sanitized["status"] = status
            decoded.append(sanitized)
    return _dedupe_dicts(decoded)


def _pending_tool_frames_from_metadata(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    frames = [
        *_decode_pending_tool_frames(
            metadata.get("pending_tool_frame"),
            source="session_metadata.pending_tool_frame",
            allow_single=True,
        ),
        *_decode_pending_tool_frames(
            metadata.get("pending_tool_frames"),
            source="session_metadata.pending_tool_frames",
        ),
    ]
    return _dedupe_dicts(frames)


def _sanitize_permission_checkpoint_evidence(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(value)
    pending_frame = sanitized.get("pending_frame")
    if isinstance(pending_frame, dict):
        sanitized["pending_frame"] = _sanitize_pending_tool_frame(pending_frame)
    return sanitized


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


def _sanitize_permission_profile_evidence(value: Any) -> dict[str, Any]:
    sanitized = _bounded_recovery_value(value, scrub_secrets=True)
    return sanitized if isinstance(sanitized, dict) else {}


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


def _identity_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _identity_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_task_identity(value: Any) -> str | None:
    text = _identity_text(value)
    if text is None:
        return None
    try:
        return uuid.UUID(text).hex
    except (TypeError, ValueError):
        return text


def _entity_identity(value: Any) -> str | None:
    """Canonicalize UUID-shaped entity IDs without changing opaque IDs."""

    text = _identity_text(value)
    if text is None:
        return None
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError):
        return text


def _agent_path_component(value: Any) -> str:
    """Return an agent identity that is safe as one configured-root child."""

    text = _identity_text(value)
    if (
        text is None
        or text in {".", ".."}
        or Path(text).is_absolute()
        or "/" in text
        or "\\" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or len(text.encode("utf-8")) > 255
    ):
        raise ValueError("agent_id must be a single safe path component")
    return text


def _session_identity(session_context: Any) -> dict[str, Any]:
    metadata = getattr(session_context, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    session_key = metadata.get("session_key")
    if not isinstance(session_key, dict):
        session_key = {}
    claim_version = _identity_int(metadata.get("claim_version"))
    claim_worker_id = _identity_text(
        metadata.get("claim_worker_id") or metadata.get("claimed_by") or session_key.get("claim_worker_id")
    )
    return {
        "session_id": _identity_text(getattr(session_context, "session_id", None)),
        "agent_id": _identity_text(metadata.get("agent_id") or session_key.get("agent_id")),
        "tenant_id": _identity_text(metadata.get("tenant_id") or session_key.get("tenant_id")),
        "runtime_task_id": _runtime_task_identity(
            metadata.get("runtime_task_id") or metadata.get("task_id") or session_key.get("runtime_task_id")
        ),
        "claim_version": claim_version,
        "claim_worker_id": claim_worker_id,
        "checkpoint_seq": _identity_int(metadata.get("recovery_checkpoint_seq")),
    }


def _identity_slug(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def recovery_manifest_path(
    agent_id: Any,
    *,
    session_id: Any,
    runtime_task_id: Any | None = None,
    data_root: str | Path | None = None,
) -> Path:
    normalized_session_id = _identity_text(session_id)
    if normalized_session_id is None:
        raise ValueError("session_id is required for a recovery manifest path")
    if data_root is None:
        from app.config import get_settings

        data_root = get_settings().AGENT_DATA_DIR
    agent_component = _agent_path_component(agent_id)
    normalized_runtime_task_id = _runtime_task_identity(runtime_task_id)
    filename = f"{_identity_slug(normalized_runtime_task_id) if normalized_runtime_task_id else 'session'}.json"
    return (
        Path(data_root)
        / agent_component
        / RECOVERY_MANIFESTS_REL_DIR
        / _identity_slug(normalized_session_id)
        / filename
    )


def _manifest_from_payload(payload: dict[str, Any]) -> RecoveryManifest:
    return RecoveryManifest(
        session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
        agent_id=_identity_text(payload.get("agent_id")),
        tenant_id=_identity_text(payload.get("tenant_id")),
        runtime_task_id=_runtime_task_identity(payload.get("runtime_task_id")),
        claim_version=_identity_int(payload.get("claim_version")),
        claim_worker_id=_identity_text(payload.get("claim_worker_id")),
        checkpoint_seq=_identity_int(payload.get("checkpoint_seq")),
        legacy_conflict=_dict_value(payload.get("legacy_conflict")),
        recovery_reconciliation_blocked=payload.get("recovery_reconciliation_blocked") is True,
        prior_run_recovery_reconciliations=_dict_list(payload.get("prior_run_recovery_reconciliations")),
        reconciliation_resolution=_dict_value(payload.get("reconciliation_resolution")),
        recent_reads=_string_list(payload.get("recent_reads")),
        recent_writes=_string_list(payload.get("recent_writes")),
        current_turn_writes=_string_list(payload.get("current_turn_writes")),
        file_snapshots=_dict_value(payload.get("file_snapshots")),
        recent_tool_outcomes=_dict_list(payload.get("recent_tool_outcomes")),
        active_skills=_string_list(payload.get("active_skills")),
        active_tool_groups=_string_list(payload.get("active_tool_groups")),
        recent_external_refs=_string_list(payload.get("recent_external_refs")),
        pending_items=_string_list(payload.get("pending_items")),
        blocked_patterns=_string_list(payload.get("blocked_patterns")),
        discovered_tools=_string_list(payload.get("discovered_tools")),
        pending_tool_frames=_decode_pending_tool_frames(
            payload.get("pending_tool_frames"),
            source="recovery_manifest.pending_tool_frames",
        ),
        permission_checkpoints=_dict_list(payload.get("permission_checkpoints")),
        hook_lifecycle_records=_dict_list(payload.get("hook_lifecycle_records")),
        compaction_lifecycle_records=_dict_list(payload.get("compaction_lifecycle_records")),
        permission_profile=_sanitize_permission_profile_evidence(payload.get("permission_profile")),
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
    session_context: Any | None = None,
    session_id: Any | None = None,
    data_root: str | Path | None = None,
) -> RecoveryManifest | None:
    """Load only recovery state that is bound to the requested session identity.

    Agent-level manifests remain read-only compatibility inputs. They are
    accepted only when they declare the exact session; anonymous legacy files
    fail closed because their paths, tool frames, and permission profile have no
    trustworthy owner.
    """

    expected_identity = _session_identity(session_context)
    expected_session_id = _identity_text(session_id) or expected_identity.get("session_id")
    if expected_session_id is None:
        logger.warning("Recovery manifest load skipped because session identity is absent")
        return None
    path = recovery_manifest_path(
        agent_id,
        session_id=expected_session_id,
        runtime_task_id=expected_identity.get("runtime_task_id"),
        data_root=data_root,
    )
    agent_root = path.parents[3]

    expected_authority = {
        **expected_identity,
        "agent_id": _identity_text(agent_id) or expected_identity.get("agent_id"),
        "session_id": expected_session_id,
    }

    def consume_canonical_snapshot(snapshot: _RegularFileSnapshot | None) -> RecoveryManifest | None:
        canonical = _manifest_from_raw(snapshot.raw, path=path) if snapshot is not None else None
        if canonical is None:
            return None
        if _block_incomplete_manifest_authority(
            canonical,
            session_context=session_context,
            expected_identity=expected_authority,
            path=path,
            snapshot=snapshot,
        ):
            return None
        if (
            not _manifest_has_authority_bearing_state(canonical)
            and not canonical.is_empty()
            and canonical.checkpoint_seq is None
            and _missing_manifest_authority_fields(canonical)
        ):
            trusted_reader = RecoveryManifest(
                session_id=_identity_text(expected_authority.get("session_id")),
                agent_id=_identity_text(expected_authority.get("agent_id")),
                tenant_id=_identity_text(expected_authority.get("tenant_id")),
                runtime_task_id=_runtime_task_identity(expected_authority.get("runtime_task_id")),
                claim_version=_identity_int(expected_authority.get("claim_version")),
                claim_worker_id=_identity_text(expected_authority.get("claim_worker_id")),
            )
            if not _missing_manifest_authority_fields(trusted_reader) and _legacy_manifest_matches_expected(
                canonical,
                expected_identity=expected_authority,
                agent_id=agent_id,
            ):
                previous_checkpoint_seq = canonical.checkpoint_seq
                canonical = _prepare_legacy_import(
                    canonical,
                    agent_id=agent_id,
                    expected_identity=expected_authority,
                )
                if canonical.checkpoint_seq is None:
                    canonical.checkpoint_seq = previous_checkpoint_seq
                _atomic_write_manifest(path, canonical.to_payload())
        if canonical.is_empty() or canonical.legacy_conflict:
            return None
        if recovery_manifest_matches_session(
            session_context,
            canonical,
            agent_id=agent_id,
            session_id=expected_session_id,
        ):
            return canonical
        logger.warning("Recovery manifest identity mismatch at %s; refusing restore", path)
        return None

    with _session_manifest_lock(path):
        canonical_snapshot = _read_regular_file_snapshot(path)
        if _path_lexists(path):
            return consume_canonical_snapshot(canonical_snapshot)

    with _legacy_manifest_lock(agent_root):
        with _session_manifest_lock(path):
            canonical_snapshot = _read_regular_file_snapshot(path)
            if _path_lexists(path):
                return consume_canonical_snapshot(canonical_snapshot)
        if _path_lexists(_legacy_cutover_path(agent_root)):
            return None

        # The workspace legacy location was Agent-writable and therefore has
        # no trustworthy provenance. Only the platform-owned runtime legacy
        # file can participate in compatibility import.
        candidate = agent_root / RECOVERY_MANIFEST_REL_PATH
        if not _path_lexists(candidate):
            _retire_legacy_manifests(agent_root, reason="untrusted_workspace_cleanup")
            return None
        manifest = _read_manifest_file(candidate, require_private=False)
        if manifest is None or manifest.session_id is None:
            _retire_legacy_manifests(agent_root, reason="invalid_or_unowned")
            return None
        if not _legacy_manifest_matches_expected(
            manifest,
            expected_identity={**expected_identity, "session_id": expected_session_id},
            agent_id=agent_id,
        ):
            logger.warning("Legacy recovery manifest at %s belongs to another runtime identity", candidate)
            return None
        imported = _prepare_legacy_import(
            manifest,
            agent_id=agent_id,
            expected_identity={**expected_identity, "session_id": expected_session_id},
        )
        if _manifest_has_authority_bearing_state(imported) and (
            imported_missing := _missing_manifest_authority_fields(imported)
        ):
            logger.error(
                "Legacy recovery import rejected because trusted reader authority is incomplete: %s",
                ", ".join(imported_missing),
            )
            _retire_legacy_manifests(agent_root, reason="incomplete_authority")
            return None
        # Commit canonical continuity first. A crash during quarantine/cutover
        # can then resume from this byte-complete checkpoint.
        with _session_manifest_lock(path):
            canonical_before_import_snapshot = _read_regular_file_snapshot(path)
            canonical_before_import = (
                _manifest_from_raw(canonical_before_import_snapshot.raw, path=path)
                if canonical_before_import_snapshot is not None
                else None
            )
            if _path_lexists(path):
                if canonical_before_import is None:
                    return None
                if _block_incomplete_manifest_authority(
                    canonical_before_import,
                    session_context=session_context,
                    expected_identity=expected_authority,
                    path=path,
                    snapshot=canonical_before_import_snapshot,
                ):
                    return None
            if canonical_before_import is None:
                _atomic_write_manifest(path, imported.to_payload())
        _retire_legacy_manifests(agent_root, reason="imported", consumed_sources={candidate})
        with _session_manifest_lock(path):
            canonical_after_cutover_snapshot = _read_regular_file_snapshot(path)
            return consume_canonical_snapshot(canonical_after_cutover_snapshot)
    return None


def _path_lexists(path: Path) -> bool:
    try:
        with _secure_recovery_parent(path, create=False) as (parent_descriptor, filename):
            os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


@dataclass(frozen=True, slots=True)
class _RegularFileSnapshot:
    raw: bytes
    device: int
    inode: int
    mode: int
    owner: int
    link_count: int
    modified_at: float


@dataclass(slots=True)
class _VerifiedPrivateFile:
    path: Path
    descriptor: int
    snapshot: _RegularFileSnapshot
    expected_sha256: str

    @property
    def raw(self) -> bytes:
        return self.snapshot.raw

    @property
    def device(self) -> int:
        return self.snapshot.device

    @property
    def inode(self) -> int:
        return self.snapshot.inode

    @property
    def mode(self) -> int:
        return self.snapshot.mode

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> _VerifiedPrivateFile:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def _normalize_trusted_platform_data_root(data_root: Path) -> Path:
    """Normalize only macOS's immutable root aliases before the secure walk."""

    if sys.platform != "darwin":
        return data_root
    parts = data_root.parts
    if len(parts) < 2 or parts[0] != os.sep or parts[1] not in {"tmp", "var"}:
        return data_root
    component = parts[1]
    alias = Path(os.sep) / component
    try:
        alias_stat = os.lstat(alias)
        target = os.readlink(alias)
    except OSError:
        return data_root
    if (
        not stat.S_ISLNK(alias_stat.st_mode)
        or alias_stat.st_uid != 0
        or target not in {f"private/{component}", f"/private/{component}"}
    ):
        return data_root
    return Path(os.sep) / "private" / component / Path(*parts[2:])


def _recovery_tree_identity(path: Path) -> tuple[Path, tuple[str, ...]]:
    """Return the lexical data root and relative recovery-tree components.

    Recovery paths always have ``<data_root>/<agent>/(runtime_artifacts|workspace)``.
    Resolve nothing here: following a symlink while deriving the authority root
    would defeat the secure directory walk below.
    """

    lexical = Path(path)
    if ".." in lexical.parts:
        raise OSError(f"Secure recovery path rejects parent traversal: {path}")
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    parts = lexical.parts
    marker_indices = [
        index for index, component in enumerate(parts) if component in {"runtime_artifacts", "workspace"} and index >= 2
    ]
    if not marker_indices:
        raise OSError(f"Secure recovery path is outside the recovery tree: {path}")
    marker_index = max(marker_indices)
    data_root = _normalize_trusted_platform_data_root(Path(*parts[: marker_index - 1]))
    relative_parts = tuple(parts[marker_index - 1 :])
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise OSError(f"Secure recovery path is invalid: {path}")
    return data_root, relative_parts


@contextmanager
def _secure_recovery_parent(path: Path, *, create: bool):
    """Open a pinned parent dirfd from filesystem root without following links."""

    data_root, relative_parts = _recovery_tree_identity(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, directory_flags)

    def descend(component: str, *, may_create: bool, require_private: bool) -> int:
        created = False
        if may_create:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                created = True
            except FileExistsError:
                pass
        try:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise OSError(f"Secure recovery path rejects symlink or non-directory component: {component}") from exc
        try:
            directory_stat = os.fstat(next_descriptor)
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise OSError(f"Secure recovery path component is not a directory: {component}")
            if created:
                os.fchmod(next_descriptor, 0o700)
                os.fsync(next_descriptor)
                os.fsync(descriptor)
            if require_private:
                if directory_stat.st_uid != os.geteuid():
                    raise OSError(f"Secure recovery directory has the wrong owner: {component}")
                if stat.S_IMODE(directory_stat.st_mode) & 0o022:
                    raise OSError(f"Secure recovery directory permissions are group/other writable: {component}")
            return next_descriptor
        except Exception:
            os.close(next_descriptor)
            raise

    try:
        data_components = tuple(part for part in data_root.parts if part not in {os.sep, ""})
        if not data_components:
            raise OSError("Secure recovery data root cannot be the filesystem root")
        for index, component in enumerate(data_components):
            next_descriptor = descend(
                component,
                may_create=create,
                require_private=index == len(data_components) - 1,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        for component in relative_parts[:-1]:
            next_descriptor = descend(component, may_create=create, require_private=True)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, relative_parts[-1]
    finally:
        os.close(descriptor)


def _read_regular_file_snapshot(
    path: Path,
    *,
    max_bytes: int = MAX_RECOVERY_MANIFEST_BYTES,
    require_private: bool = True,
) -> _RegularFileSnapshot | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with _secure_recovery_parent(path, create=False) as (parent_descriptor, filename):
            descriptor = os.open(filename, flags, dir_fd=parent_descriptor)
            try:
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    logger.warning("Recovery manifest path is not a regular file: %s", path)
                    return None
                if file_stat.st_uid != os.geteuid() or file_stat.st_nlink != 1:
                    logger.warning("Recovery manifest path owner or link count is unsafe: %s", path)
                    return None
                if require_private and stat.S_IMODE(file_stat.st_mode) != 0o600:
                    logger.warning("Recovery manifest path permissions are not 0600: %s", path)
                    return None
                if file_stat.st_size < 0 or file_stat.st_size > max_bytes:
                    logger.warning("Recovery manifest exceeds the %d byte limit: %s", max_bytes, path)
                    return None
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    chunk = os.read(descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                if len(raw) > max_bytes:
                    logger.warning("Recovery manifest exceeded the byte limit while reading: %s", path)
                    return None
                return _RegularFileSnapshot(
                    raw=raw,
                    device=file_stat.st_dev,
                    inode=file_stat.st_ino,
                    mode=file_stat.st_mode,
                    owner=file_stat.st_uid,
                    link_count=file_stat.st_nlink,
                    modified_at=file_stat.st_mtime,
                )
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Recovery manifest path rejected at %s: %s", path, exc)
        return None


def _read_regular_file_bytes(
    path: Path,
    *,
    max_bytes: int = MAX_RECOVERY_MANIFEST_BYTES,
    require_private: bool = True,
) -> bytes | None:
    snapshot = _read_regular_file_snapshot(path, max_bytes=max_bytes, require_private=require_private)
    return snapshot.raw if snapshot is not None else None


def _manifest_from_raw(raw: bytes | None, *, path: Path) -> RecoveryManifest | None:
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("Failed to inspect recovery manifest at %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        return None
    return _manifest_from_payload(payload)


def _read_manifest_file(path: Path, *, require_private: bool = True) -> RecoveryManifest | None:
    return _manifest_from_raw(_read_regular_file_bytes(path, require_private=require_private), path=path)


def _fsync_directory(path: Path) -> None:
    probe = path / ".secure-recovery-fsync-probe"
    with _secure_recovery_parent(probe, create=False) as (descriptor, _filename):
        os.fsync(descriptor)


def _ensure_private_directory(path: Path) -> None:
    probe = path / ".secure-recovery-directory-probe"
    with _secure_recovery_parent(probe, create=True):
        return


def _is_canonical_recovery_manifest_path(path: Path) -> bool:
    """Recognize the session/run checkpoint lane, excluding sidecar lanes."""

    parent = path.parent
    session_component = parent.name
    manifest_component = path.stem

    def is_digest(value: str) -> bool:
        return len(value) == 64 and all(character in "0123456789abcdef" for character in value)

    return bool(
        path.suffix == ".json"
        and is_digest(session_component)
        and (manifest_component == "session" or is_digest(manifest_component))
        and parent.parent.name == RECOVERY_MANIFESTS_REL_DIR.name
        and parent.parent.parent.name == "runtime_artifacts"
    )


def _atomic_write_manifest(path: Path, payload: dict[str, Any]) -> bytes:
    if _is_canonical_recovery_manifest_path(path):
        manifest = _manifest_from_payload(payload)
        if _manifest_has_authority_bearing_state(manifest) and (
            missing_authority := _missing_manifest_authority_fields(manifest)
        ):
            raise ValueError("Recovery manifest authority identity is incomplete: " + ", ".join(missing_authority))
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_bytes(path, raw)
    return raw


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    if len(payload) > MAX_RECOVERY_MANIFEST_BYTES:
        raise ValueError(f"Recovery manifest exceeds the {MAX_RECOVERY_MANIFEST_BYTES} byte limit: {path}")
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    with _secure_recovery_parent(path, create=True) as (parent_descriptor, filename):
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.fsync(parent_descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def _write_verified_private_bytes(path: Path, payload: bytes) -> _VerifiedPrivateFile:
    """Atomically replace private evidence and return a pinned verified fd."""

    _atomic_write_bytes(path, payload)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    with _secure_recovery_parent(path, create=False) as (parent_descriptor, filename):
        descriptor = os.open(filename, flags, dir_fd=parent_descriptor)
    snapshot = _snapshot_from_open_descriptor(descriptor, path=path)
    if snapshot is None or snapshot.raw != payload:
        os.close(descriptor)
        raise OSError(f"Private recovery evidence verification failed: {path}")
    verified = _VerifiedPrivateFile(
        path=path,
        descriptor=descriptor,
        snapshot=snapshot,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    try:
        _verify_private_file_reference(verified)
    except Exception:
        verified.close()
        raise
    return verified


def _snapshot_from_open_descriptor(
    descriptor: int,
    *,
    path: Path,
    max_bytes: int = MAX_RECOVERY_MANIFEST_BYTES,
) -> _RegularFileSnapshot | None:
    file_stat = os.fstat(descriptor)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.geteuid()
        or file_stat.st_nlink != 1
        or stat.S_IMODE(file_stat.st_mode) != 0o600
        or file_stat.st_size < 0
        or file_stat.st_size > max_bytes
    ):
        return None
    chunks: list[bytes] = []
    offset = 0
    remaining = max_bytes + 1
    while remaining > 0:
        chunk = os.pread(descriptor, min(64 * 1024, remaining), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        return None
    return _RegularFileSnapshot(
        raw=raw,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        mode=file_stat.st_mode,
        owner=file_stat.st_uid,
        link_count=file_stat.st_nlink,
        modified_at=file_stat.st_mtime,
    )


def _verify_private_file_reference(verified: _VerifiedPrivateFile) -> _RegularFileSnapshot:
    current = _snapshot_from_open_descriptor(verified.descriptor, path=verified.path)
    if (
        current is None
        or current.device != verified.device
        or current.inode != verified.inode
        or current.raw != verified.raw
        or hashlib.sha256(current.raw).hexdigest() != verified.expected_sha256
    ):
        raise OSError(f"Private recovery evidence changed after verification: {verified.path}")
    with _secure_recovery_parent(verified.path, create=False) as (parent_descriptor, filename):
        entry = os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(entry.st_mode)
            or entry.st_dev != current.device
            or entry.st_ino != current.inode
            or entry.st_uid != os.geteuid()
            or entry.st_nlink != 1
            or stat.S_IMODE(entry.st_mode) != 0o600
        ):
            raise OSError(f"Private recovery evidence reference changed: {verified.path}")
    return current


def _unlink_regular_snapshot(
    path: Path,
    snapshot: _RegularFileSnapshot,
    *,
    require_private: bool = True,
) -> bool:
    """Unlink only the exact regular inode that was previously inspected."""

    try:
        with _secure_recovery_parent(path, create=False) as (parent_descriptor, filename):
            current = os.stat(filename, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_dev != snapshot.device
                or current.st_ino != snapshot.inode
                or current.st_uid != snapshot.owner
                or current.st_nlink != 1
                or (require_private and stat.S_IMODE(current.st_mode) != 0o600)
            ):
                return False
            os.unlink(filename, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            return True
    except FileNotFoundError:
        return True


def _acquire_bounded_flock(
    descriptor: int,
    *,
    lock_path: Path,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out acquiring recovery manifest lock: {lock_path}")
            time.sleep(RECOVERY_LOCK_POLL_SECONDS)


@contextmanager
def _manifest_file_lock(lock_path: Path, *, timeout_seconds: float | None = None):
    effective_timeout = RECOVERY_LOCK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    base_flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    base_flags |= getattr(os, "O_NONBLOCK", 0)
    with _secure_recovery_parent(lock_path, create=True) as (parent_descriptor, filename):
        deadline = time.monotonic() + effective_timeout
        while True:
            try:
                descriptor = os.open(
                    filename,
                    base_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                created = True
                break
            except FileExistsError:
                descriptor = os.open(filename, base_flags, dir_fd=parent_descriptor)
                created = False
                break
            except FileNotFoundError:
                # macOS may transiently report ENOENT when two processes create
                # the same lock in a freshly-created directory tree. Keep the
                # already-pinned dirfd and retry only within the normal lock SLA.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(RECOVERY_LOCK_POLL_SECONDS)
        try:
            lock_stat = os.fstat(descriptor)
            if created:
                os.fchmod(descriptor, 0o600)
                os.fsync(parent_descriptor)
                lock_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
                or lock_stat.st_nlink != 1
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise OSError(f"Recovery manifest lock is not a private single-link regular file: {lock_path}")
            _acquire_bounded_flock(
                descriptor,
                lock_path=lock_path,
                timeout_seconds=effective_timeout,
            )
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def _session_manifest_lock(path: Path):
    with _manifest_file_lock(path.with_suffix(".lock")):
        _scavenge_stale_atomic_temps(path)
        yield


def _scavenge_stale_atomic_temps(path: Path) -> None:
    prefix = f".{path.name}."
    suffix = ".tmp"
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    read_flags |= getattr(os, "O_NONBLOCK", 0)
    now = time.time()
    with _secure_recovery_parent(path, create=False) as (parent_descriptor, _filename):
        for candidate in os.listdir(parent_descriptor):
            if not candidate.startswith(prefix) or not candidate.endswith(suffix):
                continue
            nonce = candidate[len(prefix) : -len(suffix)]
            if len(nonce) != 32 or any(character not in "0123456789abcdef" for character in nonce):
                continue
            try:
                descriptor = os.open(candidate, read_flags, dir_fd=parent_descriptor)
            except OSError:
                continue
            try:
                candidate_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(candidate_stat.st_mode)
                    or candidate_stat.st_uid != os.geteuid()
                    or candidate_stat.st_nlink != 1
                    or stat.S_IMODE(candidate_stat.st_mode) != 0o600
                    or now - candidate_stat.st_mtime < RECOVERY_TEMP_STALE_SECONDS
                ):
                    continue
                current = os.stat(candidate, dir_fd=parent_descriptor, follow_symlinks=False)
                if current.st_dev != candidate_stat.st_dev or current.st_ino != candidate_stat.st_ino:
                    continue
                os.unlink(candidate, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            finally:
                os.close(descriptor)


def _legacy_cutover_path(agent_root: Path) -> Path:
    return agent_root / RECOVERY_MANIFESTS_REL_DIR / LEGACY_RECOVERY_CUTOVER_FILENAME


def _legacy_manifest_candidates(agent_root: Path) -> tuple[Path, Path]:
    return (
        agent_root / RECOVERY_MANIFEST_REL_PATH,
        agent_root / LEGACY_RECOVERY_MANIFEST_REL_PATH,
    )


@contextmanager
def _legacy_manifest_lock(agent_root: Path):
    lock_path = agent_root / RECOVERY_MANIFESTS_REL_DIR / ".legacy.lock"
    with _manifest_file_lock(
        lock_path,
        timeout_seconds=RECOVERY_LEGACY_LOCK_TIMEOUT_SECONDS,
    ):
        yield


def _quarantine_legacy_snapshot(
    *,
    agent_root: Path,
    source: Path,
    snapshot: _RegularFileSnapshot,
    label: str,
) -> tuple[Path, bool]:
    quarantine_root = agent_root / LEGACY_RECOVERY_QUARANTINE_REL_DIR
    digest = hashlib.sha256(snapshot.raw).hexdigest()
    destination = quarantine_root / f"{label}-{digest}.json"
    with _write_verified_private_bytes(destination, snapshot.raw) as verified:
        _verify_private_file_reference(verified)
        removed = _unlink_regular_snapshot(source, snapshot, require_private=False)
        _verify_private_file_reference(verified)
        return destination, removed


def _retire_legacy_manifests(
    agent_root: Path,
    *,
    reason: str,
    consumed_sources: set[Path] | None = None,
) -> None:
    """Migrate trusted legacy state and quarantine untrusted/consumed bytes.

    ``runtime_artifacts/recovery_manifest.json`` was platform-owned and may be
    migrated. ``workspace/recovery_manifest.json`` was Agent-writable, so it is
    evidence only and can never become runtime authority.
    """

    consumed = {path.resolve(strict=False) for path in (consumed_sources or set())}
    runtime_legacy, workspace_legacy = _legacy_manifest_candidates(agent_root)
    if not any(_path_lexists(path) for path in (runtime_legacy, workspace_legacy)):
        return
    records: list[dict[str, Any]] = []
    legacy_conflicts: list[dict[str, Any]] = []
    trusted_legacy_remains = False

    for source, label, trusted in (
        (runtime_legacy, "runtime", True),
        (workspace_legacy, "workspace-untrusted", False),
    ):
        if not _path_lexists(source):
            continue
        snapshot = _read_regular_file_snapshot(source, require_private=False)
        record: dict[str, Any] = {"source": source.relative_to(agent_root).as_posix()}
        if snapshot is None:
            record["status"] = "rejected_non_regular_or_oversized"
            records.append(record)
            if trusted:
                trusted_legacy_remains = True
            continue
        digest = hashlib.sha256(snapshot.raw).hexdigest()
        record["sha256"] = digest
        manifest = _manifest_from_raw(snapshot.raw, path=source)

        if not trusted:
            record["status"] = "rejected_untrusted_agent_writable_source"
        elif source.resolve(strict=False) in consumed:
            record["status"] = "consumed_by_owner_import"
        elif manifest is None or manifest.session_id is None:
            record["status"] = "rejected_invalid_or_unowned"
        elif manifest.runtime_task_id is None:
            record["status"] = "deferred_until_owner_resume"
            records.append(record)
            trusted_legacy_remains = True
            continue
        elif manifest.agent_id is not None and _entity_identity(manifest.agent_id) != _entity_identity(agent_root.name):
            record["status"] = "rejected_agent_identity_mismatch"
        else:
            # The platform-owned agent root is trusted authority for the one
            # field it owns. No tenant/run/claim field may be synthesized here.
            manifest.agent_id = agent_root.name
            if _manifest_has_authority_bearing_state(manifest) and (
                missing_authority := _missing_manifest_authority_fields(manifest)
            ):
                record["status"] = "rejected_incomplete_authority"
                record["missing_fields"] = sorted(missing_authority)
            elif continuity_missing := _missing_manifest_authority_fields(manifest):
                # Continuity-only legacy bytes remain compatible, but only the
                # owning resumed runtime can bind their tenant/run/claim tuple.
                record["status"] = "deferred_until_owner_resume"
                record["missing_fields"] = sorted(continuity_missing)
                records.append(record)
                trusted_legacy_remains = True
                continue
            else:
                imported_path = recovery_manifest_path(
                    agent_root.name,
                    session_id=manifest.session_id,
                    runtime_task_id=manifest.runtime_task_id,
                    data_root=agent_root.parent,
                )
                with _session_manifest_lock(imported_path):
                    existing_snapshot = _read_regular_file_snapshot(imported_path)
                    existing = (
                        _manifest_from_raw(existing_snapshot.raw, path=imported_path)
                        if existing_snapshot is not None
                        else None
                    )
                    if _path_lexists(imported_path) and existing is None:
                        record["status"] = "rejected_corrupt_canonical"
                    elif (
                        existing is not None
                        and existing_snapshot is not None
                        and _manifest_has_authority_bearing_state(existing)
                        and (existing_missing := _missing_manifest_authority_fields(existing))
                    ):
                        _record_incomplete_authority_conflict(
                            path=imported_path,
                            snapshot=existing_snapshot,
                            existing=existing,
                            incoming=manifest,
                            missing_fields=existing_missing,
                        )
                        record["status"] = "preserved_canonical_incomplete_authority"
                        conflict = {
                            "reason": "stored_canonical_authority_incomplete",
                            "canonical": imported_path.relative_to(agent_root).as_posix(),
                            "canonical_sha256": hashlib.sha256(existing_snapshot.raw).hexdigest(),
                            "missing_fields": sorted(existing_missing),
                            "legacy_sha256": digest,
                        }
                        record["conflict"] = conflict
                        legacy_conflicts.append(conflict)
                    elif existing is None:
                        _atomic_write_manifest(imported_path, manifest.to_payload())
                        record["status"] = "imported"
                        record["imported_to"] = imported_path.relative_to(agent_root).as_posix()
                    elif _checkpoint_blocks_replacement(existing, manifest):
                        record["status"] = "preserved_canonical_conflict"
                        conflict = {
                            "reason": "canonical_precedence_or_same_claim_conflict",
                            "canonical": imported_path.relative_to(agent_root).as_posix(),
                            "legacy_sha256": digest,
                            "stored_claim": existing.claim_version,
                            "legacy_claim": manifest.claim_version,
                        }
                        record["conflict"] = conflict
                        legacy_conflicts.append(conflict)
                    else:
                        _atomic_write_manifest(imported_path, manifest.to_payload())
                        record["status"] = "imported_newer_claim"
                        record["imported_to"] = imported_path.relative_to(agent_root).as_posix()

        destination, removed = _quarantine_legacy_snapshot(
            agent_root=agent_root,
            source=source,
            snapshot=snapshot,
            label=label,
        )
        record["quarantine"] = destination.relative_to(agent_root).as_posix()
        record["removed"] = removed
        if trusted and not removed:
            trusted_legacy_remains = True
        records.append(record)

    if trusted_legacy_remains or _path_lexists(runtime_legacy):
        return
    _atomic_write_manifest(
        _legacy_cutover_path(agent_root),
        {
            "schema": "hive.recovery_manifest.legacy_cutover.v1",
            "legacy_fallback_disabled": True,
            "reason": reason,
            "records": records,
            "legacy_conflicts": legacy_conflicts,
        },
    )


def _legacy_manifest_matches_expected(
    manifest: RecoveryManifest,
    *,
    expected_identity: dict[str, Any],
    agent_id: Any,
) -> bool:
    """Match trusted legacy ownership while allowing a runless file to bind once."""

    if manifest.session_id != _identity_text(expected_identity.get("session_id")):
        return False
    expected_agent = _entity_identity(agent_id)
    if manifest.agent_id is not None and _entity_identity(manifest.agent_id) != expected_agent:
        return False
    expected_tenant = _entity_identity(expected_identity.get("tenant_id"))
    if manifest.tenant_id is not None and _entity_identity(manifest.tenant_id) != expected_tenant:
        return False
    expected_runtime = _runtime_task_identity(expected_identity.get("runtime_task_id"))
    if manifest.runtime_task_id is not None and manifest.runtime_task_id != expected_runtime:
        return False
    expected_claim = _identity_int(expected_identity.get("claim_version"))
    if manifest.claim_version is not None:
        if expected_claim is None or manifest.claim_version > expected_claim:
            return False
        if manifest.claim_version == expected_claim and manifest.claim_worker_id is not None:
            expected_worker = _identity_text(expected_identity.get("claim_worker_id")) or "unknown"
            if manifest.claim_worker_id != expected_worker:
                return False
    return True


def _prepare_legacy_import(
    manifest: RecoveryManifest,
    *,
    agent_id: Any,
    expected_identity: dict[str, Any],
) -> RecoveryManifest:
    expected_runtime_task_id = _runtime_task_identity(expected_identity.get("runtime_task_id"))
    if manifest.runtime_task_id is None and expected_runtime_task_id is not None:
        # An old session-only file has no trustworthy run/claim authority. Keep
        # only file continuity; capability, current-turn, and side-effect state
        # must be re-established from current DB/runtime facts.
        manifest.current_turn_writes = []
        manifest.recent_tool_outcomes = []
        manifest.active_skills = []
        manifest.active_tool_groups = []
        manifest.recent_external_refs = []
        manifest.pending_items = []
        manifest.blocked_patterns = []
        manifest.discovered_tools = []
        manifest.pending_tool_frames = []
        manifest.permission_checkpoints = []
        manifest.hook_lifecycle_records = []
        manifest.compaction_lifecycle_records = []
        manifest.permission_profile = {}
        manifest.mcp_assignments = []
        manifest.truth_evidence_refs = []
        manifest.truth_evidence = []
        manifest.pending_skill_handoffs = []
        manifest.executed_skill_handoffs = []
        manifest.continuation_records = []
        manifest.recovery_reconciliation_blocked = False
        manifest.prior_run_recovery_reconciliations = []
        manifest.reconciliation_resolution = {}

    manifest.session_id = _identity_text(expected_identity.get("session_id"))
    manifest.agent_id = _identity_text(agent_id)
    manifest.tenant_id = _identity_text(expected_identity.get("tenant_id"))
    manifest.runtime_task_id = expected_runtime_task_id
    manifest.claim_version = _identity_int(expected_identity.get("claim_version"))
    manifest.claim_worker_id = _identity_text(expected_identity.get("claim_worker_id"))
    manifest.checkpoint_seq = _identity_int(expected_identity.get("checkpoint_seq"))
    return manifest


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
    return _bounded_recovery_json(value, scrub_secrets=True).decode("utf-8", errors="replace")


def _append_unique_dicts(
    target: list[dict[str, Any]], values: list[dict[str, Any]], *, limit: int | None = None
) -> None:
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


def _mcp_server_refs_from_assignments(assignments: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for item in assignments:
        if not isinstance(item, dict):
            continue
        for key in ("server", "server_name", "name", "server_id", "url"):
            value = str(item.get(key) or "").strip()
            if value:
                refs.append(value)
                break
    return list(dict.fromkeys(refs))


def recovery_manifest_matches_session(
    session_context: Any,
    manifest: RecoveryManifest | None,
    *,
    agent_id: Any | None = None,
    session_id: Any | None = None,
) -> bool:
    """Return whether session-scoped recovery state belongs to this session.

    Session identity is mandatory. Additional tenant, agent, runtime-task, and
    claim fields are fail-closed when the manifest carries them. A newer claim
    may recover the previous claim's checkpoint; a stale claim may never read a
    checkpoint written by its successor.
    """

    if manifest is None:
        return False
    manifest_session_id = str(manifest.session_id or "").strip()
    if not manifest_session_id:
        return False
    expected = _session_identity(session_context)
    runtime_session_id = _identity_text(session_id) or expected["session_id"]
    if runtime_session_id != manifest_session_id:
        return False

    expected_agent_id = _entity_identity(agent_id) or _entity_identity(expected["agent_id"])
    if manifest.agent_id and expected_agent_id != _entity_identity(manifest.agent_id):
        return False
    expected_tenant_id = _entity_identity(expected["tenant_id"])
    manifest_tenant_id = _entity_identity(manifest.tenant_id)
    if expected_tenant_id is not None and manifest_tenant_id != expected_tenant_id:
        return False
    if manifest.tenant_id and expected_tenant_id != manifest_tenant_id:
        return False
    if expected["runtime_task_id"] is not None and manifest.runtime_task_id != expected["runtime_task_id"]:
        return False
    if manifest.runtime_task_id and expected["runtime_task_id"] != manifest.runtime_task_id:
        return False
    if manifest.claim_version is not None:
        expected_claim = expected["claim_version"]
        if expected_claim is None or expected_claim < manifest.claim_version:
            return False
        if expected_claim == manifest.claim_version:
            expected_worker = expected.get("claim_worker_id") or "unknown"
            manifest_worker = manifest.claim_worker_id or "unknown"
            if expected_worker != manifest_worker:
                return False
    return True


def hydrate_session_context_from_recovery_manifest(
    session_context: Any,
    manifest: RecoveryManifest | None,
    *,
    agent_id: Any | None = None,
) -> bool:
    """Restore machine-readable runtime state from a persisted RecoveryManifest.

    The prompt renderer still includes ``to_restoration_text()`` for model-visible
    continuity, but this function revives non-authoritative runtime state such as
    tool discovery, pending frames, MCP references, Truth evidence, and skill
    handoffs. Permission authority always remains the fresh DB/runtime profile;
    a checkpoint may preserve it as audit evidence but never restore or render
    it. The merge is idempotent so prompt assembly may call it repeatedly.
    """
    if session_context is None or manifest is None:
        return False
    expected_identity = _session_identity(session_context)
    expected_identity["agent_id"] = _identity_text(agent_id) or expected_identity.get("agent_id")
    if _block_incomplete_manifest_authority(
        manifest,
        session_context=session_context,
        expected_identity=expected_identity,
    ):
        return False
    if manifest.is_empty() or not recovery_manifest_matches_session(session_context, manifest, agent_id=agent_id):
        return False

    _append_unique_strings(getattr(session_context, "recent_files", []), manifest.recent_reads, limit=10)
    _append_unique_strings(getattr(session_context, "recent_writes", []), manifest.recent_writes, limit=10)
    _append_unique_strings(getattr(session_context, "current_turn_writes", []), manifest.current_turn_writes, limit=10)
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
        ("pending_tool_frames", [_sanitize_pending_tool_frame(item) for item in manifest.pending_tool_frames]),
        ("hook_lifecycle_records", manifest.hook_lifecycle_records),
        ("compaction_lifecycle_records", manifest.compaction_lifecycle_records),
        ("mcp_assignments", manifest.mcp_assignments),
        ("truth_evidence", manifest.truth_evidence),
        ("executed_skill_handoffs", manifest.executed_skill_handoffs),
        ("continuation_records", manifest.continuation_records),
    ):
        _merge_metadata_dict_list(metadata, key, values)
    _merge_metadata_dict_list(
        metadata,
        "recovered_permission_checkpoint_evidence",
        [_sanitize_permission_checkpoint_evidence(item) for item in manifest.permission_checkpoints],
    )
    _merge_metadata_dict_list(
        metadata,
        "pending_skill_handoffs",
        [_sanitize_recovered_skill_handoff(item) for item in manifest.pending_skill_handoffs],
    )
    _merge_metadata_string_list(metadata, "truth_evidence_refs", manifest.truth_evidence_refs)
    _merge_metadata_string_list(metadata, "evidence_refs", manifest.truth_evidence_refs)
    _merge_metadata_string_list(
        metadata, "mcp_server_refs", _mcp_server_refs_from_assignments(manifest.mcp_assignments)
    )
    if manifest.pending_tool_frames:
        recovered_frames = [_sanitize_pending_tool_frame(item) for item in manifest.pending_tool_frames]
        metadata["recovered_pending_tool_frames"] = recovered_frames
        reconciliation_frames = [
            dict(frame)
            for frame in recovered_frames
            if str(frame.get("status") or "").strip().lower() == "needs_reconciliation"
            or str(frame.get("event_type") or "").strip() == "unknown_recovered_tool_frame"
        ]
        if reconciliation_frames:
            metadata["recovered_tool_frame_reconciliation"] = reconciliation_frames
            metadata["recovery_reconciliation_blocked"] = True
    if manifest.prior_run_recovery_reconciliations:
        _merge_metadata_dict_list(
            metadata,
            "prior_run_recovery_reconciliations",
            manifest.prior_run_recovery_reconciliations,
        )
    if manifest.recovery_reconciliation_blocked:
        metadata["recovery_reconciliation_blocked"] = True
    if manifest.checkpoint_seq is not None:
        metadata["recovery_checkpoint_seq"] = manifest.checkpoint_seq
    if manifest.claim_worker_id and not metadata.get("claim_worker_id"):
        metadata["claim_worker_id"] = manifest.claim_worker_id
    metadata["recovered_from_manifest"] = True
    return True


def load_and_hydrate_recovery_manifest(
    agent_id: Any,
    session_context: Any,
    *,
    data_root: str | Path | None = None,
) -> RecoveryManifest | None:
    """Single governed reader for startup and post-compaction restoration."""

    manifest = load_recovery_manifest(
        agent_id,
        session_context=session_context,
        data_root=data_root,
    )
    if manifest is None:
        return None
    if not hydrate_session_context_from_recovery_manifest(
        session_context,
        manifest,
        agent_id=agent_id,
    ):
        return None
    return manifest


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

    identity = _session_identity(session_context)
    return RecoveryManifest(
        session_id=identity["session_id"],
        agent_id=identity["agent_id"],
        tenant_id=identity["tenant_id"],
        runtime_task_id=identity["runtime_task_id"],
        claim_version=identity["claim_version"],
        claim_worker_id=identity["claim_worker_id"],
        checkpoint_seq=identity["checkpoint_seq"],
        recent_reads=list(getattr(session_context, "recent_files", [])),
        recent_writes=list(getattr(session_context, "recent_writes", [])),
        current_turn_writes=list(getattr(session_context, "current_turn_writes", [])),
        file_snapshots=dict(getattr(session_context, "file_snapshots", {}) or {}),
        recent_tool_outcomes=list(getattr(session_context, "recent_tool_outcomes", [])),
        active_skills=list(getattr(session_context, "active_skills", [])),
        active_tool_groups=tool_group_names,
        recent_external_refs=list(getattr(session_context, "recent_external_refs", [])),
        pending_items=list(getattr(session_context, "pending_items", [])),
        discovered_tools=list(getattr(session_context, "discovered_tools", [])),
        pending_tool_frames=_pending_tool_frames_from_metadata(metadata),
        permission_checkpoints=_metadata_dict_list(metadata, "permission_checkpoint", "permission_checkpoints"),
        hook_lifecycle_records=_metadata_dict_list(metadata, "hook_lifecycle_records"),
        compaction_lifecycle_records=_metadata_dict_list(metadata, "compaction_lifecycle_records"),
        permission_profile=_sanitize_permission_profile_evidence(permission_profile),
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
        recovery_reconciliation_blocked=metadata.get("recovery_reconciliation_blocked") is True,
        prior_run_recovery_reconciliations=_metadata_dict_list(
            metadata,
            "prior_run_recovery_reconciliations",
        ),
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


def _manifest_has_authority_bearing_state(manifest: RecoveryManifest) -> bool:
    """Return whether a manifest can affect execution or assert side effects."""

    return any(
        (
            manifest.current_turn_writes,
            manifest.recent_tool_outcomes,
            manifest.active_skills,
            manifest.active_tool_groups,
            manifest.blocked_patterns,
            manifest.discovered_tools,
            manifest.pending_tool_frames,
            manifest.permission_checkpoints,
            manifest.permission_profile,
            manifest.mcp_assignments,
            manifest.truth_evidence_refs,
            manifest.truth_evidence,
            manifest.pending_skill_handoffs,
            manifest.executed_skill_handoffs,
            manifest.continuation_records,
            manifest.legacy_conflict,
            manifest.recovery_reconciliation_blocked,
            manifest.prior_run_recovery_reconciliations,
            manifest.reconciliation_resolution,
        )
    )


def _missing_manifest_authority_fields(manifest: RecoveryManifest) -> list[str]:
    required = {
        "tenant_id": manifest.tenant_id,
        "agent_id": manifest.agent_id,
        "session_id": manifest.session_id,
        "runtime_task_id": manifest.runtime_task_id,
        "claim_version": manifest.claim_version,
        "claim_worker_id": manifest.claim_worker_id,
    }
    return [key for key, value in required.items() if value is None or value == ""]


def _verify_regular_snapshot_unchanged(path: Path, expected: _RegularFileSnapshot) -> None:
    current = _read_regular_file_snapshot(path)
    if (
        current is None
        or current.device != expected.device
        or current.inode != expected.inode
        or hashlib.sha256(current.raw).digest() != hashlib.sha256(expected.raw).digest()
    ):
        raise OSError(f"Recovery manifest changed while preserving incomplete authority evidence: {path}")


def _record_incomplete_authority_conflict(
    *,
    path: Path,
    snapshot: _RegularFileSnapshot,
    existing: RecoveryManifest,
    incoming: RecoveryManifest,
    missing_fields: list[str],
) -> None:
    """Preserve raw bytes and record a secret-free, reversible conflict sidecar."""

    digest = hashlib.sha256(snapshot.raw).hexdigest()
    manifests_root = path.parents[1]
    quarantine = manifests_root / "authority_quarantine" / f"incomplete-{digest}.json"
    conflict = manifests_root / "authority_conflicts" / f"incomplete-{digest}.json"
    conflict_payload = {
        "schema": "hive.recovery_manifest.authority_conflict.v1",
        "state": "incomplete_authority_manifest",
        "raw_sha256": digest,
        "raw_bytes": len(snapshot.raw),
        "missing_fields": sorted(missing_fields),
        "canonical_ref": _checkpoint_receipt(path, snapshot.raw)["ref"],
        "quarantine_ref": _checkpoint_receipt(quarantine, snapshot.raw)["ref"],
        "stored_identity": {
            "tenant_id": existing.tenant_id,
            "agent_id": existing.agent_id,
            "session_id": existing.session_id,
            "runtime_task_id": existing.runtime_task_id,
            "claim_version": existing.claim_version,
            "claim_worker_id": existing.claim_worker_id,
        },
        "incoming_identity": {
            "tenant_id": incoming.tenant_id,
            "agent_id": incoming.agent_id,
            "session_id": incoming.session_id,
            "runtime_task_id": incoming.runtime_task_id,
            "claim_version": incoming.claim_version,
            "claim_worker_id": incoming.claim_worker_id,
        },
        "recorded_at_epoch": time.time(),
    }
    conflict_raw = (json.dumps(conflict_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    verified_quarantine: _VerifiedPrivateFile | None = None
    verified_conflict: _VerifiedPrivateFile | None = None
    try:
        _verify_regular_snapshot_unchanged(path, snapshot)
        verified_quarantine = _write_verified_private_bytes(quarantine, snapshot.raw)
        _verify_private_file_reference(verified_quarantine)
        _verify_regular_snapshot_unchanged(path, snapshot)
        verified_conflict = _write_verified_private_bytes(conflict, conflict_raw)
        _verify_private_file_reference(verified_quarantine)
        _verify_private_file_reference(verified_conflict)
        _verify_regular_snapshot_unchanged(path, snapshot)
    except Exception:
        conflict_snapshot = (
            verified_conflict.snapshot if verified_conflict is not None else _read_regular_file_snapshot(conflict)
        )
        if conflict_snapshot is not None:
            _unlink_regular_snapshot(conflict, conflict_snapshot)
        raise
    finally:
        if verified_conflict is not None:
            verified_conflict.close()
        if verified_quarantine is not None:
            verified_quarantine.close()


_INCOMPLETE_AUTHORITY_REASON = "incomplete_recovery_manifest_authority"


def _block_incomplete_manifest_authority(
    manifest: RecoveryManifest,
    *,
    session_context: Any | None,
    expected_identity: dict[str, Any],
    path: Path | None = None,
    snapshot: _RegularFileSnapshot | None = None,
) -> dict[str, Any] | None:
    """Fail closed before any authority-bearing canonical state is consumed."""

    if not _manifest_has_authority_bearing_state(manifest):
        return None
    missing_fields = _missing_manifest_authority_fields(manifest)
    if not missing_fields:
        return None

    missing_fields = sorted(missing_fields)
    evidence: dict[str, Any] = {
        "schema": "hive.recovery_manifest.authority_conflict.v1",
        "state": "incomplete_authority_manifest",
        "reason": _INCOMPLETE_AUTHORITY_REASON,
        "missing_fields": missing_fields,
    }
    if path is not None and snapshot is not None:
        digest = hashlib.sha256(snapshot.raw).hexdigest()
        quarantine = path.parents[1] / "authority_quarantine" / f"incomplete-{digest}.json"
        evidence.update(
            {
                "raw_sha256": digest,
                "raw_bytes": len(snapshot.raw),
                "canonical_ref": _checkpoint_receipt(path, snapshot.raw)["ref"],
                "quarantine_ref": _checkpoint_receipt(quarantine, snapshot.raw)["ref"],
            }
        )

    metadata = getattr(session_context, "metadata", None) if session_context is not None else None
    if session_context is not None and not isinstance(metadata, dict):
        metadata = {}
        session_context.metadata = metadata
    if isinstance(metadata, dict):
        metadata["recovery_reconciliation_blocked"] = True
        metadata["recovery_reconciliation_reason"] = _INCOMPLETE_AUTHORITY_REASON
        metadata["recovered_manifest_authority_conflict"] = dict(evidence)

    if path is not None and snapshot is not None:
        incoming = RecoveryManifest(
            tenant_id=_identity_text(expected_identity.get("tenant_id")),
            agent_id=_identity_text(expected_identity.get("agent_id")),
            session_id=_identity_text(expected_identity.get("session_id")),
            runtime_task_id=_runtime_task_identity(expected_identity.get("runtime_task_id")),
            claim_version=_identity_int(expected_identity.get("claim_version")),
            claim_worker_id=_identity_text(expected_identity.get("claim_worker_id")),
        )
        try:
            _record_incomplete_authority_conflict(
                path=path,
                snapshot=snapshot,
                existing=manifest,
                incoming=incoming,
                missing_fields=missing_fields,
            )
        except Exception as exc:  # noqa: BLE001 - blocking must survive evidence-write failure
            logger.error(
                "Failed to record incomplete recovery authority evidence at %s: %s",
                path,
                type(exc).__name__,
            )

    logger.error(
        "Recovery manifest consumption blocked because authority identity is incomplete: %s",
        ", ".join(missing_fields),
    )
    return evidence


def _checkpoint_blocks_replacement(existing: RecoveryManifest, incoming: RecoveryManifest) -> bool:
    """Return True when replacing ``existing`` would violate its identity fence."""

    if existing.legacy_conflict:
        return True
    if existing.reconciliation_resolution:
        resolution_action = str(existing.reconciliation_resolution.get("action") or "")
        if resolution_action != "retry":
            return True
        # A retry decision reopens the same RuntimeTask.  Only the next durable
        # claim may replace the tombstone; the worker/claim that was reconciled
        # remains permanently fenced.
        if (
            existing.claim_version is None
            or incoming.claim_version is None
            or incoming.claim_version <= existing.claim_version
        ):
            return True
    for field_name in ("session_id", "agent_id", "tenant_id", "runtime_task_id"):
        existing_value = getattr(existing, field_name)
        incoming_value = getattr(incoming, field_name)
        if field_name in {"agent_id", "tenant_id"}:
            existing_value = _entity_identity(existing_value)
            incoming_value = _entity_identity(incoming_value)
        if existing_value is not None and existing_value != incoming_value:
            return True
    if existing.claim_version is not None:
        if incoming.claim_version is None or incoming.claim_version < existing.claim_version:
            return True
        if incoming.claim_version > existing.claim_version:
            return False
    elif incoming.claim_version is not None:
        return False

    existing_worker = existing.claim_worker_id or "unknown"
    incoming_worker = incoming.claim_worker_id or "unknown"
    if existing_worker != incoming_worker:
        return True
    existing_seq = existing.checkpoint_seq or 0
    incoming_seq = incoming.checkpoint_seq or 0
    if incoming_seq < existing_seq:
        return True
    if incoming_seq > existing_seq:
        return False
    existing_payload = existing.to_payload()
    incoming_payload = incoming.to_payload()
    existing_payload.pop("checkpoint_seq", None)
    incoming_payload.pop("checkpoint_seq", None)
    return existing_payload != incoming_payload


def persist_recovery_manifest(
    agent_id: Any,
    session_context: Any,
    *,
    data_root: str | Path | None = None,
    delete_if_empty: bool = False,
    _receipt_sink: list[dict[str, Any]] | None = None,
) -> list[Path]:
    """Persist the current recovery manifest to the canonical runtime artifact path.

    This is intentionally shared by compaction and tool-lifecycle checkpoints:
    a process may die before compaction, so killed-process recovery cannot rely
    on the compaction path alone.
    """

    if agent_id is None or session_context is None:
        return []

    agent_component = _agent_path_component(agent_id)

    if data_root is None:
        from app.config import get_settings

        roots = [Path(get_settings().AGENT_DATA_DIR) / agent_component]
    else:
        roots = [Path(data_root) / agent_component]

    identity = _session_identity(session_context)
    session_metadata = getattr(session_context, "metadata", None)
    if not isinstance(session_metadata, dict):
        session_metadata = {}
    if identity["session_id"] is None:
        logger.warning("Recovery manifest checkpoint skipped because session identity is absent")
        return []
    manifest = build_recovery_manifest(session_context)
    manifest = merge_session_memory_into_manifest(manifest, agent_id=agent_id, data_root=data_root)
    normalized_agent_id = _identity_text(agent_id)
    if manifest.agent_id is not None and _entity_identity(manifest.agent_id) != _entity_identity(normalized_agent_id):
        logger.error("Recovery checkpoint rejected because agent identity does not match the writer authority")
        return []
    # ``agent_id`` is an explicit platform-owned writer argument, not model or
    # manifest input. Bind it before checking the composite authority key.
    manifest.agent_id = normalized_agent_id
    if _manifest_has_authority_bearing_state(manifest):
        missing_authority = _missing_manifest_authority_fields(manifest)
        if missing_authority:
            logger.error(
                "Recovery checkpoint rejected because authority identity is incomplete: %s",
                ", ".join(missing_authority),
            )
            return []
    written: list[Path] = []
    for root in roots:
        path = recovery_manifest_path(
            agent_id,
            session_id=identity["session_id"],
            runtime_task_id=identity["runtime_task_id"],
            data_root=root.parent,
        )
        with _legacy_manifest_lock(root):
            _retire_legacy_manifests(root, reason="canonical_writer")
            with _session_manifest_lock(path):
                existing_snapshot = _read_regular_file_snapshot(path)
                existing = (
                    _manifest_from_raw(existing_snapshot.raw, path=path) if existing_snapshot is not None else None
                )
                if _path_lexists(path) and (existing_snapshot is None or existing is None):
                    logger.error(
                        "Recovery checkpoint write rejected because canonical manifest is corrupt: %s",
                        path,
                    )
                    continue
                if (
                    existing is not None
                    and existing_snapshot is not None
                    and _manifest_has_authority_bearing_state(existing)
                    and (existing_missing := _missing_manifest_authority_fields(existing))
                ):
                    _record_incomplete_authority_conflict(
                        path=path,
                        snapshot=existing_snapshot,
                        existing=existing,
                        incoming=manifest,
                        missing_fields=existing_missing,
                    )
                    logger.error(
                        "Recovery checkpoint replacement rejected because stored authority is incomplete: %s",
                        ", ".join(existing_missing),
                    )
                    continue
                current_seq = _identity_int(session_metadata.get("recovery_checkpoint_seq")) or 0
                if (
                    existing is not None
                    and existing.claim_version is not None
                    and manifest.claim_version is not None
                    and existing.claim_version != manifest.claim_version
                ):
                    manifest.checkpoint_seq = 1
                else:
                    manifest.checkpoint_seq = current_seq + 1
                payload = manifest.to_payload()
                if existing is not None and _checkpoint_blocks_replacement(existing, manifest):
                    existing_payload = existing.to_payload()
                    incoming_payload = manifest.to_payload()
                    existing_payload.pop("checkpoint_seq", None)
                    incoming_payload.pop("checkpoint_seq", None)
                    if existing_payload == incoming_payload:
                        session_metadata["recovery_checkpoint_seq"] = existing.checkpoint_seq or 0
                        written.append(path)
                        if _receipt_sink is not None:
                            assert existing_snapshot is not None
                            _receipt_sink.append(_checkpoint_receipt(path, existing_snapshot.raw))
                        continue
                    logger.warning(
                        "Recovery checkpoint replacement rejected by identity/claim fence "
                        "for agent=%s session=%s runtime_task=%s incoming_claim=%s stored_claim=%s "
                        "incoming_worker=%s stored_worker=%s incoming_seq=%s stored_seq=%s",
                        agent_id,
                        manifest.session_id,
                        manifest.runtime_task_id,
                        manifest.claim_version,
                        existing.claim_version,
                        manifest.claim_worker_id,
                        existing.claim_worker_id,
                        manifest.checkpoint_seq,
                        existing.checkpoint_seq,
                    )
                    continue
                if manifest.is_empty():
                    if delete_if_empty:
                        # Keep an identity/claim tombstone so a stale worker
                        # cannot recreate an old pending frame after deletion.
                        raw = _atomic_write_manifest(path, payload)
                        written.append(path)
                        if _receipt_sink is not None:
                            _receipt_sink.append(_checkpoint_receipt(path, raw))
                        session_metadata["recovery_checkpoint_seq"] = manifest.checkpoint_seq
                    continue
                raw = _atomic_write_manifest(path, payload)
                written.append(path)
                if _receipt_sink is not None:
                    _receipt_sink.append(_checkpoint_receipt(path, raw))
                session_metadata["recovery_checkpoint_seq"] = manifest.checkpoint_seq
    return written


def _checkpoint_receipt(path: Path, raw: bytes) -> dict[str, Any]:
    try:
        runtime_index = path.parts.index("runtime_artifacts")
        ref = Path(*path.parts[runtime_index:]).as_posix()
    except ValueError:
        ref = path.name
    return {
        "path": str(path),
        "ref": ref,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "ephemeral": False,
    }


def inspect_recovery_manifest_checkpoint(
    *,
    agent_id: Any,
    tenant_id: Any,
    session_id: Any,
    runtime_task_id: Any,
    data_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return byte-bound CAS evidence for an operator recovery decision.

    This inspection never hydrates runtime authority.  It reads one canonical
    path under the same lock used by writers and reports corruption or identity
    mismatch explicitly so callers can remain fail closed.
    """

    expected = {
        "agent_id": _identity_text(agent_id),
        "tenant_id": _identity_text(tenant_id),
        "session_id": _identity_text(session_id),
        "runtime_task_id": _runtime_task_identity(runtime_task_id),
    }
    if not all(expected.values()):
        raise ValueError("Recovery manifest inspection identity is incomplete")
    path = recovery_manifest_path(
        agent_id,
        session_id=expected["session_id"],
        runtime_task_id=expected["runtime_task_id"],
        data_root=data_root,
    )
    with _session_manifest_lock(path):
        snapshot = _read_regular_file_snapshot(path)
        if snapshot is None:
            if _path_lexists(path):
                return {"state": "nonregular", "path": str(path)}
            return None
        receipt = _checkpoint_receipt(path, snapshot.raw)
        manifest = _manifest_from_raw(snapshot.raw, path=path)
        if manifest is None:
            return {"state": "corrupt", "receipt": receipt, "pending_tool_frames": []}
        authority_conflict = _block_incomplete_manifest_authority(
            manifest,
            session_context=None,
            expected_identity=expected,
            path=path,
            snapshot=snapshot,
        )
        if authority_conflict is not None:
            return {
                "state": "incomplete_authority",
                "receipt": receipt,
                "reason": _INCOMPLETE_AUTHORITY_REASON,
                "missing_fields": list(authority_conflict["missing_fields"]),
                "recovery_reconciliation_blocked": True,
                "pending_tool_frames": [],
            }
        actual = {
            "agent_id": _identity_text(manifest.agent_id),
            "tenant_id": _identity_text(manifest.tenant_id),
            "session_id": _identity_text(manifest.session_id),
            "runtime_task_id": _runtime_task_identity(manifest.runtime_task_id),
        }
        if actual != expected:
            return {
                "state": "identity_mismatch",
                "receipt": receipt,
                "actual_identity": actual,
                "pending_tool_frames": [],
            }
        return {
            "state": "valid",
            "receipt": receipt,
            "expected_checkpoint_seq": manifest.checkpoint_seq,
            "expected_claim_version": manifest.claim_version,
            "expected_claim_worker_id": manifest.claim_worker_id,
            "pending_tool_frames": [dict(frame) for frame in manifest.pending_tool_frames],
            "recent_tool_outcomes": [dict(outcome) for outcome in manifest.recent_tool_outcomes],
            "recent_writes": list(manifest.recent_writes),
            "current_turn_writes": list(manifest.current_turn_writes),
            "recovery_reconciliation_blocked": bool(manifest.recovery_reconciliation_blocked),
            "reconciliation_resolution": dict(manifest.reconciliation_resolution or {}),
        }


def reviewed_recovery_manifest_evidence(inspection: dict[str, Any] | None) -> dict[str, str | None]:
    """Normalize an inspection into the mandatory operator review CAS."""

    raw_state = str((inspection or {}).get("state") or "missing").strip()
    reviewed_state = "present" if raw_state == "valid" else raw_state
    receipt = (inspection or {}).get("receipt") if isinstance(inspection, dict) else None
    manifest_ref = _identity_text(receipt.get("ref")) if isinstance(receipt, dict) else None
    sha256 = _identity_text(receipt.get("sha256")) if isinstance(receipt, dict) else None
    return {
        "expected_manifest_state": reviewed_state,
        "expected_manifest_ref": manifest_ref,
        "expected_sha256": sha256,
    }


def persist_recovery_manifest_checkpoint(
    agent_id: Any,
    session_context: Any,
    *,
    data_root: str | Path | None = None,
    delete_if_empty: bool = False,
) -> list[dict[str, Any]]:
    """Persist and return byte-bound receipts captured under the file lock."""

    receipts: list[dict[str, Any]] = []
    persist_recovery_manifest(
        agent_id,
        session_context,
        data_root=data_root,
        delete_if_empty=delete_if_empty,
        _receipt_sink=receipts,
    )
    return receipts


class RecoveryManifestReconciliationError(RuntimeError):
    pass


class RecoveryManifestEvidenceDriftError(RecoveryManifestReconciliationError):
    """Reviewed manifest bytes or claim fields changed before resolution."""


_REVIEWABLE_MANIFEST_STATES = {
    "missing",
    "present",
    "incomplete_authority",
    "corrupt",
    "nonregular",
    "identity_mismatch",
}


def _reconciliation_journal_path(agent_root: Path, operation_id: str) -> Path:
    return agent_root / RECOVERY_MANIFESTS_REL_DIR / "reconciliation_journal" / f"{_identity_slug(operation_id)}.json"


def resolve_recovery_manifest_reconciliations(
    *,
    targets: list[dict[str, Any]],
    tenant_id: Any,
    action: str,
    reason: str,
    actor_user_id: Any,
    operation_id: Any,
    data_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Resolve multiple manifests after validating every target under one lock set.

    The filesystem cannot provide a cross-file transaction.  This routine
    therefore uses a deterministic lock order, validates all target bytes and
    CAS fields before the first mutation, then writes a durable prepared /
    completed operation journal.  A crash during the write phase is safely
    resumable with the same ``operation_id``.
    """

    normalized_tenant_id = _identity_text(tenant_id)
    normalized_operation_id = _identity_text(operation_id)
    normalized_action = str(action or "mark_resolved")
    normalized_reason = str(reason or "operator reconciliation")
    normalized_actor_user_id = _identity_text(actor_user_id)
    if not normalized_tenant_id or not normalized_operation_id or not normalized_actor_user_id or not targets:
        raise RecoveryManifestReconciliationError("Recovery reconciliation identity is incomplete")
    operator_claim_worker_id = f"operator-reconciliation:{normalized_actor_user_id}"
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise RecoveryManifestReconciliationError("Recovery reconciliation target is malformed")
        agent_id = _identity_text(target.get("agent_id"))
        session_id = _identity_text(target.get("session_id"))
        runtime_task_id = _runtime_task_identity(target.get("runtime_task_id"))
        if not agent_id or not session_id or not runtime_task_id:
            raise RecoveryManifestReconciliationError("Recovery reconciliation target identity is incomplete")
        key = (agent_id, session_id, runtime_task_id)
        if key in seen:
            continue
        seen.add(key)
        path = recovery_manifest_path(
            agent_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            data_root=data_root,
        )
        if "expected_manifest_state" not in target:
            raise RecoveryManifestReconciliationError("Recovery reconciliation target has no reviewed manifest state")
        expected_manifest_state = _identity_text(target.get("expected_manifest_state"))
        if expected_manifest_state is None:
            raise RecoveryManifestReconciliationError("Recovery reconciliation target has no reviewed manifest state")
        if expected_manifest_state not in _REVIEWABLE_MANIFEST_STATES:
            raise RecoveryManifestReconciliationError(
                f"Unsupported reviewed recovery manifest state: {expected_manifest_state}"
            )
        if "expected_manifest_ref" not in target or "expected_sha256" not in target:
            raise RecoveryManifestReconciliationError(
                "Recovery reconciliation target must explicitly bind reviewed manifest ref and sha256"
            )
        expected_manifest_ref = _identity_text(target.get("expected_manifest_ref"))
        expected_sha256 = _identity_text(target.get("expected_sha256"))
        if expected_manifest_state in {"present", "corrupt", "identity_mismatch", "incomplete_authority"} and (
            expected_manifest_ref is None
            or expected_sha256 is None
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise RecoveryManifestReconciliationError(
                f"Reviewed {expected_manifest_state} recovery manifest requires exact ref and sha256"
            )
        normalized.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "runtime_task_id": runtime_task_id,
                "path": path,
                "agent_root": path.parents[3],
                "expected_manifest_state": expected_manifest_state,
                "expected_manifest_ref": expected_manifest_ref,
                "expected_sha256": expected_sha256,
                "expected_checkpoint_seq": _identity_int(target.get("expected_checkpoint_seq")),
                "expected_claim_version": _identity_int(target.get("expected_claim_version")),
                "expected_claim_worker_id": _identity_text(target.get("expected_claim_worker_id")),
                "source": _identity_text(target.get("source")) or "runtime_task",
            }
        )

    prepared: list[dict[str, Any]] = []
    roots = sorted({item["agent_root"] for item in normalized}, key=str)
    paths = sorted({item["path"] for item in normalized}, key=str)
    with ExitStack() as stack:
        for root in roots:
            stack.enter_context(_legacy_manifest_lock(root))
        for path in paths:
            stack.enter_context(_session_manifest_lock(path))

        for target in normalized:
            path = target["path"]
            snapshot = _read_regular_file_snapshot(path)
            manifest = _manifest_from_raw(snapshot.raw, path=path) if snapshot is not None else None
            incomplete_authority_existing: RecoveryManifest | None = None
            incomplete_authority_missing: list[str] = []
            if (
                manifest is not None
                and snapshot is not None
                and _manifest_has_authority_bearing_state(manifest)
                and (missing_authority := _missing_manifest_authority_fields(manifest))
            ):
                incomplete_authority_existing = _manifest_from_payload(manifest.to_payload())
                incomplete_authority_missing = list(missing_authority)
            if manifest is not None and manifest.reconciliation_resolution:
                resolution_authority = manifest.reconciliation_resolution
                if resolution_authority.get("operation_id") != normalized_operation_id:
                    raise RecoveryManifestReconciliationError(
                        "Recovery manifest was already resolved by another operation"
                    )
                if (
                    str(resolution_authority.get("action") or "") != normalized_action
                    or str(resolution_authority.get("reason") or "") != normalized_reason
                    or _identity_text(resolution_authority.get("actor_user_id")) != normalized_actor_user_id
                ):
                    raise RecoveryManifestReconciliationError(
                        "Recovery reconciliation operation was resumed by different authority"
                    )
                prepared.append(
                    {
                        **target,
                        "snapshot": snapshot,
                        "manifest": manifest,
                        "source_state": "already_resolved",
                        "already_resolved": True,
                    }
                )
                continue
            expected_identity = {
                "session_id": target["session_id"],
                "agent_id": target["agent_id"],
                "tenant_id": normalized_tenant_id,
                "runtime_task_id": target["runtime_task_id"],
            }
            if snapshot is None:
                current_manifest_state = "nonregular" if _path_lexists(path) else "missing"
            elif manifest is None:
                current_manifest_state = "corrupt"
            elif incomplete_authority_missing:
                current_manifest_state = "incomplete_authority"
            else:
                actual_identity = {
                    "session_id": _identity_text(manifest.session_id),
                    "agent_id": _identity_text(manifest.agent_id),
                    "tenant_id": _identity_text(manifest.tenant_id),
                    "runtime_task_id": _runtime_task_identity(manifest.runtime_task_id),
                }
                current_manifest_state = "present" if actual_identity == expected_identity else "identity_mismatch"
            expected_manifest_state = target["expected_manifest_state"]
            if expected_manifest_state is not None and current_manifest_state != expected_manifest_state:
                raise RecoveryManifestEvidenceDriftError(
                    "Recovery manifest state changed since operator review "
                    f"(expected {expected_manifest_state}, found {current_manifest_state})"
                )
            current_receipt = _checkpoint_receipt(path, snapshot.raw) if snapshot is not None else None
            expected_manifest_ref = target["expected_manifest_ref"]
            current_manifest_ref = _identity_text(current_receipt.get("ref")) if current_receipt is not None else None
            if expected_manifest_ref is not None and current_manifest_ref != expected_manifest_ref:
                raise RecoveryManifestEvidenceDriftError(
                    "Recovery manifest changed since operator review (reference mismatch)"
                )
            current_sha = hashlib.sha256(snapshot.raw).hexdigest() if snapshot is not None else None
            expected_sha = target["expected_sha256"]
            if expected_sha is not None and current_sha != expected_sha:
                raise RecoveryManifestEvidenceDriftError(
                    "Recovery manifest changed since operator review (sha256 mismatch)"
                )

            if current_manifest_state == "nonregular":
                raise RecoveryManifestReconciliationError(f"Recovery manifest exists but is not a regular file: {path}")
            source_state = current_manifest_state
            if source_state == "corrupt" and any(
                target[key] is not None
                for key in (
                    "expected_checkpoint_seq",
                    "expected_claim_version",
                    "expected_claim_worker_id",
                )
            ):
                raise RecoveryManifestEvidenceDriftError(
                    "Corrupt recovery manifest cannot satisfy the reviewed claim/sequence CAS"
                )
            if manifest is None:
                manifest = RecoveryManifest(
                    session_id=target["session_id"],
                    agent_id=target["agent_id"],
                    tenant_id=normalized_tenant_id,
                    runtime_task_id=target["runtime_task_id"],
                    claim_version=(
                        target["expected_claim_version"] if target["expected_claim_version"] is not None else 0
                    ),
                    claim_worker_id=target["expected_claim_worker_id"] or operator_claim_worker_id,
                    checkpoint_seq=target["expected_checkpoint_seq"],
                )
            else:
                for field_name, expected_value in expected_identity.items():
                    current_value = getattr(manifest, field_name)
                    if current_value is not None and current_value != expected_value:
                        raise RecoveryManifestReconciliationError(
                            f"Recovery manifest {field_name} does not match the RuntimeTask authority"
                        )
                    setattr(manifest, field_name, expected_value)
                expected_seq = target["expected_checkpoint_seq"]
                if expected_seq is not None and manifest.checkpoint_seq != expected_seq:
                    raise RecoveryManifestEvidenceDriftError(
                        "Recovery manifest changed since operator review (checkpoint sequence mismatch)"
                    )
                expected_claim = target["expected_claim_version"]
                if expected_claim is not None and manifest.claim_version not in (None, expected_claim):
                    raise RecoveryManifestEvidenceDriftError(
                        "Recovery manifest changed since operator review (claim mismatch)"
                    )
                expected_worker = target["expected_claim_worker_id"]
                if expected_worker is not None and manifest.claim_worker_id not in (None, expected_worker):
                    raise RecoveryManifestEvidenceDriftError(
                        "Recovery manifest changed since operator review (claim worker mismatch)"
                    )
                if manifest.claim_version is None:
                    manifest.claim_version = expected_claim if expected_claim is not None else 0
                if manifest.claim_worker_id is None:
                    manifest.claim_worker_id = expected_worker or operator_claim_worker_id

            manifest.pending_tool_frames = []
            manifest.legacy_conflict = {}
            manifest.recovery_reconciliation_blocked = False
            manifest.prior_run_recovery_reconciliations = []
            manifest.checkpoint_seq = (
                max(
                    manifest.checkpoint_seq or 0,
                    target["expected_checkpoint_seq"] or 0,
                )
                + 1
            )
            resolution = {
                "schema": "hive.recovery_reconciliation_resolution.v1",
                "operation_id": normalized_operation_id,
                "action": normalized_action,
                "reason": normalized_reason,
                "actor_user_id": normalized_actor_user_id,
                "runtime_task_id": target["runtime_task_id"],
                "recorded_at_epoch": time.time(),
            }
            manifest.reconciliation_resolution = resolution
            manifest.continuation_records = [
                *manifest.continuation_records,
                {"source": "runtime_reconciliation", **resolution},
            ][-50:]
            prepared.append(
                {
                    **target,
                    "snapshot": snapshot,
                    "manifest": manifest,
                    "source_state": source_state,
                    "already_resolved": False,
                    "incomplete_authority_existing": incomplete_authority_existing,
                    "incomplete_authority_missing": incomplete_authority_missing,
                }
            )

        journal_targets = [
            {
                "agent_id": item["agent_id"],
                "session_id": item["session_id"],
                "runtime_task_id": item["runtime_task_id"],
                "source_state": item["source_state"],
                "expected_manifest_state": item["expected_manifest_state"],
                "expected_manifest_ref": item["expected_manifest_ref"],
                "expected_sha256": item["expected_sha256"],
                "expected_checkpoint_seq": item["expected_checkpoint_seq"],
                "expected_claim_version": item["expected_claim_version"],
                "expected_claim_worker_id": item["expected_claim_worker_id"],
            }
            for item in prepared
        ]
        journal_payload = {
            "schema": "hive.recovery_reconciliation_operation.v1",
            "operation_id": normalized_operation_id,
            "tenant_id": normalized_tenant_id,
            "action": normalized_action,
            "reason": normalized_reason,
            "actor_user_id": normalized_actor_user_id,
            "status": "prepared",
            "targets": journal_targets,
        }
        for root in roots:
            _atomic_write_manifest(
                _reconciliation_journal_path(root, normalized_operation_id),
                journal_payload,
            )

        receipts: list[dict[str, Any]] = []
        quarantine_guards: list[_VerifiedPrivateFile] = []
        try:
            for item in prepared:
                path = item["path"]
                snapshot = item["snapshot"]
                quarantine_receipt = None
                if item["source_state"] == "incomplete_authority" and snapshot is not None:
                    existing_authority = item.get("incomplete_authority_existing")
                    missing_authority = item.get("incomplete_authority_missing")
                    if not isinstance(existing_authority, RecoveryManifest) or not isinstance(missing_authority, list):
                        raise RecoveryManifestReconciliationError(
                            "Incomplete recovery authority evidence was not preserved during preflight"
                        )
                    _record_incomplete_authority_conflict(
                        path=path,
                        snapshot=snapshot,
                        existing=existing_authority,
                        incoming=item["manifest"],
                        missing_fields=missing_authority,
                    )
                    digest = hashlib.sha256(snapshot.raw).hexdigest()
                    quarantine = (
                        item["agent_root"]
                        / RECOVERY_MANIFESTS_REL_DIR
                        / "authority_quarantine"
                        / f"incomplete-{digest}.json"
                    )
                    quarantine_receipt = _checkpoint_receipt(quarantine, snapshot.raw)
                elif item["source_state"] == "corrupt" and snapshot is not None:
                    digest = hashlib.sha256(snapshot.raw).hexdigest()
                    quarantine = (
                        item["agent_root"]
                        / RECOVERY_MANIFESTS_REL_DIR
                        / "reconciliation_quarantine"
                        / f"corrupt-{digest}.json"
                    )
                    verified_quarantine = _write_verified_private_bytes(quarantine, snapshot.raw)
                    quarantine_guards.append(verified_quarantine)
                    _verify_private_file_reference(verified_quarantine)
                    quarantine_receipt = _checkpoint_receipt(quarantine, verified_quarantine.raw)
                if item["already_resolved"]:
                    assert snapshot is not None
                    raw = snapshot.raw
                else:
                    raw = _atomic_write_manifest(path, item["manifest"].to_payload())
                receipt = {
                    **_checkpoint_receipt(path, raw),
                    "agent_id": item["agent_id"],
                    "session_id": item["session_id"],
                    "runtime_task_id": item["runtime_task_id"],
                    "source": item["source"],
                    "source_state": item["source_state"],
                    "operation_id": normalized_operation_id,
                }
                if quarantine_receipt is not None:
                    receipt["quarantine_receipt"] = quarantine_receipt
                receipts.append(receipt)

            for verified in quarantine_guards:
                _verify_private_file_reference(verified)
            completed_journal = {**journal_payload, "status": "completed", "receipts": receipts}
            for root in roots:
                _atomic_write_manifest(
                    _reconciliation_journal_path(root, normalized_operation_id),
                    completed_journal,
                )
            for verified in quarantine_guards:
                _verify_private_file_reference(verified)
            return receipts
        except Exception:
            failed_journal = {
                **journal_payload,
                "status": "failed_quarantine_verification",
                "receipts": [],
            }
            for root in roots:
                _atomic_write_manifest(
                    _reconciliation_journal_path(root, normalized_operation_id),
                    failed_journal,
                )
            raise
        finally:
            for verified in quarantine_guards:
                verified.close()


def resolve_recovery_manifest_reconciliation(
    *,
    agent_id: Any,
    tenant_id: Any,
    session_id: Any,
    runtime_task_id: Any,
    action: str,
    reason: str,
    actor_user_id: Any,
    expected_manifest_state: str | None = None,
    expected_manifest_ref: str | None = None,
    expected_sha256: str | None = None,
    expected_checkpoint_seq: int | None = None,
    expected_claim_version: int | None = None,
    expected_claim_worker_id: str | None = None,
    operation_id: Any | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for resolving one durable recovery target."""

    receipts = resolve_recovery_manifest_reconciliations(
        targets=[
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "runtime_task_id": runtime_task_id,
                "expected_manifest_state": expected_manifest_state,
                "expected_manifest_ref": expected_manifest_ref,
                "expected_sha256": expected_sha256,
                "expected_checkpoint_seq": expected_checkpoint_seq,
                "expected_claim_version": expected_claim_version,
                "expected_claim_worker_id": expected_claim_worker_id,
            }
        ],
        tenant_id=tenant_id,
        action=action,
        reason=reason,
        actor_user_id=actor_user_id,
        operation_id=operation_id or uuid.uuid4().hex,
        data_root=data_root,
    )
    return receipts[0]
