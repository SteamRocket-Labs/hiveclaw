"""Plan Mode gate — the shared fail-closed decision service (§9.0 / §9.2).

``PlanModeGate.check`` is the single decision core consulted by both layers of
the Plan Mode defence (``docs/plan-mode-design.md`` §9.0):

* **Early intercept layer** (tool / REST): surfaces ``needs_plan`` so the agent
  or UI steers the user to confirm a plan *before* an autonomous action runs.
* **Fail-closed backstop layer** (wake reconciler / trigger daemon / task
  executor / delegation): the deterministic guard that, even when an upstream
  early-intercept is missed, refuses to enable a trigger or start an autonomous
  task without a confirmed plan or an explicit cutover exemption.

Both layers ask the same question — *"is this `action_kind` allowed to start
right now?"* — and must answer it identically, so the logic lives here once.

Design split (consistent with :mod:`app.services.plan_mode_service`):

* Functional core (pure, separately unit tested in ``plan_mode_core``):
  action_kind -> intent mapping, cutover-exemption extraction, confirmed-plan
  version/hash validation, and the ``needs_plan`` envelope assembly.
* Shell (this file): load the referenced plan row, resolve the artifact
  exemption (inline dict or async resolver callback), and translate the core
  decision into a :class:`PlanGateDecision`.

The gate is **read-only**: it never mutates a plan row. It accepts a live
``AsyncSession`` so callers can run it inside their own transaction (the REST
gate, the daemon preflight) without opening a nested session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from app.database import async_session
from app.models.plan_request import AgentPlanRequest
from app.services import plan_mode_core as core

logger = logging.getLogger(__name__)

#: A resolver maps an ``action_ref`` (e.g. a trigger id) to its artifact dict so
#: the gate can probe a cutover ``plan_exempt_reason`` when the caller only holds
#: an id (the backstop layer). It is awaited; returning ``None`` means "no
#: artifact / no exemption".
ArtifactResolver = Callable[[Any], Awaitable[dict | None]]


@dataclass(frozen=True)
class PlanGateDecision:
    """Structured outcome of :meth:`PlanModeGate.check`.

    * ``allowed`` — whether the autonomous action may proceed now.
    * ``reason`` — a stable machine code for logging / branching. One of:
      ``confirmed_plan_handoff`` / ``plan_exempt`` (allowed) or
      ``no_confirmed_plan`` / ``plan_not_found`` / ``plan_not_confirmed`` /
      ``version_mismatch`` / ``hash_mismatch`` (blocked).
    * ``needs_plan_payload`` — the §9.2 envelope to return to the caller when
      blocked; ``None`` when allowed.
    * ``exempt_reason`` — the cutover exemption that allowed the action, when
      ``reason == "plan_exempt"``.
    """

    allowed: bool
    reason: str
    needs_plan_payload: dict | None = None
    exempt_reason: str | None = None

    @property
    def needs_plan(self) -> bool:
        """True when the action is blocked pending a confirmed plan."""
        return not self.allowed


class PlanModeGate:
    """Stateless decision service. Holds no per-request state."""

    async def check(
        self,
        db: Any,
        *,
        agent_id: UUID,
        action_kind: str,
        action_ref: Any = None,
        confirmed_plan_id: UUID | str | None = None,
        plan_version: int | None = None,
        plan_hash: str | None = None,
        action_artifact: dict | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> PlanGateDecision:
        """Decide whether ``action_kind`` may start for ``agent_id`` (§9.0 / §9.2).

        Resolution order (first match wins, short-circuiting later probes):

        1. **Confirmed-plan handoff** — a ``confirmed_plan_id`` resolving to a
           ``confirmed`` :class:`AgentPlanRequest` whose ``plan_version`` /
           ``plan_hash`` match the submitted ones (when supplied). -> *allow*.
        2. **Cutover exemption** — the action's artifact carries
           ``plan_exempt_reason`` (probed from ``action_artifact`` or via
           ``artifact_resolver(action_ref)``). -> *allow*.
        3. Otherwise -> *needs_plan* (fail-closed), with a §9.2 envelope.

        Args:
            db: A live async session (the gate is read-only; the caller owns the
                transaction).
            agent_id: The agent the action belongs to.
            action_kind: One of :data:`plan_mode_core.ACTION_KINDS`.
            action_ref: Optional id of the artifact under scrutiny (e.g. a
                trigger id) — passed to ``artifact_resolver``.
            confirmed_plan_id: The plan claimed to authorise this action.
            plan_version / plan_hash: The version/hash the caller is handing off
                against; matched against the stored confirmed plan when present.
            action_artifact: The artifact dict, when the caller has it inline.
            artifact_resolver: Async callback to fetch the artifact from
                ``action_ref`` when not passed inline.

        Raises:
            ValueError: if ``action_kind`` is not a known gate action (a
                programmer error — fail fast per the error-handling policy).
        """
        # Fail fast on an unknown action_kind: this is a wiring bug, not a
        # runtime condition, so surface it loudly rather than silently allowing.
        intent_type = core.intent_type_for_action(action_kind)

        # 1) Confirmed-plan handoff path.
        if confirmed_plan_id is not None:
            decision = await self._check_confirmed_plan(
                db,
                action_kind=action_kind,
                intent_type=intent_type,
                agent_id=agent_id,
                confirmed_plan_id=confirmed_plan_id,
                plan_version=plan_version,
                plan_hash=plan_hash,
                action_artifact=action_artifact,
            )
            if decision is not None:
                return decision

        # 2) Cutover exemption path.
        exempt_reason = await self._resolve_exempt_reason(
            action_artifact=action_artifact,
            action_ref=action_ref,
            artifact_resolver=artifact_resolver,
        )
        if exempt_reason is not None:
            logger.info(
                "plan_gate_allow_exempt",
                extra={
                    "agent_id": str(agent_id),
                    "action_kind": action_kind,
                    "exempt_reason": exempt_reason,
                },
            )
            return PlanGateDecision(
                allowed=True,
                reason="plan_exempt",
                exempt_reason=exempt_reason,
            )

        # 3) Fail-closed: no confirmed plan, no exemption.
        logger.info(
            "plan_gate_needs_plan",
            extra={"agent_id": str(agent_id), "action_kind": action_kind, "reason": "no_confirmed_plan"},
        )
        return PlanGateDecision(
            allowed=False,
            reason="no_confirmed_plan",
            needs_plan_payload=core.build_needs_plan_payload(),
        )

    # -- step 1: confirmed-plan handoff -----------------------------------

    async def _check_confirmed_plan(
        self,
        db: Any,
        *,
        action_kind: str,
        intent_type: str,
        agent_id: UUID,
        confirmed_plan_id: UUID | str,
        plan_version: int | None,
        plan_hash: str | None,
        action_artifact: dict | None,
    ) -> PlanGateDecision | None:
        """Resolve and validate the referenced plan.

        Returns an *allow* decision on a clean confirmed handoff, a *needs_plan*
        decision when the referenced plan is missing / unconfirmed / mismatched,
        and never falls through to exemption once a ``confirmed_plan_id`` was
        supplied — a caller asserting a specific plan must satisfy it.
        """
        plan = await self._load_plan(db, confirmed_plan_id)
        if plan is None:
            logger.info(
                "plan_gate_needs_plan",
                extra={"action_kind": action_kind, "reason": "plan_not_found", "plan_id": str(confirmed_plan_id)},
            )
            return PlanGateDecision(
                allowed=False,
                reason="plan_not_found",
                needs_plan_payload=core.build_needs_plan_payload(
                    summary=(
                        "The referenced plan was not found. Create a plan and have the user confirm it before "
                        "starting this autonomous action."
                    ),
                ),
            )

        if str(getattr(plan, "agent_id", "")) != str(agent_id):
            logger.info(
                "plan_gate_needs_plan",
                extra={"action_kind": action_kind, "reason": "plan_agent_mismatch", "plan_id": str(plan.id)},
            )
            return PlanGateDecision(
                allowed=False,
                reason="plan_agent_mismatch",
                needs_plan_payload=core.build_needs_plan_payload(
                    plan_id=plan.id,
                    plan_version=plan.plan_version,
                    summary="The referenced plan belongs to a different agent. Confirm a plan for this agent first.",
                ),
            )

        if str(getattr(plan, "intent_type", "") or "") != intent_type:
            logger.info(
                "plan_gate_needs_plan",
                extra={
                    "action_kind": action_kind,
                    "reason": "plan_intent_mismatch",
                    "plan_id": str(plan.id),
                    "plan_intent": getattr(plan, "intent_type", None),
                    "required_intent": intent_type,
                },
            )
            return PlanGateDecision(
                allowed=False,
                reason="plan_intent_mismatch",
                needs_plan_payload=core.build_needs_plan_payload(
                    plan_id=plan.id,
                    plan_version=plan.plan_version,
                    summary=(
                        f"The referenced plan is for {getattr(plan, 'intent_type', None)!r}, "
                        f"but this action requires {intent_type!r}."
                    ),
                ),
            )

        check = core.validate_plan_handoff(
            status=plan.status,
            stored_version=plan.plan_version,
            stored_hash=plan.plan_hash or "",
            submitted_version=plan_version,
            submitted_hash=plan_hash,
        )
        if check.ok:
            artifact_check = self._validate_confirmed_action_artifact(plan, action_artifact)
            if action_kind == "start_workflow" and artifact_check is not None:
                logger.info(
                    "plan_gate_needs_plan",
                    extra={"action_kind": action_kind, "reason": artifact_check, "plan_id": str(plan.id)},
                )
                return PlanGateDecision(
                    allowed=False,
                    reason=artifact_check,
                    needs_plan_payload=core.build_needs_plan_payload(
                        plan_id=plan.id,
                        plan_version=plan.plan_version,
                        summary=(
                            "The confirmed plan does not match the action payload being started. "
                            "Regenerate and confirm a plan for this exact action."
                        ),
                    ),
                )
            return PlanGateDecision(allowed=True, reason="confirmed_plan_handoff")

        logger.info(
            "plan_gate_needs_plan",
            extra={"action_kind": action_kind, "reason": check.error_code, "plan_id": str(plan.id)},
        )
        return PlanGateDecision(
            allowed=False,
            reason=check.error_code or "plan_not_confirmed",
            needs_plan_payload=core.build_needs_plan_payload(
                plan_id=plan.id,
                plan_version=plan.plan_version,
                summary=check.message or core.default_needs_plan_summary(),
            ),
        )

    async def _load_plan(self, db: Any, plan_id: UUID | str) -> AgentPlanRequest | None:
        stmt = select(AgentPlanRequest).where(AgentPlanRequest.id == plan_id)
        # Lookup hint consumed by the test fakes; ignored by the real driver
        # (mirrors PlanModeService._load).
        stmt._plan_lookup_id = str(plan_id)  # type: ignore[attr-defined]
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _expected_action_artifact(plan: AgentPlanRequest) -> dict | None:
        """Return the artifact recorded at plan-confirmation time, if any.

        High-risk workflow starts pass a caller-side ``action_artifact``
        (definition hash, args hash, risk reasons). A confirmed plan must
        carry the same artifact in a durable field; otherwise a generic
        long-task plan could authorise an unrelated workflow definition or
        a different argument set.
        """
        metadata = getattr(plan, "metadata_json", None) or {}
        if isinstance(metadata.get("confirmed_action_artifact"), dict):
            return metadata["confirmed_action_artifact"]
        if isinstance(metadata.get("action_artifact"), dict):
            return metadata["action_artifact"]

        handoff = getattr(plan, "handoff_payload", None) or {}
        if isinstance(handoff, dict) and isinstance(handoff.get("action_artifact"), dict):
            return handoff["action_artifact"]

        plan_json = getattr(plan, "plan_json", None) or {}
        if isinstance(plan_json, dict):
            handoff_json = plan_json.get("handoff")
            if isinstance(handoff_json, dict) and isinstance(handoff_json.get("action_artifact"), dict):
                return handoff_json["action_artifact"]
            if isinstance(plan_json.get("action_artifact"), dict):
                return plan_json["action_artifact"]
        return None

    @classmethod
    def _validate_confirmed_action_artifact(cls, plan: AgentPlanRequest, action_artifact: dict | None) -> str | None:
        if action_artifact is None:
            return None
        expected = cls._expected_action_artifact(plan)
        if expected is None:
            return "action_artifact_missing"
        for key, value in action_artifact.items():
            if expected.get(key) != value:
                return "action_artifact_mismatch"
        return None

    # -- step 2: cutover exemption ----------------------------------------

    async def _resolve_exempt_reason(
        self,
        *,
        action_artifact: dict | None,
        action_ref: Any,
        artifact_resolver: ArtifactResolver | None,
    ) -> str | None:
        """Probe the inline artifact first, then the resolver, for an exemption."""
        reason = core.extract_plan_exempt_reason(action_artifact)
        if reason is not None:
            return reason

        if artifact_resolver is not None and action_ref is not None:
            resolved = await artifact_resolver(action_ref)
            return core.extract_plan_exempt_reason(resolved)

        return None


def get_plan_mode_gate() -> PlanModeGate:
    """Return a :class:`PlanModeGate` (the service is stateless; callers may
    also instantiate it directly)."""
    return PlanModeGate()


__all__ = ["PlanGateDecision", "PlanModeGate", "get_plan_mode_gate", "async_session"]
