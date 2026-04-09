"""Guard policy model — Cloud-managed, Desktop-enforced (ARCHITECTURE.md §6.3).

One policy per tenant. Desktop caches and enforces; Cloud is the single source of truth.
zone_guard / egress_guard are JSONB blobs whose internal schema is owned by HiveDesktop's
Guard runtime — Cloud stores and distributes them opaquely.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GuardPolicy(Base):
    """Tenant-level Guard policy distributed to Desktop clients."""

    __tablename__ = "guard_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, unique=True, index=True,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, server_default="1")
    zone_guard: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    egress_guard: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
    )
