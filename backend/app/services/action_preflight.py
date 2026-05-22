"""Action boundary preflight for owner/company-aware execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.services.privacy_layer import SensitivityLevel


class BoundaryAxisLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CharterZone(StrEnum):
    FULL_AUTHORITY = "full_authority"
    CONFIRM_FIRST = "confirm_first"
    NEVER_DO = "never_do"


class PreflightDecision(StrEnum):
    DO = "do"
    PREPARE_ONLY = "prepare_only"
    ASK = "ask"
    REFUSE = "refuse"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class ActionPreflightInput:
    action: str
    reversibility: BoundaryAxisLevel
    representativeness: BoundaryAxisLevel
    judgment_density: BoundaryAxisLevel
    visibility: BoundaryAxisLevel
    domain_specialization: BoundaryAxisLevel
    charter_zone: CharterZone
    sensitivity: SensitivityLevel = SensitivityLevel.PL1_PUBLIC
    runtime_permission_allowed: bool = True
    company_boundary_conflict: bool = False


@dataclass(frozen=True, slots=True)
class ActionPreflightResult:
    decision: PreflightDecision
    reasons: list[str] = field(default_factory=list)
    requires_checkpoint: bool = False
    requires_audit: bool = False
    escalation_target: str | None = None

    def as_decision_trace_preflight(self) -> dict[str, str]:
        return {
            "decision": self.decision.value,
            "reasons": ",".join(self.reasons),
            "requires_checkpoint": str(self.requires_checkpoint).lower(),
            "requires_audit": str(self.requires_audit).lower(),
            "escalation_target": self.escalation_target or "",
        }


_AXIS_FIELDS = (
    "reversibility",
    "representativeness",
    "judgment_density",
    "visibility",
    "domain_specialization",
)


class ActionPreflightService:
    """Classify whether an action may execute, must ask, or must refuse."""

    def evaluate(self, request: ActionPreflightInput) -> ActionPreflightResult:
        if not request.runtime_permission_allowed:
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["runtime_permission_denied"],
                requires_audit=True,
            )

        if request.sensitivity == SensitivityLevel.PL4_CREDENTIAL:
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["pl4_zero_retention"],
                requires_audit=True,
            )

        if request.charter_zone == CharterZone.NEVER_DO:
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["charter_never_do"],
                requires_audit=True,
            )

        if request.company_boundary_conflict:
            return ActionPreflightResult(
                decision=PreflightDecision.ESCALATE,
                reasons=["company_boundary_conflict"],
                requires_checkpoint=True,
                requires_audit=True,
                escalation_target="company_admin",
            )

        high_axes = _axes_at_level(request, BoundaryAxisLevel.HIGH)
        if high_axes:
            return ActionPreflightResult(
                decision=PreflightDecision.ASK,
                reasons=[f"high_risk_axis:{axis}" for axis in high_axes],
                requires_checkpoint=True,
                requires_audit=True,
            )

        if request.charter_zone == CharterZone.CONFIRM_FIRST:
            return ActionPreflightResult(
                decision=PreflightDecision.ASK,
                reasons=["charter_confirm_first"],
                requires_checkpoint=True,
                requires_audit=True,
            )

        medium_axes = _axes_at_level(request, BoundaryAxisLevel.MEDIUM)
        if medium_axes:
            return ActionPreflightResult(
                decision=PreflightDecision.PREPARE_ONLY,
                reasons=[f"medium_risk_axis:{axis}" for axis in medium_axes],
                requires_checkpoint=False,
                requires_audit=True,
            )

        return ActionPreflightResult(
            decision=PreflightDecision.DO,
            reasons=["full_authority_low_risk"],
        )


def _axes_at_level(request: ActionPreflightInput, level: BoundaryAxisLevel) -> list[str]:
    return [axis for axis in _AXIS_FIELDS if getattr(request, axis) == level]
