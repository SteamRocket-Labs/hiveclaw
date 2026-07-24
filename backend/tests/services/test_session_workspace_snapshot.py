from __future__ import annotations

import os
import asyncio
from uuid import uuid4

import pytest


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


def test_snapshot_replacement_failure_preserves_previous_checkpoint(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    session_id = uuid4()
    checkpoint_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    first = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_id,
        data_root=tmp_path,
    )
    old_manifest = tmp_path / str(agent_id) / first["manifest_path"]
    old_snapshot_file = old_manifest.parent / "files" / "report.md"
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    real_copy = snapshots.shutil.copy2

    def fail_workspace_copy(source, target):
        if str(source) == str(workspace / "report.md"):
            raise OSError("injected capture failure")
        return real_copy(source, target)

    monkeypatch.setattr(snapshots.shutil, "copy2", fail_workspace_copy)

    with pytest.raises(OSError, match="injected capture failure"):
        snapshots.capture_workspace_snapshot(
            agent_id=agent_id,
            session_id=session_id,
            checkpoint_event_id=checkpoint_id,
            data_root=tmp_path,
        )

    assert old_manifest.is_file()
    assert old_snapshot_file.read_text(encoding="utf-8") == "v1"


def test_scoped_workspace_restore_preserves_foreign_session_files(tmp_path):
    from app.services.session_workspace_snapshot import (
        capture_workspace_snapshot,
        workspace_file_state,
        restore_workspace_snapshot,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    (workspace / "foreign.md").write_text("foreign-v1", encoding="utf-8")
    checkpoint_states = {
        "workspace/report.md": workspace_file_state(
            agent_id=agent_id,
            path="workspace/report.md",
            data_root=tmp_path,
        ),
        "workspace/owned-new.md": {
            "path": "workspace/owned-new.md",
            "exists": False,
            "sha256": None,
            "size": 0,
        },
    }
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )

    (workspace / "report.md").write_text("v2", encoding="utf-8")
    (workspace / "owned-new.md").write_text("owned", encoding="utf-8")
    (workspace / "foreign.md").write_text("foreign-v2", encoding="utf-8")
    expected = {
        path: workspace_file_state(agent_id=agent_id, path=path, data_root=tmp_path)
        for path in ("workspace/report.md", "workspace/owned-new.md")
    }

    restored = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=list(expected),
        expected_current_states=expected,
        expected_lineage={
            path: [{"path": path, "before_state": checkpoint_states[path], "after_state": state}]
            for path, state in expected.items()
        },
        data_root=tmp_path,
    )

    assert restored.ok is True
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v1"
    assert not (workspace / "owned-new.md").exists()
    assert (workspace / "foreign.md").read_text(encoding="utf-8") == "foreign-v2"
    assert restored.restored_files == ["report.md"]
    assert restored.deleted_files == ["owned-new.md"]


def test_scoped_workspace_restore_fails_closed_on_a_later_writer(tmp_path):
    from app.services.session_workspace_snapshot import (
        capture_workspace_snapshot,
        workspace_file_state,
        restore_workspace_snapshot,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    checkpoint_state = workspace_file_state(
        agent_id=agent_id,
        path="workspace/report.md",
        data_root=tmp_path,
    )
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("session-write", encoding="utf-8")
    expected = workspace_file_state(
        agent_id=agent_id,
        path="workspace/report.md",
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("foreign-later-write", encoding="utf-8")

    restored = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=["workspace/report.md"],
        expected_current_states={"workspace/report.md": expected},
        expected_lineage={
            "workspace/report.md": [
                {
                    "path": "workspace/report.md",
                    "before_state": checkpoint_state,
                    "after_state": expected,
                }
            ]
        },
        data_root=tmp_path,
    )

    assert restored.ok is False
    assert "changed after session evidence" in str(restored.error)
    assert (workspace / "report.md").read_text(encoding="utf-8") == "foreign-later-write"


def test_locked_file_restore_uses_exact_current_state_without_session_lineage(tmp_path):
    from app.services.session_workspace_snapshot import (
        agent_workspace_lock,
        capture_workspace_snapshot,
        restore_workspace_snapshot_locked,
        workspace_file_state,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("checkpoint", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    report.write_text("current", encoding="utf-8")
    current = workspace_file_state(
        agent_id=agent_id,
        path="workspace/report.md",
        data_root=tmp_path,
    )

    with agent_workspace_lock(agent_id, data_root=tmp_path):
        restored = restore_workspace_snapshot_locked(
            agent_id=agent_id,
            snapshot=snapshot,
            restore_paths=["workspace/report.md"],
            expected_current_states={"workspace/report.md": current},
            require_lineage=False,
            data_root=tmp_path,
        )

    assert restored.ok is True
    assert report.read_text(encoding="utf-8") == "checkpoint"


@pytest.mark.asyncio
async def test_startup_recovery_accepts_committed_file_version_restore_audit(tmp_path, monkeypatch):
    import app.database as database
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("checkpoint", encoding="utf-8")
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    report.write_text("current", encoding="utf-8")
    restored = snapshots.restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
        defer_finalize=True,
    )
    lease = snapshots._ACTIVE_RESTORE_LEASES.pop(restored.transaction_id)
    lease.release()

    class _Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class _DB:
        def __init__(self):
            self.queries = 0

        async def execute(self, _statement):
            self.queries += 1
            return _Result(uuid4() if self.queries == 2 else None)

        async def rollback(self):
            return None

    class _Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return None

    fake_db = _DB()
    monkeypatch.setattr(database, "async_session", lambda: _Context(fake_db))
    monkeypatch.setattr(database, "enter_rls_bypass", lambda *_args, **_kwargs: _Context(fake_db))

    recovered = await snapshots.recover_workspace_restores_from_transcript(data_root=tmp_path)

    assert fake_db.queries == 2
    assert recovered["committed"] == 1
    assert report.read_text(encoding="utf-8") == "checkpoint"


def test_scoped_workspace_restore_fails_closed_on_interleaved_foreign_writer(tmp_path):
    from app.services.session_workspace_snapshot import (
        capture_workspace_snapshot,
        restore_workspace_snapshot,
        workspace_file_state,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("checkpoint", encoding="utf-8")
    checkpoint_state = workspace_file_state(
        agent_id=agent_id,
        path="workspace/report.md",
        data_root=tmp_path,
    )
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    report.write_text("session-a-first", encoding="utf-8")
    session_first = workspace_file_state(agent_id=agent_id, path="workspace/report.md", data_root=tmp_path)
    report.write_text("foreign-session-b", encoding="utf-8")
    foreign_state = workspace_file_state(agent_id=agent_id, path="workspace/report.md", data_root=tmp_path)
    report.write_text("session-a-final", encoding="utf-8")
    session_final = workspace_file_state(agent_id=agent_id, path="workspace/report.md", data_root=tmp_path)

    restored = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=["workspace/report.md"],
        expected_current_states={"workspace/report.md": session_final},
        expected_lineage={
            "workspace/report.md": [
                {
                    "path": "workspace/report.md",
                    "before_state": checkpoint_state,
                    "after_state": session_first,
                },
                {
                    "path": "workspace/report.md",
                    "before_state": foreign_state,
                    "after_state": session_final,
                },
            ]
        },
        data_root=tmp_path,
    )

    assert restored.ok is False
    assert "lineage diverged" in str(restored.error)
    assert report.read_text(encoding="utf-8") == "session-a-final"


def test_scoped_workspace_restore_is_idempotent_after_same_checkpoint_was_applied(tmp_path):
    from app.services.session_workspace_snapshot import (
        capture_workspace_snapshot,
        restore_workspace_snapshot,
        workspace_file_state,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"
    report.write_text("checkpoint", encoding="utf-8")
    checkpoint_state = workspace_file_state(agent_id=agent_id, path="workspace/report.md", data_root=tmp_path)
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    report.write_text("changed", encoding="utf-8")
    changed_state = workspace_file_state(agent_id=agent_id, path="workspace/report.md", data_root=tmp_path)
    lineage = {
        "workspace/report.md": [
            {
                "path": "workspace/report.md",
                "before_state": checkpoint_state,
                "after_state": changed_state,
            }
        ]
    }
    first = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=["workspace/report.md"],
        expected_current_states={"workspace/report.md": changed_state},
        expected_lineage=lineage,
        data_root=tmp_path,
    )
    second = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=["workspace/report.md"],
        expected_current_states={"workspace/report.md": changed_state},
        expected_lineage=lineage,
        data_root=tmp_path,
    )

    assert first.ok is True
    assert second.ok is True
    assert second.transaction_id is None
    assert second.unchanged_files == ["report.md"]
    assert report.read_text(encoding="utf-8") == "checkpoint"


def test_atomic_workspace_swap_rolls_back_when_install_fails(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    (workspace / "new.md").write_text("keep-on-failure", encoding="utf-8")
    real_replace = os.replace

    def fail_stage_install(source, destination):
        if str(source).endswith("/stage") and str(destination) == str(workspace):
            raise OSError("injected install failure")
        return real_replace(source, destination)

    monkeypatch.setattr(snapshots.os, "replace", fail_stage_install)

    restored = snapshots.restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
    )

    assert restored.ok is False
    assert "injected install failure" in str(restored.error)
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"
    assert (workspace / "new.md").read_text(encoding="utf-8") == "keep-on-failure"


def test_startup_recovery_rolls_back_crash_between_workspace_swaps(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    real_replace = os.replace

    def crash_before_stage_install(source, destination):
        if str(source).endswith("/stage") and str(destination) == str(workspace):
            raise KeyboardInterrupt("injected process crash")
        return real_replace(source, destination)

    monkeypatch.setattr(snapshots.os, "replace", crash_before_stage_install)
    with pytest.raises(KeyboardInterrupt, match="injected process crash"):
        snapshots.restore_workspace_snapshot(
            agent_id=agent_id,
            snapshot=snapshot,
            data_root=tmp_path,
            defer_finalize=True,
        )
    monkeypatch.setattr(snapshots.os, "replace", real_replace)

    recovered = snapshots.recover_workspace_restore_transactions(
        committed_transaction_ids=set(),
        data_root=tmp_path,
    )

    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"
    assert recovered["rolled_back"] == 1
    assert recovered["failed"] == 0


def test_startup_recovery_recognizes_rollback_completed_before_journal_update(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    checkpoint_state = snapshots.workspace_file_state(
        agent_id=agent_id,
        path="workspace/report.md",
        data_root=tmp_path,
    )
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    expected = snapshots.workspace_file_state(
        agent_id=agent_id,
        path="workspace/report.md",
        data_root=tmp_path,
    )
    restored = snapshots.restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        restore_paths=["workspace/report.md"],
        expected_current_states={"workspace/report.md": expected},
        expected_lineage={
            "workspace/report.md": [
                {
                    "path": "workspace/report.md",
                    "before_state": checkpoint_state,
                    "after_state": expected,
                }
            ]
        },
        data_root=tmp_path,
        defer_finalize=True,
    )
    real_atomic_write = snapshots._atomic_write_json

    def crash_before_rollback_journal(path, payload):
        if payload.get("state") == "rolled_back":
            raise KeyboardInterrupt("injected rollback journal crash")
        return real_atomic_write(path, payload)

    monkeypatch.setattr(snapshots, "_atomic_write_json", crash_before_rollback_journal)
    with pytest.raises(KeyboardInterrupt, match="injected rollback journal crash"):
        snapshots.finalize_workspace_restore(
            agent_id=agent_id,
            transaction_id=restored.transaction_id,
            commit=False,
            data_root=tmp_path,
        )
    monkeypatch.setattr(snapshots, "_atomic_write_json", real_atomic_write)

    recovered = snapshots.recover_workspace_restore_transactions(
        committed_transaction_ids=set(),
        data_root=tmp_path,
    )

    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"
    assert recovered["rolled_back"] == 1
    assert recovered["failed"] == 0


def test_workspace_restore_staging_failure_keeps_workspace_and_removes_partial_transaction(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    real_copytree = snapshots.shutil.copytree

    def fail_restore_stage(source, destination, *args, **kwargs):
        if str(destination).endswith("/stage"):
            raise OSError("injected staging failure")
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(snapshots.shutil, "copytree", fail_restore_stage)

    restored = snapshots.restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
    )

    assert restored.ok is False
    assert "injected staging failure" in str(restored.error)
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"
    transactions = tmp_path / str(agent_id) / "runtime_artifacts" / "workspace_transactions"
    assert [path for path in transactions.iterdir() if path.is_dir()] == []


@pytest.mark.parametrize("failure_state", ["prepared", "swapped"])
def test_workspace_restore_journal_write_failure_rolls_back_without_pending_swap(
    tmp_path,
    monkeypatch,
    failure_state,
):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    real_atomic_write = snapshots._atomic_write_json
    injected = False

    def fail_journal_write(path, payload):
        nonlocal injected
        if not injected and payload.get("state") == failure_state:
            injected = True
            raise OSError(f"injected {failure_state} journal disk full")
        return real_atomic_write(path, payload)

    monkeypatch.setattr(snapshots, "_atomic_write_json", fail_journal_write)

    restored = snapshots.restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
        defer_finalize=True,
    )

    assert restored.ok is False
    assert "disk full" in str(restored.error)
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"
    recovered = snapshots.recover_workspace_restore_transactions(
        committed_transaction_ids=set(),
        data_root=tmp_path,
    )
    assert recovered["scanned"] == 0
    assert recovered["failed"] == 0


def test_startup_recovery_removes_prejournal_crash_orphan(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    real_atomic_write = snapshots._atomic_write_json

    def crash_before_prepare_journal(path, payload):
        if payload.get("state") == "prepared":
            raise KeyboardInterrupt("injected prejournal crash")
        return real_atomic_write(path, payload)

    monkeypatch.setattr(snapshots, "_atomic_write_json", crash_before_prepare_journal)
    with pytest.raises(KeyboardInterrupt, match="prejournal crash"):
        snapshots.restore_workspace_snapshot(
            agent_id=agent_id,
            snapshot=snapshot,
            data_root=tmp_path,
        )
    monkeypatch.setattr(snapshots, "_atomic_write_json", real_atomic_write)

    recovered = snapshots.recover_workspace_restore_transactions(
        committed_transaction_ids=set(),
        data_root=tmp_path,
    )

    assert recovered["orphans_removed"] == 1
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v2"


@pytest.mark.parametrize("commit", [True, False])
def test_deferred_workspace_restore_can_commit_or_rollback(tmp_path, commit):
    from app.services.session_workspace_snapshot import (
        capture_workspace_snapshot,
        finalize_workspace_restore,
        restore_workspace_snapshot,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")

    restored = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
        defer_finalize=True,
    )

    assert restored.ok is True
    assert restored.transaction_id
    assert restored.requires_finalize is True
    assert (workspace / "report.md").read_text(encoding="utf-8") == "v1"

    finalized = finalize_workspace_restore(
        agent_id=agent_id,
        transaction_id=restored.transaction_id,
        commit=commit,
        data_root=tmp_path,
    )

    assert finalized is True
    expected = "v1" if commit else "v2"
    assert (workspace / "report.md").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("database_committed", [True, False])
def test_workspace_restore_startup_recovery_resolves_swapped_journal(tmp_path, database_committed):
    from app.services.session_workspace_snapshot import (
        capture_workspace_snapshot,
        recover_workspace_restore_transactions,
        restore_workspace_snapshot,
    )

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("v1", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=uuid4(),
        checkpoint_event_id=uuid4(),
        data_root=tmp_path,
    )
    (workspace / "report.md").write_text("v2", encoding="utf-8")
    restored = restore_workspace_snapshot(
        agent_id=agent_id,
        snapshot=snapshot,
        data_root=tmp_path,
        defer_finalize=True,
    )
    committed_ids = {restored.transaction_id} if database_committed else set()

    recovered = recover_workspace_restore_transactions(
        committed_transaction_ids=committed_ids,
        data_root=tmp_path,
    )

    expected = "v1" if database_committed else "v2"
    assert (workspace / "report.md").read_text(encoding="utf-8") == expected
    assert recovered["committed" if database_committed else "rolled_back"] == 1
    assert recovered["failed"] == 0


@pytest.mark.asyncio
async def test_async_agent_workspace_lock_serializes_mutations(tmp_path):
    from app.services.session_workspace_snapshot import async_agent_workspace_lock

    agent_id = uuid4()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with async_agent_workspace_lock(agent_id, data_root=tmp_path):
            first_entered.set()
            await release_first.wait()

    async def second():
        await first_entered.wait()
        async with async_agent_workspace_lock(agent_id, data_root=tmp_path):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0.1)
    assert second_entered.is_set() is False
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set() is True


def test_session_workspace_snapshot_retention_prunes_oldest_checkpoint(tmp_path, monkeypatch):
    import app.services.session_workspace_snapshot as snapshots

    monkeypatch.setattr(snapshots, "MAX_SESSION_WORKSPACE_SNAPSHOTS", 2)
    agent_id = uuid4()
    session = type("Session", (), {"id": uuid4(), "transcript_metadata_json": {}})()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    checkpoint_ids = [uuid4(), uuid4(), uuid4()]
    captured = []
    for index, checkpoint_id in enumerate(checkpoint_ids):
        (workspace / "report.md").write_text(f"v{index}", encoding="utf-8")
        captured.append(
            snapshots.capture_session_workspace_snapshot(
                agent_id=agent_id,
                session=session,
                checkpoint_event_id=checkpoint_id,
                data_root=tmp_path,
            )
        )

    indexed = session.transcript_metadata_json["workspace_snapshots"]
    assert list(indexed) == [str(checkpoint_ids[1]), str(checkpoint_ids[2])]
    first_manifest = tmp_path / str(agent_id) / captured[0]["manifest_path"]
    assert first_manifest.exists() is False
    assert (tmp_path / str(agent_id) / captured[1]["manifest_path"]).is_file()
    assert (tmp_path / str(agent_id) / captured[2]["manifest_path"]).is_file()


def test_branch_snapshot_clone_is_independent_from_source_retention(tmp_path):
    import app.services.session_workspace_snapshot as snapshots

    agent_id = uuid4()
    source_session_id = uuid4()
    source_checkpoint_id = uuid4()
    target_session_id = uuid4()
    target_checkpoint_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("branch-safe", encoding="utf-8")
    source = snapshots.capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=source_session_id,
        checkpoint_event_id=source_checkpoint_id,
        data_root=tmp_path,
    )

    cloned = snapshots.clone_workspace_snapshot_for_session(
        agent_id=agent_id,
        source_snapshot=source,
        target_session_id=target_session_id,
        target_checkpoint_event_id=target_checkpoint_id,
        data_root=tmp_path,
    )

    source_manifest = tmp_path / str(agent_id) / source["manifest_path"]
    cloned_manifest = tmp_path / str(agent_id) / cloned["manifest_path"]
    assert cloned_manifest != source_manifest
    assert cloned_manifest.is_file()
    clone_payload = __import__("json").loads(cloned_manifest.read_text(encoding="utf-8"))
    assert clone_payload["session_id"] == str(target_session_id)
    assert clone_payload["checkpoint_event_id"] == str(target_checkpoint_id)
    assert (cloned_manifest.parent / "files" / "report.md").read_text(encoding="utf-8") == "branch-safe"
    snapshots._remove_path(source_manifest.parent)
    assert (cloned_manifest.parent / "files" / "report.md").is_file()
