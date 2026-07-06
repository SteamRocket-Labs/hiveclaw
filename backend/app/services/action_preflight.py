"""Action boundary preflight for owner/company-aware execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.runtime.authorization_decision import build_authorization_decision_entry
from app.runtime.ccplus_contracts import TruthEvidencePackV1
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
    explicit_user_authorized: bool = False
    truth_evidence: tuple[TruthEvidencePackV1, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionPreflightResult:
    decision: PreflightDecision
    reasons: list[str] = field(default_factory=list)
    requires_checkpoint: bool = False
    requires_audit: bool = False
    escalation_target: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    evidence_trace: list[dict[str, Any]] = field(default_factory=list)

    def as_decision_trace_preflight(self) -> dict[str, str]:
        payload = {
            "decision": self.decision.value,
            "reasons": ",".join(self.reasons),
            "requires_checkpoint": str(self.requires_checkpoint).lower(),
            "requires_audit": str(self.requires_audit).lower(),
            "escalation_target": self.escalation_target or "",
        }
        if self.evidence_refs:
            payload["evidence_refs"] = ",".join(self.evidence_refs)
        if self.evidence_trace:
            payload["truth_evidence"] = json.dumps(self.evidence_trace, ensure_ascii=False, sort_keys=True)
        return payload

    def as_authorization_decision_entry(
        self,
        request: ActionPreflightInput,
        *,
        resource: str,
        principal: str | None = None,
        company: str | None = None,
        model_visible_message: str | None = None,
    ) -> dict[str, Any]:
        return build_authorization_decision_entry(
            resource=resource,
            action=request.action,
            principal=principal,
            company=company,
            sensitivity=request.sensitivity.value,
            policy="action_preflight",
            result=self.decision.value,
            reason=self.reasons,
            model_visible_message=model_visible_message,
            source="action_preflight",
        )


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
        evidence_refs = [pack.evidence_id for pack in request.truth_evidence if pack.evidence_id]
        evidence_trace = _truth_evidence_trace(request.truth_evidence)
        if not request.runtime_permission_allowed:
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["runtime_permission_denied"],
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        if request.sensitivity == SensitivityLevel.PL4_CREDENTIAL:
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["pl4_zero_retention"],
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        if request.charter_zone == CharterZone.NEVER_DO:
            return ActionPreflightResult(
                decision=PreflightDecision.REFUSE,
                reasons=["charter_never_do"],
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        if request.company_boundary_conflict:
            return ActionPreflightResult(
                decision=PreflightDecision.ESCALATE,
                reasons=["company_boundary_conflict"],
                requires_checkpoint=True,
                requires_audit=True,
                escalation_target="company_admin",
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        high_axes = _axes_at_level(request, BoundaryAxisLevel.HIGH)
        if _truth_search_unavailable(request.truth_evidence) and (
            high_axes or request.charter_zone == CharterZone.CONFIRM_FIRST
        ):
            return ActionPreflightResult(
                decision=PreflightDecision.ASK,
                reasons=["truth_search_unavailable", *[f"high_risk_axis:{axis}" for axis in high_axes]],
                requires_checkpoint=True,
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        if request.explicit_user_authorized and request.charter_zone == CharterZone.CONFIRM_FIRST:
            return ActionPreflightResult(
                decision=PreflightDecision.DO,
                reasons=["explicit_user_authorization"],
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        if high_axes:
            return ActionPreflightResult(
                decision=PreflightDecision.ASK,
                reasons=[f"high_risk_axis:{axis}" for axis in high_axes],
                requires_checkpoint=True,
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        if request.charter_zone == CharterZone.CONFIRM_FIRST:
            return ActionPreflightResult(
                decision=PreflightDecision.ASK,
                reasons=["charter_confirm_first"],
                requires_checkpoint=True,
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        medium_axes = _axes_at_level(request, BoundaryAxisLevel.MEDIUM)
        if medium_axes:
            return ActionPreflightResult(
                decision=PreflightDecision.PREPARE_ONLY,
                reasons=[f"medium_risk_axis:{axis}" for axis in medium_axes],
                requires_checkpoint=False,
                requires_audit=True,
                evidence_refs=evidence_refs,
                evidence_trace=evidence_trace,
            )

        return ActionPreflightResult(
            decision=PreflightDecision.DO,
            reasons=["full_authority_low_risk"],
            evidence_refs=evidence_refs,
            evidence_trace=evidence_trace,
        )


def _axes_at_level(request: ActionPreflightInput, level: BoundaryAxisLevel) -> list[str]:
    return [axis for axis in _AXIS_FIELDS if getattr(request, axis) == level]


def _truth_search_unavailable(evidence_packs: tuple[TruthEvidencePackV1, ...]) -> bool:
    for pack in evidence_packs:
        if str(pack.evidence_id or "").startswith("truth://provider-error/"):
            return True
        if pack.confidence == 0.0 and any(str(item).startswith("provider_") for item in pack.limitations):
            return True
    return False


def _truth_evidence_trace(evidence_packs: tuple[TruthEvidencePackV1, ...]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for pack in evidence_packs:
        if not pack.evidence_id:
            continue
        traces.append(
            {
                "evidence_id": pack.evidence_id,
                "source_refs": list(pack.source_refs),
                "citations": list(pack.citations),
                "digest": pack.digest,
                "provider": pack.provider,
                "limitations": list(pack.limitations),
                "prompt_injection_stripped": pack.prompt_injection_stripped,
                "trace_refs": list(pack.trace_refs),
            }
        )
    return traces
