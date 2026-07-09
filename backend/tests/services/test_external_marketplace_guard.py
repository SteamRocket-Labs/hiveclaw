from __future__ import annotations

from app.services.external_capabilities.marketplace_guard import (
    TrustedMarketplace,
    evaluate_marketplace_impersonation,
    marketplace_name_skeleton,
)

# Vendor-neutral trusted registry fixture. The guard logic never hardcodes a
# vendor identity; the trusted list is injected data (like a host allowlist).
_TRUSTED = (
    TrustedMarketplace(name="acme-official-hub", host="github.com"),
    TrustedMarketplace(name="hive-core-market", host="raw.githubusercontent.com"),
)


def _codes(warnings) -> set[str]:
    return {warning.code for warning in warnings}


def test_exact_trusted_name_and_host_is_not_flagged():
    warnings = evaluate_marketplace_impersonation(
        name="acme-official-hub",
        source_uri="https://github.com/acme-official-hub/marketplace",
        trusted=_TRUSTED,
    )
    assert warnings == []


def test_unrelated_name_is_not_flagged():
    warnings = evaluate_marketplace_impersonation(
        name="weather-plugins",
        source_uri="https://github.com/someone/weather-plugins",
        trusted=_TRUSTED,
    )
    assert warnings == []


def test_case_only_variant_flags_case_variant():
    warnings = evaluate_marketplace_impersonation(
        name="ACME-Official-Hub",
        source_uri="https://github.com/imposter/ACME-Official-Hub",
        trusted=_TRUSTED,
    )
    assert _codes(warnings) == {"case_variant"}
    warning = warnings[0]
    assert warning.trusted_name == "acme-official-hub"
    assert warning.observed == "ACME-Official-Hub"
    # Same host as trusted but a case-mutated name is still a spoofing signal.
    assert warning.host_match is True


def test_cyrillic_homograph_flags_homograph_confusable():
    # First character is Cyrillic U+0430 (а), visually identical to Latin 'a'.
    spoof = "аcme-official-hub"
    assert spoof != "acme-official-hub"
    warnings = evaluate_marketplace_impersonation(
        name=spoof,
        source_uri="https://evil.example.com/hub",
        trusted=_TRUSTED,
    )
    assert _codes(warnings) == {"homograph_confusable"}
    warning = warnings[0]
    assert warning.trusted_name == "acme-official-hub"
    assert warning.host_match is False


def test_fullwidth_homograph_is_normalized_and_flagged():
    # Fullwidth Latin letters (U+FF41.. 'ａｃｍｅ') NFKC-fold to ascii.
    spoof = "ａｃｍｅ-official-hub"
    warnings = evaluate_marketplace_impersonation(
        name=spoof,
        source_uri="https://github.com/acme-official-hub/marketplace",
        trusted=_TRUSTED,
    )
    assert _codes(warnings) == {"homograph_confusable"}


def test_suffix_affix_impersonation_flags_affix():
    warnings = evaluate_marketplace_impersonation(
        name="get-acme-official-hub",
        source_uri="https://github.com/imposter/get-acme-official-hub",
        trusted=_TRUSTED,
    )
    assert _codes(warnings) == {"affix_impersonation"}
    assert warnings[0].trusted_name == "acme-official-hub"


def test_prefix_affix_impersonation_flags_affix():
    warnings = evaluate_marketplace_impersonation(
        name="acme-official-hub-pro",
        source_uri="https://github.com/imposter/acme-official-hub-pro",
        trusted=_TRUSTED,
    )
    assert _codes(warnings) == {"affix_impersonation"}


def test_skeleton_folds_case_and_confusables():
    assert marketplace_name_skeleton("ACME-Official-Hub") == marketplace_name_skeleton("acme-official-hub")
    assert marketplace_name_skeleton("аcme") == marketplace_name_skeleton("acme")
    assert marketplace_name_skeleton("weather") != marketplace_name_skeleton("acme")


def test_warning_serializes_to_dict():
    warnings = evaluate_marketplace_impersonation(
        name="ACME-Official-Hub",
        source_uri="https://github.com/imposter/ACME-Official-Hub",
        trusted=_TRUSTED,
    )
    payload = warnings[0].to_dict()
    assert payload["code"] == "case_variant"
    assert payload["trusted_name"] == "acme-official-hub"
    assert payload["observed"] == "ACME-Official-Hub"
    assert payload["host_match"] is True


def test_empty_trusted_registry_never_flags():
    warnings = evaluate_marketplace_impersonation(
        name="acme-official-hub",
        source_uri="https://github.com/acme-official-hub/marketplace",
        trusted=(),
    )
    assert warnings == []
