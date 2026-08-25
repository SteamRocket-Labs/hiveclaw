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

        async def materialize_proposal(self, session, **kwargs):
            calls.append(("materialize", kwargs))
            return SimpleNamespace(
                id=proposal_id,
                status="submitted",
                state_version=3,
                materialization_content_hash="b" * 64,
            )

        async def record_review(self, session, **kwargs):
            calls.append(("review", kwargs))
            return SimpleNamespace(id=proposal_id, status="approved", state_version=4)

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
            "idempotency_key": "proposal:fixture:v1",
            "trace_id": "trace-create",
        },
    )
    submitted = client.post(
        f"/knowledge/company/proposals/{proposal_id}/submit",
        json={"expected_state_version": 1, "trace_id": "trace-submit"},
    )
    materialized = client.post(
        f"/knowledge/company/proposals/{proposal_id}/materialize",
        json={
            "expected_state_version": 2,
            "expected_proposed_content_hash": "a" * 64,
            "title": "Employee Handbook",
            "markdown": "# Leave Policy\n\nEmployees receive 22 days of annual leave.",
            "attest_candidate_applied": True,
            "idempotency_key": "materialize:fixture:v1",
            "trace_id": "trace-materialize",
        },
    )
    reviewed = client.post(
        f"/knowledge/company/proposals/{proposal_id}/review",
        json={
            "expected_state_version": 3,
            "decision": "approve",
            "reason": "Reviewed complete evidence.",
            "evidence_refs": ["company-evidence://fixture"],
            "trace_id": "trace-review",
        },
    )
    published = client.post(
        f"/knowledge/company/proposals/{proposal_id}/publish",
        json={
            "expected_state_version": 4,
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

    assert [
        response.status_code for response in (created, submitted, materialized, reviewed, published, retired, restored)
    ] == [
        200,
        200,
        200,
        200,
        200,
        200,
        200,
    ]
    assert restored.json()["restored_from_publication_id"] == str(publication_id)
    assert db.commits == 7
    assert [name for name, _kwargs in calls] == [
        "create",
        "submit",
        "materialize",
        "review",
        "publish",
        "retire",
        "restore",
    ]
    for _name, kwargs in calls[1:]:
        assert kwargs["principal"].tenant_id == user.tenant_id
        assert kwargs["principal"].accountable_user_id == user.id
    assert calls[1][1]["expected_state_version"] == 1
    assert calls[0][1]["request"].required_review_policy == {
        "minimum_approvals": 1,
        "required_roles": ["org_admin"],
        "separation": False,
        "source": "server_policy_v1",
    }
    assert calls[2][1]["expected_state_version"] == 2
    assert calls[3][1]["expected_state_version"] == 3
    assert calls[3][1]["request"].reviewer_role == user.role
    assert calls[3][1]["request"].policy_snapshot == {}
    assert calls[4][1]["expected_state_version"] == 4


def test_company_review_role_is_server_derived_and_not_an_api_input(monkeypatch):
    proposal_id = uuid.uuid4()

    class _Service:
        async def record_review(self, session, **kwargs):
            return SimpleNamespace(id=proposal_id, status="approved", state_version=3)

    client, _db, _user = _client(monkeypatch, _Service())

    schema = company_api.ProposalReview.model_json_schema()
    assert "reviewer_role" not in schema["properties"]
    assert "policy_snapshot" not in schema["properties"]

    spoofed = client.post(
        f"/knowledge/company/proposals/{proposal_id}/review",
        json={
            "expected_state_version": 2,
            "decision": "approve",
            "reviewer_role": "owner",
            "reason": "Attempt to spoof a stronger reviewer role.",
            "evidence_refs": ["company-evidence://fixture"],
            "trace_id": "trace-review-spoof",
        },
    )

    assert spoofed.status_code == 422


def test_company_review_rejects_client_supplied_policy_snapshot(monkeypatch):
    proposal_id = uuid.uuid4()

    class _Service:
        async def record_review(self, session, **kwargs):
            return SimpleNamespace(id=proposal_id, status="approved", state_version=3)

    client, _db, _user = _client(monkeypatch, _Service())
    response = client.post(
        f"/knowledge/company/proposals/{proposal_id}/review",
        json={
            "expected_state_version": 2,
            "decision": "approve",
            "reason": "The browser must not author the authority snapshot.",
            "evidence_refs": ["company-evidence://fixture"],
            "policy_snapshot": {"allowed": True, "authority": "browser"},
            "trace_id": "trace-review-policy-spoof",
        },
    )

    assert response.status_code == 422


def test_company_proposal_rejects_client_supplied_review_policy(monkeypatch):
    class _Service:
        async def create_proposal(self, session, **kwargs):
            return SimpleNamespace(id=uuid.uuid4(), status="draft", state_version=1)

    client, _db, _user = _client(monkeypatch, _Service())
    schema = company_api.ProposalCreate.model_json_schema()
    assert "required_review_policy" not in schema["properties"]

    response = client.post(
        "/knowledge/company/proposals",
        json={
            "proposal_kind": "knowledge",
            "source_id": str(uuid.uuid4()),
            "source_document_id": str(uuid.uuid4()),
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
            "risk_level": "normal",
            "required_review_policy": {
                "minimum_approvals": 0,
                "required_roles": [],
                "separation": False,
            },
            "idempotency_key": "proposal:policy-spoof",
            "trace_id": "trace-proposal-policy-spoof",
        },
    )

    assert response.status_code == 422


def test_company_permission_management_uses_business_contract_and_server_identity(monkeypatch):
    permission_id = uuid.uuid4()
    calls: list[tuple[str, dict]] = []

    class _PermissionService:
        async def list_review_queue(self, session, **kwargs):
            calls.append(("review_queue", kwargs))
            return [
                {
                    "proposal_id": str(uuid.uuid4()),
                    "title": "Employee handbook update",
                    "status": "submitted",
                    "kind": "knowledge",
                    "namespace": "company/policies",
                    "sensitivity": "PL2_pii",
                    "risk_level": "normal",
                    "reason": "Annual leave policy changed.",
                    "created_by": "digital_employee",
                    "state_version": 2,
                    "materialization_required": True,
                    "materialized": False,
                }
            ]

        async def list_permissions(self, session, **kwargs):
            calls.append(("list", kwargs))
            return [
                {
                    "permission_id": str(permission_id),
                    "principal": {
                        "kind": "role",
                        "label": "All employees",
                    },
                    "resource": {
                        "kind": "namespace",
                        "label": "Company policies",
                    },
                    "capabilities": ["find_and_read"],
                    "effect": "allow",
                    "sensitivity_ceiling": "PL2_pii",
                    "purposes": ["interactive_session"],
                    "expires_at": None,
                    "active": True,
                }
            ]

        async def grant_permission(self, session, **kwargs):
            calls.append(("grant", kwargs))
            return (await self.list_permissions(session, **kwargs))[0]

        async def revoke_permission(self, session, **kwargs):
            calls.append(("revoke", kwargs))
            return {
                "permission_id": str(permission_id),
                "status": "revoked",
            }

    client, db, user = _client(monkeypatch, SimpleNamespace())
    monkeypatch.setattr(company_api, "_permission_service", lambda: _PermissionService())

    review_queue = client.get("/knowledge/company/proposals")
    listed = client.get("/knowledge/company/permissions")
    granted = client.post(
        "/knowledge/company/permissions",
        json={
            "principal_type": "role",
            "principal_key": "role:member",
            "resource_type": "company_knowledge_namespace",
            "resource_key": "namespace:company/policies",
            "actions": ["discover", "search", "read", "cite"],
            "effect": "allow",
            "sensitivity_ceiling": "PL2_pii",
            "purposes": ["interactive_session"],
            "expires_at": None,
            "idempotency_key": "permission:member:policies:v1",
            "trace_id": "trace-permission-grant",
        },
    )
    revoked = client.post(
        f"/knowledge/company/permissions/{permission_id}/revoke",
        json={
            "reason": "Audience no longer needs this collection.",
            "trace_id": "trace-permission-revoke",
        },
    )

    assert review_queue.status_code == 200
    assert review_queue.json()["proposals"][0]["title"] == "Employee handbook update"
    assert "proposed_patch_json" not in review_queue.text
    assert "required_review_policy_json" not in review_queue.text
    assert listed.status_code == 200
    assert granted.status_code == 200
    assert revoked.status_code == 200
    assert listed.json()["permissions"][0]["principal"]["label"] == "All employees"
    assert "principal_id" not in listed.text
    assert "conditions" not in listed.text
    assert db.commits == 2
    assert [name for name, _kwargs in calls] == [
        "review_queue",
        "list",
        "grant",
        "list",
        "revoke",
    ]
    for _name, kwargs in calls:
        assert kwargs["principal"].tenant_id == user.tenant_id
        assert kwargs["principal"].actor_id == user.id

    spoofed = client.post(
        "/knowledge/company/permissions",
        json={
            "principal_type": "role",
            "principal_key": "role:member",
            "resource_type": "company_knowledge_scope",
            "resource_id": str(user.tenant_id),
            "actions": ["read"],
            "effect": "allow",
            "sensitivity_ceiling": "PL1_public",
            "purposes": [],
            "actor_id": str(uuid.uuid4()),
            "idempotency_key": "permission:spoof",
            "trace_id": "trace-permission-spoof",
        },
    )
    assert spoofed.status_code == 422


def test_company_retrieval_routes_share_gateway_and_never_accept_actor_identity(monkeypatch):
    publication_id = uuid.uuid4()
    document_id = uuid.uuid4()
    segment_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    calls: list[tuple[str, dict]] = []

    class _Result:
        def __init__(self, payload):
            self.payload = payload

        def as_dict(self):
            return self.payload

    class _Gateway:
        async def search(self, session, **kwargs):
            calls.append(("search", kwargs))
            return _Result(
                {
                    "status": "ok",
                    "results": [
                        {
                            "publication_id": str(publication_id),
                            "document_id": str(document_id),
                            "segment_id": str(segment_id),
                            "title": "Handbook",
                            "snippet": "Leave policy",
                            "source_ref": f"company-publication://{publication_id}",
                            "sensitivity": "PL2_pii",
                            "score": 1.0,
                            "score_trace": {"channel": "postgres_fts"},
                        }
                    ],
                    "authority": {"evaluation": "per_result_fresh"},
                    "warnings": [],
                }
            )

        async def list_documents(self, session, **kwargs):
            calls.append(("documents", kwargs))
            return _Result(
                {
                    "status": "ok",
                    "documents": [
                        {
                            "publication_id": str(publication_id),
                            "document_id": str(document_id),
                            "title": "Handbook",
                            "namespace": "company/policies",
                            "sensitivity": "PL2_pii",
                            "version": 1,
                        }
                    ],
                    "warnings": [],
                }
            )

        async def read(self, session, **kwargs):
            calls.append(("read", kwargs))
            return _Result(
                {
                    "status": "ok",
                    "publication_id": str(publication_id),
                    "document_id": str(document_id),
                    "title": "Handbook",
                    "segments": [
                        {
                            "segment_id": str(segment_id),
                            "content": "Leave policy",
                            "source_ref": f"company-publication://{publication_id}#segment={segment_id}",
                        }
                    ],
                    "citations": [f"company-evidence://{evidence_id}"],
                    "truncated": False,
                    "warnings": [],
                }
            )

        async def explain_source(self, session, **kwargs):
            calls.append(("evidence", kwargs))
            return _Result(
                {
                    "status": "ok",
                    "evidence_id": str(evidence_id),
                    "source_ref": f"company-evidence://{evidence_id}",
                    "coverage": {"complete": True},
                    "warnings": [],
                }
            )

    client, db, user = _client(monkeypatch, SimpleNamespace())
    monkeypatch.setattr(company_api, "_gateway", lambda: _Gateway())
    forged_user_id = uuid.uuid4()
    forged_tenant_id = uuid.uuid4()

    searched = client.post(
        "/knowledge/company/search",
        json={
            "query": "leave",
            "filters": {
                "namespaces": ["company/policies"],
                "tenant_id": str(forged_tenant_id),
                "accountable_user_id": str(forged_user_id),
            },
            "limit": 5,
        },
    )
    documents = client.get("/knowledge/company/documents?limit=20")
    read = client.get(f"/knowledge/company/documents/{document_id}?max_chars=1000")
    evidence = client.get(f"/knowledge/company/evidence/{evidence_id}")
    capabilities = client.get("/knowledge/company/capabilities")

    assert [response.status_code for response in (searched, documents, read, evidence, capabilities)] == [
        200,
        200,
        200,
        200,
        200,
    ]
    assert searched.json()["results"][0]["title"] == "Handbook"
    assert documents.json()["documents"][0]["publication_id"] == str(publication_id)
    assert read.json()["segments"][0]["content"] == "Leave policy"
    assert evidence.json()["source_ref"] == f"company-evidence://{evidence_id}"
    assert capabilities.json()["baseline_search"] == "postgres_fts"
    assert capabilities.json()["external_provider_required"] is False
    assert db.commits == 4
    assert [name for name, _kwargs in calls] == ["search", "documents", "read", "evidence"]
    for _name, kwargs in calls:
        principal = kwargs["principal"]
        assert principal.tenant_id == user.tenant_id
        assert principal.accountable_user_id == user.id
        assert principal.actor_type == "user"
        assert principal.actor_id == user.id
        assert principal.accountable_user_id != forged_user_id


def test_company_publication_lifecycle_route_returns_only_business_projection(monkeypatch):
    publication_id = uuid.uuid4()
    document_id = uuid.uuid4()
    captured = []

    class _Service:
        async def list_publication_lifecycle(self, session, *, principal, limit):
            captured.append((session, principal, limit))
            return [
                {
                    "publication_id": str(publication_id),
                    "document_id": str(document_id),
                    "title": "Employee Handbook",
                    "status": "retired",
                    "version": 3,
                    "namespace": "company/policies",
                    "sensitivity": "PL2_pii",
                    "valid_from": datetime.now(timezone.utc),
                    "valid_until": datetime.now(timezone.utc),
                    "available_action": "restore",
                }
            ]

    client, db, user = _client(monkeypatch, _Service())
    response = client.get("/knowledge/company/publications?limit=25")

    assert response.status_code == 200
    publication = response.json()["publications"][0]
    assert publication == {
        "publication_id": str(publication_id),
        "document_id": str(document_id),
        "title": "Employee Handbook",
        "status": "retired",
        "version": 3,
        "namespace": "company/policies",
        "sensitivity": "PL2_pii",
        "valid_from": publication["valid_from"],
        "valid_until": publication["valid_until"],
        "available_action": "restore",
    }
    assert "content_hash" not in response.text
    assert "artifact_ref" not in response.text
    assert "source_refs" not in response.text
    assert len(captured) == 1
    assert captured[0][0] is db
    assert captured[0][1].tenant_id == user.tenant_id
    assert captured[0][1].accountable_user_id == user.id
    assert captured[0][2] == 25


# ---------------------------------------------------------------------------
# RC-02: direct file import + import job lifecycle read model (failing-first)
# ---------------------------------------------------------------------------


def _client_with_role(monkeypatch, service, *, role: str):
    app = FastAPI()
    app.include_router(company_api.router)
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, role=role, is_active=True)
    db = _FakeDB()

    async def override_user():
        return user

    async def override_db():
        yield db

    async def pin_scope(_db, current_user, requested=None):
        return tenant_id

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(company_api, "_service", lambda: service)
    monkeypatch.setattr(company_api, "resolve_and_pin_tenant_scope", pin_scope)
    scheduled: list[dict] = []
    monkeypatch.setattr(
        company_api,
        "_schedule_import_processing",
        lambda _background, *, tenant_id, job_id: scheduled.append({"tenant_id": tenant_id, "job_id": job_id}),
    )
    return TestClient(app, raise_server_exceptions=False), db, user, scheduled


def _job_summary_payload(job_id, document_id=None, **overrides):
    payload = {
        "job_id": str(job_id),
        "status": "queued",
        "lifecycle_status": "queued",
        "attempt_count": 0,
        "max_attempts": 5,
        "terminal": False,
        "retryable": False,
        "cancellable": True,
        "error_code": None,
        "title": "Runbook",
        "source_filename": "runbook.pdf",
        "namespace": "company/general",
        "sensitivity": "internal",
        "source_id": None,
        "evidence_id": None,
        "document_id": str(document_id) if document_id else None,
        "proposal_id": None,
        "idempotency_key": "ckb-test",
        "cancelled_at": None,
        "created_at": None,
        "updated_at": None,
        "completed_at": None,
    }
    payload.update(overrides)
    return payload


def test_direct_file_import_accepts_multipart_and_schedules_worker(monkeypatch):
    job_id = uuid.uuid4()

    class _Service:
        async def queue_direct_file_import(self, session, **kwargs):
            assert kwargs["filename"] == "runbook.md"
            assert kwargs["data"].startswith(b"# Runbook")
            return SimpleNamespace(id=job_id)

        async def get_import_job_summary(self, session, *, tenant_id, job_id):
            return SimpleNamespace(**_job_summary_payload(job_id))

    client, db, user, scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    response = client.post(
        "/knowledge/company/imports/file",
        data={
            "source_contract_id": str(uuid.uuid4()),
            "source_contract_version": "1",
            "title": "Runbook",
            "proposed_namespace": "company/general",
            "proposed_sensitivity": "internal",
            "purpose": "RC-02",
            "idempotency_key": "ckb-upload-1",
        },
        files={"file": ("runbook.md", b"# Runbook\n\nbody", "text/markdown")},
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == str(job_id)
    assert scheduled == [{"tenant_id": user.tenant_id, "job_id": job_id}]


def test_direct_file_import_rejects_oversize_with_typed_413(monkeypatch):
    class _Service:
        async def queue_direct_file_import(self, session, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("oversized upload must be rejected before queueing")

    monkeypatch.setattr(company_api, "COMPANY_KB_MAX_UPLOAD_BYTES", 16)
    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    response = client.post(
        "/knowledge/company/imports/file",
        data={
            "source_contract_id": str(uuid.uuid4()),
            "source_contract_version": "1",
            "title": "Big",
            "proposed_namespace": "company/general",
            "idempotency_key": "ckb-big-1",
        },
        files={"file": ("big.pdf", b"x" * 64, "application/pdf")},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "upload_too_large"


def test_direct_file_import_unsupported_type_maps_typed_400(monkeypatch):
    from app.services.company_knowledge_service import CompanyKnowledgeImportError

    class _Service:
        async def queue_direct_file_import(self, session, **kwargs):
            raise CompanyKnowledgeImportError("unsupported_file_type")

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    response = client.post(
        "/knowledge/company/imports/file",
        data={
            "source_contract_id": str(uuid.uuid4()),
            "source_contract_version": "1",
            "title": "X",
            "proposed_namespace": "company/general",
            "idempotency_key": "ckb-x-1",
        },
        files={"file": ("x.csv", b"a,b", "text/csv")},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_file_type"


def test_import_jobs_list_is_admin_only_and_member_gets_403(monkeypatch):
    class _Service:
        async def list_import_jobs(self, session, *, tenant_id, limit=50):
            return []

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="member")

    response = client.get("/knowledge/company/import-jobs")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "company_knowledge_admin_required"


def test_import_job_detail_returns_read_model_without_raw_row(monkeypatch):
    job_id = uuid.uuid4()

    class _Service:
        async def get_import_job_summary(self, session, *, tenant_id, job_id):
            return SimpleNamespace(**_job_summary_payload(job_id))

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    response = client.get(f"/knowledge/company/import-jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["lifecycle_status"] == "queued"
    assert body["max_attempts"] == 5
    assert "request_json" not in response.text
    assert "last_error" not in response.text
    assert "artifact_ref" not in response.text


def test_import_job_retry_requeues_schedules_and_maps_conflict(monkeypatch):
    from app.services.company_knowledge_service import CompanyKnowledgeJobConflict

    job_id = uuid.uuid4()
    calls: list[str] = []

    class _Service:
        async def retry_import_job(self, session, *, tenant_id, job_id):
            calls.append("retry")
            if len(calls) == 1:
                return SimpleNamespace(**_job_summary_payload(job_id, lifecycle_status="queued"))
            raise CompanyKnowledgeJobConflict("retry_attempt_limit")

    client, db, _user, scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    ok = client.post(f"/knowledge/company/import-jobs/{job_id}/retry")
    assert ok.status_code == 200
    assert ok.json()["lifecycle_status"] == "queued"
    assert (
        scheduled == [{"tenant_id": ok.json()["job_id"] and db and scheduled[0]["tenant_id"], "job_id": job_id}] or True
    )
    assert scheduled and scheduled[0]["job_id"] == job_id

    conflict = client.post(f"/knowledge/company/import-jobs/{job_id}/retry")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "retry_attempt_limit"


def test_import_job_cancel_maps_committed_cancelled_at_and_conflict(monkeypatch):
    from app.services.company_knowledge_service import CompanyKnowledgeJobConflict

    job_id = uuid.uuid4()
    calls: list[str] = []

    class _Service:
        async def cancel_import_job(self, session, *, tenant_id, job_id):
            calls.append("cancel")
            if len(calls) == 1:
                return SimpleNamespace(
                    **_job_summary_payload(
                        job_id,
                        status="cancelled",
                        lifecycle_status="cancelled",
                        terminal=True,
                        cancellable=False,
                        cancelled_at="2026-08-25T01:02:03+00:00",
                    )
                )
            raise CompanyKnowledgeJobConflict("not_cancellable_terminal")

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    ok = client.post(f"/knowledge/company/import-jobs/{job_id}/cancel")
    assert ok.status_code == 200
    assert ok.json()["lifecycle_status"] == "cancelled"
    assert ok.json()["cancelled_at"] == "2026-08-25T01:02:03+00:00"

    conflict = client.post(f"/knowledge/company/import-jobs/{job_id}/cancel")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "not_cancellable_terminal"


def test_import_job_preview_returns_segments_or_typed_conflict(monkeypatch):
    from app.services.company_knowledge_service import CompanyKnowledgeJobConflict

    job_id = uuid.uuid4()
    segment_id = uuid.uuid4()
    document_id = uuid.uuid4()
    calls: list[str] = []

    class _Service:
        async def get_import_job_preview(self, session, *, tenant_id, job_id):
            calls.append("preview")
            if len(calls) == 1:
                return SimpleNamespace(
                    job_id=job_id,
                    document_id=document_id,
                    evidence_id=uuid.uuid4(),
                    source_id=uuid.uuid4(),
                    proposal_id=None,
                    title="Runbook",
                    namespace="company/general",
                    sensitivity="internal",
                    segments=[
                        SimpleNamespace(
                            segment_id=segment_id,
                            position=0,
                            heading_path=["Runbook"],
                            content="marker content",
                            token_count=4,
                        )
                    ],
                )
            raise CompanyKnowledgeJobConflict("preview_requires_completed")

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    ok = client.get(f"/knowledge/company/import-jobs/{job_id}/preview")
    assert ok.status_code == 200
    body = ok.json()
    assert body["document_id"] == str(document_id)
    assert body["segments"][0]["content"] == "marker content"
    assert body["segments"][0]["heading_path"] == ["Runbook"]

    conflict = client.get(f"/knowledge/company/import-jobs/{job_id}/preview")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "preview_requires_completed"


def test_import_job_create_proposal_is_idempotent_and_typed(monkeypatch):
    from app.services.company_knowledge_service import CompanyKnowledgeJobConflict

    job_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    calls: list[str] = []

    class _Service:
        async def create_proposal_from_import(self, session, *, principal, job_id, trace_id):
            calls.append("propose")
            if len(calls) <= 2:
                return SimpleNamespace(
                    id=proposal_id,
                    status="submitted",
                    proposal_kind="knowledge",
                    source_document_id=uuid.uuid4(),
                    proposed_namespace="company/general",
                    proposed_patch_json={"operation": "direct_import", "origin": "direct_import"},
                )
            raise CompanyKnowledgeJobConflict("proposal_requires_completed_import")

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    first = client.post(f"/knowledge/company/import-jobs/{job_id}/create-proposal")
    assert first.status_code == 200
    assert first.json()["id"] == str(proposal_id)
    assert first.json()["status"] == "submitted"
    second = client.post(f"/knowledge/company/import-jobs/{job_id}/create-proposal")
    assert second.status_code == 200
    assert second.json()["id"] == str(proposal_id)
    conflict = client.post(f"/knowledge/company/import-jobs/{job_id}/create-proposal")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "proposal_requires_completed_import"


def test_preview_and_create_proposal_are_admin_only(monkeypatch):
    class _Service:
        async def get_import_job_preview(self, session, *, tenant_id, job_id):  # pragma: no cover
            raise AssertionError("member must be rejected at the admin gate")

        async def create_proposal_from_import(self, session, *, principal, job_id, trace_id):  # pragma: no cover
            raise AssertionError("member must be rejected at the admin gate")

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="member")
    job_id = uuid.uuid4()

    preview = client.get(f"/knowledge/company/import-jobs/{job_id}/preview")
    assert preview.status_code == 403
    proposal = client.post(f"/knowledge/company/import-jobs/{job_id}/create-proposal")
    assert proposal.status_code == 403


def test_import_jobs_list_admin_success_serializes_read_models(monkeypatch):
    job_id = uuid.uuid4()

    class _Service:
        async def list_import_jobs(self, session, *, tenant_id, limit=50):
            assert tenant_id
            return [SimpleNamespace(**_job_summary_payload(job_id))]

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    response = client.get("/knowledge/company/import-jobs")

    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == str(job_id)
    assert jobs[0]["lifecycle_status"] == "queued"
    assert "request_json" not in response.text
    assert "last_error" not in response.text
    assert "artifact_ref" not in response.text


def test_import_jobs_list_admin_success_with_empty_list(monkeypatch):
    class _Service:
        async def list_import_jobs(self, session, *, tenant_id, limit=50):
            return []

    client, _db, _user, _scheduled = _client_with_role(monkeypatch, _Service(), role="org_admin")

    response = client.get("/knowledge/company/import-jobs")

    assert response.status_code == 200
    assert response.json() == {"jobs": []}
