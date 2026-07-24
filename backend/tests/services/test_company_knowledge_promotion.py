from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest


class _FakeResult:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, *, existing=None) -> None:
        self.existing = existing

    async def execute(self, statement, _parameters=None):
        if statement.__class__.__name__ == "TextClause":
            return _FakeResult()
        return _FakeResult(self.existing)


class _FakeCompanyKnowledgeService:
    def __init__(self) -> None:
        self.contract_inputs = []
        self.import_requests = []

    async def register_source_contract(
        self,
        _session,
        *,
        principal,
        contract_input,
        idempotency_key,
        trace_id,
    ):
        self.contract_inputs.append(
            {
                "principal": principal,
                "contract_input": contract_input,
                "idempotency_key": idempotency_key,
                "trace_id": trace_id,
            }
        )
        return SimpleNamespace(id=uuid.uuid4(), version=1)

    async def queue_evidence_import(self, _session, *, principal, request):
        self.import_requests.append({"principal": principal, "request": request})
        return SimpleNamespace(
            id=uuid.uuid4(),
            tenant_id=principal.tenant_id,
            status="queued",
            proposal_id=None,
            request_json={"promotion_handoff": request.promotion_handoff},
        )


class _FakePersonalKnowledgeService:
    def __init__(self, *, document) -> None:
        self.document = document
        self.calls = []

    async def get_personal_document_with_authority(self, _session, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="ok",
            document=self.document,
            credential_reference=None,
            authority=SimpleNamespace(allowed=True),
            warnings=[],
        )


class _FakeConversionService:
    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.calls = []

    def convert_bytes(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            markdown=self.markdown,
            source_sha256=hashlib.sha256(kwargs["data"]).hexdigest(),
            source_mime_type="text/markdown",
            engine="test-converter",
            warnings=(),
        )


def _principal(*, tenant_id: uuid.UUID, user_id: uuid.UUID, role: str = "member"):
    from app.services.company_knowledge_permissions import CompanyKnowledgePrincipal

    return CompanyKnowledgePrincipal(
        tenant_id=tenant_id,
        accountable_user_id=user_id,
        accountable_role=role,
        actor_type="user",
        actor_id=user_id,
        purpose="interactive_session",
    )


@pytest.mark.asyncio
async def test_personal_promotion_copies_exact_authorized_artifact_into_recoverable_handoff(tmp_path: Path) -> None:
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        PersonalPromotionIntakeRequest,
    )

    tenant_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    document_id = uuid.uuid4()
    markdown = "# Private working note\n\nShare this exact reviewed note with the company.\n"
    artifact_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    relative_path = Path("persons") / str(owner_user_id) / "kb" / "documents" / "note.md"
    artifact_path = tmp_path / relative_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(markdown, encoding="utf-8")
    document = SimpleNamespace(
        document_id=document_id,
        title="Private working note",
        sensitivity="PL2_pii",
        canonical_md_path=relative_path.as_posix(),
        canonical_md_sha256=artifact_hash,
        source_sha256="a" * 64,
        source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
        metadata={},
    )
    company_service = _FakeCompanyKnowledgeService()
    personal_service = _FakePersonalKnowledgeService(document=document)
    service = CompanyKnowledgePromotionService(
        data_root=tmp_path,
        company_service=company_service,
        personal_service=personal_service,
    )

    job = await service.queue_personal_promotion(
        _FakeSession(),
        principal=_principal(tenant_id=tenant_id, user_id=owner_user_id),
        request=PersonalPromotionIntakeRequest(
            document_id=document_id,
            proposed_namespace="company/team-notes",
            purpose="Share an owner-reviewed operating note",
            risk_level="normal",
            title=None,
            attest_scope_change=True,
            idempotency_key="personal-promotion-1",
            trace_id="trace-personal-promotion-1",
        ),
    )

    assert job.status == "queued"
    assert len(company_service.contract_inputs) == 1
    queued = company_service.import_requests[0]["request"]
    assert queued.markdown == markdown
    assert queued.proposed_sensitivity == "PL2_pii"
    assert queued.source_acl_snapshot == {
        "user_ids": [str(owner_user_id)],
        "role_names": ["org_admin", "platform_admin"],
        "scope_change_attested": True,
    }
    handoff = queued.promotion_handoff
    assert handoff.proposal_kind == "personal_promotion"
    assert handoff.original_source_ref == document.source_ref
    assert handoff.candidate_content_hash == artifact_hash
    assert handoff.explicit_scope_change_attested is True
    assert handoff.proposal_idempotency_key == (
        f"company-promotion:personal:{owner_user_id}:personal-promotion-1:proposal"
    )


@pytest.mark.asyncio
async def test_personal_promotion_fails_closed_on_artifact_drift_or_credential_content(tmp_path: Path) -> None:
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        PersonalPromotionIntakeRequest,
    )

    tenant_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    document_id = uuid.uuid4()
    relative_path = Path("persons") / str(owner_user_id) / "kb" / "documents" / "note.md"
    artifact_path = tmp_path / relative_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("tampered\n", encoding="utf-8")
    document = SimpleNamespace(
        document_id=document_id,
        title="Credential note",
        sensitivity="PL4_credential",
        canonical_md_path=relative_path.as_posix(),
        canonical_md_sha256=hashlib.sha256(b"expected\n").hexdigest(),
        source_sha256="a" * 64,
        source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
        metadata={},
    )
    service = CompanyKnowledgePromotionService(
        data_root=tmp_path,
        company_service=_FakeCompanyKnowledgeService(),
        personal_service=_FakePersonalKnowledgeService(document=document),
    )
    request = PersonalPromotionIntakeRequest(
        document_id=document_id,
        proposed_namespace="company/security",
        purpose="Invalid credential promotion",
        risk_level="critical",
        title=None,
        attest_scope_change=True,
        idempotency_key="personal-promotion-secret",
        trace_id="trace-personal-promotion-secret",
    )

    with pytest.raises(PermissionError, match="credential"):
        await service.queue_personal_promotion(
            _FakeSession(),
            principal=_principal(tenant_id=tenant_id, user_id=owner_user_id),
            request=request,
        )

    document.sensitivity = "PL3_sensitive"
    with pytest.raises(RuntimeError, match="artifact"):
        await service.queue_personal_promotion(
            _FakeSession(),
            principal=_principal(tenant_id=tenant_id, user_id=owner_user_id),
            request=request,
        )


@pytest.mark.asyncio
async def test_personal_promotion_rejects_symlinked_canonical_artifact(tmp_path: Path) -> None:
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        PersonalPromotionIntakeRequest,
    )

    tenant_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    document_id = uuid.uuid4()
    personal_root = tmp_path / "persons" / str(owner_user_id) / "kb" / "documents"
    personal_root.mkdir(parents=True)
    target = personal_root / "target.md"
    target.write_text("# Symlink target\n", encoding="utf-8")
    linked = personal_root / "linked.md"
    linked.symlink_to(target)
    artifact_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    document = SimpleNamespace(
        document_id=document_id,
        title="Symlinked note",
        sensitivity="PL2_pii",
        canonical_md_path=linked.relative_to(tmp_path).as_posix(),
        canonical_md_sha256=artifact_hash,
        source_sha256="a" * 64,
        source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
        metadata={},
    )
    service = CompanyKnowledgePromotionService(
        data_root=tmp_path,
        company_service=_FakeCompanyKnowledgeService(),
        personal_service=_FakePersonalKnowledgeService(document=document),
    )

    with pytest.raises(RuntimeError, match="artifact"):
        await service.queue_personal_promotion(
            _FakeSession(),
            principal=_principal(tenant_id=tenant_id, user_id=owner_user_id),
            request=PersonalPromotionIntakeRequest(
                document_id=document_id,
                proposed_namespace="company/general",
                purpose="A symlink must not become cross-scope evidence",
                risk_level="normal",
                title=None,
                attest_scope_change=True,
                idempotency_key="personal-symlink",
                trace_id="trace-personal-symlink",
            ),
        )


@pytest.mark.asyncio
async def test_promotion_idempotency_is_scoped_to_accountable_user(tmp_path: Path) -> None:
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        PersonalPromotionIntakeRequest,
    )

    tenant_id = uuid.uuid4()
    company_service = _FakeCompanyKnowledgeService()

    async def queue_for(owner_user_id: uuid.UUID) -> None:
        document_id = uuid.uuid4()
        markdown = f"# Note for {owner_user_id}\n"
        artifact_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        relative_path = Path("persons") / str(owner_user_id) / "kb" / "documents" / "note.md"
        artifact_path = tmp_path / relative_path
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text(markdown, encoding="utf-8")
        document = SimpleNamespace(
            document_id=document_id,
            title="Owner note",
            sensitivity="PL1_public",
            canonical_md_path=relative_path.as_posix(),
            canonical_md_sha256=artifact_hash,
            source_sha256="a" * 64,
            source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
            metadata={},
        )
        service = CompanyKnowledgePromotionService(
            data_root=tmp_path,
            company_service=company_service,
            personal_service=_FakePersonalKnowledgeService(document=document),
        )
        await service.queue_personal_promotion(
            _FakeSession(),
            principal=_principal(tenant_id=tenant_id, user_id=owner_user_id),
            request=PersonalPromotionIntakeRequest(
                document_id=document_id,
                proposed_namespace="company/general",
                purpose="Owner-scoped idempotency",
                risk_level="normal",
                title=None,
                attest_scope_change=True,
                idempotency_key="same-client-key",
                trace_id=f"trace-{owner_user_id}",
            ),
        )

    await queue_for(uuid.uuid4())
    await queue_for(uuid.uuid4())

    import_keys = [entry["request"].idempotency_key for entry in company_service.import_requests]
    proposal_keys = [
        entry["request"].promotion_handoff.proposal_idempotency_key for entry in company_service.import_requests
    ]
    assert len(set(import_keys)) == 2
    assert len(set(proposal_keys)) == 2


@pytest.mark.asyncio
async def test_promotion_idempotency_conflict_is_rejected_before_source_contract_mutation(tmp_path: Path) -> None:
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        PersonalPromotionIntakeRequest,
    )

    tenant_id = uuid.uuid4()
    owner_user_id = uuid.uuid4()
    document_id = uuid.uuid4()
    markdown = "# Current source\n"
    artifact_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    relative_path = Path("persons") / str(owner_user_id) / "kb" / "documents" / "note.md"
    artifact_path = tmp_path / relative_path
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(markdown, encoding="utf-8")
    document = SimpleNamespace(
        document_id=document_id,
        title="Current source",
        sensitivity="PL1_public",
        canonical_md_path=relative_path.as_posix(),
        canonical_md_sha256=artifact_hash,
        source_sha256="a" * 64,
        source_ref=f"kb://person/{owner_user_id}/documents/{document_id}",
        metadata={},
    )
    company_service = _FakeCompanyKnowledgeService()
    service = CompanyKnowledgePromotionService(
        data_root=tmp_path,
        company_service=company_service,
        personal_service=_FakePersonalKnowledgeService(document=document),
    )
    existing = SimpleNamespace(
        request_json={
            "artifact_hash": "0" * 64,
            "source_item_id": f"personal-document:{document_id}",
            "source_revision": artifact_hash,
            "promotion_handoff": {},
        }
    )

    with pytest.raises(ValueError, match="idempotency_conflict"):
        await service.queue_personal_promotion(
            _FakeSession(existing=existing),
            principal=_principal(tenant_id=tenant_id, user_id=owner_user_id),
            request=PersonalPromotionIntakeRequest(
                document_id=document_id,
                proposed_namespace="company/general",
                purpose="Conflicting replay",
                risk_level="normal",
                title=None,
                attest_scope_change=True,
                idempotency_key="same-key",
                trace_id="trace-conflict",
            ),
        )

    assert company_service.contract_inputs == []
    assert company_service.import_requests == []


@pytest.mark.asyncio
async def test_legacy_promotion_requires_exact_snapshot_and_records_conversion_without_host_paths(
    tmp_path: Path,
) -> None:
    from app.services.company_knowledge_promotion import (
        CompanyKnowledgePromotionService,
        LegacyPromotionIntakeRequest,
    )

    tenant_id = uuid.uuid4()
    admin_user_id = uuid.uuid4()
    company_dir = tmp_path / f"enterprise_info_{tenant_id}"
    company_dir.mkdir()
    source = company_dir / "knowledge_base" / "policy.md"
    source.parent.mkdir()
    source.write_text("# Retired policy\n\nLegacy wording.\n", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    converter = _FakeConversionService("# Retired policy\n\nReviewed canonical wording.\n")
    company_service = _FakeCompanyKnowledgeService()
    service = CompanyKnowledgePromotionService(
        data_root=tmp_path,
        company_service=company_service,
        conversion_service=converter,
    )

    job = await service.queue_legacy_promotion(
        _FakeSession(),
        principal=_principal(tenant_id=tenant_id, user_id=admin_user_id, role="org_admin"),
        company_dir=company_dir,
        request=LegacyPromotionIntakeRequest(
            relative_path="knowledge_base/policy.md",
            expected_sha256=source_hash,
            proposed_namespace="company/policies",
            proposed_sensitivity="PL2_pii",
            purpose="Review a retired policy for Company publication",
            risk_level="high",
            title="Retired policy candidate",
            attest_scope_change=True,
            idempotency_key="legacy-promotion-1",
            trace_id="trace-legacy-promotion-1",
        ),
    )

    assert job.status == "queued"
    queued = company_service.import_requests[0]["request"]
    assert queued.markdown == "# Retired policy\n\nReviewed canonical wording.\n"
    assert queued.source_item_id == f"legacy-company-file:{source_hash}"
    handoff = queued.promotion_handoff
    assert handoff.proposal_kind == "legacy_import"
    assert handoff.original_source_ref == f"legacy-company-file://{source_hash}"
    assert handoff.original_source_label == "knowledge_base/policy.md"
    assert handoff.conversion_receipt == {
        "engine": "test-converter",
        "source_mime_type": "text/markdown",
        "warnings": [],
    }
    conversion_workspace = Path(converter.calls[0]["workspace_root"])
    assert conversion_workspace.name.startswith("hive-company-promotion-")
    assert not conversion_workspace.exists()
    assert str(tmp_path) not in repr(handoff)

    with pytest.raises(RuntimeError, match="changed"):
        await service.queue_legacy_promotion(
            _FakeSession(),
            principal=_principal(tenant_id=tenant_id, user_id=admin_user_id, role="org_admin"),
            company_dir=company_dir,
            request=LegacyPromotionIntakeRequest(
                relative_path="knowledge_base/policy.md",
                expected_sha256="0" * 64,
                proposed_namespace="company/policies",
                proposed_sensitivity="PL2_pii",
                purpose="Stale selection",
                risk_level="high",
                title=None,
                attest_scope_change=True,
                idempotency_key="legacy-promotion-stale",
                trace_id="trace-legacy-promotion-stale",
            ),
        )


def test_promotion_handoff_validation_binds_candidate_to_import_artifact() -> None:
    from app.services.company_knowledge_contracts import (
        CompanyKnowledgePromotionHandoff,
        validate_company_knowledge_promotion_handoff,
    )

    markdown = "# Candidate\n"
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    handoff = CompanyKnowledgePromotionHandoff(
        proposal_kind="personal_promotion",
        title="Candidate",
        original_source_ref="kb://person/owner/documents/doc",
        original_source_label="Candidate",
        source_revision_ref=content_hash,
        proposed_namespace="company/general",
        proposed_sensitivity="PL1_public",
        risk_level="normal",
        purpose="Explicit promotion",
        candidate_content_hash=content_hash,
        explicit_scope_change_attested=True,
        proposal_idempotency_key="proposal-key",
        trace_id="trace",
        conversion_receipt={},
    )

    assert (
        validate_company_knowledge_promotion_handoff(
            handoff,
            artifact_hash=content_hash,
            markdown=markdown,
        )
        is handoff
    )
    with pytest.raises(ValueError, match="candidate"):
        validate_company_knowledge_promotion_handoff(
            handoff,
            artifact_hash="0" * 64,
            markdown=markdown,
        )


def test_promotion_intake_business_view_never_exposes_raw_failure_code() -> None:
    from app.services.company_knowledge_promotion import CompanyKnowledgePromotionService

    now = datetime.now(timezone.utc)
    view = CompanyKnowledgePromotionService._intake_view(
        SimpleNamespace(
            id=uuid.uuid4(),
            request_json={
                "promotion_handoff": {
                    "proposal_kind": "legacy_import",
                    "title": "Legacy candidate",
                    "original_source_label": "retired.md",
                    "proposed_namespace": "company/general",
                    "proposed_sensitivity": "PL2_pii",
                }
            },
            status="failed",
            attempt_count=5,
            max_attempts=5,
            proposal_id=None,
            last_error_code="PrivateProviderFailure",
            created_at=now,
            updated_at=now,
        ),
        proposal=None,
    )

    assert view["status"] == "retry_required"
    assert "failure_code" not in view


def test_ordinary_company_import_payload_keeps_pre_promotion_hash_shape() -> None:
    from app.services.company_knowledge_service import (
        CompanyEvidenceIngestRequest,
        _company_evidence_request_payload,
    )

    request = CompanyEvidenceIngestRequest(
        source_contract_id=uuid.uuid4(),
        source_contract_version=1,
        evidence_kind="document",
        source_item_id="ordinary-document",
        source_revision="v1",
        title="Ordinary document",
        markdown="# Ordinary\n",
        typed_payload=None,
        external_artifact_ref=None,
        schema_ref=None,
        source_acl_snapshot={"role_names": ["org_admin"]},
        proposed_namespace="company/general",
        proposed_sensitivity="PL1_public",
        occurred_at=None,
        effective_from=None,
        effective_until=None,
        observed_at=datetime.now(timezone.utc),
        cursor={},
        sequence=None,
        coverage_ledger={
            "complete": True,
            "total_units": 1,
            "covered_units": 1,
            "missing_units": [],
        },
        purpose="ordinary import",
        idempotency_key="ordinary-import",
        trace_id="ordinary-trace",
    )

    assert "promotion_handoff" not in _company_evidence_request_payload(request)
