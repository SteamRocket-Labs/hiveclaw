"""New uploads cannot be reached by an inactive original generation's effects."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.conftest import app_user_engine, migrated_pg_url as _migrated_pg_url, pg_container as _pg_container  # noqa: F401
from tests.integration.test_m4w_original_reconciliation_postgres import pending, provider, original_api, delivered, s3_server, BODY  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def pg_container():
    yield from _pg_container.__wrapped__()


@pytest.fixture
def migrated_pg_url(pg_container):
    return _migrated_pg_url.__wrapped__(pg_container)


def migrate(owner_sessionmaker, command, revision):
    owner = owner_sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    return subprocess.run([sys.executable, "-B", "-m", "alembic", command, revision], cwd=ROOT / "backend",
        env={**os.environ, "DATABASE_URL": owner}, capture_output=True, timeout=30)


async def test_new_same_body_upload_survives_an_old_generation_cleanup(pending, app_user_sessionmaker):  # noqa: F811
    from app.models.storage_blob import StorageBlob, StorageBlobRef
    from app.services.blob_store import BlobLocation
    from app.services.upload_originals import _recorded_store

    d, client, old_id, (selected, remote), member, *_ = pending
    recovered = await client.post(f"/enterprise/storage-originals/{old_id}/reconcile", json={
        "operation_id": str(uuid.uuid4()), "expected_content_sha256": hashlib.sha256(BODY).hexdigest(), "action": "recover"})
    assert recovered.status_code == 200 and recovered.json()["recovered"] is True
    # Author a completed, unreferenced reclamation state. The product lifecycle
    # remains separate; this fixture exercises its subsequent real upload path.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        old = await db.scalar(select(StorageBlob).where(StorageBlob.id == old_id).with_for_update())
        old.state, old.deleted_at = "deleted", datetime.now(timezone.utc)
        old_location = BlobLocation(old.provider, old.bucket, old.object_key)
        old_reader = _recorded_store(old, d.tenant)
        await db.commit()
    old_path = d.root.parent / ".storage_objects" / old_location.bucket / old_location.object_key
    if selected == "s3":
        remote.objects.pop(f"/{old_location.bucket}/{old_location.object_key}")
    else:
        assert old_path.read_bytes() == BODY
        old_path.unlink()
    refs = []
    for _ in range(2):
        uploaded = await client.post("/chat/upload", headers=member, files={"file": ("new-original.bin", BODY)},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
        assert uploaded.status_code == 200, uploaded.text
        refs.append(uploaded.json()["original_attachment"]["id"])
    assert refs[0] != refs[1]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        rows = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))).all()
        assert len(rows) == 2
        current = next(row for row in rows if row.state == "available")
        assert current.id != old_id and current.object_key != old_location.object_key
        assert str(current.id) in current.object_key
        owned = (await db.scalars(select(StorageBlobRef).where(StorageBlobRef.blob_id == current.id))).all()
        assert len(owned) == 2 and all(ref.metadata_json["owner_user_id"] == str(d.user.id) for ref in owned)
    # A delayed request for the obsolete location may arrive after the new
    # reference commits. It must be unable to delete the new payload.
    if selected == "s3":
        with old_reader._request("DELETE", old_location) as response:
            assert response.status in {204, 404}
        assert remote.writes == 2
    else:
        old_path.unlink(missing_ok=True)
    for ref in refs:
        downloaded = await client.get(f"/chat/attachments/{ref}/download", headers=member)
        assert downloaded.status_code == 200 and downloaded.content == BODY, downloaded.text
    assert (await client.get(f"/chat/attachments/{old_id}/download", headers=member)).status_code == 404


async def test_legacy_original_location_remains_readable_after_new_writer_adoption(original_api, provider, monkeypatch, owner_sessionmaker):  # noqa: F811
    from app.services import upload_originals
    from app.services.blob_store import BlobLocation

    d, client = original_api
    def legacy_location(tenant, digest, provider="filesystem", bucket="durable", **kwargs):
        return BlobLocation(provider, bucket, f"{tenant}/{digest}")

    with monkeypatch.context() as legacy:
        legacy.setattr(upload_originals, "_location", legacy_location)
        first = await client.post("/chat/upload", files={"file": ("legacy.bin", BODY)},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert first.status_code == 200, first.text
    second = await client.post("/chat/upload", files={"file": ("current.bin", BODY)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert second.status_code == 200, second.text
    # Actual old-compatible originals permit reversing only this new boundary.
    assert migrate(owner_sessionmaker, "downgrade", "chat_snapshot_payload_refs_1021").returncode == 0
    for row in (first.json(), second.json()):
        result = await client.get(f"/chat/attachments/{row['original_attachment']['id']}/download")
        assert result.status_code == 200 and result.content == BODY
    assert migrate(owner_sessionmaker, "upgrade", "head").returncode == 0


async def test_generation_data_refuses_reader_downgrade_without_losing_current_head(original_api, provider, owner_sessionmaker):  # noqa: F811
    from sqlalchemy import text

    d, client = original_api
    uploaded = await client.post("/chat/upload", files={"file": ("generation.bin", BODY)},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert uploaded.status_code == 200, uploaded.text
    async with owner_sessionmaker() as db:
        head = await db.scalar(text("SELECT version_num FROM alembic_version"))
    refused = migrate(owner_sessionmaker, "downgrade", "chat_snapshot_payload_refs_1021")
    assert refused.returncode != 0 and b"original_generations_require_recovery_before_downgrade" in refused.stderr
    async with owner_sessionmaker() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == head
    response = await client.get(f"/chat/attachments/{uploaded.json()['original_attachment']['id']}/download")
    assert response.status_code == 200 and response.content == BODY
