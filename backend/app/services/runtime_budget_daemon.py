"""Runtime budget maintenance daemon."""

from __future__ import annotations

import asyncio

from loguru import logger

from app.services.runtime_budget_service import RuntimeBudgetService

_DEFAULT_INTERVAL_SECONDS = 60


async def runtime_budget_maintenance_tick(service: RuntimeBudgetService | None = None) -> dict[str, int]:
    budget_service = service or RuntimeBudgetService()
    expired_runs = await budget_service.reap_expired_runs(limit=200)
    reconciled_reservations = await budget_service.reconcile_orphaned_reservations(limit=500)
    return {
        "expired_runs": expired_runs,
        "reconciled_reservations": reconciled_reservations,
    }


async def start_runtime_budget_daemon(*, interval_seconds: int = _DEFAULT_INTERVAL_SECONDS) -> None:
    while True:
        try:
            result = await runtime_budget_maintenance_tick()
            if result["expired_runs"] or result["reconciled_reservations"]:
                logger.info(
                    "[runtime-budget] maintenance expired_runs={} reconciled_reservations={}",
                    result["expired_runs"],
                    result["reconciled_reservations"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[runtime-budget] maintenance tick failed: {}", exc)
        await asyncio.sleep(max(5, interval_seconds))
