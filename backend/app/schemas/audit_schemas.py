"""Pydantic schemas for security audit query API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AuditQueryParams(BaseModel):
    """Query parameters for filtering security audit events."""

    event_type: str | None = None
    severity: str | None = None
    actor_id: uuid.UUID | None = None
    resource_type: str | None = None
    resource_id: uuid.UUID | None = None
    search: str | None = Field(None, max_length=200, description="Text search on action, event_type, and details")
    date_from: datetime | None = None
    date_to: datetime | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(50, ge=1, le=500)


class AuditEventOut(BaseModel):
    """Response schema for a single security audit event."""

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
    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime | None = None
    prev_hash: str = ""
    event_hash: str = ""

    # Block C placeholders: execution identity fields
    execution_identity_type: str | None = None
    execution_identity_id: uuid.UUID | None = None
    execution_identity_label: str | None = None

    model_config = {"from_attributes": True}
