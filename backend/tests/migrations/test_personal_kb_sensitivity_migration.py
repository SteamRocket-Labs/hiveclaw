from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "personal_kb_sensitivity_canonical_0715.py"


def test_personal_kb_sensitivity_migration_is_canonical_and_reversible() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "personal_kb_sensitivity_canonical_0715"' in source
    assert 'down_revision = "memory_context_warning_0714"' in source
    assert "knowledge_documents" in source
    assert "personal_knowledge_proposals" in source
    assert "PL1_public" in source
    assert "PL2_pii" in source
    assert "PL3_sensitive" in source
    assert "PL4_credential" in source
    assert "legacy_sensitivity_original" in source
    assert "def downgrade()" in source


def _alembic(database_url: str, command: str, target: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout={result.stdout[-2000:]} stderr={result.stderr[-2000:]}"


async def _read_sensitivity_state(database_url: str, document_id: uuid.UUID, proposal_id: uuid.UUID):
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            document = (
                await connection.execute(
                    text("SELECT sensitivity, doc_metadata_json FROM knowledge_documents WHERE id = :document_id"),
                    {"document_id": document_id},
                )
            ).one()
            proposal = (
                await connection.execute(
                    text(
                        "SELECT sensitivity, policy_reason_codes_json FROM personal_knowledge_proposals "
                        "WHERE id = :proposal_id"
                    ),
                    {"proposal_id": proposal_id},
                )
            ).one()
        return document, proposal
    finally:
        await engine.dispose()


async def test_personal_kb_sensitivity_real_postgres_roundtrip(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"kb_sensitivity_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    # A fresh database is bootstrapped from current ORM metadata and stamped at
    # head. Rewind it to the previous production revision so the next upgrade
    # executes this migration's data backfill instead of short-circuiting it.
    _alembic_upgrade(database_url, "head")
    _alembic(database_url, "downgrade", "memory_context_warning_0714")

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    document_id = uuid.uuid4()
    proposal_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET session_replication_role = replica"))
            await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_documents (
                        id, tenant_id, scope_type, scope_id, source_kind, source_sha256,
                        title, status, sensitivity, agent_searchable, canonical_md_path,
                        doc_metadata_json
                    ) VALUES (
                        :id, :tenant_id, 'person', :owner_id, 'paste', :source_sha,
                        'Legacy document', 'ready', 'confidential', true, 'legacy.md',
                        CAST(:metadata AS jsonb)
                    )
                    """
                ),
                {
                    "id": document_id,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "source_sha": "d" * 64,
                    "metadata": json.dumps({"keep": "document"}),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO personal_knowledge_proposals (
                        id, tenant_id, owner_user_id, proposed_by_agent_id, title,
                        proposed_content, content_hash, diff_unified, target_collection,
                        source_refs_json, sensitivity, purpose, dedupe_key, idempotency_key,
                        policy_outcome, policy_reason_codes_json, status
                    ) VALUES (
                        :id, :tenant_id, :owner_id, :agent_id, 'Legacy proposal',
                        'legacy', :content_hash, '', 'inbox', '[]'::jsonb, 'private',
                        'owner review', :dedupe_key, :idempotency_key, 'ask',
                        CAST(:reason_codes AS jsonb), 'pending'
                    )
                    """
                ),
                {
                    "id": proposal_id,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "content_hash": "c" * 64,
                    "dedupe_key": f"legacy-{proposal_id}",
                    "idempotency_key": f"legacy-{proposal_id}",
                    "reason_codes": json.dumps(["keep_proposal"]),
                },
            )
            await connection.execute(text("SET session_replication_role = DEFAULT"))
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    document, proposal = await _read_sensitivity_state(database_url, document_id, proposal_id)
    assert document.sensitivity == "PL3_sensitive"
    assert document.doc_metadata_json["keep"] == "document"
    assert "confidential" in json.dumps(document.doc_metadata_json)
    assert proposal.sensitivity == "PL3_sensitive"
    assert "keep_proposal" in proposal.policy_reason_codes_json
    assert "private" in json.dumps(proposal.policy_reason_codes_json)

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            invalid_document_id = uuid.uuid4()
            try:
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            """
                            INSERT INTO knowledge_documents (
                                id, tenant_id, scope_type, scope_id, source_kind, source_sha256,
                                title, status, sensitivity, agent_searchable, canonical_md_path,
                                doc_metadata_json
                            ) VALUES (
                                :id, :tenant_id, 'person', :owner_id, 'paste', :source_sha,
                                'Invalid', 'ready', 'legacy_unknown', true, 'invalid.md', '{}'::jsonb
                            )
                            """
                        ),
                        {
                            "id": invalid_document_id,
                            "tenant_id": tenant_id,
                            "owner_id": owner_id,
                            "source_sha": "e" * 64,
                        },
                    )
            except Exception as exc:
                assert "ck_knowledge_documents_sensitivity" in str(exc)
            else:
                raise AssertionError("canonical sensitivity constraint accepted an unknown label")
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "memory_context_warning_0714")
    document, proposal = await _read_sensitivity_state(database_url, document_id, proposal_id)
    assert document.sensitivity == "confidential"
    assert document.doc_metadata_json == {"keep": "document"}
    assert proposal.sensitivity == "private"
    assert proposal.policy_reason_codes_json == ["keep_proposal"]
