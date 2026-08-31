"""Fast, non-durable reflection candidates for next-turn learning."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.evolution_ledger import load_evolution_ledger, record_evolution_candidate
from app.services.session_learning import load_session_learning_projections, record_session_learning_projection

logger = logging.getLogger(__name__)


_SIGNAL_TYPES = {
    "user_preference_correction",
    "workflow_correction",
    "verification_failure",
    "repeated_task_pattern",
}
_SYSTEM_REFLECTION_SOURCES = frozenset(
    {
        "heartbeat_reflection",
        "heartbeat",
        "dream",
        "distiller",
        "skill_distiller",
        "t2_summary_agent",
        "t2_learning_brain",
        "t2_memory_gate",
    }
)
_DISPOSITION_SCHEMA = "fast_reflection_disposition.v1"


def _response_idempotency_key(metadata: dict[str, Any]) -> str:
    response_commit = metadata.get("response_commit") if isinstance(metadata.get("response_commit"), dict) else None
    return str((response_commit or {}).get("idempotency_key") or "").strip()


def _candidate_id_for_response(response_idempotency_key: str) -> str:
    return hashlib.sha256(f"fast-reflection:{response_idempotency_key}".encode()).hexdigest()[:24]


def _transaction_idempotency_key(response_idempotency_key: str) -> str:
    return f"fast-reflection:{response_idempotency_key}"


def _disposition_path(response_idempotency_key: str) -> str:
    digest = hashlib.sha256(response_idempotency_key.encode()).hexdigest()
    return f"runtime_artifacts/fast_reflection_dispositions/{digest}.json"


def _disposition_payload(
    *,
    response_idempotency_key: str,
    session_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": _DISPOSITION_SCHEMA,
        "response_idempotency_sha256": hashlib.sha256(response_idempotency_key.encode()).hexdigest(),
        "session_id": str(session_id),
        "status": str(result.get("status") or "unknown"),
        "reason": str(result.get("reason") or ""),
        "candidate_id": str(result.get("candidate_id") or ""),
        "signal_type": str(result.get("signal_type") or ""),
        "classification_method": str(result.get("classification_method") or ""),
    }


def _load_replay_result(
    *,
    transaction: Any,
    workspace: Path,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    response_idempotency_key: str,
) -> dict[str, Any]:
    raw = transaction.read_text(_disposition_path(response_idempotency_key))
    disposition: dict[str, Any] | None = None
    if raw:
        parsed = json.loads(raw)
        expected_digest = hashlib.sha256(response_idempotency_key.encode()).hexdigest()
        if (
            not isinstance(parsed, dict)
            or parsed.get("schema") != _DISPOSITION_SCHEMA
            or parsed.get("response_idempotency_sha256") != expected_digest
            or str(parsed.get("session_id") or "") != str(session_id)
        ):
            raise RuntimeError("fast reflection disposition no longer matches committed response authority")
        disposition = parsed

    candidate_id = str((disposition or {}).get("candidate_id") or _candidate_id_for_response(response_idempotency_key))
    candidate = next(
        (
            entry
            for entry in load_evolution_ledger(workspace)
            if entry.get("event") == "candidate" and entry.get("candidate_id") == candidate_id
        ),
        None,
    )
    if disposition is None:
        # Rolling compatibility: generation-1 candidates committed before the
        # disposition file shipped still have the same asset-transaction receipt.
        if candidate is None:
            raise RuntimeError("committed fast reflection receipt has no recoverable disposition")
        candidate_metadata = dict(candidate.get("metadata") or {})
        disposition = {
            "status": "candidate_created",
            "reason": "",
            "candidate_id": candidate_id,
            "signal_type": str(candidate_metadata.get("signal_type") or ""),
            "classification_method": str(candidate_metadata.get("classification_method") or ""),
        }

    result: dict[str, Any] = {
        "status": str(disposition.get("status") or "unknown"),
        "reason": str(disposition.get("reason") or ""),
        "idempotent_replay": True,
    }
    if result["status"] != "candidate_created":
        return result
    if candidate is None:
        raise RuntimeError("committed fast reflection candidate is missing")
    projection = next(
        (
            entry
            for entry in load_session_learning_projections(
                data_root=Path(data_root),
                agent_id=agent_id,
                session_id=str(session_id),
            )
            if entry.get("candidate_id") == candidate_id
        ),
        {},
    )
    result.update(
        {
            "candidate_id": candidate_id,
            "signal_type": str(disposition.get("signal_type") or ""),
            "classification_method": str(disposition.get("classification_method") or ""),
            "manifest": candidate.get("manifest") or {},
            "projection": projection,
            "skill_candidate": {"status": "skipped", "reason": "idempotent_replay"},
        }
    )
    return result


def load_fast_reflection_disposition(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    response_idempotency_key: str,
) -> dict[str, Any] | None:
    """Return the first committed disposition without invoking a provider."""

    durable_key = str(response_idempotency_key or "").strip()
    if not durable_key:
        return None
    from app.services.agent_asset_transaction import AgentAssetTransaction

    workspace = Path(data_root) / str(agent_id)
    with AgentAssetTransaction(
        workspace,
        operation="fast_reflection_candidate",
        idempotency_key=_transaction_idempotency_key(durable_key),
    ) as transaction:
        if not transaction.is_replay:
            return None
        return _load_replay_result(
            transaction=transaction,
            workspace=workspace,
            data_root=Path(data_root),
            agent_id=agent_id,
            session_id=session_id,
            response_idempotency_key=durable_key,
        )


def _commit_skipped_disposition(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    response_idempotency_key: str,
    evidence_refs: list[str],
    reason: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "skipped", "reason": reason}
    if not response_idempotency_key:
        return result
    from app.services.agent_asset_transaction import AgentAssetTransaction

    workspace = Path(data_root) / str(agent_id)
    with AgentAssetTransaction(
        workspace,
        operation="fast_reflection_candidate",
        idempotency_key=_transaction_idempotency_key(response_idempotency_key),
        evidence_refs=evidence_refs,
    ) as transaction:
        if transaction.is_replay:
            return _load_replay_result(
                transaction=transaction,
                workspace=workspace,
                data_root=Path(data_root),
                agent_id=agent_id,
                session_id=session_id,
                response_idempotency_key=response_idempotency_key,
            )
        transaction.stage_text(
            _disposition_path(response_idempotency_key),
            json.dumps(
                _disposition_payload(
                    response_idempotency_key=response_idempotency_key,
                    session_id=session_id,
                    result=result,
                ),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        transaction.commit()
    return {**result, "idempotent_replay": False}


def _message_digest(messages: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    for message in messages:
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "").strip()
        if content:
            snippets.append(f"{role}: {content}")
    return "\n".join(snippets)


def _classification_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("fast_reflection_classification")
    if not isinstance(raw, dict):
        return None

    signal_type = str(raw.get("signal_type") or raw.get("type") or "").strip()
    if signal_type in {"", "none", "low_signal", "no_signal", "skipped"}:
        return {
            "signal_type": "low_signal",
            "lesson": "",
            "method": str(raw.get("method") or "learning_brain_agent").strip() or "learning_brain_agent",
            "confidence": float(raw.get("confidence") or 0.0),
        }
    if signal_type not in _SIGNAL_TYPES:
        return None
    lesson = str(raw.get("lesson") or raw.get("signal") or "").strip()
    if not lesson:
        return None
    result = {
        "signal_type": signal_type,
        "lesson": lesson,
        "method": str(raw.get("method") or "learning_brain_agent").strip() or "learning_brain_agent",
        "confidence": float(raw.get("confidence") or 0.0),
    }
    learning_brain_decision = raw.get("learning_brain_decision")
    if isinstance(learning_brain_decision, dict):
        result["learning_brain_decision"] = learning_brain_decision
    return result


def _classify_signal(messages: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any] | None:
    del messages
    classifier_result = _classification_from_metadata(metadata)
    if classifier_result is not None:
        if classifier_result["signal_type"] == "low_signal":
            return None
        return classifier_result
    return None


def _skill_candidate_loop_enabled(metadata: dict[str, Any]) -> bool:
    value = metadata.get("skill_candidate_loop_enabled")
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return value is not False


def create_fast_reflection_candidate(
    *,
    data_root: Path,
    agent_id: uuid.UUID,
    session_id: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a ledger-only candidate from strong session feedback.

    This is intentionally not a durable memory writer. P2 may project the
    candidate into the same session prompt; P3 decides whether anything durable
    can be promoted.
    """

    metadata = metadata or {}
    normalized_session_id = str(session_id or metadata.get("session_id") or "unknown-session")
    metadata_source_refs = [str(ref).strip() for ref in (metadata.get("source_refs") or []) if str(ref).strip()]
    source_attempt_ids = metadata_source_refs or [normalized_session_id]
    response_idempotency_key = _response_idempotency_key(metadata)
    source = str(metadata.get("source") or "").strip().lower()
    if source in _SYSTEM_REFLECTION_SOURCES:
        if source == "heartbeat_reflection":
            try:
                from app.memory.metrics import record_heartbeat_reflection

                record_heartbeat_reflection("skipped_system_source")
            except Exception:
                pass
        return _commit_skipped_disposition(
            data_root=Path(data_root),
            agent_id=agent_id,
            session_id=normalized_session_id,
            response_idempotency_key=response_idempotency_key,
            evidence_refs=source_attempt_ids,
            reason="system_reflection_source",
        )

    signal = _classify_signal(messages, metadata)
    if signal is None:
        if str(metadata.get("source") or "") == "heartbeat_reflection":
            try:
                from app.memory.metrics import record_heartbeat_reflection

                record_heartbeat_reflection("skipped_low_signal")
            except Exception:
                pass
        return _commit_skipped_disposition(
            data_root=Path(data_root),
            agent_id=agent_id,
            session_id=normalized_session_id,
            response_idempotency_key=response_idempotency_key,
            evidence_refs=source_attempt_ids,
            reason="low_signal",
        )

    signal_type = str(signal["signal_type"])
    lesson = str(signal["lesson"])
    classification_method = str(signal.get("method") or "unknown")
    workspace = Path(data_root) / str(agent_id)
    payload = {
        "schema": "fast_reflection_candidate.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id),
        "tenant_id": str(metadata.get("tenant_id") or ""),
        "user_id": str(metadata.get("user_id") or ""),
        "session_id": normalized_session_id,
        "signal_type": signal_type,
        "lesson": lesson,
        "classification_method": classification_method,
        "classification_confidence": float(signal.get("confidence") or 0.0),
        "message_digest": _message_digest(messages),
        "final_response": str(metadata.get("final_response") or ""),
        "source": str(metadata.get("source") or "runtime"),
        "source_refs": source_attempt_ids,
        "promotion_state": "candidate",
    }
    learning_brain_decision = signal.get("learning_brain_decision")
    if isinstance(learning_brain_decision, dict):
        payload["learning_brain_decision"] = learning_brain_decision
    for key in ("loaded_skill_name", "umbrella_skill_name", "support_file_path", "repeated_workflow_signature"):
        if metadata.get(key):
            payload[key] = str(metadata[key])
    candidate_id = _candidate_id_for_response(response_idempotency_key) if response_idempotency_key else None
    idempotent_replay = False
    if response_idempotency_key:
        from app.services.agent_asset_transaction import AgentAssetTransaction

        with AgentAssetTransaction(
            workspace,
            operation="fast_reflection_candidate",
            idempotency_key=_transaction_idempotency_key(response_idempotency_key),
            evidence_refs=source_attempt_ids,
        ) as transaction:
            if transaction.is_replay:
                return _load_replay_result(
                    transaction=transaction,
                    workspace=workspace,
                    data_root=Path(data_root),
                    agent_id=agent_id,
                    session_id=normalized_session_id,
                    response_idempotency_key=response_idempotency_key,
                )
            else:
                candidate = record_evolution_candidate(
                    workspace,
                    target_type="fast_reflection",
                    target_id=f"{normalized_session_id}:{signal_type}",
                    diff=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    source_attempt_ids=source_attempt_ids,
                    baseline_version="fast-reflection@candidate",
                    candidate_id=candidate_id,
                    metadata=payload,
                    transaction=transaction,
                )
                projection = record_session_learning_projection(
                    data_root=Path(data_root),
                    agent_id=agent_id,
                    session_id=normalized_session_id,
                    candidate_id=candidate["candidate_id"],
                    lesson=lesson,
                    source_refs=source_attempt_ids,
                    evidence="user_stated" if signal_type == "user_preference_correction" else "system_observed",
                    ttl_minutes=60,
                    transaction=transaction,
                )
                transaction.stage_text(
                    _disposition_path(response_idempotency_key),
                    json.dumps(
                        _disposition_payload(
                            response_idempotency_key=response_idempotency_key,
                            session_id=normalized_session_id,
                            result={
                                "status": "candidate_created",
                                "candidate_id": candidate["candidate_id"],
                                "signal_type": signal_type,
                                "classification_method": classification_method,
                            },
                        ),
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                transaction.commit()
    else:
        candidate = record_evolution_candidate(
            workspace,
            target_type="fast_reflection",
            target_id=f"{normalized_session_id}:{signal_type}",
            diff=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            source_attempt_ids=source_attempt_ids,
            baseline_version="fast-reflection@candidate",
            metadata=payload,
        )
        projection = record_session_learning_projection(
            data_root=Path(data_root),
            agent_id=agent_id,
            session_id=normalized_session_id,
            candidate_id=candidate["candidate_id"],
            lesson=lesson,
            source_refs=source_attempt_ids
            if metadata_source_refs
            else [f"runtime_task:{item}" for item in source_attempt_ids],
            evidence="user_stated" if signal_type == "user_preference_correction" else "system_observed",
            ttl_minutes=60,
        )
    result = {
        "status": "candidate_created",
        "candidate_id": candidate["candidate_id"],
        "signal_type": signal_type,
        "classification_method": classification_method,
        "manifest": candidate["manifest"],
        "projection": projection,
        "idempotent_replay": idempotent_replay,
    }
    if idempotent_replay:
        result["skill_candidate"] = {"status": "skipped", "reason": "idempotent_replay"}
        return result
    if payload["source"] == "heartbeat_reflection":
        try:
            from app.memory.metrics import record_heartbeat_reflection

            record_heartbeat_reflection("candidate_created")
        except Exception:
            pass
    if not _skill_candidate_loop_enabled(metadata):
        result["skill_candidate"] = {
            "status": "skipped",
            "reason": "skill_candidate_loop_disabled",
        }
        return result

    # Bridge to the skill flywheel (P4). The flywheel routes/guards internally and
    # returns "skipped" for non-skill signals, so we always offer the candidate.
    # Best-effort: a skill-side failure must never discard the already-recorded
    # memory-side candidate and projection.
    try:
        from app.services.skill_flywheel import propose_skill_candidate_from_fast_reflection

        result["skill_candidate"] = propose_skill_candidate_from_fast_reflection(
            workspace=workspace,
            fast_candidate={"candidate_id": candidate["candidate_id"], "metadata": payload},
        )
    except Exception as exc:
        logger.warning(
            "[FastReflection] skill flywheel failed for agent=%s session=%s: %s",
            agent_id,
            normalized_session_id,
            exc,
        )
        result["skill_candidate"] = {"status": "error", "reason": str(exc)}
    return result
