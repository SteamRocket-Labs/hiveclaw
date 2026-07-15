from __future__ import annotations

import json

import pytest


def _legacy_path(data_root, agent_id: str, *, workspace: bool = False):
    relative = "workspace/recovery_manifest.json" if workspace else "runtime_artifacts/recovery_manifest.json"
    path = data_root / agent_id / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_legacy_repair_dry_run_is_read_only_and_reports_classification(tmp_path) -> None:
    from app.runtime.recovery_manifest_legacy_repair import repair_legacy_recovery_manifests

    unsigned = _legacy_path(tmp_path, "agent-a")
    corrupt = _legacy_path(tmp_path, "agent-b", workspace=True)
    unsigned.write_text(json.dumps({"session_id": "old-session", "pending_items": ["keep"]}), encoding="utf-8")
    corrupt.write_bytes(b'{"partial":')

    report = repair_legacy_recovery_manifests(tmp_path, apply=False)

    assert report.to_payload() == {
        "schema": "hive.recovery_manifest_legacy_repair.v1",
        "mode": "dry_run",
        "scanned": 2,
        "would_quarantine": 2,
        "quarantined": 0,
        "by_reason": {
            "corrupt_json": 1,
            "legacy_authority_unverifiable": 1,
        },
    }
    assert unsigned.exists() and corrupt.exists()
    assert not list(tmp_path.rglob("recovery_manifests/quarantine/*.json"))


def test_legacy_repair_apply_quarantines_exact_bytes_and_is_idempotent(tmp_path) -> None:
    from app.runtime.recovery_manifest_legacy_repair import repair_legacy_recovery_manifests

    first = _legacy_path(tmp_path, "agent-a")
    second = _legacy_path(tmp_path, "agent-b", workspace=True)
    first_bytes = json.dumps({"session_id": "old-a", "permission_profile": {"mode": "full_access"}}).encode()
    second_bytes = b'{"partial":'
    first.write_bytes(first_bytes)
    second.write_bytes(second_bytes)

    report = repair_legacy_recovery_manifests(tmp_path, apply=True)

    assert report.mode == "apply"
    assert report.scanned == 2
    assert report.quarantined == 2
    assert not first.exists() and not second.exists()
    quarantined = sorted(tmp_path.rglob("recovery_manifests/quarantine/*.json"))
    assert len(quarantined) == 2
    assert {path.read_bytes() for path in quarantined} == {first_bytes, second_bytes}

    rerun = repair_legacy_recovery_manifests(tmp_path, apply=True)
    assert rerun.scanned == 0
    assert rerun.quarantined == 0


def test_legacy_repair_never_follows_agent_root_symlinks(tmp_path) -> None:
    from app.runtime.recovery_manifest_legacy_repair import repair_legacy_recovery_manifests

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    outside_manifest = _legacy_path(outside, "agent-outside")
    outside_manifest.write_text("{}", encoding="utf-8")
    (tmp_path / "linked-agent").symlink_to(outside / "agent-outside", target_is_directory=True)

    report = repair_legacy_recovery_manifests(tmp_path, apply=True)

    assert report.scanned == 0
    assert outside_manifest.exists()


def test_legacy_repair_rejects_symlinked_quarantine_destination(tmp_path) -> None:
    from app.runtime.recovery_manifest_legacy_repair import repair_legacy_recovery_manifests

    legacy = _legacy_path(tmp_path, "agent-a")
    original = b'{"session_id":"legacy"}'
    legacy.write_bytes(original)
    outside = tmp_path.parent / f"{tmp_path.name}-quarantine-outside"
    outside.mkdir()
    quarantine_parent = legacy.parent / "recovery_manifests"
    quarantine_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="quarantine"):
        repair_legacy_recovery_manifests(tmp_path, apply=True)

    assert legacy.read_bytes() == original
    assert not list(outside.rglob("*.json"))


def test_legacy_repair_cli_requires_explicit_apply_confirmation(tmp_path, capsys) -> None:
    from app.scripts.repair_recovery_manifest_authority import main

    legacy = _legacy_path(tmp_path, "agent-a")
    legacy.write_text("{}", encoding="utf-8")

    assert main(["--data-root", str(tmp_path)]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["mode"] == "dry_run"
    assert legacy.exists()

    with pytest.raises(SystemExit):
        main(["--data-root", str(tmp_path), "--apply"])
    assert legacy.exists()

    assert main(["--data-root", str(tmp_path), "--apply", "--confirm"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["mode"] == "apply"
    assert applied["quarantined"] == 1
    assert not legacy.exists()


def test_workspace_bootstrap_quarantines_legacy_manifest_instead_of_recreating_singleton(tmp_path) -> None:
    from app.tools.workspace import _migrate_workspace_runtime_artifacts

    agent_root = tmp_path / "agent-a"
    legacy = _legacy_path(tmp_path, "agent-a", workspace=True)
    original = b'{"session_id":"legacy-session","pending_items":["preserve"]}'
    legacy.write_bytes(original)

    moved = _migrate_workspace_runtime_artifacts(agent_root)

    assert not legacy.exists()
    assert not (agent_root / "runtime_artifacts" / "recovery_manifest.json").exists()
    quarantined = list(agent_root.glob("runtime_artifacts/recovery_manifests/quarantine/*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == original
    assert moved == ["workspace/recovery_manifest.json->runtime_artifacts/recovery_manifests/quarantine/"]
