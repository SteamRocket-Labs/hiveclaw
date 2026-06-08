"""Plan Mode detached-background handoff — explicit fail-closed stub.

CC-align §4.3 + review amendment ②: a confirmed plan whose user explicitly asked
for background / offline / "run it and notify me" execution carries the
``detached_runtime_task`` target. The full detached runtime (a dedicated
``RuntimeTask`` + Work Ledger + cancellation endpoint + completion-notification
policy) is **net-new infrastructure that is not on the acceptance gate** (both
acceptance cases continue in the current session), so it is deliberately deferred.

What ships now is the **fail-closed stub** that §8.5 requires ("every target has a
handler or an explicit fail-closed UX"): instead of the old silent
``no_handler_registered`` → ``skipped`` (which left the agent claiming "not
confirmed"), this records ``handoff_status="failed"`` with a clear, actionable
reason. The frontend surfaces the reason; the user can re-run in the current
session or pick a scheduled/delegated target.

When the real detached runtime is built, replace this handler — the registry seam
(:func:`register_detached_runtime_task_handoff`) stays the same.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DETACHED_TARGET = "detached_runtime_task"

_NOT_AVAILABLE_MESSAGE = (
    "后台/离线执行（detached_runtime_task）暂未开放：当前版本只支持在当前对话中执行已确认的计划。"
    "请在当前会话中重新确认执行，或改用定时（objective_trigger）/ 委派（delegation）方式。"
)


class DetachedRuntimeNotAvailable(Exception):
    """Detached background execution is not yet available — fail closed, with a
    visible reason, instead of the old silent ``skipped``."""


async def detached_runtime_task_handoff(db: Any, plan: Any) -> dict[str, Any]:
    """Fail-closed stub for the detached-background target (see module docstring)."""
    logger.info(
        "plan_detached_handoff_unavailable",
        extra={"plan_id": str(getattr(plan, "id", None)), "target": DETACHED_TARGET},
    )
    raise DetachedRuntimeNotAvailable(_NOT_AVAILABLE_MESSAGE)


def register_detached_runtime_task_handoff(service: Any) -> None:
    """Register the detached-background fail-closed stub on a ``PlanModeService``."""
    service.register_handoff_handler(DETACHED_TARGET, detached_runtime_task_handoff)
