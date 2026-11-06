"""Original downloads release their verified copies even when delivery stops."""

import io
from pathlib import Path
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import UploadFile
import pytest

from tests.api.test_files_channel_download_token import _send_download


@pytest.mark.parametrize("denied", [False, True])
async def test_workspace_original_discovers_web_upload_with_existing_authorization(monkeypatch, denied):
    from fastapi import HTTPException
    from app.api.upload import discover_workspace_original
    from app.models.storage_blob import StorageBlobRef
    import app.services.upload_originals as originals

    user = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    agent_id, manifest_id, ref_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    path = "workspace/uploads/sales-e2e.csv"
    ref = StorageBlobRef(id=ref_id, tenant_id=user.tenant_id, owner_type="chat_upload", owner_id=str(ref_id),
        purpose="original_attachment", metadata_json={"schema": originals.REF_SCHEMA, "owner_user_id": str(user.id),
            "agent_id": str(agent_id), "workspace_path": path, "workspace_manifest_id": str(manifest_id),
            "filename": "sales-e2e.csv", "content_sha256": "a" * 64, "size_bytes": 64})

    class DB:
        async def scalar(self, query):
            if query.column_descriptions[0].get("entity") is StorageBlobRef:
                sql = str(query.compile(compile_kwargs={"literal_binds": True}))
                assert user.tenant_id in query.compile().params.values()
                assert str(agent_id) in sql
                assert path in sql
                assert "chat_upload" in sql
                return ref
            return manifest_id

        async def get(self, model, key):
            return None

    authorize = AsyncMock(side_effect=HTTPException(403, "Not authorized") if denied else None)
    monkeypatch.setattr(originals, "authorize_upload_original", authorize)
    db = DB()
    if denied:
        with pytest.raises(HTTPException) as failure:
            await discover_workspace_original(agent_id, path, user, db)
        assert failure.value.status_code == 403
    else:
        assert await discover_workspace_original(agent_id, path, user, db) == {
            "id": str(ref_id), "owner_user_id": str(user.id), "content_sha256": "a" * 64, "size_bytes": 64}
    authorize.assert_awaited_once_with(db, user, ref)


@pytest.mark.parametrize("mode", ["get", "invalid_range", "disconnect"])
async def test_original_download_releases_its_private_copy(tmp_path, mode):
    from app.api.upload import _OriginalDownloadResponse

    lease = tempfile.TemporaryDirectory(dir=tmp_path)
    path = Path(lease.name) / "body"
    path.write_bytes(b"ORIGINAL")
    response = _OriginalDownloadResponse(lease, path, "source.bin")
    if mode == "disconnect":
        with pytest.raises(OSError, match="fixture client disconnected"):
            await _send_download(response, disconnect=True)
    else:
        headers = ((b"range", b"bytes=9999-"),) if mode == "invalid_range" else ()
        status, body = await _send_download(response, headers=headers)
        assert status == (416 if mode == "invalid_range" else 200)
        assert body == (b"" if mode == "invalid_range" else b"ORIGINAL")
    assert not path.parent.exists()


async def test_registration_failure_does_not_unlink_a_replaced_workspace_parent(tmp_path, monkeypatch):
    import app.api.upload as upload
    import app.services.workspace_resource_authority as authority

    agent = uuid.uuid4()
    monkeypatch.setattr(upload, "WORKSPACE_ROOT", tmp_path)
    retained = tmp_path / "retained-owner"
    retained.mkdir()
    sentinel = retained / "source.bin"
    sentinel.write_bytes(b"UNRELATED_OWNER_DATA")

    async def failed_registration(*args, **kwargs):
        uploads = tmp_path / str(agent) / "workspace/uploads"
        uploads.rename(uploads.with_name("displaced-uploads"))
        uploads.symlink_to(retained, target_is_directory=True)
        raise RuntimeError("fixture receipt unavailable")

    monkeypatch.setattr(authority, "register_workspace_path", failed_registration)
    with pytest.raises(RuntimeError, match="fixture receipt unavailable"):
        await upload.save_upload_for_agent(file=UploadFile(filename="source.bin", file=io.BytesIO(b"NEW_INPUT")),
            agent_id=agent, tenant_id=uuid.uuid4(), user_id=uuid.uuid4(), db=object())
    assert sentinel.exists() and sentinel.read_bytes() == b"UNRELATED_OWNER_DATA"
    assert (tmp_path / str(agent) / "workspace/displaced-uploads/source.bin").read_bytes() == b"NEW_INPUT"
