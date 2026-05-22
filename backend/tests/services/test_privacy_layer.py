from __future__ import annotations

from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
from app.services.privacy_layer import PrivacyLayer, PrivacyStore, SensitivityLevel


def test_privacy_layer_rejects_credentials_before_memory_write() -> None:
    layer = PrivacyLayer(store=PrivacyStore())

    decision = layer.classify_and_mask("Owner shared api_key=sk-1234567890abcdefghijklmnop for setup.")

    assert decision.sensitivity == SensitivityLevel.PL4_CREDENTIAL
    assert decision.rejected
    assert "sk-1234567890abcdefghijklmnop" not in decision.sanitized_text
    assert "<Credential_1>" in decision.sanitized_text


def test_privacy_layer_uses_typed_placeholders_for_pii() -> None:
    store = PrivacyStore()
    layer = PrivacyLayer(store=store)

    decision = layer.classify_and_mask("Alice email is alice@example.com")

    assert decision.sensitivity == SensitivityLevel.PL2_PII
    assert decision.sanitized_text == "Alice email is <Email_1>"
    assert store.unmask(decision.sanitized_text) == "Alice email is alice@example.com"


def test_pl3_is_suppressed_for_non_owner_current_user() -> None:
    stack = PrincipalStack(
        company=Principal(PrincipalRole.COMPANY, "tenant-1", "Acme"),
        direct_owner=Principal(PrincipalRole.OWNER, "owner", "Owner"),
        current_user=Principal(PrincipalRole.CURRENT_USER, "viewer", "Viewer"),
    )

    assert PrivacyLayer.redact_for_principal("Q3 salary plan", SensitivityLevel.PL3_SENSITIVE, stack) == "[REDACTED_PL3]"

