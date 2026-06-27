from __future__ import annotations

from uuid import uuid4


def test_workspace_snapshot_restore_reverts_created_modified_and_deleted_files(tmp_path):
    from app.services.session_workspace_snapshot import capture_workspace_snapshot, restore_workspace_snapshot

    agent_id = uuid4()
    session_id = uuid4()
    checkpoint_event_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    (workspace / "keep" / "notes.txt").parent.mkdir()
    (workspace / "keep" / "notes.txt").write_text("notes", encoding="utf-8")

    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_event_id,
        data_root=tmp_path,
    )

    (workspace / "report.md").write_text("v2", encoding="utf-8")
    (workspace / "keep" / "notes.txt").unlink()
    (workspace / "new.txt").write_text("new", encoding="utf-8")

    restore = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
    )

    assert restore.ok is True
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v1"
    assert (workspace / "keep" / "notes.txt").read_text(encoding="utf-8") == "notes"
    assert not (workspace / "new.txt").exists()
    assert restore.restored_files == ["keep/notes.txt", "report.md"]
    assert restore.deleted_files == ["new.txt"]
    assert restore.workspace_rel_path == "workspace"


def test_workspace_snapshot_restore_fails_before_mutating_when_snapshot_file_is_missing(tmp_path):
    from app.services.session_workspace_snapshot import capture_workspace_snapshot, restore_workspace_snapshot

    agent_id = uuid4()
    session_id = uuid4()
    checkpoint_event_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")

    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_event_id,
        data_root=tmp_path,
    )

    agent_root = tmp_path / str(agent_id)
    manifest_path = agent_root / snapshot["manifest_path"]
    (manifest_path.parent / "files" / "report.md").unlink()
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    (workspace / "new.txt").write_text("new", encoding="utf-8")

    restore = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
    )

    assert restore.ok is False
    assert restore.error == "workspace snapshot file unavailable: report.md"
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"
    assert (workspace / "new.txt").read_text(encoding="utf-8") == "new"
