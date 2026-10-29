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
from tests.integration.conftest import (
    app_user_engine as app_user_engine,
    migrated_pg_url as _migrated_pg_url,
    pg_container as _pg_container,
)  # noqa: F401
from tests.integration.test_m4w_original_reconciliation_postgres import (
    pending as pending,
    provider as provider,
    original_api as original_api,
    delivered as delivered,
    s3_server as s3_server,
    BODY,
)  # noqa: F401
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
    pending, owner_sessionmaker, app_user_sessionmaker, tmp_path_factory, monkeypatch, original_state
):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun

    d, client, blob_id, (selected, remote), *_ = pending
    from app.models.user import User
    from tests.integration.test_m4ab_channel_originals_postgres import save as save_channel, BODY as CHANNEL_BODY

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        channel_path = await save_channel(db, d, "feishu")
        await db.commit()
    (d.root / channel_path).write_bytes(b"EDITED_CHANNEL_WORK_COPY")
    bound_body = b"BOUND_WORK_ORIGINAL\x00BYTES"
    uploaded = await client.post(
        "/chat/upload",
        headers=pending[4],
        files={"file": ("bound.bin", bound_body)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"},
    )
    assert uploaded.status_code == 200, uploaded.text
    bound = uploaded.json()
    input_id = uuid.uuid4()
    async with app_user_sessionmaker() as db:
        from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

        await pin_rls_tenant_context(db, d.tenant)
        frame = await resolve_session_mutation_authority(
            db,
            user=await db.get(User, d.user.id),
            agent_id=d.agent,
            session_id=d.session,
            action="mutate_session_input",
        )
        await accept_human_input(
            db,
            authority=frame,
            intent={
                "kind": "queue_next_turn",
                "input_id": str(input_id),
                "idempotency_key": f"backup-original:{input_id}",
                "session_id": str(d.session),
                "content_parts": [
                    {
                        "type": "attachment_ref",
                        "attachment": {
                            "path": bound["workspace_path"],
                            "original_attachment": bound["original_attachment"],
                        },
                    }
                ],
            },
        )
        await db.commit()
    (d.root / bound["workspace_path"]).write_bytes(b"EDITED_BOUND_WORK_COPY")
    operation = str(uuid.uuid4())
    result = await client.post(
        f"/enterprise/storage-originals/{blob_id}/reconcile",
        json={
            "operation_id": operation,
            "expected_content_sha256": hashlib.sha256(BODY).hexdigest(),
            "action": "recover",
        },
    )
    assert result.status_code == 200 and result.json()["recovered"] is True, result.text
    if original_state == "quarantined":
        from tests.integration.test_m4y_original_quarantine_postgres import apply, configure

        await configure(client)
        quarantined = await apply(client, blob_id, "quarantine")
        assert quarantined.status_code == 200 and quarantined.json()["state"] == "quarantined", quarantined.text
    work = tmp_path_factory.mktemp("original-reconciliation-recovery")
    storage = work / "object-storage.json"
    storage.write_text(
        json.dumps(
            {
                "endpoint": remote.endpoint,
                "region": "us-east-1",
                "bucket": "private-originals",
                "access_key_id": remote.access,
                "secret_access_key": remote.secret,
                "session_token": remote.token,
            }
        )
    )
    storage.chmod(0o600)
    profile = installer.configure(
        work / "source-profile",
        public_origin="http://127.0.0.1:3012",
        port=3012,
        object_storage_profile=storage if selected == "s3" else None,
    )
    source = owner_sessionmaker.kw["bind"].url
    values = installer.read_profile(profile)
    values["HIVE_INSTALL_SCHEMA_PASSWORD"] = source.password
    profile.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()))
    binary = Path(os.environ.get("M1E_PSQL") or shutil.which("psql")).parent
    environment = {
        **os.environ,
        "PGHOST": source.host,
        "PGPORT": str(source.port),
        "PGDATABASE": source.database,
        "PGUSER": source.username,
        "PGPASSWORD": source.password,
    }

    def invoke(*arguments, database=None):
        process = subprocess.run(
            [sys.executable, "-B", "-m", "app.scripts.self_hosted_recovery", *map(str, arguments)],
            cwd=ROOT / "backend",
            env={**environment, **({"PGDATABASE": database} if database else {})},
            capture_output=True,
            text=True,
            timeout=120,
        )
        receipt = json.loads(process.stdout)
        assert process.returncode == 0, receipt
        return receipt

    backup = work / "backup"
    receipt = invoke(
        "backup", "--offline", "--profile", profile, "--agents", d.root.parent, "--output", backup, "--pg-bin", binary
    )
    assert receipt["status"] == "backed_up"
    manifest = json.loads((backup / "manifest.json").read_text())
    assert manifest["schema"] == (
        "hive.self_hosted_backup.v4" if original_state == "quarantined" else "hive.self_hosted_backup.v3"
    )
    assert len(manifest["remote_objects"]) == 3 * int(selected == "s3")
    if selected == "s3":
        from app.scripts import self_hosted_recovery as recovery

        expected = (
            "backup_quarantine_schema_required"
            if original_state == "quarantined"
            else "backup_generation_schema_required"
        )
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
    target_profile = installer.configure(
        work / "target-profile",
        public_origin="http://127.0.0.1:3014",
        port=3014,
        recovery_profile=backup / "installation.env",
    )
    target_database = "m1g_" + uuid.uuid4().hex
    created = subprocess.run(
        [str(binary / "createdb"), "--no-password", target_database], env=environment, capture_output=True, timeout=20
    )
    assert created.returncode == 0
    target_agents = work / "restored-agents"
    target_agents.mkdir()
    async with owner_sessionmaker() as db:
        await db.execute(text("CREATE ROLE app_rls NOSUPERUSER NOBYPASSRLS"))
        await db.commit()
    restored = invoke(
        "restore",
        "--offline",
        "--profile",
        target_profile,
        "--backup",
        backup,
        "--manifest-sha256",
        receipt["manifest_sha256"],
        "--agents",
        target_agents,
        "--pg-bin",
        binary,
        "--uid",
        os.getuid(),
        "--gid",
        os.getgid(),
        database=target_database,
    )
    assert restored["status"] == "restored"
    runtime_url = source.set(
        database=target_database, username="app_rls", password=values["HIVE_INSTALL_RUNTIME_PASSWORD"]
    )
    # Restore explicitly hands off to canonical preparation; it does not install
    # or rotate runtime-role credentials as a side effect of importing data.
    prepared = subprocess.run(
        [sys.executable, "-B", "-m", "app.scripts.prepare_self_hosted"],
        cwd=ROOT / "backend",
        env={
            **environment,
            "DATABASE_URL": runtime_url.render_as_string(hide_password=False),
            "SCHEMA_DATABASE_URL": source.set(database=target_database).render_as_string(hide_password=False),
            "AGENT_DATA_DIR": str(target_agents),
            "RLS_APP_PASSWORD": values["HIVE_INSTALL_RUNTIME_PASSWORD"],
            "SECRET_KEY": values["HIVE_INSTALL_SECRET_KEY"],
            "JWT_SECRET_KEY": values["HIVE_INSTALL_JWT_SECRET_KEY"],
            "SECRETS_MASTER_KEY": values["HIVE_INSTALL_SECRETS_MASTER_KEY"],
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert prepared.returncode == 0 and json.loads(prepared.stdout.splitlines()[-1])["status"] == "prepared", (
        prepared.stdout
    )
    engine = create_async_engine(runtime_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            await pin_rls_tenant_context(db, d.tenant)
            assert tuple(
                (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))).one()
            ) == (False, False)
            row = await db.get(StorageBlob, blob_id)
            assert row.state == original_state and row.verified_at is not None and row.provider == "filesystem"
            assert row.content_sha256 == hashlib.sha256(BODY).hexdigest()
            assert (target_agents / ".storage_objects" / row.bucket / row.object_key).read_bytes() == BODY
            assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all() == []
            ledger = await db.scalar(
                select(StorageGCRun).where(
                    StorageGCRun.tenant_id == d.tenant,
                    StorageGCRun.receipt_json["request"]["operation_id"].astext == operation,
                )
            )
            assert ledger.receipt_json["result"] == result.json()
            assert ledger.receipt_json["evidence"]["observed"] == {
                "available": True,
                "sha256": hashlib.sha256(BODY).hexdigest(),
                "size_bytes": len(BODY),
            }
            from app.config import get_settings
            from app.services.channel_originals import find_channel_original
            from app.services.upload_originals import capture_original_download

            monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(target_agents))
            reader = await db.get(User, d.user.id)
            channel_ref = await find_channel_original(db, user=reader, agent_id=d.agent, path=channel_path)
            lease, exact, filename = await capture_original_download(db, reader, uuid.UUID(channel_ref["id"]))
            try:
                assert exact.read_bytes() == CHANNEL_BODY and filename == "source.bin"
                assert (target_agents / str(d.agent) / channel_path).read_bytes() == b"EDITED_CHANNEL_WORK_COPY"
            finally:
                lease.cleanup()
            from app.models.session_v2 import SessionTurnInput
            from app.services.original_input_refs import release_unused_original_draft

            saved_input = await db.get(SessionTurnInput, input_id)
            assert (
                saved_input.content_parts_json[0]["attachment"]["original_attachment"] == bound["original_attachment"]
            )
            pins = (
                await db.scalars(
                    select(StorageBlobRef).where(
                        StorageBlobRef.owner_type == "session_input",
                        StorageBlobRef.metadata_json["input_id"].astext == str(input_id),
                    )
                )
            ).all()
            assert len(pins) == 1 and pins[0].metadata_json["source_ref_id"] == bound["original_attachment"]["id"]
            ref_id = uuid.UUID(bound["original_attachment"]["id"])
            retained = await release_unused_original_draft(
                db, user=reader, ref_id=ref_id, digest=hashlib.sha256(bound_body).hexdigest()
            )
            assert retained["status"] == "retained" and retained["reason"] == "work_reference"
            lease, exact, filename = await capture_original_download(db, reader, ref_id)
            try:
                assert exact.read_bytes() == bound_body and filename == "bound.bin"
                assert (
                    target_agents / str(d.agent) / bound["workspace_path"]
                ).read_bytes() == b"EDITED_BOUND_WORK_COPY"
            finally:
                lease.cleanup()
            if original_state == "quarantined":
                from app.config import get_settings
                from app.services.original_retention import (
                    OriginalRetentionRequest,
                    apply_original_retention,
                    read_original_retention,
                )

                assert row.delete_after.isoformat() == quarantined.json()["delete_after"]
                assert (await read_original_retention(db, d.tenant))["version"] == 1
                monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(target_agents))
                restored_original = await apply_original_retention(
                    db,
                    tenant_id=d.tenant,
                    actor_id=uuid.UUID(ledger.receipt_json["request"]["actor_id"]),
                    blob_id=blob_id,
                    request=OriginalRetentionRequest(
                        operation_id=uuid.uuid4(),
                        expected_policy_version=1,
                        expected_content_sha256=hashlib.sha256(BODY).hexdigest(),
                        action="restore",
                    ),
                )
                assert restored_original["state"] == "available" and restored_original["observation"] == "present"
                await db.commit()
    finally:
        await engine.dispose()
