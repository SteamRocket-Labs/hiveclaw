from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _create_legacy_transaction(data_root: Path, *, agent_id: str, operation: str = "active_skill_package_install"):
    from app.services.agent_asset_transaction import AgentAssetTransaction

    agent_root = data_root / agent_id
    with AgentAssetTransaction(agent_root, operation=operation, requires_projection=True) as transaction:
        transaction.stage_text("skills/example/SKILL.md", "# Example\n")
        receipt = transaction.commit()
    journal = json.loads(receipt.journal_path.read_text(encoding="utf-8"))
    journal.pop("lifecycle_state", None)
    journal.pop("retention_class", None)
    journal.pop("rollback_deadline", None)
    journal.pop("payload_gc_at", None)
    journal["committed_at"] = "2026-01-01T00:00:00+00:00"
    receipt.journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def test_inventory_and_dry_run_are_zero_mutation_and_tenant_bound(tmp_path: Path) -> None:
    from app.services.storage_lifecycle import build_transaction_backfill_manifest, inventory_storage

    data_root = tmp_path / "agents"
    owned = "11111111-1111-4111-8111-111111111111"
    unowned = "22222222-2222-4222-8222-222222222222"
    _create_legacy_transaction(data_root, agent_id=owned)
    _create_legacy_transaction(data_root, agent_id=unowned)
    before = _tree_digest(data_root)

    inventory = inventory_storage(data_root, agent_tenants={owned: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"})
    manifest = build_transaction_backfill_manifest(
        data_root,
        agent_tenants={owned: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )

    assert inventory["transactions"]["count"] == 2
    assert len(manifest["candidates"]) == 1
    assert manifest["candidates"][0]["agent_id"] == owned
    assert any(item["reason"] == "agent_tenant_authority_missing" for item in manifest["holds"])
    assert manifest["manifest_sha256"]
    assert _tree_digest(data_root) == before


def test_backfill_and_gc_require_exact_manifest_hash(tmp_path: Path) -> None:
    from app.services.storage_lifecycle import (
        StorageManifestMismatchError,
        apply_transaction_backfill,
        build_transaction_backfill_manifest,
    )

    data_root = tmp_path / "agents"
    agent_id = "11111111-1111-4111-8111-111111111111"
    receipt = _create_legacy_transaction(data_root, agent_id=agent_id)
    manifest = build_transaction_backfill_manifest(
        data_root,
        agent_tenants={agent_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        now=datetime(2026, 7, 15, tzinfo=UTC),
    )

    with pytest.raises(StorageManifestMismatchError):
        apply_transaction_backfill(data_root, manifest, expected_manifest_sha256="wrong")

    report = apply_transaction_backfill(
        data_root,
        manifest,
        expected_manifest_sha256=manifest["manifest_sha256"],
    )
    journal = json.loads(receipt.journal_path.read_text(encoding="utf-8"))
    assert report["processed_count"] == 1
    assert journal["lifecycle_state"] == "finalized"
    assert journal["tenant_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_gc_quarantine_rechecks_pin_and_restore_is_idempotent(tmp_path: Path) -> None:
    from app.services.storage_lifecycle import (
        apply_transaction_backfill,
        apply_transaction_quarantine,
        build_transaction_backfill_manifest,
        build_transaction_gc_manifest,
        restore_transaction_quarantine,
    )

    data_root = tmp_path / "agents"
    agent_id = "11111111-1111-4111-8111-111111111111"
    receipt = _create_legacy_transaction(data_root, agent_id=agent_id)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    backfill = build_transaction_backfill_manifest(
        data_root,
        agent_tenants={agent_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        now=now,
    )
    apply_transaction_backfill(data_root, backfill, expected_manifest_sha256=backfill["manifest_sha256"])
    gc_manifest = build_transaction_gc_manifest(data_root, now=now)
    assert len(gc_manifest["candidates"]) == 1

    journal = json.loads(receipt.journal_path.read_text(encoding="utf-8"))
    journal["pinned_until"] = (now + timedelta(days=1)).isoformat()
    receipt.journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n", encoding="utf-8")
    skipped = apply_transaction_quarantine(
        data_root,
        gc_manifest,
        expected_manifest_sha256=gc_manifest["manifest_sha256"],
        now=now,
    )
    assert skipped["processed_count"] == 0
    assert skipped["skipped"][0]["reason"] == "candidate_changed"

    journal["pinned_until"] = None
    receipt.journal_path.write_text(json.dumps(journal, sort_keys=True) + "\n", encoding="utf-8")
    gc_manifest = build_transaction_gc_manifest(data_root, now=now)
    quarantined = apply_transaction_quarantine(
        data_root,
        gc_manifest,
        expected_manifest_sha256=gc_manifest["manifest_sha256"],
        now=now,
        grace=timedelta(hours=1),
    )
    assert quarantined["processed_count"] == 1
    assert not (receipt.journal_path.parent / "stage").exists()
    assert not (receipt.journal_path.parent / "backups").exists()

    restored = restore_transaction_quarantine(data_root, run_id=quarantined["run_id"])
    replay = restore_transaction_quarantine(data_root, run_id=quarantined["run_id"])
    assert restored["processed_count"] == 1
    assert replay["processed_count"] == 0
    assert (receipt.journal_path.parent / "stage").exists()


def test_sweep_keeps_journal_and_is_idempotent_after_grace(tmp_path: Path) -> None:
    from app.services.storage_lifecycle import (
        apply_transaction_backfill,
        apply_transaction_quarantine,
        apply_transaction_sweep,
        build_transaction_backfill_manifest,
        build_transaction_gc_manifest,
        build_transaction_sweep_manifest,
    )

    data_root = tmp_path / "agents"
    agent_id = "11111111-1111-4111-8111-111111111111"
    receipt = _create_legacy_transaction(data_root, agent_id=agent_id)
    now = datetime(2026, 7, 15, tzinfo=UTC)
    backfill = build_transaction_backfill_manifest(
        data_root,
        agent_tenants={agent_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        now=now,
    )
    apply_transaction_backfill(data_root, backfill, expected_manifest_sha256=backfill["manifest_sha256"])
    gc_manifest = build_transaction_gc_manifest(data_root, now=now)
    quarantined = apply_transaction_quarantine(
        data_root,
        gc_manifest,
        expected_manifest_sha256=gc_manifest["manifest_sha256"],
        now=now,
        grace=timedelta(hours=1),
    )

    early = build_transaction_sweep_manifest(data_root, now=now + timedelta(minutes=59))
    assert early["candidates"] == []
    sweep = build_transaction_sweep_manifest(data_root, now=now + timedelta(hours=1))
    report = apply_transaction_sweep(
        data_root,
        sweep,
        expected_manifest_sha256=sweep["manifest_sha256"],
        now=now + timedelta(hours=1),
    )
    replay = apply_transaction_sweep(
        data_root,
        sweep,
        expected_manifest_sha256=sweep["manifest_sha256"],
        now=now + timedelta(hours=1),
    )

    assert report["processed_count"] == 1
    assert replay["processed_count"] == 0
    assert receipt.journal_path.exists()
    journal = json.loads(receipt.journal_path.read_text(encoding="utf-8"))
    assert journal["payload_state"] == "deleted"
    assert not (data_root / ".storage_lifecycle" / "quarantine" / quarantined["run_id"]).exists()
