from __future__ import annotations

from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
from app.services.privacy_layer import PrivacyLayer, PrivacyStore, SensitivityLevel


def test_privacy_layer_does_not_turn_a_secret_shaped_fixture_into_credential_truth() -> None:
    layer = PrivacyLayer(store=PrivacyStore())

    text = "Owner documented api_key=sk-1234567890abcdefghijklmnop as a test fixture."
    decision = layer.classify_and_mask(text)

    assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC
    assert decision.rejected is False
    assert decision.sanitized_text == text
    assert decision.credential_candidate_count == 1


def test_privacy_layer_rejects_only_an_exact_authoritative_secret_binding() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary

    active_secret = "sk-live-tenant-secret-0123456789"
    layer = PrivacyLayer(
        store=PrivacyStore(),
        secret_boundary=ExactSecretBoundary.from_pairs((("tool-config://tenant-1/search/api_key", active_secret),)),
    )

    decision = layer.classify_and_mask(f"Do not persist {active_secret}.")

    assert decision.sensitivity == SensitivityLevel.PL4_CREDENTIAL
    assert decision.rejected is True
    assert decision.sanitized_text == "Do not persist [REDACTED_SECRET]."
    assert decision.secret_evidence_refs == ("tool-config://tenant-1/search/api_key",)


def test_privacy_layer_uses_typed_placeholders_for_pii() -> None:
    store = PrivacyStore()
    layer = PrivacyLayer(store=store)

    decision = layer.classify_and_mask("Alice email is alice@example.com")

    assert decision.sensitivity == SensitivityLevel.PL2_PII
    assert decision.sanitized_text == "Alice email is <Email_1>"
    assert store.unmask(decision.sanitized_text) == "Alice email is alice@example.com"


def test_clock_times_are_not_redacted_as_phone() -> None:
    # D9: redactor mistook 17:00 etc. for a phone number (Railway prod: <Phone_1>:00).
    # sensitivity hints must assist, not corrupt, content (spec §4.2 / AI-Native L1).
    layer = PrivacyLayer(store=PrivacyStore())

    # Date prefix is what triggered the bug: "YYYY-MM-DD HH" formed a >=10-digit run
    # whose match stopped at the ":" of HH:MM, redacting the date+hour as a phone.
    for clock in ("17:00", "09:30", "23:59"):
        line = f"2026-06-04 {clock} evening_scan"
        decision = layer.classify_and_mask(line)

        assert decision.sanitized_text == line, clock
        assert "<Phone" not in decision.sanitized_text, clock
        assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC, clock


def test_public_scan_log_is_not_mislabeled_pii() -> None:
    # D9: a public expo-radar scan line (date + clock) was redacted to
    # "<Phone_1>:00 evening_scan" and mislabeled PL2_pii. Public content is not PII.
    layer = PrivacyLayer(store=PrivacyStore())
    log_line = "2026-06-04 17:00 evening_scan: 14 expos in window unchanged"

    decision = layer.classify_and_mask(log_line)

    assert decision.sanitized_text == log_line
    assert "<Phone" not in decision.sanitized_text
    assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC


def test_real_phone_numbers_are_still_redacted() -> None:
    # Positive guard: tightening the regex must NOT let real phones leak through.
    plain = PrivacyLayer(store=PrivacyStore()).classify_and_mask("Reach me at 13812345678 anytime")
    assert "13812345678" not in plain.sanitized_text
    assert "<Phone_1>" in plain.sanitized_text
    assert plain.sensitivity == SensitivityLevel.PL2_PII

    spaced = PrivacyLayer(store=PrivacyStore()).classify_and_mask("Owner phone: +86 138 1234 5678")
    assert "138 1234 5678" not in spaced.sanitized_text
    assert "<Phone_1>" in spaced.sanitized_text
    assert spaced.sensitivity == SensitivityLevel.PL2_PII


def test_uuid_evidence_references_are_not_redacted_as_phone_numbers() -> None:
    layer = PrivacyLayer(store=PrivacyStore())
    source_ref = "knowledge_id=9bd5f6fa-4a09-4f08-8abc-295353671106"

    decision = layer.classify_and_mask(source_ref)

    assert decision.sanitized_text == source_ref
    assert decision.placeholders == {}
    assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC


def test_long_numeric_opaque_identifiers_are_not_redacted_as_phone_numbers() -> None:
    layer = PrivacyLayer(store=PrivacyStore())
    opaque_ref = "event_id=12345678-1234-1234-1234-123456789012"

    decision = layer.classify_and_mask(opaque_ref)

    assert decision.sanitized_text == opaque_ref
    assert decision.placeholders == {}
    assert decision.sensitivity == SensitivityLevel.PL1_PUBLIC


def test_pl3_is_suppressed_for_non_owner_current_user() -> None:
    stack = PrincipalStack(
        company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
        direct_owner=Principal(PrincipalRole.OWNER, "owner", "Owner"),
        current_user=Principal(PrincipalRole.CURRENT_USER, "viewer", "Viewer"),
    )

    assert (
        PrivacyLayer.redact_for_principal("Q3 salary plan", SensitivityLevel.PL3_SENSITIVE, stack) == "[REDACTED_PL3]"
    )
