"""Version-bound positive and negative trials for provisional Skills.

The trial ledger is the durable decision source for provisional Skill usage.
It is intentionally separate from generic lifecycle candidate aggregation:
only invocations that actually loaded the provisional Skill can advance it.
Every transition mutates content, registry, candidate manifest, and evidence in
one ``AgentAssetTransaction``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.evolution_ledger import record_promotion_decision, record_rollback_event
from app.services.skill_candidate_package import update_skill_candidate_package_status
from app.services.skill_evolution_registry import (
    STATE_ACTIVE,
    STATE_NEEDS_REVIEW,
    STATE_PROVISIONAL,
    STATE_ROLLED_BACK,
    get_skill_evolution_entry,
    restore_skill_evolution_entry,
    upsert_skill_evolution_entry,
)

SCHEMA = "skill_provisional_trial.v1"
DEFAULT_POSITIVE_SIGNAL_THRESHOLD = 3
DEFAULT_NEGATIVE_SIGNAL_THRESHOLD = 2
DEFAULT_TRIAL_WINDOW_DAYS = 14
_TERMINAL_STATES = frozenset({"promoted", STATE_ROLLED_BACK, STATE_NEEDS_REVIEW})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _safe_candidate_id(candidate_id: str) -> str:
    value = str(candidate_id or "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,199}", value):
        raise ValueError("provisional trial candidate_id must be a safe path component")
    return value


def trial_rel_path(candidate_id: str) -> str:
    return f"evolution/skill_trials/{_safe_candidate_id(candidate_id)}/trial.json"


def _rollback_rel_path(candidate_id: str) -> str:
    return f"evolution/skill_trials/{_safe_candidate_id(candidate_id)}/rollback/SKILL.md"


def load_provisional_trial(
    workspace: Path,
    candidate_id: str,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any] | None:
    relative_path = trial_rel_path(candidate_id)
    rendered = (
        transaction.read_text(relative_path)
        if transaction is not None
        else (workspace / relative_path).read_text(encoding="utf-8")
        if (workspace / relative_path).is_file()
        else None
    )
    if rendered is None:
        return None
    try:
        trial = json.loads(rendered)
    except json.JSONDecodeError:
        return None
    return trial if isinstance(trial, dict) and trial.get("schema") == SCHEMA else None


def _stage_trial(
    transaction: AgentAssetTransaction,
    trial: dict[str, Any],
) -> None:
    transaction.stage_text(
        trial_rel_path(str(trial["candidate_id"])),
        json.dumps(trial, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _public_progress(trial: dict[str, Any], *, decision: str) -> dict[str, Any]:
    signals = trial.get("signals") if isinstance(trial.get("signals"), dict) else {}
    thresholds = trial.get("thresholds") if isinstance(trial.get("thresholds"), dict) else {}
    return {
        "trial_decision": decision,
        "candidate_id": trial.get("candidate_id"),
        "trial_state": trial.get("state"),
        "positive_signal_count": len(signals.get("positive") or []),
        "positive_signal_threshold": int(thresholds.get("positive") or DEFAULT_POSITIVE_SIGNAL_THRESHOLD),
        "negative_signal_count": len(signals.get("negative") or []),
        "negative_signal_threshold": int(thresholds.get("negative") or DEFAULT_NEGATIVE_SIGNAL_THRESHOLD),
    }


def initialize_provisional_trial(
    workspace: Path,
    *,
    candidate_id: str,
    skill_name: str,
    target_path: str,
    candidate_content: bytes,
    baseline_content: bytes | None,
    baseline_registry_entry: dict[str, Any] | None,
    positive_signal_threshold: int = DEFAULT_POSITIVE_SIGNAL_THRESHOLD,
    negative_signal_threshold: int = DEFAULT_NEGATIVE_SIGNAL_THRESHOLD,
    window_days: int = DEFAULT_TRIAL_WINDOW_DAYS,
    started_at: str | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    """Create the immutable rollback anchor and mutable trial ledger atomically."""

    if transaction is None:
        with AgentAssetTransaction(
            workspace,
            operation="skill_provisional_trial_initialize",
            evidence_refs=(f"skill-candidate:{candidate_id}",),
        ) as own_transaction:
            trial = initialize_provisional_trial(
                workspace,
                candidate_id=candidate_id,
                skill_name=skill_name,
                target_path=target_path,
                candidate_content=candidate_content,
                baseline_content=baseline_content,
                baseline_registry_entry=baseline_registry_entry,
                positive_signal_threshold=positive_signal_threshold,
                negative_signal_threshold=negative_signal_threshold,
                window_days=window_days,
                started_at=started_at,
                transaction=own_transaction,
            )
            own_transaction.commit()
            return trial

    safe_id = _safe_candidate_id(candidate_id)
    stamp = started_at or _now_iso()
    candidate_hash = _sha256(candidate_content)
    current_entry = get_skill_evolution_entry(workspace, skill_name, transaction=transaction) or {}
    rollback: dict[str, Any]
    baseline_snapshot = dict(baseline_registry_entry) if isinstance(baseline_registry_entry, dict) else None
    if baseline_content is None:
        rollback = {"action": "delete", "ref": target_path, "baseline_version_hash": None}
    else:
        rollback_ref = _rollback_rel_path(safe_id)
        baseline_hash = _sha256(baseline_content)
        transaction.stage_bytes(rollback_ref, baseline_content)
        if baseline_snapshot is None:
            baseline_snapshot = {
                **current_entry,
                "skill_name": str(current_entry.get("skill_name") or skill_name),
                "target_path": target_path,
                "state": STATE_ACTIVE,
                "active_version_hash": baseline_hash,
                "last_candidate_id": current_entry.get("last_candidate_id"),
                "metadata": {
                    **(current_entry.get("metadata") if isinstance(current_entry.get("metadata"), dict) else {}),
                    "commit_status": STATE_ACTIVE,
                },
            }
            baseline_snapshot["metadata"].pop("trial_path", None)
            baseline_snapshot["metadata"].pop("trial_version_hash", None)
        rollback = {
            "action": "restore",
            "ref": rollback_ref,
            "baseline_version_hash": baseline_hash,
            "baseline_registry_entry": baseline_snapshot,
        }

    trial = {
        "schema": SCHEMA,
        "candidate_id": safe_id,
        "skill_name": skill_name,
        "target_path": target_path,
        "candidate_version_hash": candidate_hash,
        "state": STATE_PROVISIONAL,
        "started_at": stamp,
        "updated_at": stamp,
        "window_days": max(1, int(window_days)),
        "thresholds": {
            "positive": max(1, int(positive_signal_threshold)),
            "negative": max(1, int(negative_signal_threshold)),
        },
        "signals": {"positive": [], "negative": []},
        "rollback": rollback,
        "source_refs": [f"skill-candidate:{safe_id}", target_path],
    }
    _stage_trial(transaction, trial)
    upsert_skill_evolution_entry(
        workspace,
        skill_name=skill_name,
        target_path=target_path,
        skill_origin=str(current_entry.get("skill_origin") or "unknown"),
        evolvable=bool(current_entry.get("evolvable", True)),
        active_version_hash=candidate_hash,
        last_candidate_id=safe_id,
        state=STATE_PROVISIONAL,
        metadata={
            "commit_status": STATE_PROVISIONAL,
            "trial_path": trial_rel_path(safe_id),
            "trial_version_hash": candidate_hash,
        },
        transaction=transaction,
    )
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=safe_id,
        status=STATE_PROVISIONAL,
        reason="Skill entered a version-bound provisional trial.",
        extra_metadata={
            "trial_state": STATE_PROVISIONAL,
            "positive_signal_count": 0,
            "positive_signal_threshold": trial["thresholds"]["positive"],
            "negative_signal_count": 0,
            "negative_signal_threshold": trial["thresholds"]["negative"],
        },
        transaction=transaction,
    )
    return trial


def _initialize_legacy_trial(
    workspace: Path,
    *,
    entry: dict[str, Any],
    candidate_id: str,
    occurred_at: str,
    transaction: AgentAssetTransaction,
) -> dict[str, Any]:
    target_path = str(entry.get("target_path") or "").strip()
    content = transaction.read_bytes(target_path) if target_path else None
    if content is None:
        content = b""
    trial = initialize_provisional_trial(
        workspace,
        candidate_id=candidate_id,
        skill_name=str(entry.get("skill_name") or candidate_id),
        target_path=target_path,
        candidate_content=content,
        baseline_content=None,
        baseline_registry_entry=None,
        started_at=occurred_at,
        transaction=transaction,
    )
    trial["rollback"] = {
        "action": "manual_review",
        "ref": target_path,
        "baseline_version_hash": None,
        "reason": "legacy provisional state has no mechanically verified baseline backup",
    }
    trial["legacy_imported"] = True
    _stage_trial(transaction, trial)
    return trial


def _signal_id(candidate_id: str, kind: str, signal: dict[str, Any], occurred_at: str) -> str:
    identity = {
        "candidate_id": candidate_id,
        "kind": kind,
        "session_id": signal.get("session_id"),
        "runtime_task_id": signal.get("runtime_task_id"),
        "trace_id": signal.get("trace_id"),
        "status": signal.get("status"),
        "occurred_at": occurred_at,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _mark_needs_review(
    workspace: Path,
    *,
    trial: dict[str, Any],
    entry: dict[str, Any],
    reason: str,
    transaction: AgentAssetTransaction,
) -> dict[str, Any]:
    stamp = _now_iso()
    trial["state"] = STATE_NEEDS_REVIEW
    trial["updated_at"] = stamp
    trial["decision_reason"] = reason
    _stage_trial(transaction, trial)
    upsert_skill_evolution_entry(
        workspace,
        skill_name=str(entry.get("skill_name") or trial.get("skill_name") or "unknown"),
        target_path=str(entry.get("target_path") or trial.get("target_path") or ""),
        skill_origin=str(entry.get("skill_origin") or "unknown"),
        evolvable=bool(entry.get("evolvable", True)),
        active_version_hash=str(entry.get("active_version_hash") or trial.get("candidate_version_hash") or "") or None,
        last_candidate_id=str(trial["candidate_id"]),
        state=STATE_NEEDS_REVIEW,
        metadata={"commit_status": STATE_NEEDS_REVIEW, "trial_state": STATE_NEEDS_REVIEW},
        transaction=transaction,
    )
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=str(trial["candidate_id"]),
        status=STATE_NEEDS_REVIEW,
        reason=reason,
        extra_metadata={"trial_state": STATE_NEEDS_REVIEW},
        transaction=transaction,
    )
    from app.services.skill_lifecycle import record_skill_lifecycle_event

    record_skill_lifecycle_event(
        workspace,
        skill_name=str(trial.get("skill_name") or "unknown"),
        status=STATE_NEEDS_REVIEW,
        note=reason,
        transaction=transaction,
    )
    return _public_progress(trial, decision=STATE_NEEDS_REVIEW)


def record_provisional_trial_signal(
    workspace: Path,
    *,
    skill_name: str,
    kind: str,
    signal: dict[str, Any],
    occurred_at: str | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    """Record one causal trial signal and perform any terminal transition."""

    if transaction is None:
        evidence_refs = tuple(
            f"{key}:{signal[key]}" for key in ("session_id", "runtime_task_id", "trace_id") if signal.get(key)
        )
        with AgentAssetTransaction(
            workspace,
            operation="skill_provisional_trial_signal",
            evidence_refs=evidence_refs,
        ) as own_transaction:
            result = record_provisional_trial_signal(
                workspace,
                skill_name=skill_name,
                kind=kind,
                signal=signal,
                occurred_at=occurred_at,
                transaction=own_transaction,
            )
            if own_transaction.has_changes:
                own_transaction.commit()
            return result

    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in {"positive", "negative"}:
        raise ValueError("provisional trial signal kind must be positive or negative")
    stamp = occurred_at or _now_iso()
    entry = get_skill_evolution_entry(workspace, skill_name, transaction=transaction)
    if not isinstance(entry, dict) or str(entry.get("state") or "").strip().lower() != STATE_PROVISIONAL:
        return {"trial_decision": "ignored_not_provisional", "trial_state": (entry or {}).get("state")}
    candidate_id = str(entry.get("last_candidate_id") or "").strip()
    if not candidate_id:
        return {"trial_decision": "ignored_missing_candidate", "trial_state": STATE_PROVISIONAL}

    trial = load_provisional_trial(workspace, candidate_id, transaction=transaction)
    if trial is None:
        trial = _initialize_legacy_trial(
            workspace,
            entry=entry,
            candidate_id=candidate_id,
            occurred_at=stamp,
            transaction=transaction,
        )
    if str(trial.get("state") or "") in _TERMINAL_STATES:
        return _public_progress(trial, decision="ignored_terminal_trial")

    target_path = str(trial.get("target_path") or entry.get("target_path") or "")
    current_content = transaction.read_bytes(target_path) if target_path else None
    current_hash = _sha256(current_content) if current_content is not None else None
    if current_hash != trial.get("candidate_version_hash"):
        return _mark_needs_review(
            workspace,
            trial=trial,
            entry=entry,
            reason="Provisional Skill content drifted from the version bound to this trial.",
            transaction=transaction,
        )

    stamp_dt = _parse_iso(stamp) or datetime.now(timezone.utc)
    started_dt = _parse_iso(str(trial.get("started_at") or "")) or stamp_dt
    if stamp_dt > started_dt + timedelta(days=max(1, int(trial.get("window_days") or DEFAULT_TRIAL_WINDOW_DAYS))):
        return _mark_needs_review(
            workspace,
            trial=trial,
            entry=entry,
            reason="Provisional Skill trial window expired before a terminal threshold was reached.",
            transaction=transaction,
        )

    signals = trial.setdefault("signals", {"positive": [], "negative": []})
    bucket = signals.setdefault(normalized_kind, [])
    signal_id = _signal_id(candidate_id, normalized_kind, signal, stamp)
    if any(str(item.get("id") or "") == signal_id for item in bucket if isinstance(item, dict)):
        return _public_progress(trial, decision="duplicate_signal")
    bucket.append(
        {
            "id": signal_id,
            "kind": normalized_kind,
            "occurred_at": stamp,
            "source": signal.get("source"),
            "status": signal.get("status"),
            "reason": str(signal.get("reason") or "").strip(),
            "session_id": signal.get("session_id"),
            "runtime_task_id": signal.get("runtime_task_id"),
            "trace_id": signal.get("trace_id"),
        }
    )
    trial["updated_at"] = stamp
    thresholds = trial.get("thresholds") if isinstance(trial.get("thresholds"), dict) else {}
    positive_threshold = max(1, int(thresholds.get("positive") or DEFAULT_POSITIVE_SIGNAL_THRESHOLD))
    negative_threshold = max(1, int(thresholds.get("negative") or DEFAULT_NEGATIVE_SIGNAL_THRESHOLD))

    if normalized_kind == "positive" and len(signals.get("positive") or []) >= positive_threshold:
        trial["state"] = "promoted"
        trial["promoted_at"] = stamp
        trial["decision_reason"] = "Positive runtime evidence threshold reached."
        _stage_trial(transaction, trial)
        upsert_skill_evolution_entry(
            workspace,
            skill_name=str(entry.get("skill_name") or skill_name),
            target_path=target_path,
            skill_origin=str(entry.get("skill_origin") or "unknown"),
            evolvable=bool(entry.get("evolvable", True)),
            active_version_hash=str(trial.get("candidate_version_hash") or "") or None,
            last_candidate_id=candidate_id,
            state=STATE_ACTIVE,
            metadata={"commit_status": STATE_ACTIVE, "trial_state": "promoted", "promoted_at": stamp},
            transaction=transaction,
        )
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate_id,
            status="promoted",
            reason=trial["decision_reason"],
            extra_metadata={
                "trial_state": "promoted",
                "positive_signal_count": len(signals.get("positive") or []),
                "negative_signal_count": len(signals.get("negative") or []),
            },
            transaction=transaction,
        )
        from app.services.skill_lifecycle import record_skill_lifecycle_event

        record_skill_lifecycle_event(
            workspace,
            skill_name=str(trial.get("skill_name") or skill_name),
            status="promoted",
            note=trial["decision_reason"],
            transaction=transaction,
        )
        record_promotion_decision(
            workspace,
            candidate_id=candidate_id,
            decision="promoted",
            reason=trial["decision_reason"],
            rollback_ref=str((trial.get("rollback") or {}).get("ref") or target_path),
            metadata={"source": "provisional_trial", "candidate_version_hash": trial.get("candidate_version_hash")},
            transaction=transaction,
        )
        return _public_progress(trial, decision="promoted")

    if normalized_kind == "negative" and len(signals.get("negative") or []) >= negative_threshold:
        rollback = trial.get("rollback") if isinstance(trial.get("rollback"), dict) else {}
        action = str(rollback.get("action") or "manual_review")
        if action == "manual_review":
            return _mark_needs_review(
                workspace,
                trial=trial,
                entry=entry,
                reason="Negative threshold reached, but this legacy trial has no verified rollback baseline.",
                transaction=transaction,
            )
        if action == "restore":
            rollback_ref = str(rollback.get("ref") or "")
            baseline = transaction.read_bytes(rollback_ref) if rollback_ref else None
            if baseline is None or _sha256(baseline) != rollback.get("baseline_version_hash"):
                return _mark_needs_review(
                    workspace,
                    trial=trial,
                    entry=entry,
                    reason="Verified rollback backup is missing or corrupted.",
                    transaction=transaction,
                )
            transaction.stage_bytes(target_path, baseline)
            restore_skill_evolution_entry(
                workspace,
                skill_name=str(trial.get("skill_name") or skill_name),
                entry=rollback.get("baseline_registry_entry")
                if isinstance(rollback.get("baseline_registry_entry"), dict)
                else None,
                transaction=transaction,
            )
            restored_ref = rollback_ref
        else:
            transaction.stage_delete(target_path)
            upsert_skill_evolution_entry(
                workspace,
                skill_name=str(entry.get("skill_name") or skill_name),
                target_path=target_path,
                skill_origin=str(entry.get("skill_origin") or "unknown"),
                evolvable=bool(entry.get("evolvable", True)),
                active_version_hash=str(trial.get("candidate_version_hash") or "") or None,
                last_candidate_id=candidate_id,
                state=STATE_ROLLED_BACK,
                metadata={"commit_status": STATE_ROLLED_BACK, "trial_state": STATE_ROLLED_BACK},
                transaction=transaction,
            )
            restored_ref = f"deleted:{target_path}"

        trial["state"] = STATE_ROLLED_BACK
        trial["rolled_back_at"] = stamp
        trial["updated_at"] = stamp
        trial["decision_reason"] = "Negative runtime evidence threshold reached."
        _stage_trial(transaction, trial)
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate_id,
            status=STATE_ROLLED_BACK,
            reason=trial["decision_reason"],
            extra_metadata={
                "trial_state": STATE_ROLLED_BACK,
                "positive_signal_count": len(signals.get("positive") or []),
                "negative_signal_count": len(signals.get("negative") or []),
            },
            transaction=transaction,
        )
        from app.services.skill_lifecycle import record_skill_lifecycle_event

        record_skill_lifecycle_event(
            workspace,
            skill_name=str(trial.get("skill_name") or skill_name),
            status=STATE_ROLLED_BACK,
            note=trial["decision_reason"],
            transaction=transaction,
        )
        rollback_event = record_rollback_event(
            workspace,
            candidate_id=candidate_id,
            restored_ref=restored_ref,
            reason=trial["decision_reason"],
            operator="system",
            metadata={
                "source": "provisional_trial",
                "rollback_action": action,
                "candidate_version_hash": trial.get("candidate_version_hash"),
            },
            transaction=transaction,
        )
        return {**_public_progress(trial, decision=STATE_ROLLED_BACK), "rollback_event": rollback_event}

    _stage_trial(transaction, trial)
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=candidate_id,
        status=STATE_PROVISIONAL,
        extra_metadata={
            "trial_state": STATE_PROVISIONAL,
            "positive_signal_count": len(signals.get("positive") or []),
            "positive_signal_threshold": positive_threshold,
            "negative_signal_count": len(signals.get("negative") or []),
            "negative_signal_threshold": negative_threshold,
        },
        transaction=transaction,
    )
    return _public_progress(trial, decision="continue_trial")
