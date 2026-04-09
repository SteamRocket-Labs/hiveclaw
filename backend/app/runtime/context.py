"""Explicit runtime context types shared by agent entrypoints."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.execution_context import ExecutionIdentity
from app.runtime.session import SessionContext


@dataclass(slots=True)
class RuntimeContext:
    """Normalized runtime context for a single agent invocation."""

    session: SessionContext = field(default_factory=SessionContext)
    execution_identity: ExecutionIdentity | None = None
    tenant_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
