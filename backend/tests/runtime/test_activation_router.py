"""External attention router hard-mask behavior."""

from __future__ import annotations


def _candidate(*, kind: str = "agent_memory", sensitivity: str = "PL1_public", acl_scope: str = "company"):
    from app.runtime.activation_candidates import ActivationCandidate, ActivationScore, ActivationSurface

    return ActivationCandidate(
        candidate_kind=kind,
        candidate_ref={"candidate_id": f"{kind}:item:v1/hash", "kind": kind, "source_type": "test"},
        key_features={"name": [kind]},
        value_pointer={"loader": "test_loader"},
        surface=ActivationSurface(surface_kind="test", preview=f"{kind} preview"),
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
