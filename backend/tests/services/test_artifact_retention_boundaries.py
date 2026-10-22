"""Background retention must never cross filesystem aliases."""

import os
import uuid
from datetime import datetime, timezone

import pytest

from app.services import chat_artifact_delivery as delivery


@pytest.mark.parametrize("alias", ["snapshot_root", "snapshot_parent", "preview_root", "preview_parent"])
def test_retention_never_deletes_through_a_linked_storage_directory(tmp_path, alias):
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    root = tmp_path / "agent"
    root.mkdir()
    foreign = root / "workspace/retained-data"
    foreign.mkdir(parents=True)
    if alias.startswith("snapshot"):
        relative = delivery.CHAT_ARTIFACT_SNAPSHOT_DIR
        linked = root / (relative if alias == "snapshot_root" else relative.parent)
        payload = foreign / ("old.txt" if alias == "snapshot_root" else "chat_artifact_snapshots/old.txt")
    else:
        relative = root / ".office_meta/artifacts"
        linked = relative if alias == "preview_root" else relative.parent
        payload = foreign / (str(uuid.uuid4()) if alias == "preview_root" else "artifacts/" + str(uuid.uuid4())) / "preview.html"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b"KEEP_FOREIGN_BYTES")
    os.utime(payload, (0, 0))
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(foreign, target_is_directory=True)
    try:
        if alias.startswith("snapshot"):
            delivery.cleanup_chat_artifact_snapshots(workspace_root=root, referenced_snapshot_paths=[], retention_days=0)
        else:
            delivery._cleanup_orphaned_office_preview_caches(workspace_root=root, live_artifact_ids=set())
    except WorkspaceAuthorityError:
        pass
    assert payload.read_bytes() == b"KEEP_FOREIGN_BYTES"
    assert linked.is_symlink()


def test_retention_preserves_linked_files_and_directories_but_removes_real_expired_snapshots(tmp_path):
    root = tmp_path / "agent"
    snapshots = root / delivery.CHAT_ARTIFACT_SNAPSHOT_DIR
    snapshots.mkdir(parents=True)
    foreign = root / "workspace/retained"
    foreign.mkdir(parents=True)
    (foreign / "private.txt").write_bytes(b"KEEP")
    (snapshots / "linked-directory").symlink_to(foreign, target_is_directory=True)
    (snapshots / "linked-file.txt").symlink_to(foreign / "private.txt")
    os.link(foreign / "private.txt", snapshots / "shared-file.txt")
    expired = snapshots / "expired.txt"
    expired.write_bytes(b"EXPIRED")
    for path in (foreign / "private.txt", expired):
        os.utime(path, (0, 0))

    report = delivery.cleanup_chat_artifact_snapshots(workspace_root=root, referenced_snapshot_paths=[],
        retention_days=30, now=datetime(2026, 2, 15, tzinfo=timezone.utc))

    assert not expired.exists()
    assert (foreign / "private.txt").read_bytes() == b"KEEP"
    assert (snapshots / "linked-file.txt").is_symlink()
    assert (snapshots / "linked-directory").is_symlink()
    assert (snapshots / "shared-file.txt").read_bytes() == b"KEEP"
    assert report["removed_count"] == 1 and report["removed_bytes"] == len(b"EXPIRED")


def test_gc_report_does_not_overwrite_a_link_target(tmp_path):
    root = tmp_path / "agent"
    report = root / delivery.CHAT_ARTIFACT_SNAPSHOT_GC_REPORT
    report.parent.mkdir(parents=True)
    private = tmp_path / "private.txt"
    private.write_bytes(b"KEEP_PRIVATE_REPORT")
    report.with_suffix(report.suffix + ".tmp").symlink_to(private)

    delivery.cleanup_chat_artifact_snapshots(workspace_root=root, referenced_snapshot_paths=[])

    assert private.read_bytes() == b"KEEP_PRIVATE_REPORT"


def test_snapshot_retention_pins_its_directory_before_a_parent_is_replaced(tmp_path, monkeypatch):
    from app.services.workspace_resource_authority import WorkspaceAuthorityError

    root = tmp_path / "agent"
    snapshots = root / delivery.CHAT_ARTIFACT_SNAPSHOT_DIR
    snapshots.mkdir(parents=True)
    (snapshots / "old.txt").write_bytes(b"EXPIRED")
    os.utime(snapshots / "old.txt", (0, 0))
    foreign = root / "workspace/retained"
    foreign.mkdir(parents=True)
    (foreign / "old.txt").write_bytes(b"KEEP")
    os.utime(foreign / "old.txt", (0, 0))
    walk = os.fwalk
    replaced = []

    def replace_after_directory_open(*args, **kwargs):
        for item in walk(*args, **kwargs):
            # Pin inventory also walks the store. Replace the directory once,
            # after the deletion traversal has opened its own descriptor.
            if kwargs.get("topdown") is False and not replaced:
                snapshots.rename(snapshots.with_name("parked"))
                snapshots.symlink_to(foreign, target_is_directory=True)
                replaced.append(True)
            yield item

    monkeypatch.setattr(delivery.os, "fwalk", replace_after_directory_open)
    with pytest.raises(WorkspaceAuthorityError):
        delivery.cleanup_chat_artifact_snapshots(workspace_root=root, referenced_snapshot_paths=[], retention_days=0)

    assert replaced == [True]
    assert (foreign / "old.txt").read_bytes() == b"KEEP"
    assert not (snapshots.with_name("parked") / "old.txt").exists()
