"""Incremental upgrade, legacy review compatibility and strict application-role RLS."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import tenant_scoped_session
from app.models.company_knowledge import CompanyKnowledgeProposal
from app.models.tenant import Tenant
from app.models.user import User
from app.services.company_knowledge_service import CompanyEvidenceIngestRequest, CompanyKnowledgeService
from tests.integration import conftest as persistence
from tests.integration.test_company_knowledge_closed_loop import _contract, _principal

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "alembic/versions/company_knowledge_analysis_1020.py"


@asynccontextmanager
async def migration_database(container):
    from sqlalchemy.engine import make_url

    # The fixture owns the server. Keep this additional test database inside
    # it so the fixture's evidence/cleanup lifecycle also owns these rows.
    base = make_url(persistence._async_url(container))
    database = "m1g_" + uuid4().hex
    creator = create_async_engine(base, isolation_level="AUTOCOMMIT")
    try:
        async with creator.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE \"{database}\" TEMPLATE template0 ENCODING 'UTF8'"))
        yield base.set(database=database).render_as_string(hide_password=False)
    finally:
        await creator.dispose()


def upgrade(url):
    result = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]


def revision():
    spec = importlib.util.spec_from_file_location("knowledge_analysis_upgrade_probe", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def downgrade(connection):
    with Operations.context(MigrationContext.configure(connection)):
        revision().downgrade()


async def test_incremental_upgrade_preserves_old_review_and_enforces_rls_and_data_bearing_downgrade(
    app_user_engine,
    pg_container,
    tmp_path,
):
    tenant_id, user_id, other_tenant = uuid4(), uuid4(), uuid4()
    async with migration_database(pg_container) as url:
        upgrade(url)
        owner = create_async_engine(url)
        owner_factory = async_sessionmaker(owner, expire_on_commit=False)
        try:
            async with owner.begin() as db:
                await db.run_sync(downgrade)
                await db.execute(
                    text("UPDATE alembic_version SET version_num=:version"), {"version": revision().down_revision}
                )
            async with owner_factory() as db:
                db.add(Tenant(id=tenant_id, name="Legacy knowledge", slug="legacy-" + tenant_id.hex))
                await db.flush()
                db.add(
                    User(
                        id=user_id,
                        tenant_id=tenant_id,
                        role="org_admin",
                        username="legacy-" + user_id.hex,
                        email=f"{user_id}@upgrade.test",
                        password_hash="synthetic",
                        display_name="Legacy reviewer",
                    )
                )
                await db.flush()
                proposal = CompanyKnowledgeProposal(
                    tenant_id=tenant_id,
                    idempotency_key="legacy-approved",
                    proposal_kind="knowledge",
                    proposed_patch_json={"title": "Approved legacy document"},
                    proposed_content_hash="a" * 64,
                    proposed_namespace="company/policies",
                    proposed_sensitivity="PL2_pii",
                    source_refs_json=[],
                    source_coverage_json={"complete": True},
                    conflict_candidates_json=[],
                    ontology_mapping_json={},
                    status="approved",
                    risk_level="normal",
                    required_review_policy_json={},
                    created_by_type="user",
                    created_by_id=user_id,
                    accountable_user_id=user_id,
                    state_version=4,
                )
                db.add(proposal)
                await db.commit()
                proposal_id = proposal.id
            # Existing version table forces the real incremental revision path.
            upgrade(url)
            async with owner_factory() as db:
                proposal = await db.get(CompanyKnowledgeProposal, proposal_id)
                assert proposal.status == "approved" and proposal.state_version == 4
                assert proposal.proposed_content_hash == "a" * 64
                assert proposal.ontology_mapping_json["analysis_legacy_review"] == {
                    "revision": "company_knowledge_analysis_1020",
                    "candidate_hash": "a" * 64,
                    "materialized_hash": None,
                }
                from app.services.company_knowledge_analysis import CompanyKnowledgeAnalysisReview

                assert CompanyKnowledgeAnalysisReview.legacy_review(proposal)
                for table in ("company_knowledge_analysis_jobs", "company_knowledge_extractions"):
                    flags = (
                        await db.execute(
                            text("SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=:table"),
                            {"table": table},
                        )
                    ).one()
                    assert flags == (True, True)
                await db.execute(text(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{persistence.APP_USER}"'))
                await db.execute(text(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{persistence.APP_USER}"'))
                await db.commit()
            from sqlalchemy.engine import make_url

            app_url = app_user_engine.url.set(database=make_url(url).database)
            app_engine = create_async_engine(app_url)
            factory = async_sessionmaker(app_engine, expire_on_commit=False)
            try:
                service = CompanyKnowledgeService(data_root=tmp_path)
                principal = _principal(tenant_id=tenant_id, user_id=user_id)
                now = datetime.now(timezone.utc)
                async with tenant_scoped_session(tenant_id, session_factory=factory) as db:
                    contract = await service.register_source_contract(
                        db,
                        principal=principal,
                        contract_input=_contract(),
                        idempotency_key="upgrade-source",
                        trace_id="upgrade-source",
                    )
                    job = await service.queue_evidence_import(
                        db,
                        principal=principal,
                        request=CompanyEvidenceIngestRequest(
                            source_contract_id=contract.id,
                            source_contract_version=contract.version,
                            evidence_kind="document",
                            source_item_id="upgrade-source",
                            source_revision="one",
                            title="Upgrade source",
                            markdown="# Upgrade\nComplete source.\n",
                            typed_payload=None,
                            external_artifact_ref=None,
                            schema_ref="schema://company-document/v1",
                            source_acl_snapshot={"role_names": ["org_admin"]},
                            proposed_namespace="company/policies",
                            proposed_sensitivity="PL2_pii",
                            occurred_at=None,
                            effective_from=now,
                            effective_until=None,
                            observed_at=now,
                            cursor={},
                            sequence=None,
                            coverage_ledger={
                                "complete": True,
                                "total_units": 1,
                                "covered_units": 1,
                                "missing_units": [],
                            },
                            purpose="Upgrade test",
                            idempotency_key="upgrade-import",
                            trace_id="upgrade-import",
                        ),
                    )
                    await db.commit()
                result = await service.process_import_job(tenant_id=tenant_id, job_id=job.id, session_factory=factory)
                assert result.status == "completed"
                from app.models.company_knowledge_analysis import (
                    CompanyKnowledgeAnalysisJob,
                    CompanyKnowledgeExtraction,
                )
                from app.services.company_knowledge_analysis import analysis_hash

                # A non-authoritative quarantined row makes the extraction
                # policy probe non-vacuous without inventing model execution.
                async with tenant_scoped_session(tenant_id, session_factory=factory) as db:
                    analysis = await db.scalar(select(CompanyKnowledgeAnalysisJob))
                    analysis.status, analysis.attempt_count = "held", 1
                    analysis.error_code = "company_analysis_candidate_invalid"
                    analysis.dispatch_receipt_json = {"status": "received"}
                    candidate = {"unparsed_response": "Controlled invalid JSON policy fixture"}
                    db.add(
                        CompanyKnowledgeExtraction(
                            tenant_id=tenant_id,
                            job_id=analysis.id,
                            evidence_id=result.evidence_id,
                            input_hash=analysis.input_hash,
                            attempt=1,
                            validated=False,
                            candidate_hash=analysis_hash(candidate),
                            candidate_json=candidate,
                            model_receipt_json={
                                "receipt_source": "migration_rls_fixture",
                                "raw_response": candidate["unparsed_response"],
                            },
                        )
                    )
                    await db.commit()
                for scope, expected in ((str(tenant_id), 1), (str(other_tenant), 0), ("", 0), ("BYPASS", 1)):
                    async with factory() as db:
                        assert not await db.scalar(text("SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user"))
                        await db.execute(
                            text("SELECT set_config('app.current_tenant_id', :scope, true)"), {"scope": scope}
                        )
                        assert await db.scalar(text("SELECT count(*) FROM company_knowledge_analysis_jobs")) == expected
                        assert await db.scalar(text("SELECT count(*) FROM company_knowledge_extractions")) == expected
                async with owner.begin() as db:
                    with pytest.raises(RuntimeError, match="requires_empty_runtime_data"):
                        await db.run_sync(downgrade)
                    assert (
                        await db.scalar(select(text("count(*)")).select_from(text("company_knowledge_analysis_jobs")))
                        == 1
                    )
            finally:
                await app_engine.dispose()
        finally:
            await owner.dispose()
