"""Production workflow resume daemon.

This is intentionally a thin shell: the service owns run semantics, the signal
consumer owns PG Signal matching, and the leaf executor still resolves to the
normal ``spawn_subagent`` path. The daemon just makes those durable mechanisms
run continuously in production instead of existing only as test helpers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import get_settings
from app.runtime.workflow_engine import LeafExecutor
from app.services.subagent_wake_consumer import ParentWakeInvoker, drain_subagent_completion_wakes
from app.services.workflow_launch import build_resumable_workflow_leaf_executor
from app.services.workflow_runtime_service import WorkflowRuntimeService
from app.services.workflow_signal_consumer import drain_signal_resumes

logger = logging.getLogger(__name__)

_DEFAULT_WORKFLOW_SERVICE = WorkflowRuntimeService()


def get_default_workflow_service() -> WorkflowRuntimeService:
    return _DEFAULT_WORKFLOW_SERVICE


def request_default_workflow_drain() -> None:
    _DEFAULT_WORKFLOW_SERVICE.request_drain()


async def workflow_daemon_tick(
    *,
    service: WorkflowRuntimeService,
    leaf_executor: LeafExecutor,
    session_factory: Any = None,
    subagent_wake_invoker: ParentWakeInvoker | None = None,
) -> dict[str, int]:
    resumed = await service.resume_pending_runs(leaf_executor=leaf_executor)
    signal_resumed = await drain_signal_resumes(
        leaf_executor=leaf_executor,
        service=service,
        session_factory=session_factory,
    )
    subagent_wakes = await drain_subagent_completion_wakes(
        session_factory=session_factory,
        invoke_parent=subagent_wake_invoker,
    )
    return {
        "resumed_runs": len(resumed),
        "signal_resumed_runs": len(signal_resumed),
        "subagent_woken_parents": len([wake for wake in subagent_wakes if wake.status == "woken"]),
    }


async def start_workflow_daemon(
    *,
    service: WorkflowRuntimeService | None = None,
    leaf_executor: LeafExecutor | None = None,
    interval_seconds: float | None = None,
    subagent_wake_invoker: ParentWakeInvoker | None = None,
) -> None:
    runtime_service = service or get_default_workflow_service()
    runtime_service.clear_drain()
    executor = leaf_executor or build_resumable_workflow_leaf_executor()
    interval = interval_seconds
    if interval is None:
        interval = float(getattr(get_settings(), "WORKFLOW_DAEMON_INTERVAL_SECONDS", 15))

    try:
        while True:
            tick_kwargs: dict[str, Any] = {"service": runtime_service, "leaf_executor": executor}
            if subagent_wake_invoker is not None:
                tick_kwargs["subagent_wake_invoker"] = subagent_wake_invoker
            result = await workflow_daemon_tick(**tick_kwargs)
            if result["resumed_runs"] or result["signal_resumed_runs"] or result["subagent_woken_parents"]:
                logger.info("[WorkflowDaemon] tick: %s", result)
            await asyncio.sleep(max(interval, 0.1))
    except asyncio.CancelledError:
        runtime_service.request_drain()
        raise
