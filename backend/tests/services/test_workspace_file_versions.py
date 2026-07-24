from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _session_with_snapshots(session_id, snapshots):
    return SimpleNamespace(
        id=session_id,
        transcript_metadata_json={"workspace_snapshots": snapshots},
    )


def test_workspace_file_versions_project_checkpoint_content_and_deletion(tmp_path):
    from app.services.session_workspace_snapshot import capture_workspace_snapshot
    from app.services.workspace_file_versions import (
        collect_workspace_file_versions,
        read_workspace_file_version,
    )

    agent_id = uuid4()
    session_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    report = workspace / "report.md"

    checkpoint_v1 = uuid4()
    report.write_text("first version", encoding="utf-8")
    snapshot_v1 = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_v1,
        data_root=tmp_path,
    )

    checkpoint_v2 = uuid4()
    report.write_text("second version", encoding="utf-8")
    snapshot_v2 = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_v2,
        data_root=tmp_path,
    )

    checkpoint_deleted = uuid4()
    report.unlink()
    snapshot_deleted = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_deleted,
        data_root=tmp_path,
    )

    session = _session_with_snapshots(
        session_id,
        {
            str(checkpoint_v1): snapshot_v1,
            str(checkpoint_v2): snapshot_v2,
            str(checkpoint_deleted): snapshot_deleted,
        },
    )

    versions = collect_workspace_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        sessions=[session],
        data_root=tmp_path,
    )

    assert [item.state for item in versions] == ["deleted", "available", "available"]
    assert versions[0].restorable is True
    assert versions[1].content_hash
    assert str(session_id) not in versions[1].version_id
    assert str(checkpoint_v2) not in versions[1].version_id
    assert len(versions[1].version_id) == 40

    content = read_workspace_file_version(
        agent_id=agent_id,
        path="workspace/report.md",
        version_id=versions[1].version_id,
        sessions=[session],
        data_root=tmp_path,
    )
    assert content.content == b"second version"
    assert content.content_hash == versions[1].content_hash


def test_workspace_file_versions_expose_tampering_as_unavailable(tmp_path):
    from app.services.session_workspace_snapshot import capture_workspace_snapshot
    from app.services.workspace_file_versions import (
        WorkspaceFileVersionUnavailable,
        collect_workspace_file_versions,
        read_workspace_file_version,
    )

    agent_id = uuid4()
    session_id = uuid4()
    checkpoint_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.md").write_text("verified", encoding="utf-8")
    snapshot = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_id,
        data_root=tmp_path,
    )
    snapshot_file = tmp_path / str(agent_id) / snapshot["manifest_path"]
    (snapshot_file.parent / "files" / "report.md").write_text("tampered", encoding="utf-8")
    session = _session_with_snapshots(session_id, {str(checkpoint_id): snapshot})

    versions = collect_workspace_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        sessions=[session],
        data_root=tmp_path,
    )

    assert len(versions) == 1
    assert versions[0].state == "unavailable"
    assert versions[0].restorable is False
    with pytest.raises(WorkspaceFileVersionUnavailable):
        read_workspace_file_version(
            agent_id=agent_id,
            path="workspace/report.md",
            version_id=versions[0].version_id,
            sessions=[session],
            data_root=tmp_path,
        )


def test_workspace_file_versions_omit_checkpoints_before_file_existed(tmp_path):
    from app.services.session_workspace_snapshot import capture_workspace_snapshot
    from app.services.workspace_file_versions import collect_workspace_file_versions

    agent_id = uuid4()
    session_id = uuid4()
    workspace = tmp_path / str(agent_id) / "workspace"
    workspace.mkdir(parents=True)

    checkpoint_absent = uuid4()
    snapshot_absent = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_absent,
        data_root=tmp_path,
    )
    checkpoint_present = uuid4()
    (workspace / "report.md").write_text("created later", encoding="utf-8")
    snapshot_present = capture_workspace_snapshot(
        agent_id=agent_id,
        session_id=session_id,
        checkpoint_event_id=checkpoint_present,
        data_root=tmp_path,
    )
    session = _session_with_snapshots(
        session_id,
        {
            str(checkpoint_absent): snapshot_absent,
            str(checkpoint_present): snapshot_present,
        },
    )

    versions = collect_workspace_file_versions(
        agent_id=agent_id,
        path="workspace/report.md",
        sessions=[session],
        data_root=tmp_path,
    )

    assert [item.state for item in versions] == ["available"]
