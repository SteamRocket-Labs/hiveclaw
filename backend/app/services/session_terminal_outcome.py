"""Session V2 RunOutcome seal and atomic Run/Turn terminal transaction."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_artifact import ChatArtifact
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionRunOutcome
from app.services.chat_artifact_delivery import artifact_part_from_model
from app.services.runtime_root_ledger import transition_runtime_root_item_by_task
from app.services.session_round_obligation import current_run_fences, unresolved_round_obligations
from app.services.session_v2_persistence import SessionEventDraft, append_session_events


class TerminalOutcomeIneligible(RuntimeError):
    """The latest mechanical frontier does not permit Run terminal."""


class TerminalOutcomeNeedsReconciliation(RuntimeError):
    """A terminal transaction has an ambiguous durable outcome."""


async def _close_runtime_root_item(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    outcome: SessionRunOutcome,
    terminal_result_id: uuid.UUID | None,
    terminal_event_id: uuid.UUID | None,
) -> None:
    refs = [f"session-run-outcome://{outcome.id}"]
    if terminal_result_id is not None:
        refs.append(f"session-model-result://{terminal_result_id}")
    if terminal_event_id is not None:
        refs.append(f"session-event://{terminal_event_id}")
    item, _decision = await transition_runtime_root_item_by_task(
        db,
        runtime_task_id=run_id,
        requested_state="completed",
        reason_code="session_v2_terminal_outcome_committed",
        result_refs=refs,
        metadata={
            "session_v2_outcome_id": str(outcome.id),
            "terminal_result_id": str(terminal_result_id) if terminal_result_id else None,
            "terminal_event_id": str(terminal_event_id) if terminal_event_id else None,
        },
    )
    if item is None:
        raise TerminalOutcomeNeedsReconciliation("runtime root item missing during terminal commit")
    if item.state != "completed":
        raise TerminalOutcomeNeedsReconciliation(f"runtime root item conflicts with terminal outcome: {item.state}")


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _sha256(value: Any) -> str:
    raw = json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _run_scope(session_id: uuid.UUID, turn_id: str, run_id: uuid.UUID) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
    }


def _turn_scope(session_id: uuid.UUID, turn_id: str) -> dict[str, str]:
    return {
        "level": "turn",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
    }


def _round_scope(session_id: uuid.UUID, turn_id: str, run_id: uuid.UUID, round_id: str) -> dict[str, str]:
    return {
        "level": "round",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
        "round_id": round_id,
    }


def _outcome_id(run_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(run_id, "session-run-outcome")


def _render_owner_id(result_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(result_id, "visible_text_wrapper")


def _final_item_id(outcome_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(outcome_id, "assistant-final")


def _turn_item_id(session_id: uuid.UUID, turn_id: str) -> uuid.UUID:
    return uuid.uuid5(session_id, f"session-turn:{turn_id}")


def _allowed_source_blocks(seal: Mapping[str, Any]) -> list[dict[str, Any]]:
    allowed: list[dict[str, Any]] = []
    for raw in seal.get("block_ledger") or []:
        block = dict(raw or {})
        if block.get("kind") not in {"assistant_text", "assistant_final"}:
            continue
        allowed.append(
            {
                "item_id": str(block["item_id"]),
                "block_index": int(block.get("block_index") or 0),
                "content_hash": str(block["content_hash"]),
            }
        )
    return allowed


async def _declared_artifact_parts(
    db: AsyncSession,
    *,
    task: RuntimeTask,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """Resolve only the run's already-governed terminal artifact selection."""

    metadata = dict(task.metadata_json or {})
    raw_ids = list(metadata.get("artifact_ids") or [])
    if not raw_ids:
        raw_ids = [
            part.get("artifact_id")
            for part in metadata.get("artifacts") or []
            if isinstance(part, dict) and part.get("artifact_id")
        ]
    artifact_ids: list[uuid.UUID] = []
    for value in raw_ids:
        try:
            parsed = uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise TerminalOutcomeNeedsReconciliation("terminal artifact selection contains an invalid id") from exc
        if parsed not in artifact_ids:
            artifact_ids.append(parsed)
    if not artifact_ids:
        return []

    statement = select(ChatArtifact).where(
        ChatArtifact.id.in_(artifact_ids),
        ChatArtifact.tenant_id == tenant_id,
        ChatArtifact.agent_id == agent_id,
        ChatArtifact.session_id == session_id,
        ChatArtifact.runtime_task_id == run_id,
        ChatArtifact.authority_state == "owned",
    )
    if task.root_user_id is not None:
        statement = statement.where(ChatArtifact.owner_user_id == task.root_user_id)
    root_session_id = None
    try:
        root_session_id = uuid.UUID(str(task.root_session_id)) if task.root_session_id else session_id
    except (TypeError, ValueError, AttributeError):
        root_session_id = session_id
    statement = statement.where(ChatArtifact.root_session_id == root_session_id)
    rows = list((await db.execute(statement.with_for_update())).scalars())
    by_id = {row.id: row for row in rows}
    if any(artifact_id not in by_id for artifact_id in artifact_ids):
        raise TerminalOutcomeNeedsReconciliation("terminal artifact selection is not authority-complete")
    return [artifact_part_from_model(by_id[artifact_id]) for artifact_id in artifact_ids]


async def _lock_terminal_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    terminal_result_id: uuid.UUID,
) -> SessionModelResult:
    result = await db.scalar(
        select(SessionModelResult)
        .where(
            SessionModelResult.id == terminal_result_id,
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.run_id == run_id,
        )
        .with_for_update()
    )
    if result is None or result.state != "round_committed" or not result.seal_json:
        raise TerminalOutcomeIneligible("terminal candidate has no round-committed result seal")
    continuation = dict(result.seal_json.get("continuation") or {})
    if continuation.get("verdict") != "terminal_candidate":
        raise TerminalOutcomeIneligible("model result requires continuation")
    if not bool(result.seal_json.get("logical_round_complete", True)):
        raise TerminalOutcomeIneligible("physical continuation receipt is not a logical terminal candidate")
    return result


async def prepare_and_seal_run_outcome(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: str,
    run_id: uuid.UUID,
    terminal_result_id: uuid.UUID,
) -> SessionRunOutcome:
    """Seal one terminal candidate only after every current obligation is terminal."""

    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == run_id,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
        )
        .with_for_update()
    )
    if task is None:
        raise TerminalOutcomeIneligible("runtime task not found")
    if task.status in {"cancelling", "cancelled", "killed", "needs_reconciliation"}:
        raise TerminalOutcomeIneligible(f"runtime task is not terminal-eligible: {task.status}")
    result = await _lock_terminal_result(
        db,
        tenant_id=tenant_id,
        run_id=run_id,
        terminal_result_id=terminal_result_id,
    )
    unresolved = await unresolved_round_obligations(db, tenant_id=tenant_id, run_id=run_id)
    if unresolved:
        raise TerminalOutcomeIneligible(
            "terminal candidate has unresolved obligations: " + ",".join(str(row.id) for row in unresolved)
        )
    fences = await current_run_fences(db, tenant_id=tenant_id, run_id=run_id)
    if int(fences.get("cancellation_generation") or 0) > 0 and task.status not in {"running", "starting"}:
        raise TerminalOutcomeIneligible("cancellation fence is not settled")
    source_blocks = _allowed_source_blocks(result.seal_json)
    semantic_content = result.seal_json.get("semantic_content")
    if not source_blocks and isinstance(semantic_content, str) and semantic_content:
        raise TerminalOutcomeNeedsReconciliation("terminal content has no canonical source block")
    result_content_hash = _sha256([block["content_hash"] for block in source_blocks])
    artifact_parts = await _declared_artifact_parts(
        db,
        task=task,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
    )
    artifact_refs = [str(part["artifact_id"]) for part in artifact_parts]
    artifact_manifest_hash = _sha256(artifact_parts)
    eligibility = {
        "terminal_result_id": str(result.id),
        "result_version": int(result.version),
        "result_content_hash": result_content_hash,
        "artifact_manifest_hash": artifact_manifest_hash,
        "obligation_ids": [],
        "fences": fences,
        "task_claim_version": int(task.claim_version or 0),
    }
    eligibility_hash = _sha256(eligibility)
    outcome_uuid = _outcome_id(run_id)
    outcome = await db.scalar(
        select(SessionRunOutcome)
        .where(SessionRunOutcome.id == outcome_uuid, SessionRunOutcome.tenant_id == tenant_id)
        .with_for_update()
    )
    if outcome is not None and outcome.state == "terminal_committed":
        return outcome
    prior_candidates: list[dict[str, Any]] = []
    if outcome is not None:
        if outcome.state == "sealed" and outcome.eligibility_snapshot_hash == eligibility_hash:
            return outcome
        if outcome.seal_json:
            prior_candidates = list(outcome.seal_json.get("prior_candidates") or [])
            prior_candidates.append(
                {
                    "terminal_result_id": str(outcome.terminal_result_id),
                    "eligibility_snapshot_hash": outcome.eligibility_snapshot_hash,
                    "seal": _canonical(outcome.seal_json),
                }
            )
        outcome.terminal_result_id = result.id
        outcome.eligibility_snapshot_hash = eligibility_hash
        outcome.state = "prepared"
        outcome.version = int(outcome.version) + 1
    else:
        outcome = SessionRunOutcome(
            id=outcome_uuid,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            terminal_result_id=result.id,
            state="prepared",
            eligibility_snapshot_hash=eligibility_hash,
            version=1,
        )
        db.add(outcome)
        await db.flush()
    await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=outcome.id,
                item_kind="run_outcome",
                lifecycle="prepared",
                scope=_run_scope(session_id, turn_id, run_id),
                actor={"type": "runtime"},
                payload={
                    "terminal_result_id": str(result.id),
                    "eligibility_snapshot_hash": eligibility_hash,
                    "fences": fences,
                },
                content_hash=eligibility_hash,
            ),
            SessionEventDraft(
                item_id=outcome.id,
                item_kind="run_outcome",
                lifecycle="sealed",
                scope=_run_scope(session_id, turn_id, run_id),
                actor={"type": "runtime"},
                payload={
                    "terminal_result_id": str(result.id),
                    "eligibility_snapshot_hash": eligibility_hash,
                    "render_owner_id": str(_render_owner_id(result.id)),
                    "source_blocks": source_blocks,
                    "result_content_hash": result_content_hash,
                    "artifact_refs": artifact_refs,
                    "parts": artifact_parts,
                    "artifact_manifest_hash": artifact_manifest_hash,
                },
                content_hash=result_content_hash,
            ),
        ],
    )
    outcome.seal_json = {
        "outcome_id": str(outcome.id),
        "session_id": str(session_id),
        "turn_id": turn_id,
        "run_id": str(run_id),
        "terminal_result_id": str(result.id),
        "terminal_round_id": result.round_id,
        "terminal_eligibility_snapshot_hash": eligibility_hash,
        "closure_refs": {
            "tool_pair_fence": str(fences["tool_pair_generation"]),
            "input_mailbox_fence": str(fences["input_mailbox_generation"]),
            "hook_fence": str(fences["hook_generation"]),
            "compaction_fence": str(fences["compaction_generation"]),
            "cancellation_generation": int(fences["cancellation_generation"]),
        },
        "render_owner_id": str(_render_owner_id(result.id)),
        "source_blocks": source_blocks,
        "result_content_hash": result_content_hash,
        "artifact_refs": artifact_refs,
        "parts": artifact_parts,
        "artifact_manifest_hash": artifact_manifest_hash,
        "semantic_content_hash": str(result.seal_json.get("content_hash") or ""),
        "sealed_at": datetime.now(timezone.utc).isoformat(),
        "eligibility": eligibility,
        "prior_candidates": prior_candidates,
    }
    outcome.state = "sealed"
    outcome.version = int(outcome.version) + 1
    return outcome


async def commit_terminal_outcome(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    outcome_id: uuid.UUID,
) -> SessionRunOutcome:
    """Atomically publish final envelope and Run/Turn terminal facts."""

    outcome = await db.scalar(
        select(SessionRunOutcome)
        .where(
            SessionRunOutcome.id == outcome_id,
            SessionRunOutcome.tenant_id == tenant_id,
            SessionRunOutcome.session_id == session_id,
            SessionRunOutcome.run_id == run_id,
        )
        .with_for_update()
    )
    if outcome is None:
        raise TerminalOutcomeNeedsReconciliation("run outcome not found")
    if outcome.state == "terminal_committed":
        await _close_runtime_root_item(
            db,
            run_id=run_id,
            outcome=outcome,
            terminal_result_id=outcome.terminal_result_id,
            terminal_event_id=outcome.terminal_event_id,
        )
        return outcome
    if outcome.state != "sealed" or not outcome.seal_json:
        raise TerminalOutcomeIneligible("run outcome is not sealed")
    task = await db.scalar(
        select(RuntimeTask)
        .where(
            RuntimeTask.id == run_id,
            RuntimeTask.tenant_id == tenant_id,
            RuntimeTask.parent_agent_id == agent_id,
            RuntimeTask.parent_session_id == str(session_id),
        )
        .with_for_update()
    )
    if task is None:
        raise TerminalOutcomeNeedsReconciliation("runtime task missing during terminal commit")
    result = await _lock_terminal_result(
        db,
        tenant_id=tenant_id,
        run_id=run_id,
        terminal_result_id=outcome.terminal_result_id,
    )
    unresolved = await unresolved_round_obligations(db, tenant_id=tenant_id, run_id=run_id)
    current_fences = await current_run_fences(db, tenant_id=tenant_id, run_id=run_id)
    eligibility = dict(outcome.seal_json.get("eligibility") or {})
    if unresolved or dict(eligibility.get("fences") or {}) != current_fences:
        outcome.state = "failed"
        outcome.reconciliation_owner = "session_terminal_outcome:eligibility_drift"
        outcome.version = int(outcome.version) + 1
        raise TerminalOutcomeIneligible("terminal eligibility changed after outcome seal")
    try:
        current_artifact_parts = await _declared_artifact_parts(
            db,
            task=task,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
        )
    except TerminalOutcomeNeedsReconciliation as exc:
        outcome.state = "needs_reconciliation"
        outcome.reconciliation_owner = "session_terminal_outcome:artifact_authority_drift"
        outcome.version = int(outcome.version) + 1
        raise TerminalOutcomeNeedsReconciliation("terminal artifact authority changed after outcome seal") from exc
    if _sha256(current_artifact_parts) != eligibility.get("artifact_manifest_hash"):
        outcome.state = "needs_reconciliation"
        outcome.reconciliation_owner = "session_terminal_outcome:artifact_manifest_drift"
        outcome.version = int(outcome.version) + 1
        raise TerminalOutcomeNeedsReconciliation("terminal artifact authority changed after outcome seal")
    source_blocks = list(outcome.seal_json.get("source_blocks") or [])
    artifact_refs = list(outcome.seal_json.get("artifact_refs") or [])
    artifact_parts = list(outcome.seal_json.get("parts") or [])
    expected_hash = _sha256([block["content_hash"] for block in source_blocks])
    if expected_hash != outcome.seal_json.get("result_content_hash"):
        outcome.state = "needs_reconciliation"
        outcome.reconciliation_owner = "session_terminal_outcome:source_hash_mismatch"
        outcome.version = int(outcome.version) + 1
        raise TerminalOutcomeNeedsReconciliation("terminal source block hash mismatch")
    turn_id = outcome.turn_id
    semantic_content = str(result.seal_json.get("semantic_content") or "")
    events = await append_session_events(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        drafts=[
            SessionEventDraft(
                item_id=_final_item_id(outcome.id),
                item_kind="assistant_final",
                lifecycle="completed",
                scope=_round_scope(session_id, turn_id, run_id, result.round_id),
                actor={"type": "assistant", "agent_id": str(agent_id)},
                payload={
                    "zero_copy": True,
                    "outcome_id": str(outcome.id),
                    "terminal_result_id": str(result.id),
                    "render_owner_id": outcome.seal_json["render_owner_id"],
                    "source_blocks": source_blocks,
                    "result_content_hash": outcome.seal_json["result_content_hash"],
                    "artifact_refs": artifact_refs,
                    "parts": artifact_parts,
                },
                result_id=result.id,
                content_hash=str(outcome.seal_json["result_content_hash"]),
            ),
            SessionEventDraft(
                item_id=run_id,
                item_kind="run",
                lifecycle="completed",
                scope=_run_scope(session_id, turn_id, run_id),
                actor={"type": "runtime"},
                payload={"outcome_id": str(outcome.id), "terminal_result_id": str(result.id)},
            ),
            SessionEventDraft(
                item_id=_turn_item_id(session_id, turn_id),
                item_kind="turn",
                lifecycle="completed",
                scope=_turn_scope(session_id, turn_id),
                actor={"type": "runtime"},
                payload={"outcome_id": str(outcome.id), "terminal_result_id": str(result.id)},
            ),
            SessionEventDraft(
                item_id=outcome.id,
                item_kind="run_outcome",
                lifecycle="terminal_committed",
                scope=_run_scope(session_id, turn_id, run_id),
                actor={"type": "runtime"},
                payload={
                    "outcome_id": str(outcome.id),
                    "terminal_result_id": str(result.id),
                    "terminal_event_count": 4,
                },
                content_hash=str(outcome.seal_json["result_content_hash"]),
            ),
        ],
    )
    task.status = "completed"
    task.result_summary = semantic_content
    task.completed_at = datetime.now(timezone.utc)
    metadata = dict(task.metadata_json or {})
    metadata["session_v2_outcome"] = {
        "outcome_id": str(outcome.id),
        "terminal_result_id": str(result.id),
        "terminal_event_id": str(events[0].id),
        "terminal_commit_event_id": str(events[-1].id),
        "result_content_hash": outcome.seal_json["result_content_hash"],
    }
    task.metadata_json = metadata
    task.claim_version = int(task.claim_version or 0) + 1
    await _close_runtime_root_item(
        db,
        run_id=run_id,
        outcome=outcome,
        terminal_result_id=result.id,
        terminal_event_id=events[0].id,
    )
    outcome.state = "terminal_committed"
    outcome.terminal_event_id = events[0].id
    outcome.reconciliation_owner = None
    outcome.reconciliation_lease_expires_at = None
    outcome.version = int(outcome.version) + 1
    return outcome


async def recover_terminal_outcomes_once(
    db: AsyncSession,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
    run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Read-after-write recovery using the same stable outcome identity."""

    now = now or datetime.now(timezone.utc)
    candidate_statement = (
        select(SessionRunOutcome)
        .where(
            SessionRunOutcome.state.in_(("sealed", "needs_reconciliation")),
            (
                SessionRunOutcome.reconciliation_lease_expires_at.is_(None)
                | (SessionRunOutcome.reconciliation_lease_expires_at <= now)
            ),
        )
        .order_by(SessionRunOutcome.id)
        .limit(max(1, int(limit)))
        .with_for_update(skip_locked=True)
    )
    if run_id is not None:
        candidate_statement = candidate_statement.where(SessionRunOutcome.run_id == run_id)
    candidates = list((await db.execute(candidate_statement)).scalars())
    completed = failed = 0
    for outcome in candidates:
        task = await db.get(RuntimeTask, outcome.run_id)
        if task is None or task.parent_agent_id is None:
            outcome.state = "needs_reconciliation"
            outcome.reconciliation_owner = f"{worker_id}:missing_runtime_task"
            outcome.reconciliation_lease_expires_at = now + timedelta(minutes=5)
            outcome.version = int(outcome.version) + 1
            failed += 1
            continue
        outcome.reconciliation_owner = worker_id
        outcome.reconciliation_lease_expires_at = now + timedelta(minutes=5)
        try:
            await commit_terminal_outcome(
                db,
                tenant_id=outcome.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=outcome.session_id,
                run_id=outcome.run_id,
                outcome_id=outcome.id,
            )
            completed += 1
        except (TerminalOutcomeIneligible, TerminalOutcomeNeedsReconciliation):
            outcome.state = "needs_reconciliation"
            outcome.reconciliation_owner = worker_id
            outcome.reconciliation_lease_expires_at = now + timedelta(minutes=5)
            outcome.version = int(outcome.version) + 1
            failed += 1
    return {"terminal_committed": completed, "needs_reconciliation": failed}


async def recover_terminal_candidates_once(
    db: AsyncSession,
    *,
    worker_id: str,
    limit: int = 50,
    run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Close latest terminal candidates that crashed before RunOutcome creation."""

    candidate_statement = (
        select(SessionModelResult)
        .join(ChatTranscriptEvent, ChatTranscriptEvent.id == SessionModelResult.round_committed_event_id)
        .where(SessionModelResult.state == "round_committed")
        .order_by(ChatTranscriptEvent.sequence.desc())
        .limit(max(1, int(limit)) * 4)
        .with_for_update(skip_locked=True)
    )
    if run_id is not None:
        candidate_statement = candidate_statement.where(SessionModelResult.run_id == run_id)
    results = list((await db.execute(candidate_statement)).scalars())
    seen_runs: set[uuid.UUID] = set()
    completed = held = 0
    for result in results:
        if result.run_id in seen_runs or len(seen_runs) >= max(1, int(limit)):
            continue
        seen_runs.add(result.run_id)
        continuation = dict((result.seal_json or {}).get("continuation") or {})
        if continuation.get("verdict") != "terminal_candidate" or not bool(
            (result.seal_json or {}).get("logical_round_complete", True)
        ):
            continue
        existing = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == result.run_id))
        if existing is not None and existing.state == "terminal_committed":
            continue
        task = await db.get(RuntimeTask, result.run_id)
        if task is None or task.parent_agent_id is None:
            held += 1
            continue
        try:
            outcome = await prepare_and_seal_run_outcome(
                db,
                tenant_id=result.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=result.session_id,
                turn_id=result.turn_id,
                run_id=result.run_id,
                terminal_result_id=result.id,
            )
            await commit_terminal_outcome(
                db,
                tenant_id=result.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=result.session_id,
                run_id=result.run_id,
                outcome_id=outcome.id,
            )
            completed += 1
        except TerminalOutcomeIneligible:
            held += 1
    del worker_id  # reserved for structured recovery-owner telemetry
    return {"terminal_committed": completed, "held": held}


__all__ = [
    "TerminalOutcomeIneligible",
    "TerminalOutcomeNeedsReconciliation",
    "commit_terminal_outcome",
    "prepare_and_seal_run_outcome",
    "recover_terminal_candidates_once",
    "recover_terminal_outcomes_once",
]
