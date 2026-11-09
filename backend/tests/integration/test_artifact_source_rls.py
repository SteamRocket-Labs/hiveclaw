"""Artifact source checks retain tenant RLS inside a privileged worker call."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.database import enter_rls_bypass, pin_rls_tenant_context, reset_current_tenant, set_current_tenant
from tests.integration.test_artifact_publication_transactions import (
    knowledge_reports as knowledge_reports,
    publish_report,
    stage_report,
)
from tests.services.test_session_tool_artifact_settlement import _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_permission_worker_publication_keeps_source_reads_in_tenant_rls(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    from app.config import get_settings
    from app.models.chat_artifact import ChatArtifact
    from app.models.organization_identity import ContentInputReceipt
    from app.services import content_input_receipts, session_approval
    from app.services.runtime_task_worker import expire_session_permission_requests_once

    tenant, user, agent, session = await _seed(owner_sessionmaker)
    other_tenant, other_user, other_agent, other_session = await _seed(owner_sessionmaker)
    root = tmp_path / str(agent)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    paths = ["workspace/foreign-source.md", "workspace/own-report.md"]
    async with owner_sessionmaker() as db:
        foreign = await stage_report(
            db,
            tenant=other_tenant,
            agent=other_agent,
            user=other_user,
            session=other_session,
            root=tmp_path / str(other_agent),
            path="workspace/private.md",
        )
        dependency = {
            "source_kind": "workspace_artifact",
            "source_id": str(foreign.id),
            "source_version": foreign.content_sha256,
        }
        for path in paths:
            await stage_report(
                db,
                tenant=tenant,
                agent=agent,
                user=user,
                session=session,
                root=root,
                path=path,
                dependencies=[dependency] if path == paths[0] else [],
            )
        await db.commit()
    n = SimpleNamespace(tenant=tenant, agent=agent, users=[user], sessions=[session], paths=paths, root=root)
    original = content_input_receipts.validate_published_workspace_receipt
    observations = []
    source_pids = []
    worker_scopes = []
    worker_pids = []
    result = []

    async def observe_source_scope(db, *args, **kwargs):
        scope = await db.scalar(text("SELECT current_setting('app.current_tenant_id')"))
        source_pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        foreign_visible = await db.get(ContentInputReceipt, foreign.id) is not None
        role = (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
        observations.append({"scope": scope, "foreign_visible": foreign_visible, "privileged_role": any(role)})
        return await original(db, *args, **kwargs)

    async def publish_from_permission_recovery(db):
        # Use the worker's actual session boundary; its pre-effect approval
        # handoff normally skips complete_tool_invocation's publication branch.
        worker_scopes.append(await db.scalar(text("SELECT current_setting('app.current_tenant_id')")))
        worker_pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        parts, errors = await publish_report(db, n, 0, paths=paths)
        result.append((parts, errors))
        await db.commit()
        return 0

    monkeypatch.setattr(content_input_receipts, "validate_published_workspace_receipt", observe_source_scope)
    monkeypatch.setattr(session_approval, "recover_enterprise_permission_waits", publish_from_permission_recovery)
    token = set_current_tenant(str(tenant))
    try:
        assert (
            await expire_session_permission_requests_once(
                worker_id="artifact-source-rls", session_factory=app_user_sessionmaker
            )
            == 0
        )
    finally:
        reset_current_tenant(token)
    (tmp_path / "source-rls.json").write_text(
        json.dumps(
            {
                "worker_scopes": worker_scopes,
                "worker_pids": worker_pids,
                "source_pids": source_pids,
                "sources": observations,
                "results": result,
            },
            default=str,
        )
    )
    assert worker_scopes == ["BYPASS"]
    assert observations and all(
        row == {"scope": str(tenant), "foreign_visible": False, "privileged_role": False} for row in observations
    ), observations
    parts, errors = result[0]
    assert [part["path"] for part in parts] == [paths[1]]
    assert [(error["path"], error["status"]) for error in errors] == [(paths[0], "denied")]
    async with owner_sessionmaker() as db:
        assert list(await db.scalars(select(ChatArtifact.path).where(ChatArtifact.agent_id == agent))) == [paths[1]]


async def test_bypass_knowledge_publication_does_not_block_on_its_anchor(
    knowledge_reports, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    from app.models.chat_artifact import ChatArtifact
    from app.services import content_input_receipts

    n = knowledge_reports
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    original = content_input_receipts.validate_published_workspace_receipt
    activities = []
    outer_pid = None

    async def observe_knowledge_source(db, *args, **kwargs):
        value = await original(db, *args, **kwargs)
        assert await db.scalar(text("SELECT current_setting('app.current_tenant_id')")) == str(n.tenant)
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT pid,state,wait_event_type,pg_blocking_pids(pid) AS blockers FROM pg_stat_activity "
                        "WHERE pid IN (:outer_pid,pg_backend_pid()) AND xact_start IS NOT NULL"
                    ),
                    {"outer_pid": outer_pid},
                )
            )
            .mappings()
            .all()
        )
        activities.extend(dict(row) for row in rows)
        assert all(not row["blockers"] for row in rows)
        return value

    monkeypatch.setattr(content_input_receipts, "validate_published_workspace_receipt", observe_knowledge_source)
    started = time.monotonic()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        async with enter_rls_bypass(db, reason="artifact source isolation regression"):
            outer_pid = await db.scalar(text("SELECT pg_backend_pid()"))
            parts, errors = await asyncio.wait_for(publish_report(db, n, 0), timeout=5)
            assert await db.scalar(text("SELECT current_setting('app.current_tenant_id')")) == "BYPASS"
            assert [part["path"] for part in parts] == [n.paths[0]] and not errors
            await db.commit()
    async with owner_sessionmaker() as db:
        assert await db.scalar(select(ChatArtifact.path).where(ChatArtifact.agent_id == n.agent)) == n.paths[0]
    elapsed = time.monotonic() - started
    (tmp_path / "bypass-knowledge.json").write_text(
        json.dumps({"publication_seconds": elapsed, "paths": [n.paths[0]], "activities": activities})
    )
    assert elapsed < 5
