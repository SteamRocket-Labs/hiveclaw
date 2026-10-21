"""Scoped operator catalog and receipt readback consume real original operations."""

import hashlib
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import uuid

import pytest

from tests.integration.test_m4w_original_reconciliation_postgres import pending, provider, original_api, delivered, s3_server, BODY  # noqa: F401
from tests.integration.test_m4z_original_sweep_postgres import prepare, sweep

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
ROOT = Path(__file__).resolve().parents[3]
CATALOG = "/enterprise/storage-original-catalog"


async def test_catalog_pages_filters_and_current_references_stay_company_scoped(pending):  # noqa: F811
    d, client, blob_id, (_selected, remote), member, foreign = pending
    assert (await client.get(CATALOG, headers=member)).status_code == 403
    assert (await client.get(CATALOG + f"?tenant_id={d.tenant}", headers=foreign)).status_code == 403
    uploaded = await client.post("/chat/upload", headers=member, files={"file": ("owned.bin", b"OWNED_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert uploaded.status_code == 200, uploaded.text
    before = len(remote.requests)
    first = await client.get(CATALOG, params={"limit": 1})
    assert first.status_code == 200 and first.json()["next_cursor"], first.text
    second = await client.get(CATALOG, params={"limit": 1, "after": first.json()["next_cursor"]})
    assert second.status_code == 200 and second.json()["next_cursor"] is None, second.text
    rows = first.json()["items"] + second.json()["items"]
    assert len({row["blob_id"] for row in rows}) == 2
    assert next(row for row in rows if row["blob_id"] == str(blob_id))["reference_count"] == 0
    available = (await client.get(CATALOG, params={"state": "available"})).json()["items"]
    assert len(available) == 1 and available[0]["reference_count"] == 1
    assert [row["blob_id"] for row in (await client.get(CATALOG, params={"unreferenced": "true"})).json()["items"]] == [str(blob_id)]
    assert (await client.get(CATALOG, params={"state": "invented"})).status_code == 422
    assert (await client.get(CATALOG, headers=foreign)).json()["items"] == []
    assert len(remote.requests) == before
    assert all(key not in str(rows) for key in ("object_key", "bucket", remote.secret, remote.endpoint, str(d.root)))


async def test_receipt_get_is_actor_scoped_and_never_repeats_provider_observation(pending, owner_sessionmaker):  # noqa: F811
    from app.core.security import create_access_token
    from app.models.user import User

    d, client, blob_id, (_, remote), member, foreign = pending
    operation = str(uuid.uuid4())
    result = await client.post(f"/enterprise/storage-originals/{blob_id}/reconcile", json={"operation_id": operation,
        "expected_content_sha256": hashlib.sha256(BODY).hexdigest(), "action": "recover"})
    assert result.status_code == 200 and result.json()["recovered"] is True
    url = f"/enterprise/storage-originals/{blob_id}/operations/reconciliation/{operation}"
    before = len(remote.requests)
    read = await client.get(url)
    assert read.status_code == 200 and read.json()["result"] == result.json(), read.text
    assert read.json()["status"] == "completed" and len(remote.requests) == before
    assert (await client.get(url, headers=member)).status_code == 403
    assert (await client.get(url, headers=foreign)).status_code == 404
    async with owner_sessionmaker() as db:
        colleague = User(id=uuid.uuid4(), tenant_id=d.tenant, username=uuid.uuid4().hex, email=uuid.uuid4().hex + "@fixture.test",
            display_name="Other administrator", role="org_admin", password_hash="fixture")
        db.add(colleague)
        await db.commit()
    headers = {"Authorization": "Bearer " + create_access_token(str(colleague.id), "org_admin", str(d.tenant))}
    assert (await client.get(url, headers=headers)).status_code == 404
    assert (await client.get(url.replace(str(blob_id), str(uuid.uuid4())))).status_code == 404
    assert "evidence" not in read.json() and len(remote.requests) == before


async def test_unknown_sweep_is_discoverable_without_provider_replay(pending, monkeypatch):  # noqa: F811
    from app.services.blob_store import BlobDeleteReceipt, FilesystemBlobStore
    from app.services.s3_blob_store import S3BlobStore

    _d, client, blob_id, (selected, remote), *_ = pending
    snapshot = await prepare(client, blob_id)
    cls = S3BlobStore if selected == "s3" else FilesystemBlobStore
    original = cls.delete
    async def unknown(*args, **kwargs):
        return BlobDeleteReceipt("unknown", True)
    monkeypatch.setattr(cls, "delete", unknown)
    operation = str(uuid.uuid4())
    result = await sweep(client, blob_id, snapshot, operation_id=operation)
    assert result.status_code == 200 and result.json()["state"] == "deleting"
    before = len(remote.requests)
    rows = (await client.get(CATALOG, params={"state": "deleting"})).json()["items"]
    assert len(rows) == 1 and rows[0]["active_sweep"] == {
        "operation_id": operation, "status": "partial", "outcome": "unknown", "reconciled_outcome": None}
    read = await client.get(f"/enterprise/storage-originals/{blob_id}/operations/sweep/{operation}")
    assert read.status_code == 200 and read.json()["result"] == result.json(), read.text
    quarantine = await client.get(f"/enterprise/storage-originals/{blob_id}/operations/retention/{snapshot['operation_id']}")
    assert quarantine.status_code == 200 and quarantine.json()["result"]["state"] == "quarantined"
    assert len(remote.requests) == before
    monkeypatch.setattr(cls, "delete", original)
    assert (await sweep(client, blob_id, result.json(), action="reconcile")).json()["outcome"] == "present"
    assert (await sweep(client, blob_id, result.json(), retry_operation_id=operation)).json()["state"] == "deleted"


async def test_current_component_closes_real_operator_flow_and_reads_lost_ack_without_redeleting(
    pending, app_user_sessionmaker, tmp_path_factory):  # noqa: F811
    from app.config import get_settings
    from app.core.security import decode_access_token
    from app.database import pin_rls_tenant_context
    from app.models.storage_blob import StorageBlob, StorageBlobRef, StorageGCRun
    from app.services.original_sweep import ledger_identity
    from sqlalchemy import select

    d, client, blob_id, (selected, remote), *_ = pending
    work = tmp_path_factory.mktemp("original-operator-component")
    module = work / "operator_runtime.py"
    module.write_text("from fastapi import FastAPI\nfrom app.api.enterprise import router\n"
        "from app.core.tenant_middleware import TenantMiddleware\n"
        "app=FastAPI()\napp.add_middleware(TenantMiddleware)\napp.include_router(router,prefix='/api')\n")
    settings = get_settings()
    values = {}
    for key in ("SECRET_KEY", "JWT_SECRET_KEY", "BLOB_STORAGE_PROVIDER", "BLOB_S3_ENDPOINT", "BLOB_S3_REGION",
        "BLOB_S3_BUCKET", "BLOB_S3_ACCESS_KEY_ID", "BLOB_S3_SECRET_ACCESS_KEY", "BLOB_S3_SESSION_TOKEN"):
        value = getattr(settings, key)
        values[key] = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    environment = {**os.environ, **values, "DATABASE_URL": app_user_sessionmaker.kw["bind"].url.render_as_string(hide_password=False),
        "AGENT_DATA_DIR": str(d.root.parent), "PYTHONPATH": str(work) + os.pathsep + os.environ.get("PYTHONPATH", str(ROOT / "backend"))}
    process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "uvicorn", "operator_runtime:app", "--host", "127.0.0.1",
        "--port", str(port), "--no-access-log", env=environment, cwd=ROOT / "backend",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines = []
    try:
        async def started():
            while line := await process.stdout.readline():
                lines.append(line)
                if b"Uvicorn running on" in line:
                    return
            pytest.fail("Native operator API exited before its startup notification")
        await asyncio.wait_for(started(), 20)
        token = client.headers["Authorization"].removeprefix("Bearer ")
        profile = work / "bridge.json"
        output = work / "bridge-result.json"
        profile.write_text(json.dumps({"origin": f"http://127.0.0.1:{port}", "token": token, "tenant": str(d.tenant),
            "actor": decode_access_token(token)["sub"], "digest": hashlib.sha256(BODY).hexdigest(), "output": str(output)}))
        profile.chmod(0o600)
        binary = ROOT / "frontend/node_modules/.bin/vitest"
        assert binary.is_file(), "Native component integration requires the installed frontend dependencies"
        with (work / "frontend.log").open("wb") as log:
            node = await asyncio.create_subprocess_exec(str(binary), "run", "src/pages/workspace/WorkspaceOriginalStorageSection.native.test.tsx",
                cwd=ROOT / "frontend", env={**os.environ, "HIVE_ORIGINAL_OPERATIONS_BRIDGE": str(profile),
                    "NODE_OPTIONS": "--no-experimental-webstorage"}, stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                code = await asyncio.wait_for(node.wait(), 90)
            finally:
                if node.returncode is None:
                    node.terminate()
                    await asyncio.wait_for(node.wait(), 10)
        assert code == 0, (work / "frontend.log").read_text()[-4500:]
        receipt = json.loads(output.read_text())
        assert receipt["result"]["state"] == "deleted" and receipt["result"]["physical_bytes_reclaimed"] is None
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, d.tenant)
            row = await db.get(StorageBlob, blob_id)
            assert row.state == "deleted"
            assert (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == blob_id))).all() == []
            ledger = await db.get(StorageGCRun, ledger_identity(d.tenant, uuid.UUID(receipt["operation_id"])))
            assert ledger.status == "completed" and ledger.receipt_json["result"] == receipt["result"]
            if selected == "s3":
                assert len(remote.deletes) == 1 and not remote.objects
            else:
                assert not (d.root.parent / ".storage_objects" / row.bucket / row.object_key).exists()
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        lines.append(await process.stdout.read())
        (work / "server.log").write_bytes(b"".join(lines))
