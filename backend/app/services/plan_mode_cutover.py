"""Plan Mode cutover — grandfather pre-existing triggers (§9.0 / §9.3).

When the Plan Mode fail-closed backstop (``trigger_preflight`` / wake reconciler)
goes live, every *autonomous* trigger that cannot prove it came from a confirmed
plan would otherwise be quarantined. Triggers created before Plan Mode existed
have no ``config.plan_id``, so a naive cutover would silently disable historical
automation the user still relies on.

Compatibility-mode cutover (§9.0) avoids that: this one-shot operation stamps
every currently-enabled trigger with
``config.metadata.plan_exempt_reason="preexisting_before_cutover"``. The gate
then allows those grandfathered triggers (the exemption path) while still
fail-closing any *new* autonomous trigger that lacks a confirmed plan.

Idempotent and surgical:

* Triggers already tracing to a confirmed plan (``config.plan_id``) are skipped —
  they need no exemption.
* Triggers already carrying any ``plan_exempt_reason`` are skipped.
* Only ``config.metadata.plan_exempt_reason`` is written; all other config /
  metadata keys are preserved.

This is an operator action (call it once at cutover, optionally per agent), not a
daemon loop. It is intentionally *not* wired into startup so strict-mode
deployments can choose to quarantine instead.
"""

from __future__ import annotations

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.database import async_session, enter_rls_bypass
from app.models.trigger import AgentTrigger
from app.services import plan_mode_core


def _config(trigger: Any) -> dict[str, Any]:
    return dict(getattr(trigger, "config", None) or {})


def _needs_exemption(trigger: Any) -> bool:
    """True when a trigger should be grandfathered (no plan, no existing exemption)."""
    cfg = _config(trigger)
    if str(cfg.get("plan_id") or "").strip():
        return False  # already traces to a confirmed plan
    if plan_mode_core.extract_plan_exempt_reason({"config": cfg}) is not None:
        return False  # already exempt — idempotent
    return True


def stamp_exemption(trigger: Any, *, reason: str) -> bool:
    """Stamp ``config.metadata.plan_exempt_reason`` on ``trigger`` in place.

    Returns ``True`` when the trigger was modified, ``False`` when it already had
    a plan/exemption and was left untouched. Reassigns ``config`` wholesale so the
    JSONB column change is detected by SQLAlchemy.
    """
    if not _needs_exemption(trigger):
        return False
    cfg = _config(trigger)
    metadata = dict(cfg.get("metadata") or {})
    metadata["plan_exempt_reason"] = reason
    cfg["metadata"] = metadata
    trigger.config = cfg
    return True


async def mark_existing_triggers_plan_exempt(
    *,
    agent_id: uuid.UUID | None = None,
    reason: str = plan_mode_core.PLAN_EXEMPT_PREEXISTING,
    commit: bool = True,
) -> dict[str, Any]:
    """Grandfather pre-existing enabled triggers for Plan Mode cutover (§9.0).

    Args:
        agent_id: Optionally scope the cutover to a single agent; ``None`` covers
            every enabled trigger in the deployment.
        reason: The exemption reason to stamp (defaults to the canonical
            ``preexisting_before_cutover``).
        commit: Whether to commit (set ``False`` to run inside a caller's
            transaction / dry-run accounting).

    Returns:
        ``{"checked", "stamped", "skipped", "agent_id"}`` accounting.
    """
    # Operator cutover deliberately spans every tenant (agent_id=None grandfathers
    # all enabled triggers across the deployment), so it cannot run under a single
    # tenant's GUC — open an audited cross-tenant bypass.
    async with (
        async_session() as db,
        enter_rls_bypass(db, reason="plan-mode cutover grandfather (cross-tenant trigger exemption)") as bypass_db,
    ):
        stmt = select(AgentTrigger).where(AgentTrigger.is_enabled.is_(True))
        if agent_id is not None:
            stmt = stmt.where(AgentTrigger.agent_id == agent_id)
        result = await bypass_db.execute(stmt)
        triggers = list(result.scalars().all())

        stamped = 0
        for trigger in triggers:
            if stamp_exemption(trigger, reason=reason):
                stamped += 1

        if commit and stamped:
            await bypass_db.commit()

    report = {
        "agent_id": str(agent_id) if agent_id is not None else None,
        "checked": len(triggers),
        "stamped": stamped,
        "skipped": len(triggers) - stamped,
    }
    logger.info(
        "plan_mode_cutover_marked_exempt",
        **{k: v for k, v in report.items() if v is not None},
    )
    return report


__all__ = ["mark_existing_triggers_plan_exempt", "stamp_exemption"]
