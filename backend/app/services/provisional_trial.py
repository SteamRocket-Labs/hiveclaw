"""Version-bound, model-reviewed trials for provisional Skills.

Runtime signals are durable evidence only. They never auto-promote or
auto-rollback a Skill by count. A model reviewer reads the complete candidate,
rollback baseline, and signal ledger; the platform then enforces exact version,
transaction, verification, and rollback invariants around that decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.evolution_ledger import record_promotion_decision, record_rollback_event
from app.services.llm_client import get_max_tokens
from app.services.skill_candidate_package import update_skill_candidate_package_status
from app.services.skill_evolution_registry import (
    STATE_ACTIVE,
    STATE_NEEDS_REVIEW,
    STATE_PROVISIONAL,
    STATE_ROLLED_BACK,
    get_skill_evolution_entry,
    load_skill_evolution_registry,
    restore_skill_evolution_entry,
    upsert_skill_evolution_entry,
)

SCHEMA = "skill_provisional_trial.v1"
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
    return {
        "trial_decision": decision,
        "candidate_id": trial.get("candidate_id"),
        "trial_state": trial.get("state"),
        "positive_signal_count": len(signals.get("positive") or []),
        "negative_signal_count": len(signals.get("negative") or []),
        "review_status": trial.get("review_status"),
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
        "signals": {"positive": [], "negative": []},
        "review_status": "waiting_for_evidence",
        "model_reviews": [],
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
            "negative_signal_count": 0,
            "review_status": trial["review_status"],
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
    }
    # RuntimeTask/trace identities are durable replay keys.  Their terminal
    # callback may be reconstructed after a worker restart with a different
    # wall-clock timestamp, but it is still the same causal trial signal.
    # Session-only/manual signals have no per-turn identity, so they retain the
    # timestamp to avoid collapsing distinct turns in one long session.
    if not (identity["runtime_task_id"] or identity["trace_id"]):
        identity["occurred_at"] = occurred_at
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _mark_needs_review(
    workspace: Path,
    *,
    trial: dict[str, Any],
    entry: dict[str, Any],
    reason: str,
    occurred_at: str | None = None,
    transaction: AgentAssetTransaction,
) -> dict[str, Any]:
    stamp = occurred_at or _now_iso()
    trial["state"] = STATE_NEEDS_REVIEW
    trial["review_status"] = "held"
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


def _mark_orphan_provisional_needs_review(
    workspace: Path,
    *,
    entry: dict[str, Any],
    reason: str,
    occurred_at: str,
    transaction: AgentAssetTransaction,
) -> dict[str, Any]:
    """Fail closed when a provisional registry entry has no valid trial ledger."""

    skill_name = str(entry.get("skill_name") or "unknown")
    candidate_id = str(entry.get("last_candidate_id") or "").strip()
    upsert_skill_evolution_entry(
        workspace,
        skill_name=skill_name,
        target_path=str(entry.get("target_path") or ""),
        skill_origin=str(entry.get("skill_origin") or "unknown"),
        evolvable=bool(entry.get("evolvable", True)),
        active_version_hash=str(entry.get("active_version_hash") or "") or None,
        last_candidate_id=candidate_id or None,
        state=STATE_NEEDS_REVIEW,
        metadata={
            "commit_status": STATE_NEEDS_REVIEW,
            "trial_state": STATE_NEEDS_REVIEW,
            "trial_integrity_error": reason,
            "trial_reviewed_at": occurred_at,
        },
        transaction=transaction,
    )
    if candidate_id:
        update_skill_candidate_package_status(
            workspace=workspace,
            candidate_id=candidate_id,
            status=STATE_NEEDS_REVIEW,
            reason=reason,
            extra_metadata={"trial_state": STATE_NEEDS_REVIEW, "trial_integrity_error": reason},
            transaction=transaction,
        )
    from app.services.skill_lifecycle import record_skill_lifecycle_event

    record_skill_lifecycle_event(
        workspace,
        skill_name=skill_name,
        status=STATE_NEEDS_REVIEW,
        note=reason,
        transaction=transaction,
    )
    return {
        "skill_name": skill_name,
        "candidate_id": candidate_id or None,
        "decision": STATE_NEEDS_REVIEW,
    }


def _provisional_entries(
    workspace: Path,
    *,
    transaction: AgentAssetTransaction | None = None,
) -> list[dict[str, Any]]:
    registry = load_skill_evolution_registry(workspace, transaction=transaction)
    skills = registry.get("skills") if isinstance(registry.get("skills"), dict) else {}
    return [
        entry
        for entry in skills.values()
        if isinstance(entry, dict) and str(entry.get("state") or "").strip().lower() == STATE_PROVISIONAL
    ]


def sweep_expired_provisional_trials(
    workspace: Path,
    *,
    now: str | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    """Move expired or mechanically unverifiable provisional Skills to review.

    A no-signal trial must still terminate.  The sweep is transactionally
    coupled to the registry, candidate manifest, lifecycle evidence, and trial
    ledger.  It is safe to run on every heartbeat: terminal entries disappear
    from the next sweep and therefore cannot emit duplicate decisions.
    """

    stamp = now or _now_iso()
    stamp_dt = _parse_iso(stamp) or datetime.now(timezone.utc)
    if transaction is None:
        entries = _provisional_entries(workspace)
        due = False
        for entry in entries:
            candidate_id = str(entry.get("last_candidate_id") or "").strip()
            trial = load_provisional_trial(workspace, candidate_id) if candidate_id else None
            if trial is None:
                due = True
                break
            started = _parse_iso(str(trial.get("started_at") or ""))
            window_days = max(1, int(trial.get("window_days") or DEFAULT_TRIAL_WINDOW_DAYS))
            if started is None or stamp_dt > started + timedelta(days=window_days):
                due = True
                break
        if not due:
            return {"checked": len(entries), "expired": 0, "needs_review": 0, "results": []}

        with AgentAssetTransaction(
            workspace,
            operation="skill_provisional_trial_expiry_sweep",
            evidence_refs=("evolution/skill_registry.json", "evolution/skill_trials"),
        ) as own_transaction:
            result = sweep_expired_provisional_trials(
                workspace,
                now=stamp,
                transaction=own_transaction,
            )
            if own_transaction.has_changes:
                own_transaction.commit()
            return result

    entries = _provisional_entries(workspace, transaction=transaction)
    expired = 0
    results: list[dict[str, Any]] = []
    for entry in entries:
        skill_name = str(entry.get("skill_name") or "unknown")
        candidate_id = str(entry.get("last_candidate_id") or "").strip()
        trial = load_provisional_trial(workspace, candidate_id, transaction=transaction) if candidate_id else None
        if trial is None:
            results.append(
                _mark_orphan_provisional_needs_review(
                    workspace,
                    entry=entry,
                    reason="Provisional Skill has no valid version-bound trial ledger.",
                    occurred_at=stamp,
                    transaction=transaction,
                )
            )
            continue

        started = _parse_iso(str(trial.get("started_at") or ""))
        window_days = max(1, int(trial.get("window_days") or DEFAULT_TRIAL_WINDOW_DAYS))
        if started is not None and stamp_dt <= started + timedelta(days=window_days):
            continue
        expired += 1
        _mark_needs_review(
            workspace,
            trial=trial,
            entry=entry,
            reason="Provisional Skill trial window expired without a terminal model review decision.",
            occurred_at=stamp,
            transaction=transaction,
        )
        results.append(
            {
                "skill_name": skill_name,
                "candidate_id": candidate_id,
                "decision": STATE_NEEDS_REVIEW,
            }
        )

    return {
        "checked": len(entries),
        "expired": expired,
        "needs_review": len(results),
        "results": results,
    }


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
            occurred_at=stamp,
            transaction=transaction,
        )

    stamp_dt = _parse_iso(stamp) or datetime.now(timezone.utc)
    started_dt = _parse_iso(str(trial.get("started_at") or "")) or stamp_dt
    if stamp_dt > started_dt + timedelta(days=max(1, int(trial.get("window_days") or DEFAULT_TRIAL_WINDOW_DAYS))):
        return _mark_needs_review(
            workspace,
            trial=trial,
            entry=entry,
            reason="Provisional Skill trial window expired before the pending model review completed.",
            occurred_at=stamp,
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
    # Every new model-authored outcome is reviewable immediately. Counts and
    # ordering stay in the ledger, but no fixed threshold owns the meaning.
    trial["review_status"] = "pending_model_review"
    trial["review_requested_at"] = stamp
    _stage_trial(transaction, trial)
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=candidate_id,
        status=STATE_PROVISIONAL,
        extra_metadata={
            "trial_state": STATE_PROVISIONAL,
            "positive_signal_count": len(signals.get("positive") or []),
            "negative_signal_count": len(signals.get("negative") or []),
            "review_status": trial["review_status"],
        },
        transaction=transaction,
    )
    return _public_progress(trial, decision="model_review_pending")


TrialReviewer = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
_MODEL_TRIAL_DECISIONS = frozenset({"promote", "rollback", "continue", "hold"})


def _trial_evidence_digest(trial: dict[str, Any]) -> str:
    evidence = {
        "candidate_id": trial.get("candidate_id"),
        "candidate_version_hash": trial.get("candidate_version_hash"),
        "signals": trial.get("signals"),
        "rollback": trial.get("rollback"),
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _trial_review_payload(workspace: Path, trial: dict[str, Any]) -> dict[str, Any]:
    target_path = str(trial.get("target_path") or "")
    candidate = (workspace / target_path).read_bytes()
    if _sha256(candidate) != trial.get("candidate_version_hash"):
        raise ValueError("candidate_version_drift")
    try:
        candidate_content = candidate.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("candidate_is_not_utf8_markdown") from exc

    rollback = trial.get("rollback") if isinstance(trial.get("rollback"), dict) else {}
    baseline_content: str | None = None
    if rollback.get("action") == "restore":
        rollback_ref = str(rollback.get("ref") or "")
        baseline = (workspace / rollback_ref).read_bytes()
        if _sha256(baseline) != rollback.get("baseline_version_hash"):
            raise ValueError("rollback_baseline_drift")
        try:
            baseline_content = baseline.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("rollback_baseline_is_not_utf8_markdown") from exc

    return {
        "schema": "skill_provisional_trial_review_input.v1",
        "candidate_id": trial.get("candidate_id"),
        "skill_name": trial.get("skill_name"),
        "target_path": target_path,
        "candidate_version_hash": trial.get("candidate_version_hash"),
        "candidate_content": candidate_content,
        "baseline_content": baseline_content,
        "rollback_contract": rollback,
        "signals": trial.get("signals") if isinstance(trial.get("signals"), dict) else {},
        "source_refs": list(trial.get("source_refs") or []),
        "started_at": trial.get("started_at"),
        "review_requested_at": trial.get("review_requested_at"),
        "evidence_digest": _trial_evidence_digest(trial),
    }


def _append_model_review(
    trial: dict[str, Any],
    *,
    decision: str,
    reason: str,
    evidence_digest: str,
    occurred_at: str,
) -> None:
    reviews = trial.setdefault("model_reviews", [])
    if not isinstance(reviews, list):
        reviews = []
        trial["model_reviews"] = reviews
    reviews.append(
        {
            "schema": "skill_provisional_trial_model_review.v1",
            "decision": decision,
            "reason": reason,
            "evidence_digest": evidence_digest,
            "occurred_at": occurred_at,
        }
    )


def _apply_model_promotion(
    workspace: Path,
    *,
    trial: dict[str, Any],
    entry: dict[str, Any],
    reason: str,
    occurred_at: str,
    transaction: AgentAssetTransaction,
) -> dict[str, Any]:
    candidate_id = str(trial["candidate_id"])
    target_path = str(trial.get("target_path") or entry.get("target_path") or "")
    signals = trial.get("signals") if isinstance(trial.get("signals"), dict) else {}
    trial["state"] = "promoted"
    trial["review_status"] = "reviewed"
    trial["promoted_at"] = occurred_at
    trial["updated_at"] = occurred_at
    trial["decision_reason"] = reason
    _stage_trial(transaction, trial)
    upsert_skill_evolution_entry(
        workspace,
        skill_name=str(entry.get("skill_name") or trial.get("skill_name") or candidate_id),
        target_path=target_path,
        skill_origin=str(entry.get("skill_origin") or "unknown"),
        evolvable=bool(entry.get("evolvable", True)),
        active_version_hash=str(trial.get("candidate_version_hash") or "") or None,
        last_candidate_id=candidate_id,
        state=STATE_ACTIVE,
        metadata={
            "commit_status": STATE_ACTIVE,
            "trial_state": "promoted",
            "promoted_at": occurred_at,
            "semantic_authority": "model_review",
        },
        transaction=transaction,
    )
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=candidate_id,
        status="promoted",
        reason=reason,
        extra_metadata={
            "trial_state": "promoted",
            "positive_signal_count": len(signals.get("positive") or []),
            "negative_signal_count": len(signals.get("negative") or []),
            "semantic_authority": "model_review",
        },
        transaction=transaction,
    )
    from app.services.skill_lifecycle import record_skill_lifecycle_event

    record_skill_lifecycle_event(
        workspace,
        skill_name=str(trial.get("skill_name") or candidate_id),
        status="promoted",
        note=reason,
        transaction=transaction,
    )
    record_promotion_decision(
        workspace,
        candidate_id=candidate_id,
        decision="promoted",
        reason=reason,
        rollback_ref=str((trial.get("rollback") or {}).get("ref") or target_path),
        metadata={
            "source": "provisional_trial_model_review",
            "candidate_version_hash": trial.get("candidate_version_hash"),
        },
        transaction=transaction,
    )
    return _public_progress(trial, decision="promoted")


def _apply_model_rollback(
    workspace: Path,
    *,
    trial: dict[str, Any],
    entry: dict[str, Any],
    reason: str,
    occurred_at: str,
    transaction: AgentAssetTransaction,
) -> dict[str, Any]:
    candidate_id = str(trial["candidate_id"])
    target_path = str(trial.get("target_path") or entry.get("target_path") or "")
    rollback = trial.get("rollback") if isinstance(trial.get("rollback"), dict) else {}
    action = str(rollback.get("action") or "manual_review")
    if action == "manual_review":
        return _mark_needs_review(
            workspace,
            trial=trial,
            entry=entry,
            reason=f"Model recommended rollback, but no verified rollback baseline exists. {reason}",
            occurred_at=occurred_at,
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
                reason="Model recommended rollback, but the verified rollback backup is missing or corrupted.",
                occurred_at=occurred_at,
                transaction=transaction,
            )
        transaction.stage_bytes(target_path, baseline)
        restore_skill_evolution_entry(
            workspace,
            skill_name=str(trial.get("skill_name") or candidate_id),
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
            skill_name=str(entry.get("skill_name") or trial.get("skill_name") or candidate_id),
            target_path=target_path,
            skill_origin=str(entry.get("skill_origin") or "unknown"),
            evolvable=bool(entry.get("evolvable", True)),
            active_version_hash=str(trial.get("candidate_version_hash") or "") or None,
            last_candidate_id=candidate_id,
            state=STATE_ROLLED_BACK,
            metadata={
                "commit_status": STATE_ROLLED_BACK,
                "trial_state": STATE_ROLLED_BACK,
                "semantic_authority": "model_review",
            },
            transaction=transaction,
        )
        restored_ref = f"deleted:{target_path}"

    signals = trial.get("signals") if isinstance(trial.get("signals"), dict) else {}
    trial["state"] = STATE_ROLLED_BACK
    trial["review_status"] = "reviewed"
    trial["rolled_back_at"] = occurred_at
    trial["updated_at"] = occurred_at
    trial["decision_reason"] = reason
    _stage_trial(transaction, trial)
    update_skill_candidate_package_status(
        workspace=workspace,
        candidate_id=candidate_id,
        status=STATE_ROLLED_BACK,
        reason=reason,
        extra_metadata={
            "trial_state": STATE_ROLLED_BACK,
            "positive_signal_count": len(signals.get("positive") or []),
            "negative_signal_count": len(signals.get("negative") or []),
            "semantic_authority": "model_review",
        },
        transaction=transaction,
    )
    from app.services.skill_lifecycle import record_skill_lifecycle_event

    record_skill_lifecycle_event(
        workspace,
        skill_name=str(trial.get("skill_name") or candidate_id),
        status=STATE_ROLLED_BACK,
        note=reason,
        transaction=transaction,
    )
    rollback_event = record_rollback_event(
        workspace,
        candidate_id=candidate_id,
        restored_ref=restored_ref,
        reason=reason,
        operator="model_reviewer",
        metadata={
            "source": "provisional_trial_model_review",
            "rollback_action": action,
            "candidate_version_hash": trial.get("candidate_version_hash"),
        },
        transaction=transaction,
    )
    return {**_public_progress(trial, decision=STATE_ROLLED_BACK), "rollback_event": rollback_event}


async def review_pending_provisional_trials(
    workspace: Path,
    *,
    reviewer: TrialReviewer,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Apply model-authored trial decisions behind version/rollback hard gates."""

    stamp = occurred_at or _now_iso()
    reviewed = 0
    decisions: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    for entry_snapshot in _provisional_entries(workspace):
        candidate_id = str(entry_snapshot.get("last_candidate_id") or "").strip()
        if not candidate_id:
            continue
        trial_snapshot = load_provisional_trial(workspace, candidate_id)
        if not isinstance(trial_snapshot, dict) or trial_snapshot.get("review_status") != "pending_model_review":
            continue
        try:
            payload = _trial_review_payload(workspace, trial_snapshot)
            raw_review = await reviewer(payload)
            if not isinstance(raw_review, dict):
                raise ValueError("model review must be a JSON object")
            decision = str(raw_review.get("decision") or "").strip().lower()
            reason = str(raw_review.get("reason") or "").strip()
            if decision not in _MODEL_TRIAL_DECISIONS or not reason:
                raise ValueError("model review requires decision=promote|rollback|continue|hold and a reason")
        except Exception as exc:  # noqa: BLE001 - observable retry, never a mechanical semantic fallback
            errors.append({"candidate_id": candidate_id, "error": f"{type(exc).__name__}: {exc}"})
            continue

        with AgentAssetTransaction(
            workspace,
            operation="skill_provisional_trial_model_review",
            evidence_refs=(f"skill-candidate:{candidate_id}", trial_rel_path(candidate_id)),
        ) as transaction:
            entry = get_skill_evolution_entry(
                workspace,
                str(entry_snapshot.get("skill_name") or trial_snapshot.get("skill_name") or ""),
                transaction=transaction,
            )
            trial = load_provisional_trial(workspace, candidate_id, transaction=transaction)
            if not isinstance(entry, dict) or not isinstance(trial, dict):
                continue
            if trial.get("review_status") != "pending_model_review":
                continue
            if _trial_evidence_digest(trial) != payload["evidence_digest"]:
                # New evidence arrived while the model was reviewing. Preserve
                # it and retry with the complete ledger on the next pass.
                continue
            target_path = str(trial.get("target_path") or entry.get("target_path") or "")
            current = transaction.read_bytes(target_path) if target_path else None
            if current is None or _sha256(current) != trial.get("candidate_version_hash"):
                _mark_needs_review(
                    workspace,
                    trial=trial,
                    entry=entry,
                    reason="Provisional Skill content drifted during model review.",
                    occurred_at=stamp,
                    transaction=transaction,
                )
                transaction.commit()
                reviewed += 1
                decisions["hold"] += 1
                continue

            _append_model_review(
                trial,
                decision=decision,
                reason=reason,
                evidence_digest=str(payload["evidence_digest"]),
                occurred_at=stamp,
            )
            if decision == "promote":
                _apply_model_promotion(
                    workspace,
                    trial=trial,
                    entry=entry,
                    reason=reason,
                    occurred_at=stamp,
                    transaction=transaction,
                )
            elif decision == "rollback":
                _apply_model_rollback(
                    workspace,
                    trial=trial,
                    entry=entry,
                    reason=reason,
                    occurred_at=stamp,
                    transaction=transaction,
                )
            elif decision == "hold":
                _mark_needs_review(
                    workspace,
                    trial=trial,
                    entry=entry,
                    reason=reason,
                    occurred_at=stamp,
                    transaction=transaction,
                )
            else:
                trial["review_status"] = "reviewed"
                trial["updated_at"] = stamp
                trial["decision_reason"] = reason
                _stage_trial(transaction, trial)
                signals = trial.get("signals") if isinstance(trial.get("signals"), dict) else {}
                update_skill_candidate_package_status(
                    workspace=workspace,
                    candidate_id=candidate_id,
                    status=STATE_PROVISIONAL,
                    reason=reason,
                    extra_metadata={
                        "trial_state": STATE_PROVISIONAL,
                        "review_status": "reviewed",
                        "positive_signal_count": len(signals.get("positive") or []),
                        "negative_signal_count": len(signals.get("negative") or []),
                        "semantic_authority": "model_review",
                    },
                    transaction=transaction,
                )
            transaction.commit()
            reviewed += 1
            decisions[decision] += 1

    return {
        "reviewed": reviewed,
        "decisions": dict(decisions),
        "errors": errors,
        "pending": sum(
            1
            for entry in _provisional_entries(workspace)
            if (trial := load_provisional_trial(workspace, str(entry.get("last_candidate_id") or "")))
            and trial.get("review_status") == "pending_model_review"
        ),
    }


async def review_pending_provisional_trials_with_model(
    workspace: Path,
    *,
    model: Any,
    agent_id: Any = None,
    tenant_id: Any = None,
) -> dict[str, Any]:
    """Run complete-input model review for every pending provisional trial."""

    from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context
    from app.services.semantic_input_coverage import prepare_covered_semantic_input

    client = create_llm_client_from_config(
        with_llm_usage_context(
            {
                "provider": getattr(model, "provider"),
                "model": getattr(model, "model"),
                "api_key": getattr(model, "api_key"),
                "base_url": getattr(model, "base_url", None),
            },
            source="skill_provisional_trial_review",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )
    )
    try:
        try:
            max_input_tokens = int(getattr(model, "max_input_tokens", None) or 128_000)
        except (TypeError, ValueError):
            max_input_tokens = 128_000
        semantic_budget = max(max_input_tokens - 16_000, 8_000) * 3
        output_tokens = get_max_tokens(
            str(getattr(model, "provider", "") or ""),
            str(getattr(model, "model", "") or ""),
            getattr(model, "max_output_tokens", None),
        )

        async def reviewer(payload: dict[str, Any]) -> dict[str, Any]:
            candidate_id = str(payload.get("candidate_id") or "unknown")
            full_input = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

            async def review_chunk(review_phase: str, review_prompt: str) -> str:
                response = await client.complete(
                    messages=[
                        LLMMessage(
                            role="system",
                            content=(
                                "Preserve every Skill trial fact, exception, failure, success, source ref, "
                                "and rollback constraint for the final reviewer. Return coverage notes only."
                            ),
                        ),
                        LLMMessage(role="user", content=review_prompt),
                    ],
                    temperature=0.1,
                    max_tokens=output_tokens,
                )
                return str(response.content or "").strip()

            covered = await prepare_covered_semantic_input(
                phase=f"skill_provisional_trial_{candidate_id}",
                sections=[(f"skill_trial:{candidate_id}", full_input)],
                max_chars=semantic_budget,
                coverage_path=workspace
                / "memory"
                / "control"
                / "skill_trial_reviews"
                / f"{_safe_candidate_id(candidate_id)}.coverage.json",
                review_chunk=review_chunk,
            )
            response = await client.complete(
                messages=[
                    LLMMessage(
                        role="system",
                        content=(
                            "You are the semantic authority for a provisional Skill trial. Review all supplied "
                            "candidate, baseline, and signal evidence. Decide promote, rollback, continue, or hold. "
                            "Counts and dates are observations, never automatic criteria. Platform version and "
                            "rollback gates run after your decision. Return only JSON: "
                            '{"decision":"promote|rollback|continue|hold","reason":"complete evidence-grounded reason"}'
                        ),
                    ),
                    LLMMessage(role="user", content=covered),
                ],
                temperature=0.1,
                max_tokens=output_tokens,
            )
            parsed = json.loads(str(response.content or "").strip())
            if not isinstance(parsed, dict):
                raise ValueError("model review response must be a JSON object")
            return parsed

        return await review_pending_provisional_trials(workspace, reviewer=reviewer)
    finally:
        await client.close()
