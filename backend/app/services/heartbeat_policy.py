"""Platform-managed heartbeat contract.

Heartbeat is an internal memory/evolution maintenance loop. It is always on
for runnable native employees and uses the platform cadence, not per-agent or
tenant UI settings.
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings

MANAGED_HEARTBEAT_ENABLED = True
MANAGED_HEARTBEAT_ACTIVE_HOURS = "00:00-23:59"
MANAGED_HEARTBEAT_FIELDS = frozenset(
    {
        "heartbeat_enabled",
        "heartbeat_interval_minutes",
        "heartbeat_active_hours",
    }
)


def managed_heartbeat_interval_minutes() -> int:
    return get_settings().HEARTBEAT_DEFAULT_INTERVAL_MINUTES


def managed_heartbeat_payload() -> dict[str, Any]:
    return {
        "heartbeat_enabled": MANAGED_HEARTBEAT_ENABLED,
        "heartbeat_interval_minutes": managed_heartbeat_interval_minutes(),
        "heartbeat_active_hours": MANAGED_HEARTBEAT_ACTIVE_HOURS,
    }


def apply_managed_heartbeat_fields(agent: Any) -> None:
    agent.heartbeat_enabled = MANAGED_HEARTBEAT_ENABLED
    agent.heartbeat_interval_minutes = managed_heartbeat_interval_minutes()
    agent.heartbeat_active_hours = MANAGED_HEARTBEAT_ACTIVE_HOURS


def normalize_agent_heartbeat_output(data: dict[str, Any]) -> dict[str, Any]:
    data.update(managed_heartbeat_payload())
    return data
