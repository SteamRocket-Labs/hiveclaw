"""Reconciled originals and their authority-free receipts survive offline recovery."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import pin_rls_tenant_context
from tests.integration.conftest import app_user_engine, migrated_pg_url as _migrated_pg_url, pg_container as _pg_container  # noqa: F401
from tests.integration.test_m4w_original_reconciliation_postgres import pending, provider, original_api, delivered, s3_server, BODY  # noqa: F401
from tests.scripts.test_self_hosted_installation import installer

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def pg_container():
    yield from _pg_container.__wrapped__()


@pytest.fixture
def migrated_pg_url(pg_container):
    return _migrated_pg_url.__wrapped__(pg_container)


@pytest.mark.parametrize("original_state", ["available", "quarantined"])
async def test_offline_recovery_preserves_reconciled_unreferenced_original_and_receipt(
    pending, owner_sessionmaker, tmp_path_factory, monkeypatch, original_state):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun

    d, client, blob_id, (selected, remote), *_ = pending
    operation = str(uuid.uuid4())
    result = await client.post(f"/enterprise/storage-originals/{blob_id}/reconcile", json={
        "operation_id": operation, "expected_content_sha256": hashlib.sha256(BODY).hexdigest(), "action": "recover"})
    assert result.status_code == 200 and result.json()["recovered"] is True, result.text
    if original_state == "quarantined":
        from tests.integration.test_m4y_original_quarantine_postgres import apply, configure

        await configure(client)
        quarantined = await apply(client, blob_id, "quarantine")
        assert quarantined.status_code == 200 and quarantined.json()["state"] == "quarantined", quarantined.text
    work = tmp_path_factory.mktemp("original-reconciliation-recovery")
    storage = work / "object-storage.json"
    storage.write_text(json.dumps({"endpoint": remote.endpoint, "region": "us-east-1", "bucket": "private-originals",
        "access_key_id": remote.access, "secret_access_key": remote.secret, "session_token": remote.token}))
    storage.chmod(0o600)
    profile = installer.configure(work / "source-profile", public_origin="http://127.0.0.1:3012", port=3012,
        object_storage_profile=storage if selected == "s3" else None)
    source = owner_sessionmaker.kw["bind"].url
    values = installer.read_profile(profile)
    values["HIVE_INSTALL_SCHEMA_PASSWORD"] = source.password
    profile.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    binary = Path(os.environ.get("M1E_PSQL") or shutil.which("psql")).parent
    environment = {**os.environ, "PGHOST": source.host, "PGPORT": str(source.port), "PGDATABASE": source.database,
        "PGUSER": source.username, "PGPASSWORD": source.password}

    def invoke(*arguments, database=None):
        process = subprocess.run([sys.executable, "-B", "-m", "app.scripts.self_hosted_recovery", *map(str, arguments)],
            cwd=ROOT / "backend", env={**environment, **({"PGDATABASE": database} if database else {})},
            capture_output=True, text=True, timeout=120)
        receipt = json.loads(process.stdout)
        assert process.returncode == 0, receipt
        return receipt

    backup = work / "backup"
    receipt = invoke("backup", "--offline", "--profile", profile, "--agents", d.root.parent, "--output", backup, "--pg-bin", binary)
    assert receipt["status"] == "backed_up"
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["schema"] == ("hive.self_hosted_backup.v4" if original_state == "quarantined" else "hive.self_hosted_backup.v3")
    assert len(manifest["remote_objects"]) == int(selected == "s3")
    if selected == "s3":
        from app.scripts import self_hosted_recovery as recovery

        expected = "backup_quarantine_schema_required" if original_state == "quarantined" else "backup_generation_schema_required"
        with pytest.raises(recovery.RecoveryError, match=expected):
            recovery.verify_contents(backup, {**manifest, "schema": recovery.REMOTE_SCHEMA})
        if original_state == "quarantined":
            with pytest.raises(recovery.RecoveryError, match="backup_quarantine_schema_required"):
                recovery.verify_contents(backup, {**manifest, "schema": recovery.GENERATION_SCHEMA})
    if original_state == "quarantined":
        from datetime import datetime, timedelta
        from app.services import original_sweep
        from tests.integration.test_m4z_original_sweep_postgres import sweep

        class AfterGrace(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)

        with monkeypatch.context() as clock:
            clock.setattr(original_sweep, "datetime", AfterGrace)
            removed = await sweep(client, blob_id, quarantined.json())
        assert removed.status_code == 200 and removed.json()["state"] == "deleted", removed.text
        # The exact portable copy must recover the original even after the
        # source object is retired; sweep never follows backup references.
    remote.stop()
    target_profile = installer.configure(work / "target-profile", public_origin="http://127.0.0.1:3014", port=3014,
        recovery_profile=backup / "installation.env")
    target_database = "m1g_" + uuid.uuid4().hex
    created = subprocess.run([str(binary / "createdb"), "--no-password", target_database], env=environment,
        capture_output=True, timeout=20)
    assert created.returncode == 0
    target_agents = work / "restored-agents"
    target_agents.mkdir()
    restored = invoke("restore", "--offline", "--profile", target_profile, "--backup", backup,
        "--manifest-sha256", receipt["manifest_sha256"], "--agents", target_agents, "--pg-bin", binary,
        "--uid", os.getuid(), "--gid", os.getgid(), database=target_database)
    assert restored["status"] == "restored"
    runtime_url = source.set(database=target_database, username="app_rls", password=values["HIVE_INSTALL_RUNTIME_PASSWORD"])
    # Restore explicitly hands off to canonical preparation; it does not install
    # or rotate runtime-role credentials as a side effect of importing data.
    prepared = subprocess.run([sys.executable, "-B", "-m", "app.scripts.prepare_self_hosted"], cwd=ROOT / "backend",
        env={**environment, "DATABASE_URL": runtime_url.render_as_string(hide_password=False),
            "SCHEMA_DATABASE_URL": source.set(database=target_database).render_as_string(hide_password=False),
            "AGENT_DATA_DIR": str(target_agents), "RLS_APP_PASSWORD": values["HIVE_INSTALL_RUNTIME_PASSWORD"],
            "SECRET_KEY": values["HIVE_INSTALL_SECRET_KEY"], "JWT_SECRET_KEY": values["HIVE_INSTALL_JWT_SECRET_KEY"],
            "SECRETS_MASTER_KEY": values["HIVE_INSTALL_SECRETS_MASTER_KEY"]}, capture_output=True, text=True, timeout=180)
    assert prepared.returncode == 0 and json.loads(prepared.stdout.splitlines()[-1])["status"] == "prepared", prepared.stdout
    engine = create_async_engine(runtime_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            await pin_rls_tenant_context(db, d.tenant)
            assert tuple((await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()) == (False, False)
            row = await db.get(StorageBlob, blob_id)
            assert row.state == original_state and row.verified_at is not None and row.provider == "filesystem"
            assert row.content_sha256 == hashlib.sha256(BODY).hexdigest()
            assert (target_agents / ".storage_objects" / row.bucket / row.object_key).read_bytes() == BODY
            assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all() == []
            ledger = await db.scalar(select(StorageGCRun).where(StorageGCRun.tenant_id == d.tenant,
                StorageGCRun.receipt_json["request"]["operation_id"].astext == operation))
            assert ledger.receipt_json["result"] == result.json()
            assert ledger.receipt_json["evidence"]["observed"] == {
                "available": True, "sha256": hashlib.sha256(BODY).hexdigest(), "size_bytes": len(BODY)}
            if original_state == "quarantined":
                from app.config import get_settings
                from app.services.original_retention import OriginalRetentionRequest, apply_original_retention, read_original_retention

                assert row.delete_after.isoformat() == quarantined.json()["delete_after"]
                assert (await read_original_retention(db, d.tenant))["version"] == 1
                monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(target_agents))
                restored_original = await apply_original_retention(db, tenant_id=d.tenant,
                    actor_id=uuid.UUID(ledger.receipt_json["request"]["actor_id"]), blob_id=blob_id,
                    request=OriginalRetentionRequest(operation_id=uuid.uuid4(), expected_policy_version=1,
                        expected_content_sha256=hashlib.sha256(BODY).hexdigest(), action="restore"))
                assert restored_original["state"] == "available" and restored_original["observation"] == "present"
                await db.commit()
    finally:
        await engine.dispose()
