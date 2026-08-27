from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import uuid

import pytest

from app.services.company_knowledge_contracts import (
    CanonicalEvidenceInput,
    SourceContractInput,
    build_canonical_evidence_envelope,
    company_knowledge_artifact_path,
    compute_source_contract_hash,
    default_company_knowledge_review_policy,
    evaluate_company_review_set,
    next_company_proposal_status,
    satisfied_review_roles,
    validate_source_contract,
)


def _source_contract() -> SourceContractInput:
    return SourceContractInput(
        source_kind="document",
        provider_kind="native",
        stable_source_id="policy-handbook",
        owner_principal_ref="user:00000000-0000-0000-0000-000000000001",
        accountable_steward_ref="role:org_admin",
        connection_ref=None,
        schema_ref="schema://company-policy/v1",
        schema_version="1",
        identity_keys=("document_id",),
        relation_keys=(),
        ingest_mode="manual",
        cursor_kind=None,
        cursor_policy={},
        watermark_field=None,
        temporal_mapping={"observed_at": "ingest_time"},
        source_acl_mapping_policy={"mode": "required_snapshot"},
        default_sensitivity="PL2_pii",
        export_policy={"allowed": False},
        retention_policy={"class": "company_record"},
        legal_hold_policy={"supported": True},
        allowed_namespaces=("company/policies",),
        precedence_policy_ref=None,
        acceptance_suite_ref="acceptance://company-policy/v1",
        idempotency_policy={"key": "source_item_id+revision"},
    )


def test_source_contract_is_canonical_and_never_accepts_raw_credentials() -> None:
    contract = _source_contract()
    validated = validate_source_contract(contract)
    reordered = replace(
        contract,
        cursor_policy=dict(reversed(list(contract.cursor_policy.items()))),
        temporal_mapping=dict(reversed(list(contract.temporal_mapping.items()))),
    )

    assert validated == contract
    assert compute_source_contract_hash(contract) == compute_source_contract_hash(reordered)
    assert len(compute_source_contract_hash(contract)) == 64

    with pytest.raises(ValueError, match="managed credential reference"):
        validate_source_contract(replace(contract, connection_ref="postgresql://user:password@example/db"))
    with pytest.raises(ValueError, match="allowed_namespaces"):
        validate_source_contract(replace(contract, allowed_namespaces=()))


def test_lossless_evidence_envelope_requires_acl_hash_and_complete_coverage() -> None:
    evidence = CanonicalEvidenceInput(
        evidence_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        source_contract_id=uuid.uuid4(),
        source_contract_version=1,
        evidence_kind="structured_record",
        source_item_id="policy:42",
        source_revision="7",
        artifact_ref=None,
        schema_ref="schema://policy/v1",
        typed_payload_ref="inline://typed-payload",
        typed_payload={"policy_id": 42, "status": "active", "owners": ["legal", "security"]},
        content_hash="a" * 64,
        source_acl_snapshot_hash="b" * 64,
        source_acl_snapshot={"role_names": ["member"]},
        occurred_at=None,
        effective_from=datetime(2026, 7, 1, tzinfo=timezone.utc),
        effective_until=None,
        observed_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
        cursor={"offset": 42},
        sequence="42",
        idempotency_key="policy:42:7",
        coverage_ledger_ref="coverage://policy-42-v7",
        coverage_ledger={"complete": True, "total_units": 3, "covered_units": 3, "missing_units": []},
        ingestion_receipt_ref="receipt://policy-42-v7",
    )

    envelope = build_canonical_evidence_envelope(evidence)

    assert envelope["schema"] == "hive.company_knowledge_evidence.v1"
    assert envelope["typed_payload"]["policy_id"] == 42
    assert envelope["coverage_ledger"]["complete"] is True
    assert envelope["source_acl_snapshot_hash"] == "b" * 64

    with pytest.raises(ValueError, match="source ACL"):
        build_canonical_evidence_envelope(replace(evidence, source_acl_snapshot_hash=""))
    with pytest.raises(ValueError, match="complete coverage"):
        build_canonical_evidence_envelope(
            replace(
                evidence,
                coverage_ledger={"complete": False, "total_units": 3, "covered_units": 2, "missing_units": ["owners"]},
            )
        )


def test_company_artifact_path_is_content_addressed_and_tenant_bounded(tmp_path) -> None:
    tenant_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    path = company_knowledge_artifact_path(
        tmp_path,
        tenant_id=tenant_id,
        evidence_id=evidence_id,
        content_hash="c" * 64,
        suffix=".md",
    )

    assert path == (
        tmp_path / "companies" / str(tenant_id) / "knowledge" / "evidence" / "cc" / f"{evidence_id}-{'c' * 64}.md"
    )
    with pytest.raises(ValueError, match="suffix"):
        company_knowledge_artifact_path(
            tmp_path,
            tenant_id=tenant_id,
            evidence_id=evidence_id,
            content_hash="c" * 64,
            suffix="../../secret",
        )


@pytest.mark.parametrize(
    ("current", "command", "expected"),
    [
        ("draft", "submit", "submitted"),
        ("submitted", "begin_review", "in_review"),
        ("in_review", "request_changes", "changes_requested"),
        ("changes_requested", "submit", "submitted"),
        ("in_review", "approve", "approved"),
        ("in_review", "reject", "rejected"),
        ("approved", "begin_publish", "publishing"),
        ("publishing", "publish_succeeded", "published"),
        ("publishing", "publish_failed", "publish_failed"),
        ("publish_failed", "begin_publish", "publishing"),
        ("draft", "withdraw", "withdrawn"),
        ("submitted", "withdraw", "withdrawn"),
    ],
)
def test_proposal_state_machine_allows_only_explicit_edges(current: str, command: str, expected: str) -> None:
    assert next_company_proposal_status(current, command) == expected


def test_review_set_enforces_distinct_high_risk_roles_and_never_agent_self_approves() -> None:
    creator_id = uuid.uuid4()
    steward_id = uuid.uuid4()
    security_id = uuid.uuid4()
    reviews = [
        {
            "reviewer_user_id": str(steward_id),
            "reviewer_role": "domain_steward",
            "decision": "approve",
            "decision_hash": "a" * 64,
        },
        {
            "reviewer_user_id": str(security_id),
            "reviewer_role": "security",
            "decision": "approve",
            "decision_hash": "b" * 64,
        },
    ]

    accepted = evaluate_company_review_set(
        reviews,
        policy={"minimum_approvals": 2, "required_roles": ["domain_steward", "security"], "separation": True},
        created_by_type="agent",
        created_by_id=creator_id,
        risk_level="high",
    )
    same_reviewer = evaluate_company_review_set(
        [reviews[0], {**reviews[1], "reviewer_user_id": str(steward_id)}],
        policy={"minimum_approvals": 2, "required_roles": ["domain_steward", "security"], "separation": True},
        created_by_type="user",
        created_by_id=creator_id,
        risk_level="high",
    )
    creator_as_reviewer = evaluate_company_review_set(
        [reviews[0], {**reviews[1], "reviewer_user_id": str(creator_id)}],
        policy={"minimum_approvals": 2, "required_roles": ["domain_steward", "security"], "separation": True},
        created_by_type="user",
        created_by_id=creator_id,
        risk_level="high",
    )
    agent_self_claim = evaluate_company_review_set(
        [
            {
                **reviews[0],
                "reviewer_user_id": str(creator_id),
                "reviewer_role": "agent",
            }
        ],
        policy={"minimum_approvals": 1, "required_roles": [], "separation": False},
        created_by_type="agent",
        created_by_id=creator_id,
        risk_level="normal",
    )

    assert accepted["approved"] is True
    assert len(accepted["review_set_hash"]) == 64
    assert same_reviewer == {
        "approved": False,
        "reason_codes": ["reviewer_separation_required"],
        "review_set_hash": None,
    }
    assert creator_as_reviewer == {
        "approved": False,
        "reason_codes": ["creator_reviewer_separation_required"],
        "review_set_hash": None,
    }
    assert agent_self_claim == {
        "approved": False,
        "reason_codes": ["agent_cannot_review_or_approve"],
        "review_set_hash": None,
    }


def _approval(reviewer_id: uuid.UUID, role: str, decision_hash: str) -> dict[str, str]:
    return {
        "reviewer_user_id": str(reviewer_id),
        "reviewer_role": role,
        "decision": "approve",
        "decision_hash": decision_hash,
    }


def test_satisfied_review_roles_uses_canonical_admin_hierarchy() -> None:
    assert satisfied_review_roles("platform_admin") == frozenset({"member", "org_admin", "platform_admin"})
    assert satisfied_review_roles("org_admin") == frozenset({"member", "org_admin"})
    assert satisfied_review_roles("member") == frozenset({"member"})
    # Roles outside the canonical hierarchy satisfy only themselves.
    assert satisfied_review_roles("domain_steward") == frozenset({"domain_steward"})
    assert satisfied_review_roles("agent") == frozenset({"agent"})
    assert satisfied_review_roles(None) == frozenset({""})


def test_review_set_platform_admin_satisfies_default_org_admin_review_authority() -> None:
    creator_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    policy = default_company_knowledge_review_policy(
        proposed_sensitivity="PL2_pii",
        risk_level="normal",
        created_by_type="user",
    )
    assert policy == {
        "minimum_approvals": 1,
        "required_roles": ["org_admin"],
        "separation": False,
        "source": "server_policy_v1",
    }

    evaluation = evaluate_company_review_set(
        [_approval(reviewer_id, "platform_admin", "c" * 64)],
        policy=policy,
        created_by_type="user",
        created_by_id=creator_id,
        risk_level="normal",
    )

    assert evaluation["approved"] is True
    assert evaluation["reason_codes"] == []
    assert len(evaluation["review_set_hash"]) == 64


def test_review_set_org_admin_still_satisfies_default_review_authority_exactly() -> None:
    evaluation = evaluate_company_review_set(
        [_approval(uuid.uuid4(), "org_admin", "d" * 64)],
        policy=default_company_knowledge_review_policy(
            proposed_sensitivity="PL2_pii",
            risk_level="normal",
            created_by_type="user",
        ),
        created_by_type="user",
        created_by_id=uuid.uuid4(),
        risk_level="normal",
    )

    assert evaluation["approved"] is True


@pytest.mark.parametrize("role", ["member", "domain_steward", "security", "reviewer", "agent", ""])
def test_review_set_arbitrary_roles_do_not_satisfy_org_admin_review_authority(role: str) -> None:
    evaluation = evaluate_company_review_set(
        [_approval(uuid.uuid4(), role, "e" * 64)],
        policy=default_company_knowledge_review_policy(
            proposed_sensitivity="PL2_pii",
            risk_level="normal",
            created_by_type="user",
        ),
        created_by_type="user",
        created_by_id=uuid.uuid4(),
        risk_level="normal",
    )

    assert evaluation["approved"] is False
    assert "required_review_roles_missing" in evaluation["reason_codes"]
    assert evaluation["review_set_hash"] is None


def test_review_set_hierarchy_does_not_weaken_governance_guards() -> None:
    creator_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    other_id = uuid.uuid4()
    platform_approval = _approval(reviewer_id, "platform_admin", "f" * 64)

    # A reject decision still blocks approval even with a platform_admin approval present.
    rejected = evaluate_company_review_set(
        [
            platform_approval,
            {
                "reviewer_user_id": str(other_id),
                "reviewer_role": "org_admin",
                "decision": "reject",
                "decision_hash": "1" * 64,
            },
        ],
        policy={"minimum_approvals": 1, "required_roles": ["org_admin"], "separation": False},
        created_by_type="user",
        created_by_id=creator_id,
        risk_level="normal",
    )
    # minimum_approvals is still enforced against actual approval rows.
    insufficient = evaluate_company_review_set(
        [platform_approval],
        policy={"minimum_approvals": 2, "required_roles": ["org_admin"], "separation": False},
        created_by_type="user",
        created_by_id=creator_id,
        risk_level="normal",
    )
    # Creator separation still binds the platform_admin reviewer.
    creator_self_review = evaluate_company_review_set(
        [platform_approval],
        policy={"minimum_approvals": 1, "required_roles": ["org_admin"], "separation": True},
        created_by_type="user",
        created_by_id=reviewer_id,
        risk_level="normal",
    )
    # Reviewer separation still requires distinct reviewers at high risk.
    duplicate_reviewer = evaluate_company_review_set(
        [platform_approval, _approval(reviewer_id, "platform_admin", "2" * 64)],
        policy={"minimum_approvals": 2, "required_roles": ["org_admin"], "separation": False},
        created_by_type="user",
        created_by_id=creator_id,
        risk_level="high",
    )

    assert rejected == {
        "approved": False,
        "reason_codes": ["review_rejected"],
        "review_set_hash": None,
    }
    assert insufficient == {
        "approved": False,
        "reason_codes": ["minimum_approvals_not_met"],
        "review_set_hash": None,
    }
    assert creator_self_review == {
        "approved": False,
        "reason_codes": ["creator_reviewer_separation_required"],
        "review_set_hash": None,
    }
    assert duplicate_reviewer == {
        "approved": False,
        "reason_codes": ["reviewer_separation_required"],
        "review_set_hash": None,
    }
