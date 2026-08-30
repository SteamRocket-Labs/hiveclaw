"""Pydantic schemas for security audit query API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


ADMIN_AUDIT_DETAIL_KEYS = frozenset(
    {
        "capability",
        "changed_fields",
        "force",
        "latency_ms",
        "max_tokens",
        "model",
        "outcome",
        "phase",
        "probe_id",
        "provider",
        "retry_count",
        "status",
        "success",
        "tool",
    }
)


def project_admin_audit_details(value: object) -> dict:
    """Return the machine-safe control-plane summary; raw evidence stays canonical."""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in ADMIN_AUDIT_DETAIL_KEYS}


class AuditQueryParams(BaseModel):
    """Query parameters for filtering security audit events."""

    event_type: str | None = None
    severity: str | None = None
    actor_id: uuid.UUID | None = None
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    search: str | None = Field(None, max_length=200, description="Text search on summary action and event_type")
    date_from: datetime | None = None
    date_to: datetime | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=500)


class AuditEventOut(BaseModel):
    """Admin summary for one security event; raw evidence is operator-only."""

    id: uuid.UUID
    event_type: str
    severity: str
    actor_type: str
    actor_id: uuid.UUID | None = None
    tenant_id: uuid.UUID
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    action: str
    details: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    prev_hash: str = ""
    event_hash: str = ""
    execution_identity_type: str | None = None
    execution_identity_id: uuid.UUID | None = None

    _project_details = field_validator("details", mode="before")(project_admin_audit_details)

    model_config = {"from_attributes": True}


class AuditLogSummaryOut(BaseModel):
    """Legacy admin audit summary without user, network, or business payload."""

    id: uuid.UUID
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    action: str
    details: dict = Field(default_factory=dict)
    created_at: datetime

    _project_details = field_validator("details", mode="before")(project_admin_audit_details)

    model_config = {"from_attributes": True}
