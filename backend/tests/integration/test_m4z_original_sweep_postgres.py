"""Physical original effects retain durable authority, recovery and generations."""

import hashlib
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4w_original_reconciliation_postgres import (
    pending as pending,
    provider as provider,
    original_api as original_api,
    delivered as delivered,
    s3_server as s3_server,
    BODY,
)  # noqa: F401
from tests.integration.test_m4y_original_quarantine_postgres import apply, configure, recover

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def prepare(client, blob_id):
    await recover(client, blob_id)
    await configure(client, grace_days=0)
    response = await apply(client, blob_id, "quarantine")
    assert response.status_code == 200 and response.json()["sweep_eligible"] is True, response.text
    return response.json()


async def sweep(client, blob_id, snapshot, *, action="sweep", headers=None, **changes):
    return await client.post(
        f"/enterprise/storage-originals/{blob_id}/sweep",
        headers=headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "expected_content_sha256": hashlib.sha256(BODY).hexdigest(),
            "expected_policy_version": snapshot["policy_version"],
            "expected_manifest_sha256": snapshot.get("manifest_sha256", "0" * 64),
            "action": action,
            **changes,
        },
    )


async def test_sweep_commits_exact_intent_before_effect_and_never_replays_deletion(
    pending, app_user_sessionmaker, monkeypatch
):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageGCRun
    from app.services.blob_store import FilesystemBlobStore
    from app.services.s3_blob_store import S3BlobStore

    d, client, blob_id, (selected, remote), member, foreign = pending
    snapshot = await prepare(client, blob_id)
    operation = str(uuid.uuid4())
    before = len(remote.requests)
    assert (await sweep(client, blob_id, snapshot, headers=member)).status_code == 403
    assert (await sweep(client, blob_id, snapshot, headers=foreign)).status_code == 404
    assert len(remote.requests) == before
    cls = S3BlobStore if selected == "s3" else FilesystemBlobStore
    original = cls.delete
    observed = []

    async def inspect_durable_intent(store, location, **arguments):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            row = await db.get(StorageBlob, blob_id)
            ledger = await db.get(StorageGCRun, uuid.UUID(arguments["gc_receipt_id"]))
            assert row.state == "deleting" and ledger.status == "running"
            assert ledger.receipt_json["request"]["operation_id"] == operation
            assert ledger.receipt_json["evidence"]["object_key"] == row.object_key
            observed.append(True)
        return await original(store, location, **arguments)

    monkeypatch.setattr(cls, "delete", inspect_durable_intent)
    response = await sweep(client, blob_id, snapshot, operation_id=operation)
    assert response.status_code == 200 and response.json()["state"] == "deleted", response.text
    result = response.json()
    assert observed == [True] and result["outcome"] == "absent" and result["physical_bytes_reclaimed"] is None
    requests = len(remote.requests)
    repeated = await sweep(client, blob_id, snapshot, operation_id=operation)
    assert repeated.status_code == 200 and repeated.json() == {**result, "replayed": True}, repeated.text
    assert observed == [True] and len(remote.requests) == requests
    # A later identical upload owns a new object. Old operation replay stays
    # historical and cannot delete or grant access to the new payload.
    uploaded = await client.post(
        "/chat/upload",
        headers=member,
        files={"file": ("new.bin", BODY)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert (await sweep(client, blob_id, snapshot, operation_id=operation)).json()["replayed"] is True
    ref = uploaded.json()["original_attachment"]["id"]
    assert (await client.get(f"/chat/attachments/{ref}/download", headers=member)).content == BODY


async def test_lost_final_receipt_preserves_unknown_then_read_only_reconciliation(
    pending, app_user_sessionmaker, monkeypatch
):  # noqa: F811
    from app.models.storage_blob import StorageBlob
    from app.services.blob_store import BlobStoreError, FilesystemBlobStore
    from app.services.s3_blob_store import S3BlobStore

    d, client, blob_id, (selected, remote), *_ = pending
    snapshot = await prepare(client, blob_id)
    cls = S3BlobStore if selected == "s3" else FilesystemBlobStore
    original = cls.delete

    async def lose_final_ack(store, location, **arguments):
        await original(store, location, **arguments)
        raise BlobStoreError("Synthetic lost final receipt")

    with monkeypatch.context() as lost:
        lost.setattr(cls, "delete", lose_final_ack)
        response = await sweep(client, blob_id, snapshot)
    assert response.status_code == 200 and response.json()["state"] == "deleting", response.text
    assert response.json()["outcome"] == "unknown"
    assert (await apply(client, blob_id, "restore")).json()["hold_reason"] == "unresolved_state"
    inspected = await apply(client, blob_id, "inspect")
    before = len([row for row in remote.requests if row[0] == "DELETE"])
    resolved = await sweep(client, blob_id, inspected.json(), action="reconcile")
    assert resolved.status_code == 200 and resolved.json()["state"] == "deleted", resolved.text
    assert len([row for row in remote.requests if row[0] == "DELETE"]) == before
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        assert (await db.get(StorageBlob, blob_id)).deleted_at is not None


async def test_current_references_and_increased_grace_hold_physical_sweep(pending, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlobRef

    d, client, blob_id, (_, remote), *_ = pending
    snapshot = await prepare(client, blob_id)
    await configure(client, grace_days=1)
    assert (await sweep(client, blob_id, snapshot)).status_code == 409
    inspected = (await apply(client, blob_id, "inspect", version=2)).json()
    assert inspected["sweep_eligible"] is False and inspected["delete_after"] > snapshot["delete_after"]
    held = await sweep(client, blob_id, inspected)
    assert held.json()["hold_reason"] == "grace_period", held.text
    await configure(client, grace_days=0)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        db.add(
            StorageBlobRef(
                tenant_id=d.tenant,
                blob_id=blob_id,
                owner_type="unknown_work",
                owner_id=str(uuid.uuid4()),
                purpose="retained_evidence",
                legal_hold=True,
                metadata_json={},
            )
        )
        await db.commit()
    inspected = (await apply(client, blob_id, "inspect", version=3)).json()
    held = await sweep(client, blob_id, inspected)
    assert held.json()["hold_reason"] == "referenced" and not remote.deletes, held.text


async def test_unknown_present_effect_requires_reconciliation_and_explicit_predecessor_retry(pending, monkeypatch):  # noqa: F811
    from app.services.blob_store import BlobDeleteReceipt, FilesystemBlobStore

    d, client, blob_id, (selected, remote), member, *_ = pending
    snapshot = await prepare(client, blob_id)
    operation = str(uuid.uuid4())
    with monkeypatch.context() as failure:
        if selected == "s3":
            remote.mode = "delete_unknown"
        else:

            async def unknown(*args, **kwargs):
                return BlobDeleteReceipt("unknown", True)

            failure.setattr(FilesystemBlobStore, "delete", unknown)
        first = await sweep(client, blob_id, snapshot, operation_id=operation)
    assert first.json()["state"] == "deleting" and first.json()["outcome"] == "unknown", first.text
    remote.mode = None
    uploaded = await client.post(
        "/chat/upload",
        headers=member,
        files={"file": ("new-generation.bin", BODY)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"},
    )
    assert uploaded.status_code == 200, uploaded.text
    before = len(remote.deletes)
    assert (await sweep(client, blob_id, snapshot, operation_id=operation)).json()["replayed"] is True
    assert len(remote.deletes) == before
    inspected = (await apply(client, blob_id, "inspect")).json()
    refused = await sweep(client, blob_id, inspected, retry_operation_id=operation)
    assert refused.json()["hold_reason"] == "explicit_inspected_retry_required", refused.text
    reconciled = await sweep(client, blob_id, inspected, action="reconcile")
    assert reconciled.json()["outcome"] == "present" and len(remote.deletes) == before, reconciled.text
    # A known pre-effect rejection of a retry cannot revive an old generation
    # whose previous DELETE may still complete late.
    with monkeypatch.context() as failure:
        if selected == "s3":
            remote.mode = "missing_etag"
        else:

            async def unavailable(*args, **kwargs):
                return BlobDeleteReceipt("unavailable")

            failure.setattr(FilesystemBlobStore, "delete", unavailable)
        rejected = await sweep(client, blob_id, inspected, retry_operation_id=operation)
    assert rejected.json()["state"] == "deleting" and rejected.json()["outcome"] == "unavailable", rejected.text
    remote.mode = None
    assert (await apply(client, blob_id, "restore")).json()["hold_reason"] == "unresolved_state"
    assert (await sweep(client, blob_id, inspected, action="reconcile")).json()["outcome"] == "present"
    retry = await sweep(client, blob_id, inspected, retry_operation_id=rejected.json()["operation_id"])
    assert retry.json()["state"] == "deleted", retry.text
    assert len(remote.deletes) == before + int(selected == "s3")
    ref = uploaded.json()["original_attachment"]["id"]
    downloaded = await client.get(f"/chat/attachments/{ref}/download", headers=member)
    assert downloaded.status_code == 200 and downloaded.content == BODY, downloaded.text


async def test_real_process_exit_after_delete_preserves_intent_and_blocks_incomplete_backup(
    pending, app_user_sessionmaker, owner_sessionmaker, tmp_path_factory
):  # noqa: F811
    from app.config import get_settings
    from app.models.storage_blob import StorageBlob, StorageGCRun
    from tests.scripts.test_self_hosted_installation import installer

    d, client, blob_id, (selected, remote), *_ = pending
    snapshot = await prepare(client, blob_id)
    operation = str(uuid.uuid4())
    work = tmp_path_factory.mktemp("native-original-sweep-crash")
    module = work / "crash_sweep.py"
    module.write_text(
        "import os\nfrom fastapi import FastAPI\nfrom app.api.enterprise import router\n"
        "from app.core.tenant_middleware import TenantMiddleware\n"
        "from app.services.blob_store import FilesystemBlobStore\nfrom app.services.s3_blob_store import S3BlobStore\n"
        "cls=S3BlobStore if os.environ['BLOB_STORAGE_PROVIDER']=='s3' else FilesystemBlobStore\n"
        "original=cls.delete\nasync def crash(*args,**kwargs):\n"
        "    result=await original(*args,**kwargs)\n    assert result.outcome=='absent'\n    os._exit(73)\n"
        "cls.delete=crash\napp=FastAPI()\napp.add_middleware(TenantMiddleware)\napp.include_router(router)\n"
    )
    settings = get_settings()
    values = {}
    for key in (
        "SECRET_KEY",
        "JWT_SECRET_KEY",
        "BLOB_STORAGE_PROVIDER",
        "BLOB_S3_ENDPOINT",
        "BLOB_S3_REGION",
        "BLOB_S3_BUCKET",
        "BLOB_S3_ACCESS_KEY_ID",
        "BLOB_S3_SECRET_ACCESS_KEY",
        "BLOB_S3_SESSION_TOKEN",
    ):
        value = getattr(settings, key)
        values[key] = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    runtime = app_user_sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    environment = {
        **os.environ,
        **values,
        "DATABASE_URL": runtime,
        "AGENT_DATA_DIR": str(d.root.parent),
        "PYTHONPATH": os.pathsep.join(
            filter(
                None,
                (
                    str(work),
                    str(Path(__file__).resolve().parents[2]),
                    os.environ.get("PYTHONPATH"),
                ),
            )
        ),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "uvicorn",
        "crash_sweep:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
        env=environment,
        cwd=Path(__file__).resolve().parents[2],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines = []
    try:

        async def started():
            while line := await process.stdout.readline():
                lines.append(line)
                if b"Uvicorn running on" in line:
                    return
            pytest.fail("Native server exited before its startup notification")

        await asyncio.wait_for(started(), 20)
        body = {
            "operation_id": operation,
            "expected_content_sha256": hashlib.sha256(BODY).hexdigest(),
            "expected_policy_version": 1,
            "expected_manifest_sha256": snapshot["manifest_sha256"],
            "action": "sweep",
        }
        async with httpx.AsyncClient(timeout=20, trust_env=False) as wire:
            with pytest.raises(httpx.HTTPError):
                await wire.post(
                    f"http://127.0.0.1:{port}/enterprise/storage-originals/{blob_id}/sweep",
                    headers={"Authorization": client.headers["Authorization"]},
                    json=body,
                )
        assert await asyncio.wait_for(process.wait(), 20) == 73
        (work / "crash-receipt.json").write_text(
            json.dumps(
                {
                    "pid": process.pid,
                    "exit_code": process.returncode,
                    "provider": selected,
                    "request": body,
                    "exit_boundary": "after provider absence before final database commit",
                }
            )
        )
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        lines.append(await process.stdout.read())
        (work / "server.log").write_bytes(b"".join(lines))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        row = await db.get(StorageBlob, blob_id)
        ledger = await db.scalar(
            select(StorageGCRun).where(
                StorageGCRun.tenant_id == d.tenant,
                StorageGCRun.receipt_json["request"]["operation_id"].astext == operation,
            )
        )
        assert row.state == "deleting" and ledger.status == "running"
        if selected == "s3":
            assert not remote.objects
        else:
            assert not (d.root.parent / ".storage_objects" / row.bucket / row.object_key).exists()
    source = owner_sessionmaker.kw["bind"].url
    profile = installer.configure(work / "profile", public_origin="http://127.0.0.1:3012", port=3012)
    profile_values = installer.read_profile(profile)
    profile_values["HIVE_INSTALL_SCHEMA_PASSWORD"] = source.password
    profile.write_text("".join(f"{key}='{value}'\n" for key, value in profile_values.items()))
    target = work / "incomplete-backup"
    binary = Path(os.environ.get("M1E_PSQL") or shutil.which("psql")).parent
    backed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "app.scripts.self_hosted_recovery",
            "backup",
            "--offline",
            "--profile",
            str(profile),
            "--agents",
            str(d.root.parent),
            "--output",
            str(target),
            "--pg-bin",
            str(binary),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "PGHOST": source.host,
            "PGPORT": str(source.port),
            "PGDATABASE": source.database,
            "PGUSER": source.username,
            "PGPASSWORD": source.password,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        backed.returncode == 3 and json.loads(backed.stdout)["code"] == "original_delete_intents_require_reconciliation"
    ), backed.stdout
    assert not target.exists()
    inspected = (await apply(client, blob_id, "inspect")).json()
    resolved = await sweep(client, blob_id, inspected, action="reconcile")
    assert resolved.json()["state"] == "deleted", resolved.text


async def test_revoked_role_after_durable_intent_is_denied_before_provider_effect(
    pending, owner_sessionmaker, monkeypatch
):  # noqa: F811
    from app.core.security import decode_access_token
    from app.models.user import User
    from app.services import original_sweep

    _d, client, blob_id, (_, remote), *_ = pending
    snapshot = await prepare(client, blob_id)
    actor = uuid.UUID(decode_access_token(client.headers["Authorization"].removeprefix("Bearer "))["sub"])
    operation = str(uuid.uuid4())
    original = original_sweep._prepare

    async def revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with owner_sessionmaker() as db:
            (await db.get(User, actor)).role = "member"
            await db.commit()
        return result

    try:
        monkeypatch.setattr(original_sweep, "_prepare", revoke)
        denied = await sweep(client, blob_id, snapshot, operation_id=operation)
        assert denied.status_code == 403 and not remote.deletes, denied.text
    finally:
        async with owner_sessionmaker() as db:
            (await db.get(User, actor)).role = "org_admin"
            await db.commit()
    monkeypatch.setattr(original_sweep, "_prepare", original)
    inspected = (await apply(client, blob_id, "inspect")).json()
    reconciled = await sweep(client, blob_id, inspected, action="reconcile")
    assert reconciled.json()["outcome"] == "present", reconciled.text
    recovered = await sweep(client, blob_id, inspected, retry_operation_id=operation)
    assert recovered.json()["state"] == "deleted", recovered.text


async def test_platform_sweep_uses_live_audited_selected_company_at_both_gates(
    pending, owner_sessionmaker, app_user_sessionmaker, monkeypatch
):  # noqa: F811
    from app.core.security import create_access_token, decode_access_token
    from app.core.tenant_middleware import TenantMiddleware
    from app.api import enterprise
    from fastapi import FastAPI
    from app.models.user import User

    d, client, blob_id, (_, _remote), _member, foreign = pending
    snapshot = await prepare(client, blob_id)
    identity = decode_access_token(foreign["Authorization"].removeprefix("Bearer "))
    actor, home = uuid.UUID(identity["sub"]), uuid.UUID(identity["tid"])
    async with owner_sessionmaker() as db:
        (await db.get(User, actor)).role = "platform_admin"
        await db.commit()
    # Pin the actual independently committed audit consumer, not an audit stub.
    monkeypatch.setattr("app.services.audit_logger.async_session", app_user_sessionmaker)
    headers = {
        "Authorization": "Bearer " + create_access_token(str(actor), "platform_admin", str(home)),
        "X-Tenant-Id": str(d.tenant),
    }
    # The company-only fixture pins token home directly. This cross-company
    # path needs the real middleware and get_db selected-company consumer.
    app = FastAPI()
    app.add_middleware(TenantMiddleware)
    app.include_router(enterprise.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://selected-original.test", headers=headers
    ) as selected:
        response = await sweep(selected, blob_id, snapshot)
    assert response.status_code == 200 and response.json()["state"] == "deleted", response.text
