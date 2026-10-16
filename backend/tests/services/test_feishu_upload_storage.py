"""Provider upload publication never follows aliases or replaces existing bytes."""

import pytest
from fastapi import HTTPException

from app.services.feishu_uploads import _store_upload, _upload_name


@pytest.mark.parametrize("alias", ["ancestor", "destination"])
def test_upload_storage_refuses_filesystem_aliases(tmp_path, alias):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_bytes(b"retained")
    root = tmp_path / "uploads"
    root.mkdir()
    if alias == "ancestor":
        (root / "child").symlink_to(outside, target_is_directory=True)
    else:
        (root / "child").mkdir()
        (root / "child/victim.txt").symlink_to(victim)
    with pytest.raises(HTTPException) as denied:
        _store_upload(root, ("child", "victim.txt"), b"replacement")
    assert denied.value.status_code == 409 and victim.read_bytes() == b"retained"


def test_upload_publication_is_immutable_and_retryable(tmp_path):
    _store_upload(tmp_path, ("child", "document.txt"), b"one")
    _store_upload(tmp_path, ("child", "document.txt"), b"one")
    with pytest.raises(HTTPException) as changed:
        _store_upload(tmp_path, ("child", "document.txt"), b"two")
    assert changed.value.status_code == 409
    assert (tmp_path / "child/document.txt").read_bytes() == b"one"
    assert list((tmp_path / "child").glob(".pending-*")) == []
    assert _upload_name("../../document.txt") == "document.txt"
    assert len(_upload_name("文" * 200).encode()) <= 128
    assert _upload_name("文" * 200 + ".docx").endswith(".docx")
