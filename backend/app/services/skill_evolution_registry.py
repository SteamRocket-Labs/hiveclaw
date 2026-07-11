"""Skill self-evolution provenance registry.

This registry is the control-plane sidecar for agent-owned Skill evolution.
It does not author Skill semantics. It records where an active Skill came from,
whether it is allowed to enter the self-evolution patch chain, and which
candidate/version currently backs it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.agent_asset_transaction import AgentAssetTransaction

SCHEMA = "skill_registry.v1"

ORIGIN_SYSTEM_BUILTIN = "system_builtin"
ORIGIN_USER_SKILL_CREATOR = "user_skill_creator"
ORIGIN_T3_AUTO_CREATED = "t3_auto_created"
ORIGIN_RUNTIME_EVIDENCE = "runtime_evidence"
ORIGIN_MANUAL_IMPORT = "manual_import"
ORIGIN_UNKNOWN = "unknown"

EVOLVABLE_ORIGINS = frozenset({ORIGIN_USER_SKILL_CREATOR, ORIGIN_T3_AUTO_CREATED})
KNOWN_ORIGINS = frozenset(
    {
        ORIGIN_SYSTEM_BUILTIN,
        ORIGIN_USER_SKILL_CREATOR,
        ORIGIN_T3_AUTO_CREATED,
        ORIGIN_RUNTIME_EVIDENCE,
        ORIGIN_MANUAL_IMPORT,
        ORIGIN_UNKNOWN,
    }
)

AUTHORING_CONTRACT = {
    "final_skill_body": "agent_llm_authored",
    "platform_role": "scaffold_validate_govern_commit_exact",
    "candidate_signal_role": "evidence_only_not_active_skill_patch",
}

_REGISTRY_REL_PATH = Path("evolution") / "skill_registry.json"

STATE_ACTIVE = "active"
STATE_PROVISIONAL = "provisional"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
STATE_ROLLED_BACK = "rolled_back"
STATE_NEEDS_REVIEW = "needs_review"
STATE_BLOCKED = "blocked"

LOADABLE_STATES = frozenset({STATE_ACTIVE, STATE_PROVISIONAL, STATE_STALE})
BLOCKED_RUNTIME_STATES = frozenset({STATE_ARCHIVED, STATE_ROLLED_BACK, STATE_NEEDS_REVIEW, STATE_BLOCKED})


def registry_rel_path() -> str:
    return _REGISTRY_REL_PATH.as_posix()


def normalize_skill_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-") or "unknown-skill"


def normalize_skill_origin(value: str | None) -> str:
    normalized = str(value or ORIGIN_UNKNOWN).strip().lower().replace("-", "_")
    return normalized if normalized in KNOWN_ORIGINS else ORIGIN_UNKNOWN


def default_evolvable_for_origin(skill_origin: str) -> bool:
    return normalize_skill_origin(skill_origin) in EVOLVABLE_ORIGINS


def default_origin_for_candidate(*, package_type: str, draft_filename: str) -> str:
    normalized_type = str(package_type or "").strip().lower()
    if normalized_type == "save_skill":
        return ORIGIN_USER_SKILL_CREATOR
    if normalized_type in {"t3_auto_created", "memory_skill_candidate", "t3_skill_candidate"}:
        return ORIGIN_T3_AUTO_CREATED
    if draft_filename == "candidate_signal.md":
        return ORIGIN_RUNTIME_EVIDENCE
    return ORIGIN_T3_AUTO_CREATED


def load_skill_evolution_registry(
    workspace: Path,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    path = workspace / _REGISTRY_REL_PATH
    rendered = (
        transaction.read_text(_REGISTRY_REL_PATH.as_posix())
        if transaction is not None
        else path.read_text(encoding="utf-8")
        if path.exists()
        else None
    )
    if rendered is None:
        return {"schema": SCHEMA, "skills": {}}
    try:
        data = json.loads(rendered)
    except (OSError, json.JSONDecodeError):
        return {"schema": SCHEMA, "skills": {}}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "skills": {}}
    skills = data.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    normalized_skills: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in skills.items():
        if not isinstance(raw_entry, dict):
            continue
        key = normalize_skill_name(str(raw_entry.get("skill_name") or raw_key))
        origin = normalize_skill_origin(str(raw_entry.get("skill_origin") or ORIGIN_UNKNOWN))
        entry = dict(raw_entry)
        entry["skill_name"] = str(raw_entry.get("skill_name") or key)
        entry["skill_origin"] = origin
        entry["evolvable"] = bool(raw_entry.get("evolvable")) and default_evolvable_for_origin(origin)
        normalized_skills[key] = entry
    return {"schema": SCHEMA, "skills": normalized_skills}


def save_skill_evolution_registry(
    workspace: Path,
    registry: dict[str, Any],
    *,
    transaction: AgentAssetTransaction | None = None,
) -> None:
    if transaction is None:
        with AgentAssetTransaction(workspace, operation="skill_registry_save") as own_transaction:
            save_skill_evolution_registry(workspace, registry, transaction=own_transaction)
            own_transaction.commit()
        return
    path = workspace / _REGISTRY_REL_PATH
    payload = {
        "schema": SCHEMA,
        "skills": registry.get("skills") if isinstance(registry.get("skills"), dict) else {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    transaction.stage_text(
        path.relative_to(workspace).as_posix(),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def upsert_skill_evolution_entry(
    workspace: Path,
    *,
    skill_name: str,
    target_path: str,
    skill_origin: str,
    evolvable: bool | None = None,
    active_version_hash: str | None = None,
    last_candidate_id: str | None = None,
    state: str = "active",
    source_refs: list[str] | tuple[str, ...] | None = None,
    metadata: dict[str, Any] | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    if transaction is None:
        with AgentAssetTransaction(
            workspace,
            operation="skill_registry_upsert",
            evidence_refs=tuple(str(ref) for ref in (source_refs or ())),
        ) as own_transaction:
            entry = upsert_skill_evolution_entry(
                workspace,
                skill_name=skill_name,
                target_path=target_path,
                skill_origin=skill_origin,
                evolvable=evolvable,
                active_version_hash=active_version_hash,
                last_candidate_id=last_candidate_id,
                state=state,
                source_refs=source_refs,
                metadata=metadata,
                transaction=own_transaction,
            )
            own_transaction.commit()
            return entry

    registry = load_skill_evolution_registry(workspace, transaction=transaction)
    skills = registry.setdefault("skills", {})
    key = normalize_skill_name(skill_name)
    origin = normalize_skill_origin(skill_origin)
    allowed = default_evolvable_for_origin(origin)
    entry_evolvable = allowed if evolvable is None else bool(evolvable) and allowed
    staged_target = transaction.read_bytes(target_path)
    version_hash = active_version_hash or (
        "sha256:" + hashlib.sha256(staged_target).hexdigest()
        if staged_target is not None
        else file_sha256(workspace / target_path)
    )

    existing = skills.get(key) if isinstance(skills.get(key), dict) else {}
    entry: dict[str, Any] = {
        **existing,
        "skill_name": skill_name,
        "target_path": target_path,
        "skill_origin": origin,
        "evolvable": entry_evolvable,
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if version_hash:
        entry["active_version_hash"] = version_hash
    if last_candidate_id:
        entry["last_candidate_id"] = last_candidate_id
    if source_refs is not None:
        entry["source_refs"] = list(source_refs)
    if metadata:
        entry.setdefault("metadata", {}).update(metadata)
    entry.setdefault("authoring_contract", AUTHORING_CONTRACT)

    skills[key] = entry
    save_skill_evolution_registry(workspace, registry, transaction=transaction)
    return entry


def get_skill_evolution_entry(
    workspace: Path,
    skill_name_or_path: str,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any] | None:
    registry = load_skill_evolution_registry(workspace, transaction=transaction)
    skills = registry.get("skills") if isinstance(registry.get("skills"), dict) else {}
    key = normalize_skill_name(skill_name_or_path)
    if key in skills and isinstance(skills[key], dict):
        return skills[key]
    normalized_path = str(skill_name_or_path or "").strip().lower()
    for entry in skills.values():
        if not isinstance(entry, dict):
            continue
        if normalize_skill_name(str(entry.get("skill_name") or "")) == key:
            return entry
        if str(entry.get("target_path") or "").strip().lower() == normalized_path:
            return entry
    return None


def restore_skill_evolution_entry(
    workspace: Path,
    *,
    skill_name: str,
    entry: dict[str, Any] | None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any] | None:
    """Restore one registry entry as part of the same native-asset revision.

    Trial rollback uses the exact pre-trial registry snapshot instead of
    reconstructing it from the provisional entry. This keeps provenance,
    version, and evolvability coupled to the content that is restored.
    """

    if transaction is None:
        with AgentAssetTransaction(workspace, operation="skill_registry_restore") as own_transaction:
            restored = restore_skill_evolution_entry(
                workspace,
                skill_name=skill_name,
                entry=entry,
                transaction=own_transaction,
            )
            own_transaction.commit()
            return restored

    registry = load_skill_evolution_registry(workspace, transaction=transaction)
    skills = registry.setdefault("skills", {})
    key = normalize_skill_name(skill_name)
    if entry is None:
        skills.pop(key, None)
        restored = None
    else:
        restored = dict(entry)
        restored["skill_name"] = str(restored.get("skill_name") or skill_name)
        skills[key] = restored
    save_skill_evolution_registry(workspace, registry, transaction=transaction)
    return restored


def skill_runtime_state(
    workspace: Path,
    skill_name_or_path: str,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> str:
    """Return the authoritative runtime state; unregistered legacy skills stay active."""

    entry = get_skill_evolution_entry(workspace, skill_name_or_path, transaction=transaction)
    return str(entry.get("state") or STATE_ACTIVE).strip().lower() if entry is not None else STATE_ACTIVE


def is_skill_runtime_loadable(
    workspace: Path,
    skill_name_or_path: str,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> bool:
    return skill_runtime_state(workspace, skill_name_or_path, transaction=transaction) in LOADABLE_STATES


def can_self_evolve_skill(workspace: Path, skill_name_or_path: str) -> bool:
    """Return whether an existing active Skill can enter patch/tuning.

    Missing registry entries remain evolvable for backwards compatibility with
    older agent-authored skills. Explicit registry metadata always wins, and a
    system builtin is never evolvable even if a caller attempts to opt it in.
    """

    entry = get_skill_evolution_entry(workspace, skill_name_or_path)
    if entry is None:
        return True
    if str(entry.get("state") or STATE_ACTIVE).strip().lower() not in LOADABLE_STATES:
        return False
    origin = normalize_skill_origin(str(entry.get("skill_origin") or ORIGIN_UNKNOWN))
    return bool(entry.get("evolvable")) and default_evolvable_for_origin(origin)
