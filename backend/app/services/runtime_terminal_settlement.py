"""Shared mechanical RuntimeTask terminal settlement (RC-10A).

Every durable terminal writer — the web-chat lifecycle owner, the canonical
ambiguous-provider-send commit in ``session_model_round``, operator
reconciliation actions, and the exact projection-recovery sweep — stamps the
same terminal execution fence, transitions the runtime root item, and settles
pending session controls through this one mechanical boundary. Callers own
the semantic field mutations that precede it; nothing here decides a status.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask

TERMINAL_SETTLEMENT_STATUSES = frozenset({"completed", "failed", "killed", "skipped", "needs_reconciliation"})

# Bounded retry rounds for the advisory-first batch lock below when a task is
# admitted between the unlocked prescan and the FOR UPDATE batch. Each round
# rolls back to a savepoint (releasing only that round's rows and advisories)
# and rescans, so the newly admitted task's session is included in the next
# round's advisory set. Persistent admission pressure ends in the typed,
# retryable exception below — never in a row → advisory acquisition.
_RUNTIME_TASK_LATE_ADMISSION_ROUNDS = 4


class RuntimeTaskLateAdmissionConflict(Exception):
    """Tasks kept arriving while a batch terminal writer was locking rows.

    The transaction must roll back and be retried; acquiring the new sessions'
    advisories under already-held RuntimeTask row locks would be the row →
    advisory side of the global order and can deadlock an in-flight
    transcript append of that session.
    """


class RuntimeTaskSessionBindingConflict(Exception):
    """A RuntimeTask's session binding changed across the advisory pre-read.

    ``parent_session_id`` is set at admission and never mutated, so this is
    unreachable through supported paths; raising (and rolling back) keeps the
    advisory → row order an invariant instead of an assumption if that ever
    changes.
    """


async def lock_runtime_task_with_session_authority(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
) -> RuntimeTask | None:
    """Lock ONE RuntimeTask row in the canonical advisory → row order.

    Single-task terminal writers (``update_runtime_task_record``, workflow
    kill/finalize, business-task finalization, ...) previously took the
    ``FOR UPDATE`` row first and reached this module's entry advisory (or
    ``append_session_events``' own advisory) afterwards — the exact edge that
    kills a concurrent transcript append of the same session. This helper
    reads the row's immutable session binding unlocked, acquires that session
    advisory, and only then takes the row lock.
    """

    from app.services.chat_transcript import lock_transcript_session

    binding = (
        await db.execute(
            select(RuntimeTask.parent_session_id, RuntimeTask.parent_agent_id).where(RuntimeTask.id == task_id)
        )
    ).first()
    session_id_raw = str(binding.parent_session_id or "") if binding is not None else ""
    if binding is not None and session_id_raw and binding.parent_agent_id is not None:
        await lock_transcript_session(db, session_id=uuid.UUID(session_id_raw))
    task = (
        await db.execute(select(RuntimeTask).where(RuntimeTask.id == task_id).with_for_update())
    ).scalar_one_or_none()
    if task is not None and session_id_raw and str(getattr(task, "parent_session_id", "") or "") != session_id_raw:
        raise RuntimeTaskSessionBindingConflict(f"runtime task {task_id} session binding changed under lock")
    return task


async def lock_runtime_task_batch_with_session_authority(
    db: AsyncSession,
    *,
    statement,
    rounds: int = _RUNTIME_TASK_LATE_ADMISSION_ROUNDS,
    skip_locked: bool = False,
) -> list[RuntimeTask]:
    """Lock a batch of RuntimeTask rows in the canonical advisory → row order.

    ``statement`` is a ``select(RuntimeTask)`` with the caller's filters (it
    may carry its own ``order_by``/``limit``; ``with_for_update`` is applied
    here). Each round: prescan the matching rows' ids and session bindings
    unlocked, acquire every session advisory (canonical sorted order), take
    the ``FOR UPDATE`` batch inside a savepoint, and compare the locked ids
    against the prescan ids. A row that only appeared AFTER the prescan (a
    task admitted in between) is not covered by a held advisory, so the round
    rolls back to the savepoint — releasing exactly that round's rows and
    advisories — and rescans; the committed newcomer is then in the next
    round's prescan and advisory set. Nothing is ever settled while an
    uncovered session exists, and no advisory is ever requested while a row
    lock is held.

    ``skip_locked=True`` keeps a sweep's non-blocking semantics: rows
    currently locked by another writer are skipped this pass (they are
    already covered by that writer's own terminal transaction) instead of
    blocking the sweep; the locked set then only ever shrinks relative to
    the prescan, which never triggers a restart round.

    Freshness contract across a restart round: the caller holds NO row lock
    between the rollback and the re-lock, so a concurrent transaction may
    commit changes to these rows in that window. SQLAlchemy's nested
    rollback expires only instances it considers dirty; rows that were
    merely LOADED in the rolled-back round stay in the identity map, and a
    plain re-SELECT returns those stale instances instead of the committed
    values. The locked re-read therefore runs with
    ``populate_existing=True`` so every loaded attribute is refreshed from
    the row the lock actually covers. This is deliberately NOT a blanket
    ``session.refresh``/expire-all: unflushed caller-side mutations are
    flushed by the prescan's autoflush BEFORE the savepoint, so valid caller
    writes survive the restart, while the read state the settlement decides
    on is the post-restart committed truth.
    """

    from app.services.chat_transcript import lock_transcript_sessions

    prescan = statement.with_only_columns(RuntimeTask.id, RuntimeTask.parent_session_id, RuntimeTask.parent_agent_id)
    for _ in range(max(1, int(rounds))):
        bindings = (await db.execute(prescan)).all()
        prescan_ids = {row.id for row in bindings}
        nested = await db.begin_nested()
        await lock_transcript_sessions(
            db,
            session_ids=[
                row.parent_session_id for row in bindings if row.parent_session_id and row.parent_agent_id is not None
            ],
        )
        locked_statement = statement.with_for_update(skip_locked=skip_locked).execution_options(populate_existing=True)
        rows = list((await db.execute(locked_statement)).scalars().all())
        if all(row.id in prescan_ids for row in rows):
            await nested.commit()
            return rows
        await nested.rollback()
    raise RuntimeTaskLateAdmissionConflict(
        "runtime tasks were admitted faster than the batch terminal writer could lock them"
    )


def _terminal_fence_ref(task: RuntimeTask) -> str:
    fence_payload = {
        "run_id": str(task.id),
        "status": task.status,
        "claim_version": int(getattr(task, "claim_version", 0) or 0),
        "completed_at": (task.completed_at.isoformat() if task.completed_at is not None else None),
    }
    fence_sha = hashlib.sha256(
        json.dumps(fence_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"runtime-task-terminal:{fence_sha}"


async def settle_runtime_task_terminal(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    terminal_source: str,
    root_reason_code: str | None = None,
    root_state: str | None = None,
    settle_root: bool = True,
) -> str:
    """Stamp the terminal fence, transition the root item, settle controls.

    ``task`` is the caller-mutated instance inside the still-open transaction.
    The fence ref is reused when the task already carries one, so repeated
    settlement of the same terminal lifecycle is idempotent. Returns the
    effective fence ref.
    """

    if str(task.status or "") not in TERMINAL_SETTLEMENT_STATUSES:
        raise ValueError("terminal_runtime_task_status_required")

    # Canonical advisory-first terminal path: the session advisory is the
    # first mutation authority for a session (see ``chat_transcript``).  A
    # caller that reaches this boundary with dirty RuntimeTask state but the
    # row lock still un-taken must acquire the advisory BEFORE the metadata
    # flush below takes that row lock — flushing first would put this shared
    # writer on the inverse (row → advisory) side of the global order and can
    # deadlock against any concurrent transcript append.  The advisory is
    # transaction-scoped and reentrant, so callers that already hold it (the
    # web-chat terminal writer, ``commit_terminal_outcome``) are unaffected,
    # and ``lock_transcript_session`` is autoflush-guarded, so no dirty row
    # can be flushed ahead of its lock here either.
    session_id_raw = str(getattr(task, "parent_session_id", "") or "")
    if session_id_raw and getattr(task, "parent_agent_id", None) is not None:
        from app.services.chat_transcript import lock_transcript_session

        await lock_transcript_session(db, session_id=uuid.UUID(session_id_raw))

    from app.services.runtime_budget_service import (
        runtime_task_outer_budget_actuals,
        stamp_runtime_task_budget_actuals,
    )

    outer_actuals = runtime_task_outer_budget_actuals(task)
    if outer_actuals:
        stamp_runtime_task_budget_actuals(task, outer_actuals)
    metadata = dict(task.metadata_json or {})
    existing_fence = str(metadata.get("terminal_execution_fence_ref") or "")
    existing_committed_status = str(metadata.get("terminal_committed_status") or "")
    # A fence belongs to one terminal lifecycle. Reuse it only when the task
    # settles the SAME committed status (projection repair, idempotent
    # re-settlement); a real status transition — for example the operator
    # moving needs_reconciliation to completed or killed — generates a new
    # fence and records the new committing source. A same-status repair with
    # an existing source preserves that original provenance; the repair
    # records its own provenance separately.
    same_status_settlement = bool(existing_fence) and existing_committed_status == str(task.status)
    terminal_fence = existing_fence if same_status_settlement else _terminal_fence_ref(task)
    if not (same_status_settlement and str(metadata.get("terminal_commit_source") or "")):
        metadata["terminal_commit_source"] = str(terminal_source)
    metadata.update(
        {
            "terminal_execution_fence_ref": terminal_fence,
            "terminal_committed_status": task.status,
        }
    )
    task.metadata_json = metadata
    await db.flush()

    if settle_root and getattr(task, "root_runtime_task_id", None) is not None:
        from app.services.runtime_root_ledger import transition_runtime_root_item_by_task

        await transition_runtime_root_item_by_task(
            db,
            runtime_task_id=task.id,
            requested_state=str(root_state or task.status),
            reason_code=str(root_reason_code or f"runtime_task_terminal:{terminal_source}"),
            result_refs=(f"runtime-task://{task.id}",),
            metadata={"terminal_execution_fence_ref": terminal_fence},
        )

    session_id_raw = str(getattr(task, "parent_session_id", "") or "")
    if session_id_raw and getattr(task, "parent_agent_id", None) is not None:
        from app.services.session_control_input import settle_pending_controls_for_run

        await settle_pending_controls_for_run(
            db,
            task=task,
            execution_fence_ref=terminal_fence,
            terminal_source=str(terminal_source),
        )
    # Callers still own commit, but no lease renewal may race the prepared
    # terminal transaction after every shared mechanical write has succeeded.
    from app.services.runtime_task_fence import finish_current_runtime_task_claim

    finish_current_runtime_task_claim(task_id=task.id)
    return terminal_fence


async def settle_and_enqueue_runtime_task_terminal(
    db: AsyncSession,
    task: RuntimeTask,
    *,
    terminal_source: str,
    root_reason_code: str | None = None,
    root_state: str | None = None,
    settle_root: bool = True,
) -> str:
    """Commit the mechanical terminal evidence and required outbox atomically."""

    terminal_fence = await settle_runtime_task_terminal(
        db,
        task,
        terminal_source=terminal_source,
        root_reason_code=root_reason_code,
        root_state=root_state,
        settle_root=settle_root,
    )
    if task.terminal_boundary_generation is None:
        return terminal_fence

    from app.services.web_chat_runtime import EXECUTABLE_CHAT_TASK_TYPES

    if task.task_type in EXECUTABLE_CHAT_TASK_TYPES:
        if task.tenant_id is None or task.parent_agent_id is None or not str(task.parent_session_id or "").strip():
            raise ValueError("web_terminal_runtime_authority_required")
        session_id = uuid.UUID(str(task.parent_session_id))
        # Transcript lifecycle vocabulary: the session event contract has no
        # ``run.killed``; the product's canonical phase mapping (see
        # ``_phase_for_terminal_status``) presents a killed run as cancelled,
        # so the terminal transcript event uses that same legal lifecycle
        # while the task row keeps its exact ``killed`` status.
        terminal_lifecycle = "cancelled" if str(task.status) == "killed" else str(task.status)
        event_type = f"run.{terminal_lifecycle}"
        terminal_event = await db.scalar(
            select(ChatTranscriptEvent.id)
            .where(
                ChatTranscriptEvent.tenant_id == task.tenant_id,
                ChatTranscriptEvent.agent_id == task.parent_agent_id,
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.run_id == task.id,
                ChatTranscriptEvent.event_type == event_type,
            )
            .limit(1)
        )
        if terminal_event is None:
            from app.services.session_v2_persistence import SessionEventDraft, append_session_events

            metadata = dict(task.metadata_json or {})
            turn_id = str(metadata.get("turn_id") or f"turn-{task.id.hex}")
            await append_session_events(
                db,
                tenant_id=task.tenant_id,
                agent_id=task.parent_agent_id,
                session_id=session_id,
                drafts=[
                    SessionEventDraft(
                        item_id=task.id,
                        item_kind="run",
                        lifecycle=terminal_lifecycle,
                        scope={
                            "level": "run",
                            "session_id": str(session_id),
                            "thread_id": str(session_id),
                            "turn_id": turn_id,
                            "run_id": str(task.id),
                        },
                        actor={"type": "runtime"},
                        payload={"reason_code": str(root_reason_code or terminal_source)},
                    )
                ],
            )
    from app.services.runtime_terminal_boundary_outbox import enqueue_required_terminal_boundary_for_task

    await enqueue_required_terminal_boundary_for_task(db, task)
    return terminal_fence


__all__ = [
    "TERMINAL_SETTLEMENT_STATUSES",
    "RuntimeTaskLateAdmissionConflict",
    "RuntimeTaskSessionBindingConflict",
    "lock_runtime_task_batch_with_session_authority",
    "lock_runtime_task_with_session_authority",
    "settle_and_enqueue_runtime_task_terminal",
    "settle_runtime_task_terminal",
]
