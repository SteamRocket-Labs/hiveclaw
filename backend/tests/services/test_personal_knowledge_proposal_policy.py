from __future__ import annotations

from app.services.personal_knowledge_proposals import (
    build_personal_knowledge_unified_diff,
    evaluate_proposal_content,
)


def test_proposal_policy_rejects_credentials_without_persisting_plaintext() -> None:
    decision = evaluate_proposal_content(
        content="Deploy with api_key=sk-abcdefghijklmnopqrstuvwxyz123456",
        declared_sensitivity="internal",
        max_chars=20_000,
    )

    assert decision.outcome == "reject"
    assert decision.sensitivity == "PL4_credential"
    assert "credential_zero_retention" in decision.reason_codes
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in decision.content


def test_proposal_policy_upgrades_underclassified_pii_and_preserves_evidence_shape() -> None:
    decision = evaluate_proposal_content(
        content="Owner contact: owner@example.com",
        declared_sensitivity="public",
        max_chars=20_000,
    )

    assert decision.outcome == "ask"
    assert decision.sensitivity == "PL2_pii"
    assert decision.content == "Owner contact: <Email_1>"
    assert "sensitivity_upgraded" in decision.reason_codes


def test_proposal_policy_uses_the_shared_canonical_pl3_aliases() -> None:
    decision = evaluate_proposal_content(
        content="Board-only operating context.",
        declared_sensitivity="confidential",
        max_chars=20_000,
    )

    assert decision.outcome == "ask"
    assert decision.sensitivity == "PL3_sensitive"


def test_proposal_policy_rejects_oversized_content() -> None:
    decision = evaluate_proposal_content(
        content="x" * 101,
        declared_sensitivity="internal",
        max_chars=100,
    )

    assert decision.outcome == "reject"
    assert decision.reason_codes == ("content_too_large",)


def test_personal_knowledge_proposal_builds_reviewable_unified_diff() -> None:
    diff = build_personal_knowledge_unified_diff(
        previous="# Release\n\nUse a canary first.",
        proposed="# Release\n\nUse a canary first, then verify rollback telemetry.",
    )

    assert "--- current" in diff
    assert "+++ proposed" in diff
    assert "-Use a canary first." in diff
    assert "+Use a canary first, then verify rollback telemetry." in diff
