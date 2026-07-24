from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.knowledge_company as company_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _client(monkeypatch, service):
    app = FastAPI()
    app.include_router(company_api.router)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    user = SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        role="org_admin",
        is_active=True,
    )
    db = _FakeDB()

    async def override_user():
        return user

    async def override_db():
        yield db

    async def pin_scope(_db, current_user, requested=None):
        assert _db is db
        assert current_user is user
        assert requested is None
        return tenant_id

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(company_api, "_service", lambda: service)
    monkeypatch.setattr(company_api, "resolve_and_pin_tenant_scope", pin_scope)
    monkeypatch.setattr(company_api, "_schedule_import_processing", lambda *args, **kwargs: None)
    return TestClient(app, raise_server_exceptions=False), db, user


def test_company_source_and_import_routes_derive_principal_from_authentication(monkeypatch):
    contract_id = uuid.uuid4()
    job_id = uuid.uuid4()
    captured: list[tuple[str, object, object]] = []

    class _Service:
        async def register_source_contract(self, session, *, principal, contract_input, idempotency_key, trace_id):
            captured.append(("contract", principal, contract_input))
            assert idempotency_key == "contract:handbook:v1"
            assert trace_id == "trace-contract"
            return SimpleNamespace(
                id=contract_id,
                tenant_id=principal.tenant_id,
                version=1,
                status="active",
                stable_source_id=contract_input.stable_source_id,
                contract_hash="a" * 64,
            )

        async def queue_evidence_import(self, session, *, principal, request):
            captured.append(("import", principal, request))
            return SimpleNamespace(
                id=job_id,
                tenant_id=principal.tenant_id,
                status="queued",
                source_contract_id=request.source_contract_id,
                source_contract_version=request.source_contract_version,
                idempotency_key=request.idempotency_key,
                trace_id=request.trace_id,
            )

    client, db, user = _client(monkeypatch, _Service())
    contract = client.post(
        "/knowledge/company/source-contracts",
        json={
            "source_kind": "document",
            "provider_kind": "native",
            "stable_source_id": "employee-handbook",
            "owner_principal_ref": "role:org_admin",
            "accountable_steward_ref": "role:org_admin",
            "connection_ref": None,
            "schema_ref": "schema://company-document/v1",
            "schema_version": "1",
            "identity_keys": ["source_item_id"],
            "relation_keys": [],
            "ingest_mode": "manual",
            "cursor_kind": None,
            "cursor_policy": {},
            "watermark_field": None,
            "temporal_mapping": {"observed_at": "ingest_time"},
            "source_acl_mapping_policy": {"mode": "required_snapshot"},
            "default_sensitivity": "PL2_pii",
            "export_policy": {"allowed": False},
            "retention_policy": {"class": "company_record"},
            "legal_hold_policy": {"supported": True},
            "allowed_namespaces": ["company/policies"],
            "precedence_policy_ref": None,
            "acceptance_suite_ref": "acceptance://company-document/v1",
            "idempotency_policy": {"key": "source_item_id+revision"},
            "idempotency_key": "contract:handbook:v1",
            "trace_id": "trace-contract",
        },
    )
    imported = client.post(
        "/knowledge/company/imports",
        json={
            "source_contract_id": str(contract_id),
            "source_contract_version": 1,
            "evidence_kind": "document",
            "source_item_id": "employee-handbook",
            "source_revision": "2026-07-24",
            "title": "Employee Handbook",
            "markdown": "# Leave Policy\\n\\n20 days.",
            "typed_payload": None,
            "external_artifact_ref": None,
            "schema_ref": "schema://company-document/v1",
            "source_acl_snapshot": {"role_names": ["member", "org_admin"]},
            "proposed_namespace": "company/policies",
            "proposed_sensitivity": "PL2_pii",
            "occurred_at": None,
            "effective_from": datetime.now(timezone.utc).isoformat(),
            "effective_until": None,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "cursor": {},
            "sequence": None,
            "coverage_ledger": {
                "complete": True,
                "total_units": 1,
                "covered_units": 1,
                "missing_units": [],
            },
            "purpose": "publish policy",
            "idempotency_key": "import:handbook:v1",
            "trace_id": "trace-import",
        },
    )

    assert contract.status_code == 200
    assert imported.status_code == 202
    assert contract.json()["id"] == str(contract_id)
    assert imported.json()["id"] == str(job_id)
    assert db.commits == 2
    assert [item[0] for item in captured] == ["contract", "import"]
    for _name, principal, _request in captured:
        assert principal.tenant_id == user.tenant_id
        assert principal.accountable_user_id == user.id
        assert principal.actor_id == user.id
        assert principal.actor_type == "user"
        assert principal.accountable_role == "org_admin"


def test_company_proposal_review_publish_and_recovery_routes_use_exact_state_versions(monkeypatch):
    proposal_id = uuid.uuid4()
    publication_id = uuid.uuid4()
    restored_id = uuid.uuid4()
    calls: list[tuple[str, dict]] = []

    class _Service:
        async def create_proposal(self, session, *, principal, request):
            calls.append(("create", {"principal": principal, "request": request}))
            return SimpleNamespace(id=proposal_id, status="draft", state_version=1)

        async def submit_proposal(self, session, **kwargs):
            calls.append(("submit", kwargs))
            return SimpleNamespace(id=proposal_id, status="submitted", state_version=2)

        async def record_review(self, session, **kwargs):
            calls.append(("review", kwargs))
            return SimpleNamespace(id=proposal_id, status="approved", state_version=3)

        async def publish_proposal(self, session, **kwargs):
            calls.append(("publish", kwargs))
            return SimpleNamespace(id=publication_id, status="active", version=1, document_id=uuid.uuid4())

        async def retire_publication(self, session, **kwargs):
            calls.append(("retire", kwargs))
            return SimpleNamespace(id=publication_id, status="retired", version=1)

        async def restore_publication(self, session, **kwargs):
            calls.append(("restore", kwargs))
            return SimpleNamespace(
                id=restored_id,
                status="active",
                version=2,
                restored_from_publication_id=publication_id,
            )

    client, db, user = _client(monkeypatch, _Service())
    created = client.post(
        "/knowledge/company/proposals",
        json={
            "proposal_kind": "knowledge",
            "source_id": str(uuid.uuid4()),
            "source_document_id": str(uuid.uuid4()),
            "source_revision_ref": "v1",
            "baseline_publication_id": None,
            "baseline_version": None,
            "proposed_patch": {"operation": "publish_document"},
            "proposed_namespace": "company/policies",
            "proposed_sensitivity": "PL2_pii",
            "source_refs": ["company-evidence://fixture"],
            "source_coverage": {
                "complete": True,
                "total_units": 1,
                "covered_units": 1,
                "missing_units": [],
            },
            "conflict_candidates": [],
            "ontology_mapping": {},
            "risk_level": "normal",
            "required_review_policy": {
                "minimum_approvals": 1,
                "required_roles": ["org_admin"],
                "separation": False,
            },
            "idempotency_key": "proposal:fixture:v1",
            "trace_id": "trace-create",
        },
    )
    submitted = client.post(
        f"/knowledge/company/proposals/{proposal_id}/submit",
        json={"expected_state_version": 1, "trace_id": "trace-submit"},
    )
    reviewed = client.post(
        f"/knowledge/company/proposals/{proposal_id}/review",
        json={
            "expected_state_version": 2,
            "decision": "approve",
            "reviewer_role": "org_admin",
            "reason": "Reviewed complete evidence.",
            "evidence_refs": ["company-evidence://fixture"],
            "policy_snapshot": {"policy": "v1"},
            "trace_id": "trace-review",
        },
    )
    published = client.post(
        f"/knowledge/company/proposals/{proposal_id}/publish",
        json={
            "expected_state_version": 3,
            "valid_from": datetime.now(timezone.utc).isoformat(),
            "valid_until": None,
            "trace_id": "trace-publish",
        },
    )
    retired = client.post(
        f"/knowledge/company/publications/{publication_id}/retire",
        json={"reason": "Withdrawn", "trace_id": "trace-retire"},
    )
    restored = client.post(
        f"/knowledge/company/publications/{publication_id}/restore",
        json={
            "reason": "Re-approved",
            "valid_from": datetime.now(timezone.utc).isoformat(),
            "trace_id": "trace-restore",
        },
    )

    assert [response.status_code for response in (created, submitted, reviewed, published, retired, restored)] == [
        200,
        200,
        200,
        200,
        200,
        200,
    ]
    assert restored.json()["restored_from_publication_id"] == str(publication_id)
    assert db.commits == 6
    assert [name for name, _kwargs in calls] == ["create", "submit", "review", "publish", "retire", "restore"]
    for _name, kwargs in calls[1:]:
        assert kwargs["principal"].tenant_id == user.tenant_id
        assert kwargs["principal"].accountable_user_id == user.id
    assert calls[1][1]["expected_state_version"] == 1
    assert calls[2][1]["expected_state_version"] == 2
    assert calls[3][1]["expected_state_version"] == 3
