"""Durable Stop-hook outcome fence for committed-round recovery.

The kernel's Stop decision is authoritative continuation semantics: it may
block stopping (continue the turn), prevent continuation (return the final
state), or allow the stop.  Its governed ``command``/``http`` handlers can own
genuinely non-idempotent external effects, so a worker-restart replay of a
committed round must recover the ALREADY-COMPLETED decision instead of
re-executing its effect, and an interrupted effect must surface as a truthful
typed unknown with a reachable reconciliation path — never a blind re-run and
never a silently abandoned decision.

Storage is the existing canonical ``hook`` transcript item (boundary ``Stop``,
run scope): two lifecycles around the effect — ``started`` before any handler
runs, then ``completed``/``blocked``/``prevented``/``failed`` with the exact
decision.  Both writes go through ``append_session_events`` in their own
transaction, so the fence itself is either fully durable or raises.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from loguru import logger

STOP_HOOK_UNKNOWN = "unknown"
STOP_HOOK_COMPLETED = "completed"

# Bounded contention budget for the fence's claim compare-and-set.  The claim
# re-read never WAITS on the RuntimeTask row (SKIP LOCKED): when a concurrent
# terminal writer holds the row, the fence releases everything and retries the
# whole advisory-first sequence after a short backoff.  Settlements are short
# transactions, so a handful of sub-second attempts covers them; exhaustion
# surfaces as the typed ``claim_lock_contention`` failure with the existing
# recovery path — never a deadlock and never an unfenced governed effect.
_FENCE_CLAIM_ATTEMPTS = 6
_FENCE_CLAIM_BACKOFF_SECONDS = 0.05


class StopHookFenceUnavailable(RuntimeError):
    """The durable Stop-hook fence could not be established or read."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(f"stop_hook_fence_unavailable:{reason_code}")
        self.reason_code = str(reason_code)


def _item_id(run_id: uuid.UUID, provider_request_id: str) -> uuid.UUID:
    return uuid.uuid5(run_id, f"stop-hook-outcome:{provider_request_id}")


def _scope(session_id: str, turn_id: str, run_id: uuid.UUID) -> dict[str, str]:
    return {
        "level": "run",
        "session_id": str(session_id),
        "thread_id": str(session_id),
        "turn_id": str(turn_id),
        "run_id": str(run_id),
    }


def _decision_payload(provider_request_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "boundary": "Stop",
        "provider_request_id": str(provider_request_id),
    }
    if extra:
        payload.update(extra)
    return payload


async def _load_events(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str,
    item_id: uuid.UUID,
) -> list[Any]:
    from sqlalchemy import select

    from app.models.chat_transcript_event import ChatTranscriptEvent

    import app.services.web_chat_runtime as _runtime

    async with _runtime.tenant_scoped_session(tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.tenant_id == tenant_id,
                        ChatTranscriptEvent.session_id == uuid.UUID(str(session_id)),
                        ChatTranscriptEvent.item_id == item_id,
                        ChatTranscriptEvent.item_kind == "hook",
                    )
                    .order_by(ChatTranscriptEvent.sequence.desc())
                    .limit(2)
                )
            ).scalars()
        )
    return rows


async def load_stop_hook_outcome(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_id: str,
    run_id: uuid.UUID,
    provider_request_id: str,
) -> dict[str, Any] | None:
    """Return the durable Stop decision for one committed round.

    ``None`` — the Stop boundary never started under this round identity.
    ``{"state": "unknown"}`` — it started but never completed (interrupted
    non-idempotent effect; recovery must not re-run it blindly).
    ``{"state": "completed", "decision": {...}}`` — the exact durable
    ``block``/``prevent_continuation``/``reason`` semantics to replay.
    """

    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    rows = await _load_events(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        item_id=_item_id(run_id, provider_request_id),
    )
    if not rows:
        return None
    latest = rows[0]
    if str(latest.lifecycle) == "started":
        return {"state": STOP_HOOK_UNKNOWN}
    if str(latest.lifecycle) in {"completed", "blocked", "prevented", "failed", "cancelled"}:
        payload = dict((latest.metadata_json or {}).get("v2_payload") or {})
        return {
            "state": STOP_HOOK_COMPLETED,
            "decision": dict(payload.get("decision") or {}),
        }
    return None


async def record_stop_hook_started(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_id: str,
    run_id: uuid.UUID,
    provider_request_id: str,
) -> None:
    """Persist the pre-effect fence: a governed Stop effect is about to run."""

    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    import app.services.web_chat_runtime as _runtime

    async with _runtime.tenant_scoped_session(tenant_id) as db:
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=uuid.UUID(str(session_id)),
            drafts=[
                SessionEventDraft(
                    item_id=_item_id(run_id, provider_request_id),
                    item_kind="hook",
                    lifecycle="started",
                    scope=_scope(session_id, turn_id, run_id),
                    actor={"type": "hook"},
                    payload=_decision_payload(provider_request_id),
                )
            ],
        )
        await db.commit()


def stop_hook_outcome_lifecycle(decision: dict[str, Any]) -> str:
    if decision.get("prevent_continuation"):
        return "prevented"
    if decision.get("block"):
        return "blocked"
    return "completed"


async def record_stop_hook_completed(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_id: str,
    run_id: uuid.UUID,
    provider_request_id: str,
    decision: dict[str, Any],
) -> None:
    """Persist the completed Stop decision (block / prevent / allow)."""

    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(agent_id)
    import app.services.web_chat_runtime as _runtime

    lifecycle = stop_hook_outcome_lifecycle(decision)
    async with _runtime.tenant_scoped_session(tenant_id) as db:
        await append_session_events(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=uuid.UUID(str(session_id)),
            drafts=[
                SessionEventDraft(
                    item_id=_item_id(run_id, provider_request_id),
                    item_kind="hook",
                    lifecycle=lifecycle,
                    scope=_scope(session_id, turn_id, run_id),
                    actor={"type": "hook"},
                    payload=_decision_payload(
                        provider_request_id,
                        {
                            "decision": {
                                "block": bool(decision.get("block")),
                                "prevent_continuation": bool(decision.get("prevent_continuation")),
                                "reason": str(decision.get("reason") or ""),
                                "stop_reason": str(decision.get("stop_reason") or ""),
                            }
                        },
                    ),
                )
            ],
        )
        await db.commit()


def stop_fence_identity(
    *,
    agent_id: uuid.UUID | None,
    session_id: str | None,
    turn_id: str | None,
    provider_request_id: str | None,
) -> dict[str, Any] | None:
    """Resolve the exact durable identity for one round's Stop boundary.

    Requires the full authority chain (agent, session, turn) plus the round's
    durable provider request id; the run id is the authoritative runtime task
    id when present, else parsed from the ``hive:{run_id}:...`` request id.
    ``None`` means this caller has no exact identity and keeps legacy fencing
    behavior (disclosed, never silently weaker for identified sessions).
    """

    if agent_id is None or not session_id or not turn_id or not provider_request_id:
        return None
    run_id: uuid.UUID | None = None
    raw = str(provider_request_id)
    if raw.startswith("hive:"):
        parts = raw.split(":")
        if len(parts) >= 3:
            try:
                run_id = uuid.UUID(parts[1])
            except (TypeError, ValueError):
                run_id = None
    if run_id is None:
        return None
    return {
        "agent_id": agent_id,
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "run_id": run_id,
        "provider_request_id": raw,
    }


async def _safe_load(identity: dict[str, Any]) -> str | dict[str, Any] | None:
    try:
        return await load_stop_hook_outcome(**identity)
    except Exception as exc:  # fence read failure must fail closed below
        logger.warning("[StopHookFence] load failed for round {}: {}", identity["provider_request_id"], exc)
        raise StopHookFenceUnavailable("load_failed") from exc


async def _record_started_fence_if_claim_current(
    identity: dict[str, Any],
    attempt_owner: str | None,
) -> bool:
    """Write the pre-effect fence only when the caller still owns the claim.

    A plain claim SELECT followed by an append is NOT atomic ownership: a
    concurrent reclaim can commit BETWEEN the read and the fence append, and
    the stale worker still arms the consequential effect.  The fence here is
    a compare-and-set in the repository's canonical lock order — session
    advisory first, RuntimeTask row second (``chat_transcript`` documents
    advisory-before-row as the global order; ``commit_terminal_outcome`` and
    the web-chat terminal writer already follow it):

    1. take the session advisory lock (``lock_transcript_session``);
    2. re-read the claim with ``FOR NO KEY UPDATE SKIP LOCKED`` — the lock
       serializes against the reclaim path (its ``FOR UPDATE SKIP LOCKED``
       claim defers this row) and blocks any claim-field UPDATE until commit,
       while NEVER waiting on a writer that already holds the row;
    3. compare ``claim_version:attempt_count`` with the caller's fencing
       suffix — a mismatch (the reclaim already committed) rolls the whole
       transaction back, so no stale fence row survives;
    4. append the ``started`` draft (the advisory is reentrant) and commit.

    Because the transaction never waits on the RuntimeTask row while holding
    the advisory, it cannot form a lock cycle with terminal writers of either
    order (a plain metadata flush, or a caller holding a full ``FOR UPDATE``
    before shared settlement): a busy row only costs a bounded retry of this
    whole sequence.  Returns ``True`` when the fence was written; ``False``
    when the claim was superseded or the task row is gone.
    """

    from sqlalchemy import select

    from app.models.runtime_task import RuntimeTask
    from app.services.chat_transcript import lock_transcript_session
    from app.services.session_v2_persistence import SessionEventDraft, append_session_events
    from app.services.tenant_resolver import resolve_tenant_for_agent

    tenant_id = await resolve_tenant_for_agent(identity["agent_id"])
    import app.services.web_chat_runtime as _runtime

    def _claim_current(task: RuntimeTask) -> bool:
        if not attempt_owner:
            # Owners without the ``{worker}:{version}:{attempt}`` fencing
            # suffix cannot be checked and keep legacy behavior.
            return True
        suffix = str(attempt_owner).rsplit(":", 2)
        if len(suffix) != 3 or not suffix[1].isdigit() or not suffix[2].isdigit():
            return True
        return f"{suffix[1]}:{suffix[2]}" == f"{task.claim_version}:{task.attempt_count}"

    backoff = _FENCE_CLAIM_BACKOFF_SECONDS
    for _attempt in range(_FENCE_CLAIM_ATTEMPTS):
        async with _runtime.tenant_scoped_session(tenant_id) as db:
            await lock_transcript_session(db, session_id=uuid.UUID(str(identity["session_id"])))
            task = (
                await db.execute(
                    select(RuntimeTask)
                    .where(RuntimeTask.id == identity["run_id"])
                    .with_for_update(key_share=True, skip_locked=True)
                )
            ).scalar_one_or_none()
            if task is None:
                # SKIP LOCKED returns no row while a concurrent writer holds
                # it; distinguish that from a genuinely missing task row with
                # a plain (never-waiting) read before retrying.
                row_exists = await db.scalar(select(RuntimeTask.id).where(RuntimeTask.id == identity["run_id"]))
                await db.rollback()
                if row_exists is None:
                    return False
            else:
                if not _claim_current(task):
                    await db.rollback()
                    return False
                await append_session_events(
                    db,
                    tenant_id=tenant_id,
                    agent_id=identity["agent_id"],
                    session_id=uuid.UUID(str(identity["session_id"])),
                    drafts=[
                        SessionEventDraft(
                            item_id=_item_id(identity["run_id"], identity["provider_request_id"]),
                            item_kind="hook",
                            lifecycle="started",
                            scope=_scope(identity["session_id"], identity["turn_id"], identity["run_id"]),
                            actor={"type": "hook"},
                            payload=_decision_payload(identity["provider_request_id"]),
                        )
                    ],
                )
                await db.commit()
                return True
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 1.0)
    raise StopHookFenceUnavailable("claim_lock_contention")


async def recover_or_fence_stop_boundary(
    *,
    agent_id: uuid.UUID | None,
    session_id: str | None,
    turn_id: str | None,
    provider_request_id: str | None,
    governed_stop_hooks_registered: bool,
    attempt_owner: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Recover a completed Stop decision or establish the pre-effect fence.

    Returns ``(mode, outcome)``:

    * ``("emit", None)`` — no durable outcome; caller must run the governed
      handlers.  When governed Stop handlers exist, the ``started`` fence has
      been committed first, and the caller MUST record the completed decision
      through :func:`complete_stop_boundary` afterwards.
    * ``("recovered", decision)`` — a completed durable decision; reuse its
      block/prevent/allow semantics without re-running any handler.
    * ``("unknown", None)`` — an interrupted non-idempotent Stop effect; the
      caller must surface a truthful typed unknown (never a blind re-run).

    Raises :class:`StopHookFenceUnavailable` when governed Stop handlers exist
    and the durable fence cannot be read or established, or when the calling
    worker's claim was superseded: a non-idempotent effect must not run
    unfenced or from a stale owner (denial stays local to this effect
    boundary).  With NO governed Stop handler the emit can own no consequential
    effect, so a fence read outage degrades to an unfenced read-only attempt —
    a recoverable prior decision is still honored when the read succeeds, and
    a read failure must not fail the already-committed turn.
    """

    identity = stop_fence_identity(
        agent_id=agent_id,
        session_id=session_id,
        turn_id=turn_id,
        provider_request_id=provider_request_id,
    )
    if identity is None:
        return "emit", None
    prior: dict[str, Any] | None = None
    try:
        prior = await _safe_load(identity)
    except StopHookFenceUnavailable:
        if governed_stop_hooks_registered:
            raise
        # Zero governed Stop handlers: the emit owns no consequential effect,
        # so losing the (read-only) recovery lookup cannot strand the turn.
        prior = None
    if prior is not None and prior.get("state") == STOP_HOOK_COMPLETED:
        return "recovered", dict(prior.get("decision") or {})
    if prior is not None and prior.get("state") == STOP_HOOK_UNKNOWN:
        # A ``started`` fence row proves a governed effect began; its outcome
        # stays unknown even if the registry no longer holds the handler.
        return "unknown", None
    if not governed_stop_hooks_registered:
        # No governed Stop handler exists: the emit cannot own a consequential
        # effect, so no fence row (and no ambiguous window) is created.
        return "emit", None
    try:
        claim_current = await _record_started_fence_if_claim_current(identity, attempt_owner)
    except StopHookFenceUnavailable:
        # Typed fence failures (including the bounded claim_lock_contention
        # budget) keep their exact reason code for recovery routing.
        raise
    except Exception as exc:
        logger.warning(
            "[StopHookFence] failed to persist started fence for round {}: {}",
            identity["provider_request_id"],
            exc,
        )
        raise StopHookFenceUnavailable("started_fence_write_failed") from exc
    if not claim_current:
        # The calling worker's lease was superseded: the new claim owner owns
        # this round's consequential Stop effect, not this worker.
        raise StopHookFenceUnavailable("claim_superseded")
    return "emit", None


async def complete_stop_boundary(
    *,
    agent_id: uuid.UUID | None,
    session_id: str | None,
    turn_id: str | None,
    provider_request_id: str | None,
    decision: dict[str, Any],
) -> None:
    """Record the completed Stop decision durably (best effort post-effect)."""

    identity = stop_fence_identity(
        agent_id=agent_id,
        session_id=session_id,
        turn_id=turn_id,
        provider_request_id=provider_request_id,
    )
    if identity is None:
        return
    try:
        await record_stop_hook_completed(**identity, decision=dict(decision))
    except Exception as exc:
        logger.warning(
            "[StopHookFence] failed to persist completed decision for round {}: {}",
            identity["provider_request_id"],
            exc,
        )


__all__ = [
    "STOP_HOOK_COMPLETED",
    "STOP_HOOK_UNKNOWN",
    "StopHookFenceUnavailable",
    "complete_stop_boundary",
    "load_stop_hook_outcome",
    "record_stop_hook_completed",
    "record_stop_hook_started",
    "recover_or_fence_stop_boundary",
    "stop_fence_identity",
]
