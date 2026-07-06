from __future__ import annotations

import json

import pytest


def test_activation_score_computes_weighted_total_and_reasons() -> None:
    from app.runtime.activation_candidates import ActivationScore

    score = ActivationScore(
        head_scores={"semantic": 0.9, "recency": 0.3, "authority": 0.7},
        weights={"semantic": 0.5, "recency": 0.2, "authority": 0.3},
        reasons=("semantic_match", "source_backed"),
    )

    assert score.total_score == pytest.approx(0.72)
    assert score.to_manifest()["schema"] == "hive.ccplus.activation_score.v1"
    assert score.to_manifest()["reasons"] == ["semantic_match", "source_backed"]


def test_activation_candidate_manifest_roundtrips_with_surface_and_hard_mask() -> None:
    from app.runtime.activation_candidates import ActivationCandidate, ActivationHardMask, ActivationScore

    candidate = ActivationCandidate(
        candidate_kind="agent_memory",
        candidate_ref={
            "schema": "hive.ccplus.context_candidate_ref.v1",
            "candidate_id": "agent_memory:t3_profile:20260705/abcdef123456",
            "kind": "agent_memory",
            "item_id": "t3_profile",
            "version": "20260705",
            "content_hash": "abcdef123456",
        },
        key_features={"concepts": ["memory", "runtime"], "risk_flags": ["architecture_drift"]},
        value_pointer={"loader": "memory_slice", "path": "memory/t3/user.md", "heading": "Runtime"},
        surface={
            "surface_kind": "hint",
            "preview": "Runtime memory design decision",
            "token_estimate": 18,
            "source_refs": ["t0:session-1:segment-1"],
        },
        score=ActivationScore(head_scores={"semantic": 0.8}, reasons=("semantic_match",)),
        hard_mask=ActivationHardMask(allowed=False, reason="acl_denied", judge="platform_gate"),
        metadata={"lane": "memory"},
    )

    manifest = candidate.to_manifest()

    assert manifest["schema"] == "hive.ccplus.activation_candidate.v1"
    assert manifest["candidate_id"] == "agent_memory:t3_profile:20260705/abcdef123456"
    assert manifest["hard_mask"]["allowed"] is False
    assert manifest["hard_mask"]["reason"] == "acl_denied"
    json.dumps(manifest)
    assert ActivationCandidate.from_manifest(manifest) == candidate


def test_activation_candidate_rejects_wrong_schema() -> None:
    from app.runtime.activation_candidates import ActivationCandidate

    with pytest.raises(ValueError, match="activation candidate schema"):
        ActivationCandidate.from_manifest({"schema": "wrong"})
