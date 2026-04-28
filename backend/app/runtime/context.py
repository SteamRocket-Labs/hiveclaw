"""Explicit runtime context types shared by agent entrypoints.

P1-W3-5: this module is the convergence point for what used to be four
independent helpers (context.py / context_engine.py / context_budget.py
/ coordinator.py). Each piece keeps its own focused module — the engine
encapsulates context-block fencing, the budget profile encapsulates
char/token allocation, the coordinator picks the per-turn dispatch shape
— but `RuntimeContext` now exposes them as composed members and offers a
single `for_invocation` factory so downstream callers stop having to
import all four modules to build one consistent context.

Adoption is incremental: existing callers that touch only `session` /
`execution_identity` / `tenant_id` / `metadata` keep working unchanged
because the new fields default to None.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.execution_context import ExecutionIdentity
from app.runtime.session import SessionContext

if TYPE_CHECKING:  # avoid circular imports at runtime
    from app.runtime.context_budget import ContextBudget
    from app.runtime.context_engine import ContextEngine


@dataclass(slots=True)
class RuntimeContext:
    """Normalized runtime context for a single agent invocation.

    Composes the four W3-5 primitives in one place so callers don't have
    to import context_budget + context_engine + coordinator separately
    when wiring an invocation.
    """

    session: SessionContext = field(default_factory=SessionContext)
    execution_identity: ExecutionIdentity | None = None
    tenant_id: uuid.UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # P1-W3-5 composition members. Optional so the dataclass keeps its
    # existing default-construction surface; callers that need them set
    # explicit values via `for_invocation` (or the field directly).
    engine: "ContextEngine | None" = None
    budget: "ContextBudget | None" = None

    @classmethod
    def for_invocation(
        cls,
        *,
        session: SessionContext | None = None,
        execution_identity: ExecutionIdentity | None = None,
        tenant_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
        engine: "ContextEngine | None" = None,
        budget: "ContextBudget | None" = None,
    ) -> "RuntimeContext":
        """Build a RuntimeContext with all primitives wired in one call.

        Lazily defaults `engine` to `DefaultContextEngine()` so callers
        that don't specify one still get the standard fencing behaviour.
        Budget stays None unless caller passes one (compute_context_budget
        needs request-shaped inputs that the factory can't synthesize).
        """
        if engine is None:
            from app.runtime.context_engine import DefaultContextEngine

            engine = DefaultContextEngine()
        return cls(
            session=session if session is not None else SessionContext(),
            execution_identity=execution_identity,
            tenant_id=tenant_id,
            metadata=metadata if metadata is not None else {},
            engine=engine,
            budget=budget,
        )
