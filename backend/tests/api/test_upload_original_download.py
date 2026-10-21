"""Original downloads release their verified copies even when delivery stops."""

import io
from pathlib import Path
import tempfile
import uuid

from fastapi import UploadFile
import pytest

from tests.api.test_files_channel_download_token import _send_download


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
