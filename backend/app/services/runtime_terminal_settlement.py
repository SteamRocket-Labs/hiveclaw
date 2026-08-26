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

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime_task import RuntimeTask

TERMINAL_SETTLEMENT_STATUSES = frozenset({"completed", "failed", "killed", "skipped", "needs_reconciliation"})


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
) -> str:
    """Stamp the terminal fence, transition the root item, settle controls.

    ``task`` is the caller-mutated instance inside the still-open transaction.
    The fence ref is reused when the task already carries one, so repeated
    settlement of the same terminal lifecycle is idempotent. Returns the
    effective fence ref.
    """

    if str(task.status or "") not in TERMINAL_SETTLEMENT_STATUSES:
        raise ValueError("terminal_runtime_task_status_required")

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

    if getattr(task, "root_runtime_task_id", None) is not None:
        from app.services.runtime_root_ledger import transition_runtime_root_item_by_task

        await transition_runtime_root_item_by_task(
            db,
            runtime_task_id=task.id,
            requested_state=str(task.status),
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
    return terminal_fence


__all__ = [
    "TERMINAL_SETTLEMENT_STATUSES",
    "settle_runtime_task_terminal",
]
