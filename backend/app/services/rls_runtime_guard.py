"""Runtime database-role guard for PostgreSQL RLS enforcement.

FORCE ROW LEVEL SECURITY does not protect sessions connected as a PostgreSQL
superuser or a role with BYPASSRLS. This guard verifies the app runtime role at
startup and exposes a non-sensitive health component for operators.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger(__name__)

_ENFORCEMENT_VALUES = {"off", "warn", "strict"}
_latest_snapshot: RlsRuntimeRoleSnapshot | None = None


@dataclass(frozen=True)
class RlsRuntimeRoleSnapshot:
    role_name: str | None
    superuser: bool
    bypassrls: bool
    enforcement: str
    checked: bool
    error: str | None = None

    @property
    def violations(self) -> tuple[str, ...]:
        values: list[str] = []
        if self.superuser:
            values.append("superuser")
        if self.bypassrls:
            values.append("bypassrls")
        return tuple(values)

    @property
    def health_status(self) -> str:
        if self.enforcement == "off":
            return "disabled"
        if self.error:
            return "critical" if self.enforcement == "strict" else "degraded"
        if self.violations:
            return "critical" if self.enforcement == "strict" else "degraded"
        if not self.checked:
            return "unknown"
        return "ok"

    def to_health_component(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.health_status,
            "runtime_role_checked": self.checked,
            "role_name": self.role_name,
            "superuser": self.superuser,
            "bypassrls": self.bypassrls,
            "enforcement": self.enforcement,
            "violations": list(self.violations),
        }
        if self.error:
            payload["last_error"] = self.error
        return payload


def normalize_rls_runtime_role_enforcement(value: str | None) -> str:
    normalized = (value or "strict").strip().lower()
    if normalized not in _ENFORCEMENT_VALUES:
        raise ValueError(f"RLS_RUNTIME_ROLE_ENFORCEMENT must be one of {sorted(_ENFORCEMENT_VALUES)}, got {value!r}")
    return normalized


def latest_runtime_rls_role_health() -> dict[str, Any]:
    snapshot = _latest_snapshot
    if snapshot is None:
        snapshot = RlsRuntimeRoleSnapshot(
            role_name=None,
            superuser=False,
            bypassrls=False,
            enforcement="strict",
            checked=False,
        )
    return snapshot.to_health_component()


async def check_runtime_rls_role(
    *,
    session_factory=async_session,
    enforcement: str | None = None,
) -> RlsRuntimeRoleSnapshot:
    effective_enforcement = normalize_rls_runtime_role_enforcement(enforcement)
    if effective_enforcement == "off":
        snapshot = RlsRuntimeRoleSnapshot(
            role_name=None,
            superuser=False,
            bypassrls=False,
            enforcement=effective_enforcement,
            checked=False,
        )
        _set_latest_snapshot(snapshot)
        return snapshot

    try:
        async with session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT current_user AS role_name, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
            )
            row = result.mappings().one_or_none()
    except Exception as exc:
        snapshot = RlsRuntimeRoleSnapshot(
            role_name=None,
            superuser=False,
            bypassrls=False,
            enforcement=effective_enforcement,
            checked=False,
            error=str(exc),
        )
        _set_latest_snapshot(snapshot)
        message = "RLS runtime role guard could not verify the current PostgreSQL role"
        if effective_enforcement == "strict":
            raise RuntimeError(f"{message}: {exc}") from exc
        logger.warning("%s: %s", message, exc)
        return snapshot

    if row is None:
        snapshot = RlsRuntimeRoleSnapshot(
            role_name=None,
            superuser=False,
            bypassrls=False,
            enforcement=effective_enforcement,
            checked=False,
            error="current_user was not found in pg_roles",
        )
        _set_latest_snapshot(snapshot)
        if effective_enforcement == "strict":
            raise RuntimeError("RLS runtime role guard could not verify current_user in pg_roles")
        logger.warning("RLS runtime role guard could not verify current_user in pg_roles")
        return snapshot

    snapshot = RlsRuntimeRoleSnapshot(
        role_name=str(row["role_name"]),
        superuser=bool(row["rolsuper"]),
        bypassrls=bool(row["rolbypassrls"]),
        enforcement=effective_enforcement,
        checked=True,
    )
    _set_latest_snapshot(snapshot)

    if snapshot.violations:
        message = (
            f"Unsafe RLS runtime database role {snapshot.role_name!r}: violations={', '.join(snapshot.violations)}"
        )
        if effective_enforcement == "strict":
            raise RuntimeError(message)
        logger.warning(message)
    else:
        logger.info("RLS runtime role verified: role=%s", snapshot.role_name)

    return snapshot


def _set_latest_snapshot(snapshot: RlsRuntimeRoleSnapshot) -> None:
    global _latest_snapshot
    _latest_snapshot = snapshot


def reset_runtime_rls_role_guard_for_tests() -> None:
    _set_latest_snapshot(
        RlsRuntimeRoleSnapshot(
            role_name=None,
            superuser=False,
            bypassrls=False,
            enforcement="strict",
            checked=False,
        )
    )


def set_runtime_rls_role_snapshot_for_tests(snapshot: RlsRuntimeRoleSnapshot) -> None:
    _set_latest_snapshot(snapshot)
