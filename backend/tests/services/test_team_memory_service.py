from __future__ import annotations

from pathlib import Path

import pytest


def test_team_memory_store_upserts_lists_and_searches_entries(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)

    entry = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Use the canary rollout first, then verify logs before promoting globally.",
    )

    listed = store.list_entries("tenant-1", "workspace-alpha")
    search_results = store.search_entries("tenant-1", "workspace-alpha", "canary rollout")

    assert entry.key == "deploy-playbook"
    assert entry.title == "Deploy Playbook"
    assert len(listed) == 1
    assert listed[0].path.endswith("deploy-playbook.md")
    assert [result.key for result in search_results] == ["deploy-playbook"]
    assert "canary rollout" in search_results[0].snippet


def test_team_memory_store_append_mode_preserves_existing_content(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="review-checklist",
        title="Review Checklist",
        content="Check diff size.",
    )

    updated = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="review-checklist",
        title="Review Checklist",
        content="Verify rollback notes.",
        mode="append",
    )

    content = Path(updated.absolute_path).read_text(encoding="utf-8")
    assert "Check diff size." in content
    assert "Verify rollback notes." in content


def test_team_memory_store_rejects_secret_like_content(tmp_path: Path) -> None:
    from app.services.team_memory import SecretScanError, TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)

    with pytest.raises(SecretScanError):
        store.upsert_entry(
            tenant_id="tenant-1",
            workspace_key="workspace-alpha",
            key="unsafe",
            title="Unsafe",
            content="OPENAI_API_KEY=sk-live-abcdefghijklmnopqrstuvwxyz123456",
        )


def test_team_memory_store_can_read_full_entry_content(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Step 1: canary rollout.\nStep 2: verify logs.\nStep 3: promote globally.",
    )

    entry = store.get_entry("tenant-1", "workspace-alpha", "deploy-playbook")

    assert entry is not None
    assert entry.content is not None
    assert "verify logs" in entry.content


def test_team_memory_store_prevents_workspace_path_escape(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)

    entry = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="..",
        key="..",
        title="Escaped",
        content="This must stay under the shared memory root.",
    )

    resolved = Path(entry.absolute_path).resolve()
    assert resolved.is_relative_to(tmp_path.resolve())
    assert "shared_memory" in resolved.parts
    assert ".." not in resolved.parts


def test_team_memory_store_tolerates_malformed_frontmatter(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    broken_path = tmp_path / "shared_memory" / "tenant-1" / "workspace-alpha" / "broken.md"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text("---\nkey: broken\ntitle: Broken Entry\n# Missing closing fence", encoding="utf-8")

    entries = store.list_entries("tenant-1", "workspace-alpha")

    assert len(entries) == 1
    assert entries[0].key == "broken"
    assert "Missing closing fence" in entries[0].snippet


def test_team_memory_store_rejects_unknown_mode(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)

    with pytest.raises(ValueError, match="mode"):
        store.upsert_entry(
            tenant_id="tenant-1",
            workspace_key="workspace-alpha",
            key="deploy-playbook",
            title="Deploy Playbook",
            content="Use the canary rollout first.",
            mode="apend",
        )


def test_team_memory_store_delete_entry_reports_deleted_state(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Delete me.",
    )

    assert store.delete_entry("tenant-1", "workspace-alpha", "deploy-playbook", updated_by="admin-user") is True
    assert store.delete_entry("tenant-1", "workspace-alpha", "deploy-playbook", updated_by="admin-user") is False
    assert store.get_entry("tenant-1", "workspace-alpha", "deploy-playbook") is None
    deleted_entry = store.get_entry("tenant-1", "workspace-alpha", "deploy-playbook", include_deleted=True)
    assert deleted_entry is not None
    assert deleted_entry.revision == 2


def test_team_memory_store_tracks_revision_checksum_and_updated_by(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    entry = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Use the canary rollout first.",
        updated_by="user-1",
    )
    updated = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Use the canary rollout first.\nVerify rollback notes.",
        updated_by="user-2",
        base_revision=1,
    )

    assert entry.revision == 1
    assert entry.checksum
    assert entry.updated_by == "user-1"
    assert updated.revision == 2
    assert updated.updated_by == "user-2"
    assert updated.checksum != entry.checksum


def test_team_memory_store_rejects_conflicting_base_revision(tmp_path: Path) -> None:
    import pytest

    from app.services.team_memory import TeamMemoryConflictError, TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Initial content.",
        updated_by="user-1",
    )

    with pytest.raises(TeamMemoryConflictError) as exc_info:
        store.upsert_entry(
            tenant_id="tenant-1",
            workspace_key="workspace-alpha",
            key="deploy-playbook",
            title="Deploy Playbook",
            content="Overwriting stale content.",
            updated_by="user-2",
            base_revision=0,
        )

    assert exc_info.value.current_entry.key == "deploy-playbook"
    assert exc_info.value.current_entry.revision == 1


def test_team_memory_store_exposes_sync_token_and_rotates_it_on_update_and_delete(tmp_path: Path) -> None:
    from app.services.team_memory import TeamMemoryStore

    store = TeamMemoryStore(data_root=tmp_path)
    created = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Initial content.",
        updated_by="user-1",
    )
    updated = store.upsert_entry(
        tenant_id="tenant-1",
        workspace_key="workspace-alpha",
        key="deploy-playbook",
        title="Deploy Playbook",
        content="Initial content.\nAdd rollback checks.",
        updated_by="user-2",
        sync_token=created.sync_token,
    )
    assert store.delete_entry(
        "tenant-1",
        "workspace-alpha",
        "deploy-playbook",
        updated_by="user-3",
        sync_token=updated.sync_token,
    ) is True

    deleted = store.get_entry("tenant-1", "workspace-alpha", "deploy-playbook", include_deleted=True)

    assert created.sync_token
    assert updated.sync_token
    assert deleted is not None
    assert deleted.sync_token
    assert created.sync_token != updated.sync_token
    assert updated.sync_token != deleted.sync_token
    assert deleted.deleted is True
