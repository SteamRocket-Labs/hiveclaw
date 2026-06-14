"""CI-only behavior eval entrypoints.

This module runs inside the deployed backend, so it exercises the real
tenant-scoped runtime without requiring GitHub Actions to SSH into Railway.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.database import enter_rls_bypass
from app.evals.hive_live_runner import (
    BEHAVIOR_EVAL_RUNTIME_SETTING_KEY,
    DETERMINISTIC_BEHAVIOR_SCENARIOS,
    _coerce_uuid,
    _model_runtime_metadata,
    _require_uuid,
    build_invoke_agent_runner,
    resolve_production_eval_runtime,
    run_hive_behavior_eval,
)
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.secrets_provider import get_secrets_provider


BEHAVIOR_EVAL_RUNTIME_MODEL_MIRROR_SETTING_KEY = "behavior_eval_runtime_model_mirror"
EVAL_RUNTIME_MAX_OUTPUT_TOKENS = 524288


class EvalRuntimeModelConfig(BaseModel):
    """Server-to-server payload for mirroring a company model into eval runtime."""

    source_model_id: str | None = None
    source_tenant_id: str | None = None
    provider: str
    model: str
    api_key: str = Field(min_length=1)
    base_url: str | None = None
    label: str
    enabled: bool = True
    supports_vision: bool = False
    max_tokens_per_day: int | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=EVAL_RUNTIME_MAX_OUTPUT_TOKENS)
    max_input_tokens: int | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None
    reasoning_budget_tokens: int | None = Field(default=None, ge=1, le=200000)
    reasoning_display: str | None = None
    preserve_reasoning: bool | None = None
    text_verbosity: str | None = None
    provider_options: dict[str, Any] | None = None


def _configured_eval_tenant_id() -> uuid.UUID:
    return _require_uuid(os.environ.get("HIVE_EVAL_TENANT_ID", "").strip(), field_name="HIVE_EVAL_TENANT_ID")


async def _load_eval_runtime_setting(db: Any, *, tenant_id: uuid.UUID) -> dict[str, Any]:
    result = await db.execute(
        select(TenantSetting.value).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == BEHAVIOR_EVAL_RUNTIME_SETTING_KEY,
        )
    )
    value = result.scalar_one_or_none()
    return value if isinstance(value, dict) else {}


async def _resolve_eval_identity(db: Any, *, tenant_id: uuid.UUID) -> tuple[Agent, User]:
    agent_id = os.environ.get("HIVE_EVAL_AGENT_ID", "").strip()
    user_id = os.environ.get("HIVE_EVAL_USER_ID", "").strip()
    if not agent_id or not user_id:
        runtime_setting = await _load_eval_runtime_setting(db, tenant_id=tenant_id)
        agent_id = agent_id or str(runtime_setting.get("agent_id") or "")
        user_id = user_id or str(runtime_setting.get("user_id") or "")

    normalized_agent_id = _require_uuid(agent_id, field_name=f"{BEHAVIOR_EVAL_RUNTIME_SETTING_KEY}.agent_id")
    normalized_user_id = _require_uuid(user_id, field_name=f"{BEHAVIOR_EVAL_RUNTIME_SETTING_KEY}.user_id")

    agent_result = await db.execute(select(Agent).where(Agent.id == normalized_agent_id))
    agent = agent_result.scalar_one_or_none()
    if agent is None:
        raise RuntimeError(f"eval agent {normalized_agent_id} not found")
    agent_tenant_id = _require_uuid(getattr(agent, "tenant_id", None), field_name="agent.tenant_id")
    if agent_tenant_id != tenant_id:
        raise RuntimeError(f"eval agent tenant mismatch: expected {tenant_id}, got {agent_tenant_id}")

    user_result = await db.execute(
        select(User).where(
            User.id == normalized_user_id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise RuntimeError(f"eval user {normalized_user_id} is not active in tenant {tenant_id}")
    return agent, user


def _public_model_metadata(model: Any | None, *, source: str | None = None) -> dict[str, Any] | None:
    if model is None:
        return None
    metadata = _model_runtime_metadata(model, source=source)
    metadata.update(
        {
            "label": getattr(model, "label", None),
            "enabled": bool(getattr(model, "enabled", False)),
            "supports_vision": bool(getattr(model, "supports_vision", False)),
            "base_url": getattr(model, "base_url", None),
            "max_output_tokens": getattr(model, "max_output_tokens", None),
            "max_input_tokens": getattr(model, "max_input_tokens", None),
            "reasoning_mode": getattr(model, "reasoning_mode", None),
            "reasoning_effort": getattr(model, "reasoning_effort", None),
            "reasoning_budget_tokens": getattr(model, "reasoning_budget_tokens", None),
            "reasoning_display": getattr(model, "reasoning_display", None),
            "preserve_reasoning": getattr(model, "preserve_reasoning", None),
            "text_verbosity": getattr(model, "text_verbosity", None),
        }
    )
    return metadata


async def _load_model(db: Any, *, tenant_id: uuid.UUID, model_id: Any, source: str) -> LLMModel | None:
    normalized_model_id = _coerce_uuid(model_id, field_name=f"{source}.model_id")
    if normalized_model_id is None:
        return None
    result = await db.execute(
        select(LLMModel).where(
            LLMModel.id == normalized_model_id,
            LLMModel.tenant_id == tenant_id,
        )
    )
    model = result.scalar_one_or_none()
    if model is not None and getattr(model, "tenant_id", None) != tenant_id:
        raise RuntimeError(f"{source} returned a non tenant-scoped model for behavior eval")
    return model


def _mirror_setting_payload(*, model: LLMModel, config: EvalRuntimeModelConfig) -> dict[str, Any]:
    return {
        "model_id": str(model.id),
        "source_model_id": config.source_model_id,
        "source_tenant_id": config.source_tenant_id,
        "provider": config.provider,
        "model": config.model,
        "label": config.label,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_production_behavior_eval_runtime_status(db: Any) -> dict[str, Any]:
    """Return eval backend runtime status without exposing provider secrets."""

    tenant_id = _configured_eval_tenant_id()
    async with enter_rls_bypass(db, reason="behavior eval runtime status"):
        agent, user = await _resolve_eval_identity(db, tenant_id=tenant_id)
        primary_model = await _load_model(
            db,
            tenant_id=tenant_id,
            model_id=getattr(agent, "primary_model_id", None),
            source="agent.primary_model_id",
        )
        fallback_model = await _load_model(
            db,
            tenant_id=tenant_id,
            model_id=getattr(agent, "fallback_model_id", None),
            source="agent.fallback_model_id",
        )
        mirror_result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == BEHAVIOR_EVAL_RUNTIME_MODEL_MIRROR_SETTING_KEY,
            )
        )
        mirror_setting = mirror_result.scalar_one_or_none()
        return {
            "configured": primary_model is not None and bool(getattr(primary_model, "enabled", False)),
            "tenant_id": str(tenant_id),
            "agent": {
                "id": str(agent.id),
                "name": getattr(agent, "name", None),
                "status": getattr(agent, "status", None),
                "primary_model_id": str(getattr(agent, "primary_model_id", "") or ""),
                "fallback_model_id": str(getattr(agent, "fallback_model_id", "") or ""),
            },
            "user": {
                "id": str(user.id),
                "display_name": getattr(user, "display_name", None),
                "email": getattr(user, "email", None),
            },
            "model": _public_model_metadata(primary_model, source="agent.primary_model_id"),
            "fallback_model": _public_model_metadata(fallback_model, source="agent.fallback_model_id"),
            "mirror": getattr(mirror_setting, "value", None) if mirror_setting else None,
        }


async def configure_production_behavior_eval_model(db: Any, config: EvalRuntimeModelConfig) -> dict[str, Any]:
    """Mirror a company model into the isolated eval tenant and bind the eval agent.

    This endpoint is intentionally server-to-server only. The browser selects a
    source model by ID; the production backend decrypts the key and sends it over
    the internal CI-token channel to this eval backend.
    """

    tenant_id = _configured_eval_tenant_id()
    async with enter_rls_bypass(db, reason="behavior eval runtime model sync"):
        agent, user = await _resolve_eval_identity(db, tenant_id=tenant_id)

        mirror_result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == BEHAVIOR_EVAL_RUNTIME_MODEL_MIRROR_SETTING_KEY,
            )
        )
        mirror_setting = mirror_result.scalar_one_or_none()

        existing_model = None
        if mirror_setting and isinstance(mirror_setting.value, dict):
            existing_model = await _load_model(
                db,
                tenant_id=tenant_id,
                model_id=mirror_setting.value.get("model_id"),
                source=BEHAVIOR_EVAL_RUNTIME_MODEL_MIRROR_SETTING_KEY,
            )

        encrypted_key = get_secrets_provider().encrypt(config.api_key)
        if existing_model is None:
            existing_model = LLMModel(id=uuid.uuid4(), tenant_id=tenant_id)
            db.add(existing_model)

        existing_model.provider = config.provider
        existing_model.model = config.model
        existing_model.api_key_encrypted = encrypted_key
        existing_model.base_url = config.base_url
        existing_model.label = config.label
        existing_model.max_tokens_per_day = config.max_tokens_per_day
        existing_model.enabled = True
        existing_model.supports_vision = config.supports_vision
        existing_model.max_output_tokens = config.max_output_tokens
        existing_model.max_input_tokens = config.max_input_tokens
        existing_model.temperature = config.temperature
        existing_model.reasoning_mode = config.reasoning_mode
        existing_model.reasoning_effort = config.reasoning_effort
        existing_model.reasoning_budget_tokens = config.reasoning_budget_tokens
        existing_model.reasoning_display = config.reasoning_display
        existing_model.preserve_reasoning = config.preserve_reasoning
        existing_model.text_verbosity = config.text_verbosity
        existing_model.provider_options = config.provider_options

        await db.flush()
        agent.primary_model_id = existing_model.id

        mirror_value = _mirror_setting_payload(model=existing_model, config=config)
        if mirror_setting:
            mirror_setting.value = mirror_value
        else:
            mirror_setting = TenantSetting(
                tenant_id=tenant_id,
                key=BEHAVIOR_EVAL_RUNTIME_MODEL_MIRROR_SETTING_KEY,
                value=mirror_value,
            )
            db.add(mirror_setting)

        await db.commit()
        return {
            "configured": True,
            "tenant_id": str(tenant_id),
            "agent": {
                "id": str(agent.id),
                "name": getattr(agent, "name", None),
                "primary_model_id": str(agent.primary_model_id),
            },
            "user": {
                "id": str(user.id),
                "display_name": getattr(user, "display_name", None),
                "email": getattr(user, "email", None),
            },
            "model": _public_model_metadata(existing_model, source="agent.primary_model_id"),
            "mirror": mirror_value,
        }


async def run_production_behavior_eval_for_ci(*, scenarios: Sequence[str] | None = None) -> dict[str, Any]:
    """Run the live behavior eval against the production runtime configured for
    the eval tenant.

    The eval tenant/model/user/agent are resolved from normal backend config.
    No provider key or model override is accepted from CI.
    """

    tenant_id = os.environ.get("HIVE_EVAL_TENANT_ID", "").strip()
    if not tenant_id:
        raise RuntimeError("HIVE_EVAL_TENANT_ID is required")

    runtime = await resolve_production_eval_runtime(
        agent_id=os.environ.get("HIVE_EVAL_AGENT_ID") or None,
        user_id=os.environ.get("HIVE_EVAL_USER_ID") or None,
        expected_tenant_id=tenant_id,
    )
    runner = build_invoke_agent_runner(
        model=runtime.model,
        fallback_model=runtime.fallback_model,
        agent_name=runtime.agent_name,
        role_description=runtime.role_description,
        agent_id=runtime.agent_id,
        user_id=runtime.user_id,
        tenant_id=runtime.tenant_id,
    )
    selected_scenarios = tuple(scenarios) if scenarios else DETERMINISTIC_BEHAVIOR_SCENARIOS
    report = await run_hive_behavior_eval(
        agent_runner=runner,
        output_dir=Path(tempfile.mkdtemp(prefix="hive-behavior-eval-ci-")),
        scenarios=selected_scenarios,
    )
    report.setdefault("runtime", {}).update(
        {
            **_model_runtime_metadata(runtime.model, source=runtime.model_source),
            "tenant_id": str(runtime.tenant_id),
            "agent_id": str(runtime.agent_id),
            "user_id": str(runtime.user_id),
        }
    )
    if runtime.fallback_model is not None:
        report["runtime"]["fallback_model"] = _model_runtime_metadata(runtime.fallback_model)
    return report
