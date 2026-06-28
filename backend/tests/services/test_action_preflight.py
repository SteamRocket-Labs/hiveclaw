from __future__ import annotations

import json

from app.runtime.ccplus_contracts import TruthEvidencePackV1
from app.services.action_preflight import (
    ActionPreflightInput,
    ActionPreflightService,
    BoundaryAxisLevel,
    CharterZone,
    PreflightDecision,
    SensitivityLevel,
)


def _low_risk_input(**overrides) -> ActionPreflightInput:
    data = {
        "action": "prepare local research summary",
        "reversibility": BoundaryAxisLevel.LOW,
        "representativeness": BoundaryAxisLevel.LOW,
        "judgment_density": BoundaryAxisLevel.LOW,
        "visibility": BoundaryAxisLevel.LOW,
        "domain_specialization": BoundaryAxisLevel.LOW,
        "charter_zone": CharterZone.FULL_AUTHORITY,
        "sensitivity": SensitivityLevel.PL1_PUBLIC,
        "runtime_permission_allowed": True,
        "company_boundary_conflict": False,
    }
    data.update(overrides)
    return ActionPreflightInput(**data)


def test_low_risk_full_authority_action_can_execute() -> None:
    result = ActionPreflightService().evaluate(_low_risk_input())

    assert result.decision == PreflightDecision.DO
    assert result.requires_checkpoint is False
    assert result.requires_audit is False
    assert result.reasons == ["full_authority_low_risk"]


def test_confirm_first_action_asks_even_when_axis_risk_is_low() -> None:
    result = ActionPreflightService().evaluate(_low_risk_input(charter_zone=CharterZone.CONFIRM_FIRST))

    assert result.decision == PreflightDecision.ASK
    assert result.requires_checkpoint is True
    assert "charter_confirm_first" in result.reasons


def test_high_external_visibility_requires_owner_checkpoint() -> None:
    result = ActionPreflightService().evaluate(
        _low_risk_input(
            action="send external vendor reply",
            representativeness=BoundaryAxisLevel.HIGH,
            visibility=BoundaryAxisLevel.HIGH,
        )
    )

    assert result.decision == PreflightDecision.ASK
    assert result.requires_checkpoint is True
    assert "high_risk_axis:representativeness" in result.reasons
    assert "high_risk_axis:visibility" in result.reasons


def test_company_boundary_conflict_escalates_above_owner_preference() -> None:
    result = ActionPreflightService().evaluate(_low_risk_input(company_boundary_conflict=True))

    assert result.decision == PreflightDecision.ESCALATE
    assert result.requires_checkpoint is True
    assert result.requires_audit is True
    assert result.escalation_target == "company_admin"
    assert "company_boundary_conflict" in result.reasons


def test_never_do_or_runtime_denied_refuses() -> None:
    service = ActionPreflightService()

    never_do = service.evaluate(_low_risk_input(charter_zone=CharterZone.NEVER_DO))
    runtime_denied = service.evaluate(_low_risk_input(runtime_permission_allowed=False))

    assert never_do.decision == PreflightDecision.REFUSE
    assert "charter_never_do" in never_do.reasons
    assert runtime_denied.decision == PreflightDecision.REFUSE
    assert "runtime_permission_denied" in runtime_denied.reasons


def test_pl4_credential_action_refuses_even_with_full_authority() -> None:
    result = ActionPreflightService().evaluate(_low_risk_input(sensitivity=SensitivityLevel.PL4_CREDENTIAL))

    assert result.decision == PreflightDecision.REFUSE
    assert result.requires_audit is True
    assert "pl4_zero_retention" in result.reasons


def test_preflight_trace_persists_truth_evidence_payload() -> None:
    evidence = TruthEvidencePackV1(
        evidence_id="truth://policy/email-confirmation",
        query="send email",
        source_refs=("knowledge://policy/email",),
        citations=("policy/email",),
        snippets=("External email requires owner confirmation.",),
        digest="digest-1",
        provider="openviking",
        limitations=("prompt_injection_stripped",),
        prompt_injection_stripped=True,
        trace_refs=("truth_search:digest-1",),
    )

    result = ActionPreflightService().evaluate(
        _low_risk_input(
            representativeness=BoundaryAxisLevel.HIGH,
            visibility=BoundaryAxisLevel.HIGH,
            truth_evidence=(evidence,),
        )
    )
    trace_payload = result.as_decision_trace_preflight()

    assert trace_payload["evidence_refs"] == "truth://policy/email-confirmation"
    persisted = json.loads(trace_payload["truth_evidence"])
    assert persisted == [
        {
            "evidence_id": "truth://policy/email-confirmation",
            "source_refs": ["knowledge://policy/email"],
            "citations": ["policy/email"],
            "digest": "digest-1",
            "provider": "openviking",
            "limitations": ["prompt_injection_stripped"],
            "prompt_injection_stripped": True,
            "trace_refs": ["truth_search:digest-1"],
        }
    ]
