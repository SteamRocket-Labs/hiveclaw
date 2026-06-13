"""Controlled proactive employee loop planning for heartbeat runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.coordination import CoordinationRuntime, SentinelEmission, coordination_runtime
from app.services.action_preflight import (
    ActionPreflightInput,
    ActionPreflightResult,
    ActionPreflightService,
    BoundaryAxisLevel,
    PreflightDecision,
)
from app.services.agency_charter import AgentAccountabilityContext
from app.services.privacy_layer import SensitivityLevel


@dataclass(frozen=True, slots=True)
class ProactiveCandidate:
    action: str
    evidence: str
    objective_id: str
    preflight: ActionPreflightResult
    posture_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ProactiveEmployeePlan:
    candidates: list[ProactiveCandidate]
    emissions: list[SentinelEmission]
    markdown: str


def build_proactive_employee_plan(
    *,
    agent_id: str,
    accountability: AgentAccountabilityContext,
    recent_activities: list[Any],
    coordination: CoordinationRuntime | None = None,
    preflight_service: ActionPreflightService | None = None,
) -> ProactiveEmployeePlan:
    runtime = coordination or coordination_runtime
    service = preflight_service or ActionPreflightService()
    candidates: list[ProactiveCandidate] = []
    emissions: list[SentinelEmission] = []

    for index, activity in enumerate(recent_activities[:10]):
        detail = _activity_detail(activity)
        if not _looks_like_open_loop(activity, detail):
            continue

        action = str(detail.get("proactive_action") or _default_prepare_action(activity)).strip()
        objective_id = str(detail.get("objective_id") or f"activity-{index}")
        posture = accountability.action_posture(action)
        preflight = service.evaluate(_build_preflight_input(action, posture))
        candidate = ProactiveCandidate(
            action=action,
            evidence=f"{getattr(activity, 'action_type', 'activity')}: {getattr(activity, 'summary', '')}",
            objective_id=objective_id,
            preflight=preflight,
            posture_reasons=posture.reasons,
        )
        candidates.append(candidate)

        # A2 (docs/agent-lifecycle-cc-alignment.md 主题 A): preflight is a
        # boundary PROVIDER, not the decider. A DO-cleared candidate is NOT
        # pre-fired by the system — it reaches the agent as decision input
        # (markdown below) and the agent judges in its own run whether and
        # how to act. Only governance stays system-owned: checkpoint creation
        # for approval-gated actions, and REFUSE enforcement.
        sentinel_id = f"proactive:{agent_id}:{objective_id}:{index}"
        if preflight.requires_checkpoint and preflight.decision != PreflightDecision.REFUSE:
            escalation_chain = [
                target for target in (posture.escalation_target, preflight.escalation_target, "company_admin") if target
            ]
            sentinel = runtime.register_sentinel(
                sentinel_id=sentinel_id,
                owner_agent_id=accountability.owner_charter.owner_id,
                target_agent_id=agent_id,
                condition=action,
                runtime_path="checkpoint",
                checkpoint_approver_id=accountability.owner_charter.owner_id,
                escalation_chain=escalation_chain[:1],
            )
            emissions.append(
                runtime.fire_sentinel(
                    sentinel.id,
                    content=action,
                    thread_id=objective_id,
                    metadata={
                        "objective_id": objective_id,
                        "evidence": candidate.evidence,
                        "preflight_decision": preflight.decision.value,
                    },
                )
            )

    return ProactiveEmployeePlan(
        candidates=candidates,
        emissions=emissions,
        markdown=_render_proactive_plan_markdown(candidates),
    )


def _activity_detail(activity: Any) -> dict[str, Any]:
    detail = getattr(activity, "detail_json", None)
    return dict(detail) if isinstance(detail, dict) else {}


def _looks_like_open_loop(activity: Any, detail: dict[str, Any]) -> bool:
    if detail.get("open_loop") or detail.get("proactive_action"):
        return True
    text = f"{getattr(activity, 'summary', '')} {detail}".lower()
    return any(marker in text for marker in ("waiting", "follow-up", "follow up", "ready to send", "blocked"))


def _default_prepare_action(activity: Any) -> str:
    summary = str(getattr(activity, "summary", "")).strip()
    return "prepare local draft" + (f" for {summary}" if summary else "")


def _build_preflight_input(action: str, posture) -> ActionPreflightInput:
    lower = action.lower()
    external = any(term in lower for term in ("send external", "vendor", "customer", "reply externally"))
    credential = any(term in lower for term in ("credential", "api key", "secret", "password"))
    if external:
        reversibility = BoundaryAxisLevel.MEDIUM
        representativeness = BoundaryAxisLevel.HIGH
        judgment_density = BoundaryAxisLevel.HIGH
        visibility = BoundaryAxisLevel.HIGH
        domain_specialization = BoundaryAxisLevel.MEDIUM
    else:
        reversibility = BoundaryAxisLevel.LOW
        representativeness = BoundaryAxisLevel.LOW
        judgment_density = BoundaryAxisLevel.LOW
        visibility = BoundaryAxisLevel.LOW
        domain_specialization = BoundaryAxisLevel.LOW

    return ActionPreflightInput(
        action=action,
        reversibility=reversibility,
        representativeness=representativeness,
        judgment_density=judgment_density,
        visibility=visibility,
        domain_specialization=domain_specialization,
        charter_zone=posture.charter_zone,
        sensitivity=SensitivityLevel.PL4_CREDENTIAL if credential else SensitivityLevel.PL1_PUBLIC,
        company_boundary_conflict=posture.company_boundary_conflict,
    )


def _render_proactive_plan_markdown(candidates: list[ProactiveCandidate]) -> str:
    """Render open-loop candidates as DECISION INPUT for the agent (A2).

    Preflight assessments are boundaries, not verdicts: the agent judges what
    is worth doing within them. Boundaries are enforced by the system
    regardless of the agent's choice (checkpoint/refuse stay system-owned).
    """
    if not candidates:
        return ""
    lines = [
        "---",
        "## Proactive Steward Context",
        "Open loops detected from recent activity. Preflight gives boundary assessments "
        "as INPUT — the judgment of whether and how to act is yours, within the stated "
        "boundaries (which the system enforces regardless).",
    ]
    for candidate in candidates:
        action = candidate.action[:240]
        if candidate.preflight.decision == PreflightDecision.DO:
            label = "Cleared — your call"
            directive = (
                "Preflight cleared this as low-risk. Decide in this run whether it is worth "
                "doing now: if yes, do the reversible preparation yourself (local draft / "
                "read-only artifact, nothing externally visible); if not, record why and move on."
            )
        elif candidate.preflight.decision in {PreflightDecision.ASK, PreflightDecision.ESCALATE}:
            label = "Checkpoint pending"
            directive = (
                "A checkpoint was created for this action (external-visible — needs approval). "
                "You may prepare the artifact now, but do NOT perform the visible action until approved."
            )
        elif candidate.preflight.decision == PreflightDecision.REFUSE:
            label = "Refused by boundary"
            directive = "Do not perform this action; record the boundary and escalate only through policy."
        else:
            label = "Prepare only"
            directive = "Prepare reversible local work only."
        lines.extend(
            [
                f"- {label}: {_sentence_case(action)}",
                f"  Action: {candidate.action[:240]}",
                f"  Evidence: {candidate.evidence[:240]}",
                f"  Preflight: {candidate.preflight.decision.value} ({', '.join(candidate.preflight.reasons)})",
                f"  Runtime: {directive}",
            ]
        )
    return "\n".join(lines)


def _sentence_case(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text
