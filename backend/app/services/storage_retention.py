"""Company retention settings and observation of the runtime's shared volume."""

import math
from pathlib import Path
import shutil
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.tenant_setting import TenantSetting

POLICY_KEY = "storage_retention"


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    retention_days: float = Field(strict=True, ge=0, allow_inf_nan=False)


class RetentionPolicyUpdate(RetentionPolicy):
    expected_version: int = Field(strict=True, ge=0)


def _project_policy(value: dict | None) -> dict:
    if not isinstance(value, dict) or "version" not in value:
        raise ValueError("Invalid storage retention policy")
    version = value.get("version", 0) if isinstance(value, dict) else 0
    if type(version) is not int or version < 0:
        raise ValueError("Invalid storage retention version")
    if version == 0:
        if value is not None and value != {"version": 0}:
            raise ValueError("Invalid storage retention default")
        days = float(get_settings().CHAT_ARTIFACT_SNAPSHOT_RETENTION_DAYS)
        policy = RetentionPolicy(enabled=True, retention_days=days)
    else:
        policy = RetentionPolicy.model_validate({key: value.get(key) for key in ("enabled", "retention_days")})
    return {**policy.model_dump(), "version": version,
        "source": "company_admin" if version else "installation_default"}


async def read_retention_policy(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    row = await db.scalar(select(TenantSetting).where(
        TenantSetting.tenant_id == tenant_id, TenantSetting.key == POLICY_KEY))
    # An existing JSON null is corrupt configuration, not an absent company policy.
    return _project_policy(row.value if row is not None else {"version": 0})


class RetentionPolicyConflict(Exception):
    pass


async def save_retention_policy(db: AsyncSession, tenant_id: uuid.UUID, actor_id: uuid.UUID,
    data: RetentionPolicyUpdate) -> dict:
    from app.core.policy import write_audit_event

    # The unique key also serializes two first-time saves; an absent row cannot be locked.
    await db.execute(insert(TenantSetting).values(tenant_id=tenant_id, key=POLICY_KEY, value={"version": 0})
        .on_conflict_do_nothing(index_elements=[TenantSetting.tenant_id, TenantSetting.key]))
    row = await db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == tenant_id,
        TenantSetting.key == POLICY_KEY).with_for_update().execution_options(populate_existing=True))
    before = _project_policy(row.value)
    if before["version"] != data.expected_version:
        raise RetentionPolicyConflict()
    row.value = {"enabled": data.enabled, "retention_days": data.retention_days, "version": before["version"] + 1}
    after = _project_policy(row.value)
    await write_audit_event(db, event_type="storage.retention_policy_updated", severity="info",
        actor_type="user", actor_id=actor_id, tenant_id=tenant_id, resource_type="storage_retention",
        action="update_storage_retention", details={"before": before, "after": after})
    await db.flush()
    return after


def runtime_volume_capacity(*, operator: bool) -> dict:
    """Observe the mounted volume, never a parent/API filesystem or a tenant quota."""
    if not operator:
        return {"status": "operator_only", "scope": "shared_agent_volume"}
    root = Path(get_settings().AGENT_DATA_DIR)
    try:
        if not root.is_dir():
            raise OSError("Agent volume is unavailable")
        total, used, free = shutil.disk_usage(root)
        if not all(math.isfinite(value) and value >= 0 for value in (total, used, free)):
            raise OSError("Capacity is unavailable")
    except OSError:
        return {"status": "unavailable", "scope": "shared_agent_volume", "retryable": True}
    return {"status": "available", "scope": "shared_agent_volume",
        "total_bytes": total, "used_bytes": used, "free_bytes": free}
