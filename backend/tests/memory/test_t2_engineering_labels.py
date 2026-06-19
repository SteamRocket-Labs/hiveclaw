from __future__ import annotations

import pytest


def test_confidence_is_formula_based_and_rounded_to_nearest_005() -> None:
    from app.memory.t2.engineering_labels import compute_engineering_confidence

    score = compute_engineering_confidence(
        evidence_coverage=0.80,
        source_integrity="complete",
        label_specificity=0.75,
        internal_consistency=1.0,
        closure_status="closed",
        penalties=["principal_scope_unknown_visibility"],
    )

    assert score == 0.80


@pytest.mark.parametrize(
    ("source_integrity", "expected"),
    [("complete", 1.0), ("partial", 0.7), ("replayed", 0.6), ("missing_refs", 0.25), ("unknown", 0.25)],
)
def test_source_integrity_scores_are_bounded(source_integrity: str, expected: float) -> None:
    from app.memory.t2.engineering_labels import source_integrity_score

    assert source_integrity_score(source_integrity) == expected


def test_risk_flags_are_deterministic_from_evidence_boundary() -> None:
    from app.memory.t2.engineering_labels import derive_risk_flags

    flags = derive_risk_flags(
        text="Railway production login failed because RLS auth token crossed tenant boundary.",
        source_integrity="missing_refs",
        principal_scope="unknown",
        sensitivity="PL3",
    )

    assert flags == [
        "cross_tenant",
        "security_relevant",
        "production_impact",
        "evidence_gap",
    ]


def test_systems_are_capped_and_registry_bound() -> None:
    from app.memory.t2.engineering_labels import normalize_systems

    systems = normalize_systems(
        ["memory", "runtime", "auth", "railway", "workflow", "unknown", "office"],
        registry={"memory", "runtime", "auth", "railway", "workflow", "office"},
    )

    assert systems == ["memory", "runtime", "auth", "railway", "workflow"]
