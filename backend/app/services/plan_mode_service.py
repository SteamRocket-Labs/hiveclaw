"""Plan Mode imperative shell — DB + filesystem orchestration.

This service wires the pure :mod:`app.services.plan_mode_core` helpers to the
``agent_plan_requests`` table and the per-agent ``plans/`` directory. It is the
single place that mutates a :class:`AgentPlanRequest` row, and it enforces the
state machine and confirmation rules from ``docs/plan-mode-design.md``.

Design split:

* Functional core (pure, separately unit tested): hashing, skeleton, schema
  validation, transition legality, authenticated-user + version/hash checks,
  and markdown rendering all live in ``plan_mode_core``.
* Shell (this file): load/save rows, write markdown to disk, translate core
  decisions into raised errors / updated rows.

Phase 1 scope (see §14): create / generate / revise / confirm / reject plus the
handoff *mechanics* (status flow + ``handoff_status``). The concrete
``objective_trigger`` / ``long_task`` create logic lands in Phase 4 and plugs in
via :meth:`PlanModeService.register_handoff_handler` — Phase 1 ships the
contract, not the downstream side effects.
"""

from __future__ import annotations

import logging
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.plan_request import AgentPlanRequest
from app.services import plan_mode_core as core

logger = logging.getLogger(__name__)

#: A handoff handler takes the caller-owned DB session plus confirmed plan row
#: and returns an audit payload
#: describing what it created (e.g. ``{"created_objective_id": ...}``). It runs
#: inside the shell's DB transaction context.
HandoffHandler = Callable[[Any, AgentPlanRequest], dict[str, Any] | Awaitable[dict[str, Any] | None] | None]


class PlanModeError(Exception):
    """Base class for Plan Mode shell errors."""


class PlanConflictError(PlanModeError):
    """A state/version/hash conflict that maps onto a 4xx (usually 409).

    ``error_code`` is one of the codes produced by
    :func:`plan_mode_core.validate_confirmation` plus the shell-specific
    ``not_confirmed`` / ``illegal_transition``.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def _agent_data_dir() -> Path:
    """Return the configured agent data root (patched in tests)."""
    return Path(get_settings().AGENT_DATA_DIR)


def _plan_markdown_path(agent_id: UUID | str, plan_id: UUID | str) -> Path:
    return _agent_data_dir() / str(agent_id) / "plans" / f"{plan_id}.md"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlanModeService:
    """Stateful-but-thin service over the plan ledger.

    The service instance only holds the handoff handler registry; all plan
    state is in the database.
    """

    def __init__(self) -> None:
        self._handoff_handlers: dict[str, HandoffHandler] = {}

    # -- handoff registry -------------------------------------------------

    def register_handoff_handler(self, target: str, handler: HandoffHandler) -> None:
        """Register the concrete creator for a handoff ``target`` (§13).

        Phase 4 registers ``objective_trigger`` / ``long_task`` / etc. Phase 1
        leaves the registry empty so handoff resolves to ``skipped`` rather
        than silently succeeding.
        """
        self._handoff_handlers[target] = handler

    # -- internal row IO --------------------------------------------------

    async def _load(self, db: Any, plan_id: UUID | str) -> AgentPlanRequest | None:
        stmt = select(AgentPlanRequest).where(AgentPlanRequest.id == plan_id)
        # Lookup hint consumed by test fakes; ignored by the real DB driver.
        stmt._plan_lookup_id = str(plan_id)  # type: ignore[attr-defined]
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    def _render_and_write_markdown(self, plan: AgentPlanRequest) -> str:
        md = core.render_plan_markdown(
            plan_id=plan.id,
            agent_id=plan.agent_id,
            tenant_id=plan.tenant_id,
            status=plan.status,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash or "",
            intent_type=plan.intent_type,
            created_at=plan.created_at.isoformat() if plan.created_at else _now().isoformat(),
            plan_json=plan.plan_json,
            confirmed_by=plan.confirmed_by_user_id,
            confirmed_at=plan.confirmed_at.isoformat() if plan.confirmed_at else None,
        )
        path = _plan_markdown_path(plan.agent_id, plan.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        return str(path)

    # -- create -----------------------------------------------------------

    async def create_plan_request(
        self,
        *,
        agent_id: UUID,
        requested_by_user_id: UUID | None,
        original_request: str,
        intent_type: str,
        source: str = "web_chat",
        tenant_id: UUID | None = None,
        session_id: str | None = None,
        runtime_task_id: UUID | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> AgentPlanRequest:
        """Create a ``draft`` PlanRequest (§7). No plan is generated yet.

        Raises:
            ValueError: if ``intent_type`` is not a known intent.
        """
        if intent_type not in core.INTENT_TYPES:
            raise ValueError(f"unknown intent_type {intent_type!r}; expected one of {core.INTENT_TYPES}")

        async with async_session() as db:
            try:
                plan = AgentPlanRequest(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    runtime_task_id=runtime_task_id,
                    requested_by_user_id=requested_by_user_id,
                    source=source,
                    intent_type=intent_type,
                    original_request=original_request,
                    status="draft",
                    plan_version=1,
                    plan_json={},
                    metadata_json=metadata_json or {},
                )
                db.add(plan)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return plan

    # -- intercept-then-create (§9.2) -------------------------------------

    async def ensure_awaiting_plan(
        self,
        *,
        agent_id: UUID,
        action_kind: str,
        tool_name: str,
        arguments: dict[str, Any],
        source: str = "tool_runtime",
        tenant_id: UUID | None = None,
        session_id: str | None = None,
        runtime_task_id: UUID | None = None,
        requested_by_user_id: UUID | None = None,
    ) -> AgentPlanRequest:
        """Materialise an ``awaiting_confirmation`` plan for a blocked action (§9.2).

        Called by the tool gate and the chat/Feishu auto-sync paths when a
        Plan-Mode-gated action is intercepted with no confirmed plan: instead of
        returning an empty ``needs_plan`` shell, we seed a concrete, confirmable
        plan from the action's own ``arguments`` (``tool_args_to_plan_fill``).

        Idempotent per ``(agent_id, intent_type, signature)``: a repeat of the
        *same* logical blocked action reuses the existing awaiting plan rather
        than spawning duplicates (the agent retries a blocked tool every round).

        ``requested_by_user_id`` is intentionally ``None`` by default — an
        intercepted plan has no authenticated requester yet; confirmation still
        must come through the plan API with a real current user (§8.1/§8.5).

        Raises:
            ValueError: if ``action_kind`` is not a known gate action.
        """
        intent_type, signature = core.action_kind_to_intent_signature(
            action_kind=action_kind, tool_name=tool_name, arguments=arguments
        )

        existing = await self._find_awaiting_by_signature(agent_id=agent_id, signature=signature)
        if existing is not None:
            return existing

        fill = core.tool_args_to_plan_fill(tool_name=tool_name, action_kind=action_kind, arguments=arguments)
        metadata = {
            "intercept_signature": signature,
            "intercept_action_kind": action_kind,
            "intercept_tool": tool_name,
            "intercept_source": source,
        }
        plan = await self.create_plan_request(
            agent_id=agent_id,
            requested_by_user_id=requested_by_user_id,
            original_request=str(fill.get("objective") or fill.get("title") or tool_name),
            intent_type=intent_type,
            source=source,
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            metadata_json=metadata,
        )
        return await self.generate_plan(plan_id=plan.id, fill=fill)

    async def _find_awaiting_by_signature(self, *, agent_id: UUID, signature: str) -> AgentPlanRequest | None:
        """Return the agent's most recent awaiting plan with this intercept signature.

        The dedupe predicate (status + signature) is applied in Python over the
        agent's rows: the signature lives in ``metadata_json`` (a JSONB column),
        and matching it in Python keeps the query a plain ``agent_id`` filter that
        the established session fakes already support.
        """
        candidates = await self.list_plans_for_agent(agent_id, limit=50)
        for plan in candidates:
            if plan.status != "awaiting_confirmation":
                continue
            if (plan.metadata_json or {}).get("intercept_signature") == signature:
                return plan
        return None

    # -- generate ---------------------------------------------------------

    async def generate_plan(
        self,
        *,
        plan_id: UUID,
        fill: dict[str, Any] | None = None,
    ) -> AgentPlanRequest:
        """Generate ``plan_json`` for a draft/planning plan (§10.2).

        Builds the deterministic skeleton for the plan's ``intent_type``,
        merges in the ``fill`` produced by the restricted planning invocation,
        validates the schema, computes the hash and writes markdown. On schema
        failure the plan becomes ``planning_failed`` (no markdown, no hash).

        Raises:
            LookupError: if ``plan_id`` does not exist.
            PlanConflictError: if the plan is not in a generatable status.
        """
        async with async_session() as db:
            try:
                plan = await self._load(db, plan_id)
                if plan is None:
                    raise LookupError(f"plan {plan_id} not found")

                self._move_to_planning_if_needed(plan)
                self._apply_generation(plan, fill or {})
                await db.commit()
            except (LookupError, PlanConflictError):
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise
        return plan

    def _move_to_planning_if_needed(self, plan: AgentPlanRequest) -> None:
        if plan.status == "planning":
            return
        if plan.status in ("draft", "planning_failed"):
            plan.status = "planning"
            return
        raise PlanConflictError(
            "illegal_transition",
            f"cannot generate a plan from status {plan.status!r}",
        )

    def _apply_generation(self, plan: AgentPlanRequest, fill: dict[str, Any]) -> None:
        skeleton = core.build_plan_skeleton(
            intent_type=plan.intent_type,
            title=str(fill.get("title") or plan.plan_json.get("title") or plan.intent_type),
            original_request=plan.original_request,
        )
        merged = {**skeleton, **{k: v for k, v in fill.items() if k != "schema"}}
        # intent_type is owned by the row, never by caller-supplied fill.
        merged["schema"] = core.PLAN_SCHEMA
        merged["intent_type"] = plan.intent_type

        errors = core.validate_plan_json(merged)
        if errors:
            plan.status = "planning_failed"
            plan.plan_hash = None
            metadata = dict(plan.metadata_json or {})
            metadata["planning_errors"] = errors
            plan.metadata_json = metadata
            logger.info(
                "plan_generation_failed",
                extra={"plan_id": str(plan.id), "errors": errors},
            )
            return

        plan.plan_json = merged
        plan.plan_hash = core.compute_plan_hash(merged)
        plan.status = "awaiting_confirmation"
        plan.plan_markdown_path = self._render_and_write_markdown(plan)
        # Clear any stale failure metadata from a previous attempt.
        if plan.metadata_json and "planning_errors" in plan.metadata_json:
            metadata = dict(plan.metadata_json)
            metadata.pop("planning_errors", None)
            plan.metadata_json = metadata

    # -- revise -----------------------------------------------------------

    async def revise_plan(
        self,
        *,
        plan_id: UUID,
        fill: dict[str, Any] | None = None,
    ) -> AgentPlanRequest:
        """Supersede ``plan_id`` with a new version and regenerate (§8.3).

        A confirmed plan is immutable; revising creates a *new* PlanRequest row
        at ``plan_version + 1``, marks the old row ``superseded`` and points
        ``superseded_by_plan_id`` at the new row. The new row is generated and
        ends in ``awaiting_confirmation`` (or ``planning_failed``).

        Raises:
            LookupError: if ``plan_id`` does not exist.
        """
        async with async_session() as db:
            try:
                old = await self._load(db, plan_id)
                if old is None:
                    raise LookupError(f"plan {plan_id} not found")

                new_plan = AgentPlanRequest(
                    tenant_id=old.tenant_id,
                    agent_id=old.agent_id,
                    session_id=old.session_id,
                    runtime_task_id=old.runtime_task_id,
                    requested_by_user_id=old.requested_by_user_id,
                    source=old.source,
                    intent_type=old.intent_type,
                    original_request=old.original_request,
                    status="planning",
                    plan_version=old.plan_version + 1,
                    plan_json={"title": old.plan_json.get("title")} if old.plan_json else {},
                    metadata_json={"revised_from_plan_id": str(old.id)},
                )
                db.add(new_plan)
                await db.flush()  # assign new_plan.id

                self._apply_generation(new_plan, fill or {})

                old.status = "superseded"
                old.superseded_by_plan_id = new_plan.id

                await db.commit()
            except LookupError:
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise
        return new_plan

    # -- confirm ----------------------------------------------------------

    async def confirm_plan(
        self,
        *,
        plan_id: UUID,
        confirming_user_id: UUID | None,
        plan_version: int,
        plan_hash: str,
        reason: str | None = None,
    ) -> AgentPlanRequest:
        """Confirm a plan (§8.1 + §8.2 + §8.5).

        Delegates the decision entirely to
        :func:`plan_mode_core.validate_confirmation` and translates the result:

        * ``missing_confirming_user`` -> :class:`PermissionError`
        * ``not_confirmable`` / ``version_mismatch`` / ``hash_mismatch`` ->
          :class:`PlanConflictError`

        Raises:
            LookupError: if ``plan_id`` does not exist.
            PermissionError: when no authenticated confirming user is present.
            PlanConflictError: on status/version/hash conflict.
        """
        async with async_session() as db:
            try:
                plan = await self._load(db, plan_id)
                if plan is None:
                    raise LookupError(f"plan {plan_id} not found")

                check = core.validate_confirmation(
                    status=plan.status,
                    stored_version=plan.plan_version,
                    stored_hash=plan.plan_hash or "",
                    requested_by_user_id=plan.requested_by_user_id,
                    submitted_version=plan_version,
                    submitted_hash=plan_hash,
                    confirming_user_id=confirming_user_id,
                )
                if not check.ok:
                    self._raise_confirmation_error(check)

                plan.status = "confirmed"
                plan.confirmed_by_user_id = confirming_user_id
                plan.confirmed_at = _now()
                plan.handoff_status = "not_started"
                if reason:
                    metadata = dict(plan.metadata_json or {})
                    metadata["confirm_reason"] = reason
                    plan.metadata_json = metadata
                plan.plan_markdown_path = self._render_and_write_markdown(plan)

                await db.commit()
            except (LookupError, PermissionError, PlanConflictError):
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise
        return plan

    @staticmethod
    def _raise_confirmation_error(check: core.ConfirmationCheck) -> None:
        code = check.error_code or "not_confirmable"
        message = check.message or "plan cannot be confirmed"
        if code == "missing_confirming_user":
            raise PermissionError(message)
        raise PlanConflictError(code, message)

    # -- reject -----------------------------------------------------------

    async def reject_plan(
        self,
        *,
        plan_id: UUID,
        rejecting_user_id: UUID | None,
        reason: str | None = None,
    ) -> AgentPlanRequest:
        """Reject an ``awaiting_confirmation`` plan (§7, terminal).

        Raises:
            LookupError: if ``plan_id`` does not exist.
            PlanConflictError: if the plan is not in ``awaiting_confirmation``.
        """
        async with async_session() as db:
            try:
                plan = await self._load(db, plan_id)
                if plan is None:
                    raise LookupError(f"plan {plan_id} not found")

                if not core.can_transition(plan.status, "rejected"):
                    raise PlanConflictError(
                        "illegal_transition",
                        f"cannot reject a plan in status {plan.status!r}",
                    )

                plan.status = "rejected"
                plan.rejected_by_user_id = rejecting_user_id
                plan.rejected_at = _now()
                if reason:
                    metadata = dict(plan.metadata_json or {})
                    metadata["reject_reason"] = reason
                    plan.metadata_json = metadata

                await db.commit()
            except (LookupError, PlanConflictError):
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise
        return plan

    # -- handoff ----------------------------------------------------------

    async def handoff_confirmed_plan(self, *, plan_id: UUID) -> AgentPlanRequest:
        """Hand a confirmed plan to its execution target (§13).

        The plan's ``status`` stays ``confirmed`` throughout; the outcome is
        recorded on ``handoff_status`` (``completed`` / ``failed`` /
        ``skipped``). A handoff that already ``completed`` is not re-run
        (idempotency). When no handler is registered for the plan's target
        (Phase 1), the result is ``skipped`` with a reason — never a silent
        success and never a status mutation.

        Raises:
            LookupError: if ``plan_id`` does not exist.
            PlanConflictError: if the plan is not ``confirmed``.
        """
        async with async_session() as db:
            try:
                plan = await self._load(db, plan_id)
                if plan is None:
                    raise LookupError(f"plan {plan_id} not found")

                if plan.status != "confirmed":
                    raise PlanConflictError(
                        "not_confirmed",
                        f"only confirmed plans can be handed off (status={plan.status!r})",
                    )

                if plan.handoff_status == "completed":
                    return plan  # idempotent: already done

                await self._run_handoff(db, plan)
                await db.commit()
            except (LookupError, PlanConflictError):
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise
        return plan

    async def _run_handoff(self, db: Any, plan: AgentPlanRequest) -> None:
        target = (plan.plan_json or {}).get("handoff", {}).get("target")
        handler = self._handoff_handlers.get(target) if target else None

        if handler is None:
            plan.handoff_status = "skipped"
            plan.handoff_payload = {
                "reason": "no_handler_registered",
                "target": target,
                "skipped_at": _now().isoformat(),
            }
            logger.info(
                "plan_handoff_skipped",
                extra={"plan_id": str(plan.id), "target": target},
            )
            return

        try:
            maybe_payload = handler(db, plan)
            if inspect.isawaitable(maybe_payload):
                maybe_payload = await maybe_payload
            payload = maybe_payload or {}
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            plan.handoff_status = "failed"
            plan.handoff_payload = {
                "error": str(exc),
                "target": target,
                "failed_at": _now().isoformat(),
            }
            logger.warning(
                "plan_handoff_failed",
                extra={"plan_id": str(plan.id), "target": target, "error": str(exc)},
            )
            return

        plan.handoff_status = "completed"
        plan.handoff_payload = {
            **payload,
            "target": target,
            "completed_at": _now().isoformat(),
        }

    # -- reads (API support) ---------------------------------------------

    async def get_plan(self, plan_id: UUID) -> AgentPlanRequest | None:
        async with async_session() as db:
            try:
                return await self._load(db, plan_id)
            except Exception:
                await db.rollback()
                raise

    async def list_plans_for_agent(self, agent_id: UUID, *, limit: int = 50) -> list[AgentPlanRequest]:
        async with async_session() as db:
            try:
                stmt = (
                    select(AgentPlanRequest)
                    .where(AgentPlanRequest.agent_id == agent_id)
                    .order_by(AgentPlanRequest.created_at.desc())
                    .limit(limit)
                )
                stmt._plan_lookup_agent_id = str(agent_id)  # type: ignore[attr-defined]
                result = await db.execute(stmt)
                return list(result.scalars().all())
            except Exception:
                await db.rollback()
                raise


#: The single shared :class:`PlanModeService` instance. Handoff handlers are
#: registered onto this exact object at startup
#: (``plan_mode_registry.register_plan_mode_handoffs``), and every caller — the
#: REST API, the tool gate, the chat/Feishu auto-sync paths — must reuse it so a
#: confirmed plan handed off over any surface creates the same objective/trigger.
_service = PlanModeService()


def get_plan_mode_service() -> PlanModeService:
    """Return the shared :class:`PlanModeService` (the one handoffs register on)."""
    return _service


__all__ = [
    "HandoffHandler",
    "PlanConflictError",
    "PlanModeError",
    "PlanModeService",
    "get_plan_mode_service",
]
