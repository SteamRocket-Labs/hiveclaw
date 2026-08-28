from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _write_t0_index(
    data_root: Path,
    agent_id: object,
    session_id: str,
    *,
    sealed_segments: tuple[str, ...],
    open_segments: tuple[str, ...] = (),
) -> None:
    session_dir = data_root / str(agent_id) / "memory" / "t0" / "sessions" / session_id
    segments: list[dict[str, object]] = []
    for segment_id, state in [
        *((segment_id, "sealed") for segment_id in sealed_segments),
        *((segment_id, "open") for segment_id in open_segments),
    ]:
        segment_dir = session_dir / "segments" / segment_id
        segment_dir.mkdir(parents=True, exist_ok=True)
        (segment_dir / "events.jsonl").write_text('{"schema_version":"t0.event-record.v2"}\n', encoding="utf-8")
        (segment_dir / "source.md").write_text("<t0_session_segment />\n", encoding="utf-8")
        segments.append(
            {
                "segment_id": segment_id,
                "state": state,
                "path": f"segments/{segment_id}/source.md",
                "events_path": f"segments/{segment_id}/events.jsonl",
            }
        )
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "index.json").write_text(
        json.dumps(
            {
                "schema_version": "t0.session-ledger.v1",
                "agent_id": str(agent_id),
                "session_id": session_id,
                "segments": segments,
            }
        ),
        encoding="utf-8",
    )


def _write_t2_manifest(
    data_root: Path,
    agent_id: object,
    session_id: str,
    segment_id: str,
    *,
    status: str = "reviewed",
) -> None:
    package_dir = data_root / str(agent_id) / "memory" / "t2" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "summary.md").write_text("<t2_summary />\n", encoding="utf-8")
    (package_dir / "labels.md").write_text("<t2_labels />\n", encoding="utf-8")
    (package_dir / "review.md").write_text("<t2_review />\n", encoding="utf-8")
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_status": status,
                "session_id": session_id,
                "t0_segment_id": segment_id,
                "source_refs": [f"t0://session/{session_id}/segment/{segment_id}#seq=1..2"],
            }
        ),
        encoding="utf-8",
    )


def test_t2_backfill_inventory_is_complete_and_never_treats_open_t0_as_memory(tmp_path: Path) -> None:
    from app.scripts.backfill_t0_to_t2 import inventory_t0_to_t2_backfill

    agent_id = uuid4()
    _write_t0_index(
        tmp_path,
        agent_id,
        "session-a",
        sealed_segments=("already-packaged", "retired-package", "invalid-package", "missing-a", "missing-b"),
        open_segments=("still-open",),
    )
    _write_t2_manifest(tmp_path, agent_id, "session-a", "already-packaged")
    _write_t2_manifest(tmp_path, agent_id, "session-a", "retired-package", status="retired")
    invalid_manifest = (
        tmp_path
        / str(agent_id)
        / "memory"
        / "t2"
        / "sessions"
        / "session-a"
        / "segments"
        / "invalid-package"
        / "manifest.json"
    )
    invalid_manifest.parent.mkdir(parents=True, exist_ok=True)
    invalid_manifest.write_text("{not-json", encoding="utf-8")

    report = inventory_t0_to_t2_backfill(
        data_root=tmp_path,
        agent_id=agent_id,
        limit_segments=None,
    )

    assert report["sealed_segments"] == 5
    assert report["existing_t2_packages"] == 2
    assert report["invalid_t2_packages"] == 1
    assert report["candidate_segments"] == 2
    assert report["selected_segments"] == 2
    assert report["remaining_segments"] == 0
    assert report["batch_selection_complete"] is True
    assert report["coverage_complete"] is False
    assert report["open_segments_skipped"] == 1
    assert report["warnings"] == [
        {
            "session_id": "session-a",
            "segment_id": "invalid-package",
            "reason": "invalid_existing_t2_package",
        }
    ]
    assert report["candidates"] == [
        {"session_id": "session-a", "segment_id": "missing-a"},
        {"session_id": "session-a", "segment_id": "missing-b"},
    ]


def test_t2_backfill_inventory_only_reports_complete_for_valid_existing_packages(tmp_path: Path) -> None:
    from app.scripts.backfill_t0_to_t2 import inventory_t0_to_t2_backfill

    agent_id = uuid4()
    _write_t0_index(tmp_path, agent_id, "session-a", sealed_segments=("already-packaged",))
    _write_t2_manifest(tmp_path, agent_id, "session-a", "already-packaged")

    report = inventory_t0_to_t2_backfill(
        data_root=tmp_path,
        agent_id=agent_id,
        limit_segments=None,
    )

    assert report["candidate_segments"] == 0
    assert report["remaining_segments"] == 0
    assert report["warnings"] == []
    assert report["batch_selection_complete"] is True
    assert report["coverage_complete"] is True


def test_t2_backfill_treats_non_promotion_terminal_packages_as_existing(tmp_path: Path) -> None:
    from app.scripts.backfill_t0_to_t2 import inventory_t0_to_t2_backfill

    agent_id = uuid4()
    _write_t0_index(
        tmp_path,
        agent_id,
        "session-a",
        sealed_segments=("archived-package", "rejected-package"),
    )
    _write_t2_manifest(tmp_path, agent_id, "session-a", "archived-package", status="archived_recall_only")
    _write_t2_manifest(tmp_path, agent_id, "session-a", "rejected-package", status="rejected")

    report = inventory_t0_to_t2_backfill(
        data_root=tmp_path,
        agent_id=agent_id,
        limit_segments=None,
    )

    assert report["sealed_segments"] == 2
    assert report["existing_t2_packages"] == 2
    assert report["invalid_t2_packages"] == 0
    assert report["candidate_segments"] == 0
    assert report["coverage_complete"] is True


@pytest.mark.asyncio
async def test_t2_backfill_dry_run_has_zero_mutation(tmp_path: Path, monkeypatch) -> None:
    from app.scripts import backfill_t0_to_t2

    agent_id = uuid4()
    tenant_id = uuid4()
    _write_t0_index(tmp_path, agent_id, "session-a", sealed_segments=("segment-a",))

    async def reject_runner(**_kwargs):
        raise AssertionError("dry-run must not start a T2 job")

    monkeypatch.setattr(backfill_t0_to_t2, "run_t2_segment_package_job", reject_runner)

    report = await backfill_t0_to_t2.run_t0_to_t2_backfill(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        apply=False,
    )

    assert report["mode"] == "dry_run"
    assert report["candidate_segments"] == 1
    assert report["started"] == 0
    assert report["batch_selection_complete"] is True
    assert report["coverage_complete"] is False
    assert not (
        tmp_path
        / str(agent_id)
        / "memory"
        / "t2"
        / "sessions"
        / "session-a"
        / "segments"
        / "segment-a"
        / "manifest.json"
    ).exists()


@pytest.mark.asyncio
async def test_t2_backfill_apply_requires_exact_confirmation_and_uses_canonical_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.scripts import backfill_t0_to_t2

    agent_id = uuid4()
    tenant_id = uuid4()
    _write_t0_index(tmp_path, agent_id, "session-a", sealed_segments=("segment-a", "segment-b"))
    calls: list[dict[str, object]] = []

    async def fake_runner(**kwargs):
        calls.append(kwargs)
        status = "held" if kwargs["t0_segment_id"] == "segment-b" else "committed"
        issues = ("summary model unavailable",) if status == "held" else ()
        return SimpleNamespace(status=status, issues=issues)

    monkeypatch.setattr(backfill_t0_to_t2, "run_t2_segment_package_job", fake_runner)

    with pytest.raises(ValueError, match="exact confirmation"):
        await backfill_t0_to_t2.run_t0_to_t2_backfill(
            data_root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            apply=True,
            confirmation="yes",
        )

    report = await backfill_t0_to_t2.run_t0_to_t2_backfill(
        data_root=tmp_path,
        agent_id=agent_id,
        tenant_id=tenant_id,
        apply=True,
        confirmation="APPLY_T0_TO_T2_BACKFILL",
    )

    assert report["mode"] == "apply"
    assert report["started"] == 2
    assert report["committed"] == 1
    assert report["held"] == 1
    assert report["failed"] == 0
    assert report["batch_selection_complete"] is True
    assert report["coverage_complete"] is False
    assert report["outcomes"] == [
        {"session_id": "session-a", "segment_id": "segment-a", "status": "committed", "issues": []},
        {
            "session_id": "session-a",
            "segment_id": "segment-b",
            "status": "held",
            "issues": ["summary model unavailable"],
        },
    ]
    assert [
        {
            "agent_id": call["agent_id"],
            "tenant_id": call["tenant_id"],
            "session_id": call["session_id"],
            "t0_segment_id": call["t0_segment_id"],
            "data_root": call["data_root"],
        }
        for call in calls
    ] == [
        {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "session_id": "session-a",
            "t0_segment_id": "segment-a",
            "data_root": tmp_path,
        },
        {
            "agent_id": agent_id,
            "tenant_id": tenant_id,
            "session_id": "session-a",
            "t0_segment_id": "segment-b",
            "data_root": tmp_path,
        },
    ]


@pytest.mark.asyncio
async def test_t2_backfill_cli_initializes_secrets_before_running_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.scripts import backfill_t0_to_t2

    agent_id = uuid4()
    tenant_id = uuid4()
    settings = SimpleNamespace(
        AGENT_DATA_DIR=str(tmp_path),
        DEBUG=False,
        SECRETS_MASTER_KEY="primary-key",
        SECRETS_MASTER_KEY_PREVIOUS="previous-a, previous-b",
    )
    events: list[str] = []

    def fake_init_script_secrets_provider(actual_settings: object) -> None:
        assert actual_settings is settings
        events.append("secrets_initialized")

    async def fake_run(**kwargs):
        assert events == ["secrets_initialized"]
        assert kwargs["data_root"] == Path(settings.AGENT_DATA_DIR)
        return {"schema": "hive.t0-to-t2-backfill.v1", "mode": "dry_run"}

    monkeypatch.setattr(backfill_t0_to_t2, "get_settings", lambda: settings)
    monkeypatch.setattr(backfill_t0_to_t2, "_init_script_secrets_provider", fake_init_script_secrets_provider)
    monkeypatch.setattr(backfill_t0_to_t2, "run_t0_to_t2_backfill", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backfill_t0_to_t2.py",
            "--agent-id",
            str(agent_id),
            "--tenant-id",
            str(tenant_id),
        ],
    )

    await backfill_t0_to_t2._main()

    assert events == ["secrets_initialized"]


def test_t2_backfill_script_secrets_match_runtime_rotation_contract(monkeypatch) -> None:
    from app.scripts import backfill_t0_to_t2
    from app.services import secrets_provider

    calls: list[tuple[object, ...]] = []
    settings = SimpleNamespace(
        DEBUG=False,
        SECRETS_MASTER_KEY="primary-key",
        SECRETS_MASTER_KEY_PREVIOUS=" previous-a, ,previous-b ",
    )

    monkeypatch.setattr(
        secrets_provider,
        "validate_secrets_provider_config",
        lambda key, *, debug: calls.append(("validate", key, debug)),
    )
    monkeypatch.setattr(
        secrets_provider,
        "init_secrets_provider",
        lambda key, *, previous_master_keys: calls.append(("init", key, previous_master_keys)),
    )

    backfill_t0_to_t2._init_script_secrets_provider(settings)

    assert calls == [
        ("validate", "primary-key", False),
        ("init", "primary-key", ("previous-a", "previous-b")),
    ]
