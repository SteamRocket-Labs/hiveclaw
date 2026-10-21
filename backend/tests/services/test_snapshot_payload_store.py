"""Shared snapshot bytes retain distinct delivery identities and live pins."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import os
from types import SimpleNamespace
import uuid

import pytest

from app.services.chat_artifact_delivery import build_artifact_candidate, cleanup_chat_artifact_snapshots


def candidate(root, name, *, run=None):
    return build_artifact_candidate(agent_id=uuid.UUID(int=1), session_id=uuid.uuid4(),
        runtime_task_id=run or uuid.uuid4(), workspace_root=root, path="workspace/" + name)


def test_parallel_equal_payloads_reuse_one_file_without_merging_delivery_identity(tmp_path):
    (tmp_path / "workspace").mkdir()
    for name in ("one.txt", "two.txt"):
        (tmp_path / "workspace" / name).write_bytes(b"IDENTICAL_BYTES")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda name: candidate(tmp_path, name), ("one.txt", "two.txt")))
    assert first["snapshot_hash"] != second["snapshot_hash"] and first["artifact_id"] != second["artifact_id"]
    assert first["snapshot"]["snapshot_storage_path"] == second["snapshot"]["snapshot_storage_path"]
    path = tmp_path / first["snapshot"]["snapshot_storage_path"]
    assert path.read_bytes() == b"IDENTICAL_BYTES" and path.stat().st_nlink == 1
    assert path.name == hashlib.sha256(b"IDENTICAL_BYTES").hexdigest() + ".txt"
    assert len(list(path.parent.iterdir())) == 1
    inodes = {path.stat().st_ino}
    inodes.update((tmp_path / "workspace" / name).stat().st_ino for name in ("one.txt", "two.txt"))
    assert len(inodes) == 3
    assert first["snapshot"]["snapshot_ref_path"] != second["snapshot"]["snapshot_ref_path"]


def test_recent_delivery_keeps_an_old_reused_body_without_renewing_body_mtime(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/report.txt").write_bytes(b"STABLE_BYTES")
    first = candidate(tmp_path, "report.txt")
    path = tmp_path / first["snapshot"]["snapshot_storage_path"]
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=60)).timestamp()
    os.utime(path, (old, old))
    os.utime(tmp_path / first["snapshot"]["snapshot_ref_path"], (old, old))
    second = candidate(tmp_path, "report.txt")
    assert second["snapshot"]["snapshot_storage_path"] == first["snapshot"]["snapshot_storage_path"]
    assert path.stat().st_mtime == old
    report = cleanup_chat_artifact_snapshots(workspace_root=tmp_path, referenced_snapshot_paths=[], retention_days=30, now=now)
    assert path.read_bytes() == b"STABLE_BYTES" and report["removed_payload_count"] == 0


def test_corrupt_pin_retains_uncertain_payload_but_does_not_block_legacy_orphan_cleanup(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace/report.txt").write_bytes(b"RECOVERABLE_BYTES")
    row = candidate(tmp_path, "report.txt")
    pin = tmp_path / row["snapshot"]["snapshot_ref_path"]
    pin.write_text("{}")
    legacy = tmp_path / "runtime_artifacts/chat_artifact_snapshots/old-session/no-runtime-task/orphan.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"DISPOSABLE_ORPHAN")
    for path in (pin, legacy, tmp_path / row["snapshot"]["snapshot_storage_path"]):
        os.utime(path, (1, 1))
    report = cleanup_chat_artifact_snapshots(workspace_root=tmp_path, referenced_snapshot_paths=[], retention_days=0)
    assert (tmp_path / row["snapshot"]["snapshot_storage_path"]).read_bytes() == b"RECOVERABLE_BYTES"
    assert pin.exists() and not legacy.exists()
    assert report["manifest_errors"] and report["kept_uncertain_count"] == 1


@pytest.mark.parametrize("suffix", [".docx", ".xlsx", ".pptx"])
def test_office_capture_preserves_delivery_format_and_private_copy_lifetime(tmp_path, suffix):
    from app.services.chat_artifact_delivery import captured_chat_artifact_file

    source = tmp_path / ("workspace/report" + suffix)
    source.parent.mkdir()
    source.write_bytes(b"EXACT_DELIVERED_OFFICE_BYTES")
    row = candidate(tmp_path, source.name)
    source.write_bytes(b"LATER_EDIT")
    artifact = SimpleNamespace(snapshot_json=row["snapshot"], path=row["path"])
    with captured_chat_artifact_file(artifact, tmp_path) as (captured, origin):
        assert origin == "delivery_snapshot" and captured.suffix == suffix
        assert captured.read_bytes() == b"EXACT_DELIVERED_OFFICE_BYTES"
        assert captured != tmp_path / row["snapshot"]["snapshot_storage_path"]
    assert not captured.exists() and source.read_bytes() == b"LATER_EDIT"
