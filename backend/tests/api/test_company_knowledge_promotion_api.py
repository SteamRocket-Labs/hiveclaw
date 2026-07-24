from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError


def test_company_promotion_routes_are_live_under_both_api_prefixes() -> None:
    from app.main import app

    paths = set(app.openapi()["paths"])
    suffixes = {
        "/knowledge/company/promotion-intakes",
        "/knowledge/company/promotion-intakes/personal",
        "/knowledge/company/promotion-intakes/legacy-candidates",
        "/knowledge/company/promotion-intakes/legacy",
        "/knowledge/company/promotion-intakes/{job_id}",
        "/knowledge/company/promotion-intakes/{job_id}/candidate",
        "/knowledge/company/promotion-intakes/{job_id}/retry",
    }
    for prefix in ("/api", "/api/v1"):
        assert {f"{prefix}{suffix}" for suffix in suffixes} <= paths


def test_promotion_request_schemas_require_explicit_scope_change_and_block_personal_declassification() -> None:
    from app.api.knowledge_company import (
        LegacyPromotionCreate,
        PersonalPromotionCreate,
    )

    with pytest.raises(ValidationError):
        PersonalPromotionCreate(
            document_id=uuid.uuid4(),
            proposed_namespace="company/general",
            proposed_sensitivity="PL1_public",
            purpose="Client must not choose a lower Personal sensitivity",
            risk_level="normal",
            attest_scope_change=True,
            idempotency_key="personal",
            trace_id="trace",
        )
    with pytest.raises(ValidationError):
        PersonalPromotionCreate(
            document_id=uuid.uuid4(),
            proposed_namespace="company/general",
            purpose="Missing explicit attestation",
            risk_level="normal",
            attest_scope_change=False,
            idempotency_key="personal",
            trace_id="trace",
        )
    with pytest.raises(ValidationError):
        LegacyPromotionCreate(
            relative_path="policy.md",
            expected_sha256="a" * 64,
            proposed_namespace="company/policies",
            proposed_sensitivity="PL2_pii",
            purpose="Missing explicit attestation",
            risk_level="normal",
            attest_scope_change=False,
            idempotency_key="legacy",
            trace_id="trace",
        )
