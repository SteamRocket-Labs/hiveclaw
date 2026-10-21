"""Actual authenticated original refs consume configured remote object storage."""

from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from tests.integration.test_m4s_original_attachments_postgres import original_api, delivered  # noqa: F401
from tests.services.test_s3_blob_store import s3_server  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_remote_originals_keep_recorded_provider_and_private_authority(
    original_api, s3_server, app_user_sessionmaker, monkeypatch  # noqa: F811
):  # noqa: F811
    from app.config import get_settings
    from app.models.storage_blob import StorageBlob

    d, client = original_api
    settings = get_settings()
    for key, value in {
        "BLOB_STORAGE_PROVIDER": "s3",
        "BLOB_S3_ENDPOINT": s3_server.endpoint,
        "BLOB_S3_REGION": "us-east-1",
        "BLOB_S3_BUCKET": "private-originals",
        "BLOB_S3_ACCESS_KEY_ID": s3_server.access,
        "BLOB_S3_SECRET_ACCESS_KEY": s3_server.secret,
        "BLOB_S3_SESSION_TOKEN": s3_server.token,
    }.items():
        monkeypatch.setattr(settings, key, value, raising=False)

    async def upload():
        return await client.post(
            "/chat/upload",
            files={"file": ("source.bin", b"REMOTE_ORIGINAL")},
            data={"agent_id": str(d.agent), "skip_personal_kb": "true"},
        )

    result = await upload()
    assert result.status_code == 200, result.text
    projection = result.json()
    assert s3_server.writes == 1 and not (Path(settings.AGENT_DATA_DIR) / ".storage_objects").exists()
    (d.root / projection["workspace_path"]).write_bytes(b"EDITED_WORKSPACE")
    url = f"/chat/attachments/{projection['original_attachment']['id']}/download"
    response = await client.get(url)
    assert response.status_code == 200 and response.content == b"REMOTE_ORIGINAL", response.text
    monkeypatch.setattr(settings, "BLOB_STORAGE_PROVIDER", "filesystem")
    repeated = await upload()
    assert (
        repeated.status_code == 200
        and repeated.json()["original_attachment"]["id"] != projection["original_attachment"]["id"]
    ), repeated.text
    assert s3_server.writes == 1 and not (Path(settings.AGENT_DATA_DIR) / ".storage_objects").exists()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        blobs = (await db.scalars(select(StorageBlob).where(StorageBlob.tenant_id == d.tenant))).all()
        assert len(blobs) == 1 and blobs[0].provider == "s3"
        assert s3_server.secret not in str(blobs[0].metadata_json)
    requests = len(s3_server.requests)
    monkeypatch.setattr(settings, "BLOB_S3_ENDPOINT", "http://127.0.0.1:1")
    unavailable = await client.get(url)
    assert unavailable.status_code == 503 and len(s3_server.requests) == requests, unavailable.text
