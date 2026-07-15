from __future__ import annotations

import pytest

from app.services.privacy_layer import (
    SensitivityLevel,
    canonicalize_sensitivity,
    is_sensitive_extraction_blocked,
    sensitivity_rank,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("public", SensitivityLevel.PL1_PUBLIC),
        ("internal", SensitivityLevel.PL1_PUBLIC),
        ("PL1_public", SensitivityLevel.PL1_PUBLIC),
        ("pii", SensitivityLevel.PL2_PII),
        ("PL2_pii", SensitivityLevel.PL2_PII),
        ("private", SensitivityLevel.PL3_SENSITIVE),
        ("confidential", SensitivityLevel.PL3_SENSITIVE),
        ("secret", SensitivityLevel.PL3_SENSITIVE),
        ("restricted", SensitivityLevel.PL3_SENSITIVE),
        ("PL3_sensitive", SensitivityLevel.PL3_SENSITIVE),
        ("credential", SensitivityLevel.PL4_CREDENTIAL),
        ("PL4_credential", SensitivityLevel.PL4_CREDENTIAL),
    ],
)
def test_personal_knowledge_sensitivity_aliases_have_one_canonical_enum(
    raw: str,
    expected: SensitivityLevel,
) -> None:
    assert canonicalize_sensitivity(raw) is expected
    assert sensitivity_rank(raw) == sensitivity_rank(expected)


@pytest.mark.parametrize(
    "raw",
    [
        "private",
        "confidential",
        "secret",
        "restricted",
        "PL3_sensitive",
        "credential",
        "PL4_credential",
        "future_unknown_level",
    ],
)
def test_personal_knowledge_extraction_block_is_alias_complete_and_unknown_fail_closed(raw: str) -> None:
    assert is_sensitive_extraction_blocked(raw) is True


def test_invalid_sensitivity_is_rejected_at_write_boundaries() -> None:
    with pytest.raises(ValueError, match="unsupported sensitivity"):
        canonicalize_sensitivity("future_unknown_level")
