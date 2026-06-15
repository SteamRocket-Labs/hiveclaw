"""Tenant-scoped behavior eval publisher for self-evolution promotion gates.

The eval-ci endpoint writes reports only for the isolated eval tenant. This
service is the production path for ordinary tenants: when a skill candidate is
ready to be promoted, it can run the same trusted live behavior harness and
persist the latest report under that tenant. Promotion still fail-closes unless
``behavior_eval_passed`` accepts the report.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.database import async_session, enter_rls_bypass
from app.evals.hive_live_runner import (
    BEHAVIOR_EVAL_LATEST_REPORT_SETTING_KEY,
    BEHAVIOR_EVAL_RUNTIME_SETTING_KEY,
    DETERMINISTIC_BEHAVIOR_SCENARIOS,
    _coerce_uuid,
    _model_runtime_metadata,
    behavior_eval_passed,
    build_invoke_agent_runner,
    resolve_production_eval_runtime,
    run_hive_behavior_eval,
)
from app.models.agent import Agent
from app.models.tenant_setting import TenantSetting
from app.services.eval_ci_service import build_behavior_eval_rebaseline_candidate


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _auto_publish_enabled() -> bool:
    return bool(get_settings().BEHAVIOR_EVAL_AUTO_PUBLISH_ENABLED)


def _report_max_age_hours() -> int:
    return max(1, int(get_settings().BEHAVIOR_EVAL_REPORT_MAX_AGE_HOURS))


def _parse_stored_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _latest_value_is_fresh(value: dict[str, Any] | None, *, max_age_hours: int | None = None) -> bool:
    if not isinstance(value, dict):
        return False
    stored_at = _parse_stored_at(value.get("stored_at"))
    if stored_at is None:
        return False
    age = _now_utc() - stored_at
    return age <= timedelta(hours=max_age_hours or _report_max_age_hours())


async def load_latest_behavior_eval_value_for_tenant(*, tenant_id: uuid.UUID | str) -> dict[str, Any] | None:
    normalized_tenant_id = _coerce_uuid(tenant_id, field_name="tenant_id")
    if normalized_tenant_id is None:
        return None
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="tenant behavior eval latest report read"):
            result = await db.execute(
                select(TenantSetting.value).where(
                    TenantSetting.tenant_id == normalized_tenant_id,
                    TenantSetting.key == BEHAVIOR_EVAL_LATEST_REPORT_SETTING_KEY,
                )
            )
            value = result.scalar_one_or_none()
    return value if isinstance(value, dict) else None


async def store_latest_behavior_eval_report_for_tenant(
    *,
    tenant_id: uuid.UUID | str,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Persist the latest behavior eval report for a concrete tenant."""

    normalized_tenant_id = _coerce_uuid(tenant_id, field_name="tenant_id")
    if normalized_tenant_id is None:
        raise ValueError("tenant_id is required")
    stored_at = _now_utc().isoformat()
    value: dict[str, Any] = {
        "stored_at": stored_at,
        "tenant_id": str(normalized_tenant_id),
        "setting_key": BEHAVIOR_EVAL_LATEST_REPORT_SETTING_KEY,
        "promotion_gate_eligible": behavior_eval_passed(report),
        "report": report,
        "rebaseline_candidate": build_behavior_eval_rebaseline_candidate(report, generated_at=stored_at),
    }
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="tenant behavior eval latest report write"):
            result = await db.execute(
                select(TenantSetting).where(
                    TenantSetting.tenant_id == normalized_tenant_id,
                    TenantSetting.key == BEHAVIOR_EVAL_LATEST_REPORT_SETTING_KEY,
                )
            )
            setting = result.scalar_one_or_none()
            if setting is None:
                db.add(
                    TenantSetting(
                        tenant_id=normalized_tenant_id,
                        key=BEHAVIOR_EVAL_LATEST_REPORT_SETTING_KEY,
                        value=value,
                    )
                )
            else:
                setting.value = value
        await db.commit()
    return value


async def _fallback_user_id_for_agent(
    *,
    tenant_id: uuid.UUID,
    fallback_agent_id: uuid.UUID | str | None,
) -> uuid.UUID | None:
    normalized_agent_id = _coerce_uuid(fallback_agent_id, field_name="fallback_agent_id")
    if normalized_agent_id is None:
        return None
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="tenant behavior eval fallback agent bootstrap"):
            result = await db.execute(select(Agent).where(Agent.id == normalized_agent_id))
            agent = result.scalar_one_or_none()
            if agent is None or _coerce_uuid(agent.tenant_id, field_name="agent.tenant_id") != tenant_id:
                return None
            return _coerce_uuid(agent.creator_id, field_name="agent.creator_id")


async def _tenant_has_explicit_behavior_eval_runtime(*, tenant_id: uuid.UUID) -> bool:
    async with async_session() as db:
        async with enter_rls_bypass(db, reason="tenant behavior eval runtime preflight"):
            result = await db.execute(
                select(TenantSetting.value).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == BEHAVIOR_EVAL_RUNTIME_SETTING_KEY,
                )
            )
            value = result.scalar_one_or_none()
    if not isinstance(value, dict):
        return False
    return bool(str(value.get("agent_id") or "").strip() or str(value.get("user_id") or "").strip())


async def resolve_tenant_behavior_eval_runtime(
    *,
    tenant_id: uuid.UUID | str,
    fallback_agent_id: uuid.UUID | str | None = None,
):
    """Resolve a tenant's eval runtime, falling back to the current agent.

    Preferred path: the tenant configures ``behavior_eval_runtime`` with a
    dedicated eval agent/user. Fallback path: use the current agent and creator
    for the same tenant, so ordinary tenants are not permanently dark when no
    eval-ci tenant exists. Both paths still require an enabled tenant-scoped
    model through ``resolve_production_eval_runtime``.
    """

    normalized_tenant_id = _coerce_uuid(tenant_id, field_name="tenant_id")
    if normalized_tenant_id is None:
        raise ValueError("tenant_id is required")
    has_explicit_runtime = await _tenant_has_explicit_behavior_eval_runtime(tenant_id=normalized_tenant_id)
    try:
        return await resolve_production_eval_runtime(
            agent_id=None,
            user_id=None,
            expected_tenant_id=normalized_tenant_id,
        )
    except Exception as first_error:
        if has_explicit_runtime:
            raise first_error
        fallback_user_id = await _fallback_user_id_for_agent(
            tenant_id=normalized_tenant_id,
            fallback_agent_id=fallback_agent_id,
        )
        if fallback_agent_id is None or fallback_user_id is None:
            raise first_error
        return await resolve_production_eval_runtime(
            agent_id=fallback_agent_id,
            user_id=fallback_user_id,
            expected_tenant_id=normalized_tenant_id,
        )


async def run_and_store_tenant_behavior_eval(
    *,
    tenant_id: uuid.UUID | str,
    fallback_agent_id: uuid.UUID | str | None = None,
    scenarios: tuple[str, ...] = DETERMINISTIC_BEHAVIOR_SCENARIOS,
    output_root: Path | None = None,
    runtime_resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the trusted live behavior eval and store the tenant-local report."""

    runtime = await (runtime_resolver or resolve_tenant_behavior_eval_runtime)(
        tenant_id=tenant_id,
        fallback_agent_id=fallback_agent_id,
    )
    output_dir = output_root or Path(tempfile.mkdtemp(prefix=f"hive-tenant-behavior-eval-{runtime.tenant_id}-"))
    runner = build_invoke_agent_runner(
        model=runtime.model,
        fallback_model=runtime.fallback_model,
        agent_name=runtime.agent_name,
        role_description=runtime.role_description,
        agent_id=runtime.agent_id,
        user_id=runtime.user_id,
        tenant_id=runtime.tenant_id,
    )
    report = await run_hive_behavior_eval(agent_runner=runner, output_dir=output_dir, scenarios=scenarios)
    report.setdefault("runtime", {}).update(
        {
            **_model_runtime_metadata(runtime.model, source=runtime.model_source),
            "tenant_id": str(runtime.tenant_id),
            "agent_id": str(runtime.agent_id),
            "user_id": str(runtime.user_id),
            "publisher": "tenant_behavior_eval_publisher",
        }
    )
    if runtime.fallback_model is not None:
        report["runtime"]["fallback_model"] = _model_runtime_metadata(runtime.fallback_model)
    await store_latest_behavior_eval_report_for_tenant(tenant_id=runtime.tenant_id, report=report)
    return report


async def ensure_skill_distiller_behavior_report(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    runtime_config: Any,
    output_root: Path | None = None,
) -> dict[str, Any] | None:
    """Ensure ``runtime_config`` has a passing tenant-local behavior report.

    Returns ``None`` on any missing prerequisite or failed run. That is
    intentional: promotion remains fail-closed and the distiller will record a
    held decision.
    """

    existing = getattr(runtime_config, "skill_distiller_behavior_report", None)
    if isinstance(existing, dict) and behavior_eval_passed(existing):
        return existing
    if tenant_id is None or not _auto_publish_enabled():
        return None

    try:
        latest = await load_latest_behavior_eval_value_for_tenant(tenant_id=tenant_id)
        latest_report = latest.get("report") if isinstance(latest, dict) else None
        if _latest_value_is_fresh(latest):
            if isinstance(latest_report, dict) and behavior_eval_passed(latest_report):
                setattr(runtime_config, "skill_distiller_behavior_report", latest_report)
                return latest_report
            return None

        report = await run_and_store_tenant_behavior_eval(
            tenant_id=tenant_id,
            fallback_agent_id=agent_id,
            output_root=output_root,
        )
        if behavior_eval_passed(report):
            setattr(runtime_config, "skill_distiller_behavior_report", report)
            return report
        return None
    except Exception as exc:  # noqa: BLE001 - promotion must fail closed, not break heartbeat
        logger.warning(
            "[TenantBehaviorEval] failed to publish behavior report for tenant={} agent={}: {}",
            tenant_id,
            agent_id,
            exc,
        )
        return None
