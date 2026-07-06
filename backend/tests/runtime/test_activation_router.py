"""External attention router hard-mask behavior."""

from __future__ import annotations


def _candidate(
    *,
    kind: str = "agent_memory",
    sensitivity: str = "PL1_public",
    acl_scope: str = "company",
    key_features: dict | None = None,
    token_estimate: int = 1,
):
    from app.runtime.activation_candidates import ActivationCandidate, ActivationScore, ActivationSurface

    return ActivationCandidate(
        candidate_kind=kind,
        candidate_ref={"candidate_id": f"{kind}:item:v1/hash", "kind": kind, "source_type": "test"},
        key_features=key_features or {"name": [kind]},
        value_pointer={"loader": "test_loader"},
        surface=ActivationSurface(surface_kind="test", preview=f"{kind} preview", token_estimate=token_estimate),
        score=ActivationScore(head_scores={"seed": 0.5}, total_score=0.5),
        metadata={"sensitivity": sensitivity, "acl_scope": acl_scope},
    )


def _principal_stack(*, current_is_owner: bool = False):
    from app.services.principal_context import Principal, PrincipalRole, PrincipalStack

    owner = Principal(role=PrincipalRole.OWNER, id="owner-1")
    current = Principal(
        role=PrincipalRole.OWNER if current_is_owner else PrincipalRole.CURRENT_USER,
        id="owner-1" if current_is_owner else "user-2",
    )
    return PrincipalStack(
        company=Principal(role=PrincipalRole.COMPANY, id="company-1"),
        direct_owner=owner,
        current_user=current,
    )


def test_activation_router_suppresses_owner_acl_for_non_owner() -> None:
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    output = route_activation_candidates(
        [_candidate(acl_scope="owner")],
        context=ActivationRouterContext(principal_stack=_principal_stack(current_is_owner=False)),
    )
    manifest = output.to_manifest()

    assert manifest["schema"] == "hive.ccplus.activation_router_output.v1"
    assert manifest["top_activation_candidates"] == []
    suppressed = manifest["suppressed_activation_candidates"][0]
    assert suppressed["hard_mask"]["allowed"] is False
    assert suppressed["hard_mask"]["reason"] == "acl_denied"
    assert suppressed["hard_mask"]["policy_ref"] == "activation_router.acl_scope"


def test_activation_router_suppresses_inaccessible_sensitivity() -> None:
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    output = route_activation_candidates(
        [_candidate(sensitivity="PL3_sensitive")],
        context=ActivationRouterContext(principal_stack=_principal_stack(current_is_owner=False)),
    )
    suppressed = output.to_manifest()["suppressed_activation_candidates"][0]

    assert suppressed["hard_mask"]["allowed"] is False
    assert suppressed["hard_mask"]["reason"] == "sensitivity_denied"
    assert suppressed["hard_mask"]["details"]["sensitivity"] == "PL3_sensitive"


def test_activation_router_suppresses_policy_denied_candidate_kind() -> None:
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    output = route_activation_candidates(
        [_candidate(kind="tool")],
        context=ActivationRouterContext(
            principal_stack=_principal_stack(current_is_owner=True),
            denied_candidate_kinds=("tool",),
        ),
    )
    suppressed = output.to_manifest()["suppressed_activation_candidates"][0]

    assert suppressed["hard_mask"]["allowed"] is False
    assert suppressed["hard_mask"]["reason"] == "policy_denied"
    assert suppressed["hard_mask"]["details"]["candidate_kind"] == "tool"


def test_activation_router_keeps_allowed_candidates_in_input_order() -> None:
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    candidates = [_candidate(kind="agent_memory"), _candidate(kind="skill")]
    output = route_activation_candidates(
        candidates,
        context=ActivationRouterContext(principal_stack=_principal_stack(current_is_owner=True)),
    )
    manifest = output.to_manifest()

    assert [item["candidate_kind"] for item in manifest["top_activation_candidates"]] == ["agent_memory", "skill"]
    assert manifest["suppressed_activation_candidates"] == []


def test_activation_router_scores_and_sorts_by_multi_head_query_match() -> None:
    from app.runtime.activation_query import ActivationQuery
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    query = ActivationQuery(
        raw_prompt="Find Acme pricing policy for a concise investor update this week.",
        session_id="s1",
        turn_id="t1",
        intent_id="i1",
        concepts=("pricing", "policy"),
        entities=({"name": "Acme", "type": "org"},),
        temporal_hints=({"kind": "recent", "value": "this_week"},),
        owner_context={"profile_terms": ["concise", "investor"]},
    )
    weak = _candidate(
        kind="skill",
        key_features={
            "concepts": ["python"],
            "entities": ["backend"],
            "temporal_hints": ["evergreen"],
            "profile_terms": ["verbose"],
        },
    )
    strong = _candidate(
        kind="agent_memory",
        key_features={
            "concepts": ["pricing", "policy"],
            "entities": ["acme"],
            "temporal_hints": ["recent", "this_week"],
            "profile_terms": ["concise", "investor"],
        },
    )

    output = route_activation_candidates(
        [weak, strong],
        context=ActivationRouterContext(
            principal_stack=_principal_stack(current_is_owner=True),
            activation_query=query,
        ),
    )
    manifest = output.to_manifest()
    top = manifest["top_activation_candidates"]

    assert [item["candidate_kind"] for item in top] == ["agent_memory", "skill"]
    head_scores = top[0]["score"]["head_scores"]
    assert head_scores["semantic"] == 1.0
    assert head_scores["entity"] == 1.0
    assert head_scores["temporal"] == 1.0
    assert head_scores["profile"] == 1.0
    assert top[0]["score"]["scorer"] == "activation_router_multi_head"


def test_activation_router_applies_budget_aware_top_k_and_token_pressure() -> None:
    from app.runtime.activation_query import ActivationQuery
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    query = ActivationQuery(
        raw_prompt="Find Acme pricing policy.",
        session_id="s1",
        turn_id="t1",
        intent_id="i1",
        concepts=("pricing",),
        entities=({"name": "Acme"},),
        budget_policy={"max_candidates": 2, "max_surface_tokens": 12},
    )
    high = _candidate(
        kind="agent_memory",
        key_features={"concepts": ["pricing"], "entities": ["acme"]},
        token_estimate=8,
    )
    medium = _candidate(
        kind="knowledge_base",
        key_features={"concepts": ["pricing"], "entities": ["acme"]},
        token_estimate=8,
    )
    low = _candidate(
        kind="skill",
        key_features={"concepts": ["python"], "entities": ["backend"]},
        token_estimate=1,
    )

    output = route_activation_candidates(
        [low, high, medium],
        context=ActivationRouterContext(
            principal_stack=_principal_stack(current_is_owner=True),
            activation_query=query,
        ),
    )
    manifest = output.to_manifest()

    assert [item["candidate_kind"] for item in manifest["top_activation_candidates"]] == ["agent_memory"]
    suppressed = manifest["suppressed_activation_candidates"]
    assert [item["hard_mask"]["reason"] for item in suppressed] == ["budget_exceeded", "budget_exceeded"]
    assert {item["candidate_kind"] for item in suppressed} == {"knowledge_base", "skill"}
    assert manifest["metadata"]["budget"]["selected_tokens"] == 8
    assert manifest["metadata"]["budget"]["max_surface_tokens"] == 12


def test_activation_router_manifest_summarizes_suppression_reasons() -> None:
    from app.runtime.activation_router import ActivationRouterContext, route_activation_candidates

    output = route_activation_candidates(
        [
            _candidate(kind="agent_memory", acl_scope="owner"),
            _candidate(kind="knowledge_base", sensitivity="PL3_sensitive"),
            _candidate(kind="tool"),
        ],
        context=ActivationRouterContext(
            principal_stack=_principal_stack(current_is_owner=False),
            denied_candidate_kinds=("tool",),
        ),
    )
    manifest = output.to_manifest()

    reasons = {item["reason"]: item for item in manifest["suppression_reasons"]}
    assert reasons["acl_denied"]["count"] == 1
    assert reasons["sensitivity_denied"]["policy_refs"] == ["activation_router.sensitivity"]
    assert reasons["policy_denied"]["candidate_kinds"] == ["tool"]
    assert all(item["candidate_ids"] for item in reasons.values())
