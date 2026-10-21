"""Company retention controls consume real work and file ownership state."""

import os
import asyncio
import hashlib
import uuid

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_retention_keeps_active_and_unknown_work_but_releases_terminal_orphans(delivered, app_user_sessionmaker):  # noqa: F811
    from app.models.runtime_task import RuntimeTask
    from app.services.chat_artifact_delivery import CHAT_ARTIFACT_SNAPSHOT_DIR
    from app.services.heartbeat import _run_chat_artifact_snapshot_retention

    d = delivered
    root = d.root / CHAT_ARTIFACT_SNAPSHOT_DIR / str(d.session)
    active = root / str(d.run) / "unpublished.txt"
    terminal = root / str(uuid.uuid4()) / "orphan.txt"
    for path in (active, terminal):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PRESERVE_ACTIVE_WORK")
        os.utime(path, (0, 0))
    for status in ("running", "needs_reconciliation", "completed"):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            task = await db.get(RuntimeTask, d.run)
            task.status = status
            await db.commit()
            await pin_rls_tenant_context(db, d.tenant)
            result = await _run_chat_artifact_snapshot_retention(d.agent, db=db)
            await db.commit()
        assert not terminal.exists()
        if status != "completed":
            assert active.is_file(), "A nonterminal work snapshot must remain available even before artifact publication"
            assert result["kept_active_count"] == 1
        else:
            assert not active.exists()
        assert (d.root / d.first.snapshot_json["snapshot_storage_path"]).is_file()


async def test_company_admin_policy_is_scoped_versioned_and_consumed_by_maintenance(
    delivered, owner_sessionmaker, app_user_sessionmaker, monkeypatch):  # noqa: F811
    from app.api import enterprise
    from app.config import get_settings
    from app.core.security import create_access_token, decode_access_token
    from app.database import get_db
    from app.models.security_audit import SecurityAuditEvent
    from app.models.tenant_setting import TenantSetting
    from app.models.user import User
    from app.services.chat_artifact_delivery import CHAT_ARTIFACT_SNAPSHOT_DIR
    from app.services.heartbeat import _run_chat_artifact_snapshot_retention
    from tests.services.test_session_input_control_v2 import _seed_session

    d = delivered
    foreign, foreign_user, *_ = await _seed_session(owner_sessionmaker, active_run=True)
    async with owner_sessionmaker() as db:
        (await db.get(User, d.user.id)).role = "org_admin"
        (await db.get(User, foreign_user)).role = "org_admin"
        await db.commit()

    async def scoped_db(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, uuid.UUID(decode_access_token(token)["tid"]))
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    app = FastAPI()
    app.include_router(enterprise.router)
    app.dependency_overrides[get_db] = scoped_db
    headers = {"Authorization": "Bearer " + create_access_token(str(d.user.id), "org_admin", str(d.tenant))}
    foreign_headers = {"Authorization": "Bearer " + create_access_token(str(foreign_user), "org_admin", str(foreign))}
    url = f"/enterprise/storage-retention?tenant_id={d.tenant}"
    orphan = d.root / CHAT_ARTIFACT_SNAPSHOT_DIR / "expired/orphan.txt"
    orphan.parent.mkdir()
    orphan.write_bytes(b"DISPOSABLE")
    os.utime(orphan, (0, 0))
    preview = d.root / ".office_meta/artifacts" / str(uuid.uuid4()) / "preview.html"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"DISPOSABLE_PREVIEW")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://retention.test") as client:
        default = await client.get(url, headers=headers)
        assert default.status_code == 200, default.text
        assert default.json()["policy"] == {"enabled": True, "retention_days": 30.0, "version": 0, "source": "installation_default"}
        assert default.json()["capacity"] == {"status": "operator_only", "scope": "shared_agent_volume"}
        assert (await client.get(url, headers=foreign_headers)).status_code == 403
        body = {"enabled": False, "retention_days": 0.25, "expected_version": 0}
        # Two initial requests race against an absent row; exactly one can advance it.
        responses = await asyncio.gather(*(client.put(url, headers=headers, json=body) for _ in range(2)))
        assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
        readback = (await client.get(url, headers=headers)).json()
        assert readback["policy"] == {"enabled": False, "retention_days": 0.25, "version": 1, "source": "company_admin"}
        assert orphan.exists(), "Saving must not start an immediate purge"
        assert (await client.put(url, headers=foreign_headers, json={**body, "expected_version": 1})).status_code == 403
        for invalid in ({**body, "enabled": "false"}, {**body, "retention_days": -1},
            {**body, "retention_days": True}, {**body, "expected_version": True}):
            assert (await client.put(url, headers=headers, json=invalid)).status_code == 422
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            assert (await _run_chat_artifact_snapshot_retention(d.agent, db=db))["skipped"] == "company_retention_paused"
            await db.commit()
        assert orphan.is_file() and preview.is_file()
        resume = await client.put(url, headers=headers, json={"enabled": True, "retention_days": 0, "expected_version": 1})
        assert resume.status_code == 200 and resume.json()["policy"]["version"] == 2, resume.text
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            assert (await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one() == (False, False)
            result = await _run_chat_artifact_snapshot_retention(d.agent, db=db)
            assert result["policy"]["version"] == 2 and result["removed_count"] == 1
            events = (await db.scalars(select(SecurityAuditEvent).where(SecurityAuditEvent.event_type == "storage.retention_policy_updated"))).all()
            assert len(events) == 2 and all(event.actor_id == d.user.id for event in events)
            assert await db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == foreign, TenantSetting.key == "storage_retention")) is None
            await db.commit()
        assert not orphan.exists() and not preview.exists()
        assert (d.root / d.first.snapshot_json["snapshot_storage_path"]).is_file()
        async with owner_sessionmaker() as db:
            (await db.get(User, d.user.id)).role = "member"
            await db.commit()
        # The live role wins over the stale administrator claim in this token.
        assert (await client.put(url, headers=headers, json={**body, "expected_version": 2})).status_code == 403
        async with owner_sessionmaker() as db:
            (await db.get(User, d.user.id)).role = "platform_admin"
            await db.commit()
        operator = (await client.get(url, headers=headers)).json()
        assert operator["capacity"]["status"] == "available"
        assert operator["capacity"]["total_bytes"] >= operator["capacity"]["used_bytes"] > 0
        monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(d.root / "missing-volume"))
        unavailable = (await client.get(url, headers=headers)).json()
        assert unavailable["capacity"]["status"] == "unavailable"
        assert "free_bytes" not in unavailable["capacity"]


async def test_retention_waits_for_real_publication_commit(delivered, app_user_sessionmaker):  # noqa: F811
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message
    from app.services.content_input_receipts import latest_workspace_receipt, record_workspace_receipt
    from app.services.heartbeat import _run_chat_artifact_snapshot_retention

    d = delivered
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        (await db.get(RuntimeTask, d.run)).status = "completed"
        await db.commit()
    d.target.write_bytes(b"BBBB")
    async with app_user_sessionmaker() as publisher:
        await pin_rls_tenant_context(publisher, d.tenant)
        record_workspace_receipt(publisher, d.tenant, d.agent, "workspace/report.txt", hashlib.sha256(b"BBBB").hexdigest(),
            await latest_workspace_receipt(publisher, d.tenant, d.agent, "workspace/report.txt"))
        message = ChatMessage(id=uuid.uuid4(), tenant_id=d.tenant, agent_id=d.agent, user_id=d.user.id,
            role="assistant", content="Delivered", conversation_id=str(d.session))
        publisher.add(message)
        await publisher.flush()
        parts = await create_chat_artifacts_for_message(db=publisher, tenant_id=d.tenant, agent_id=d.agent,
            session_id=d.session, runtime_task_id=d.run, message_id=message.id,
            paths=["workspace/report.txt"], workspace_root=d.root)
        assert len(parts) == 1
        from app.models.chat_artifact import ChatArtifact
        artifact = await publisher.get(ChatArtifact, uuid.UUID(parts[0]["artifact_id"]))
        captured = d.root / artifact.snapshot_json["snapshot_storage_path"]
        os.utime(captured, (0, 0))
        ready = asyncio.Future()

        async def collect():
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, d.tenant)
                ready.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
                result = await _run_chat_artifact_snapshot_retention(d.agent, db=db)
                await db.commit()
                return result

        collector = asyncio.create_task(collect())
        try:
            pid = await asyncio.wait_for(ready, 5)
            async with app_user_sessionmaker() as observer:
                async with asyncio.timeout(5):
                    while not await observer.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}):
                        assert not collector.done()
                        await asyncio.sleep(0.02)
            assert captured.read_bytes() == b"BBBB"
            await publisher.commit()
            result = await asyncio.wait_for(collector, 5)
            assert result["kept_referenced_count"] == 2
            assert captured.read_bytes() == b"BBBB"
            download = await d.client.get(f"/agents/{d.agent}/files/artifacts/{artifact.id}/download")
            assert download.status_code == 200 and download.content == b"BBBB"
        finally:
            await publisher.rollback()
            if not collector.done():
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)


async def test_invalid_saved_policy_retains_files_for_repair(delivered, app_user_sessionmaker):  # noqa: F811
    from app.models.tenant_setting import TenantSetting
    from app.services.chat_artifact_delivery import CHAT_ARTIFACT_SNAPSHOT_DIR
    from app.services.heartbeat import _run_chat_artifact_snapshot_retention

    d = delivered
    orphan = d.root / CHAT_ARTIFACT_SNAPSHOT_DIR / "expired/orphan.txt"
    orphan.parent.mkdir()
    orphan.write_bytes(b"REPAIR_FIRST")
    os.utime(orphan, (0, 0))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        setting = TenantSetting(tenant_id=d.tenant, key="storage_retention", value={"enabled": True})
        db.add(setting)
        await db.commit()
        for malformed in ({"enabled": True}, None, [], {"version": 1, "enabled": True, "retention_days": "0"}):
            await pin_rls_tenant_context(db, d.tenant)
            setting.value = malformed
            await db.commit()
            await pin_rls_tenant_context(db, d.tenant)
            result = await _run_chat_artifact_snapshot_retention(d.agent, db=db)
            assert result.get("skipped") == "storage_retention_policy_unavailable" and result["retryable"]
            await db.commit()
            assert orphan.read_bytes() == b"REPAIR_FIRST"
    assert orphan.read_bytes() == b"REPAIR_FIRST"


async def test_pending_and_unknown_approval_keeps_original_work_snapshots(delivered, app_user_sessionmaker):  # noqa: F811
    from app.models.audit import ApprovalRequest
    from app.models.runtime_task import RuntimeTask
    from app.services.chat_artifact_delivery import CHAT_ARTIFACT_SNAPSHOT_DIR
    from app.services.heartbeat import _run_chat_artifact_snapshot_retention

    d = delivered
    snapshot = d.root / CHAT_ARTIFACT_SNAPSHOT_DIR / d.session.hex / d.run.hex / "pending.txt"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"PENDING_EVIDENCE")
    os.utime(snapshot, (0, 0))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        (await db.get(RuntimeTask, d.run)).status = "completed"
        ticket = ApprovalRequest(tenant_id=d.tenant, agent_id=d.agent, action_type="test_retention",
            status="pending", execution_status="pending", execution_envelope={"runtime_task_id": str(d.run)})
        db.add(ticket)
        await db.commit()
        for status, execution in (("pending", "pending"), ("approved", "queued"), ("approved", "needs_reconciliation"),
            ("rejected", "rejected")):
            await pin_rls_tenant_context(db, d.tenant)
            ticket.status, ticket.execution_status = status, execution
            await db.commit()
            await pin_rls_tenant_context(db, d.tenant)
            result = await _run_chat_artifact_snapshot_retention(d.agent, db=db)
            await db.commit()
            assert snapshot.exists() is (status != "rejected")
            assert result["kept_active_count"] == int(status != "rejected")
        assert (d.root / d.first.snapshot_json["snapshot_storage_path"]).is_file()


async def test_no_volume_api_never_reports_its_own_disk(monkeypatch):
    from app.main import app, settings

    monkeypatch.setattr(settings, "HIVE_PROCESS_ROLE", "api")

    def forbidden_capacity(*_args, **_kwargs):
        pytest.fail("The no-volume API must not probe a different filesystem")

    monkeypatch.setattr("app.services.storage_retention.shutil.disk_usage", forbidden_capacity)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api-role.test") as client:
        for path in ("/api/enterprise/storage-retention", "/api/v1/enterprise/storage-retention"):
            result = await client.get(path)
            assert result.status_code == 404 and result.json()["process_role"] == "api"
