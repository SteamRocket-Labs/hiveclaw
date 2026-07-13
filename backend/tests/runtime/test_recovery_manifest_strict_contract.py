from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.recovery_manifest import (
    load_and_hydrate_recovery_manifest,
    load_recovery_manifest,
    persist_recovery_manifest,
    recovery_manifest_path,
)
from app.runtime.session import SessionContext


def _authority_metadata(**overrides):
    metadata = {
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-1",
        "claim_version": 1,
        "claim_worker_id": "worker-1",
    }
    metadata.update(overrides)
    return metadata


def _assert_final_canonical_writer_guard_rejects(tmp_path: Path, missing_field: str) -> None:
    import app.runtime.recovery_manifest as recovery

    # Intentionally use a hand-written partial payload: the final guard is
    # path-based and must not depend on callers supplying dataclass marker keys.
    payload = {
        "session_id": "writer-inventory-session",
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "runtime_task_id": "writer-inventory-run",
        "claim_version": 1,
        "claim_worker_id": "worker-1",
    }
    if missing_field == "agent_id":
        payload["legacy_conflict"] = {"reason": "legacy sources diverged"}
    elif missing_field == "session_id":
        payload["blocked_patterns"] = ["do not retry a prior unsafe action"]
    else:
        payload["pending_tool_frames"] = [{"tool_call_id": "call-1", "tool_name": "read_file", "status": "running"}]
    payload.pop(missing_field)
    path = recovery_manifest_path(
        "agent-1",
        session_id="writer-inventory-session",
        runtime_task_id="writer-inventory-run",
        data_root=tmp_path,
    )

    with pytest.raises(ValueError, match="authority identity is incomplete"):
        recovery._atomic_write_manifest(path, payload)

    assert not path.exists()


@pytest.mark.parametrize(
    ("raw_frames", "reason"),
    [
        ([{"tool_call_id": "call-1", "tool_name": "send_email", "status": "executing"}], "unknown_status"),
        ([{"tool_call_id": "call-1", "status": "running"}], "missing_tool_name"),
        ({"tool_call_id": "call-1", "tool_name": "send_email", "status": "running"}, "invalid_container"),
        (["not-a-frame"], "non_object_frame"),
        ([{"tool_call_id": "call-1", "tool_name": "send_email", "status": "completed"}], "terminal_status"),
    ],
)
def test_strict_pending_frame_decoder_synthesizes_auditable_fail_closed_frame(
    tmp_path: Path,
    raw_frames,
    reason: str,
) -> None:
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=raw_frames,
            permission_profile={"mode": "bypassPermissions", "secret": "must-not-leak"},
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    [frame] = payload["pending_tool_frames"]

    assert frame["event_type"] == "unknown_recovered_tool_frame"
    assert frame["status"] == "needs_reconciliation"
    assert reason in frame["reason"]
    assert len(frame["raw_sha256"]) == 64
    rendered = json.dumps(frame, sort_keys=True)
    assert "must-not-leak" not in rendered
    assert "permission_profile" not in rendered
    assert "authorization_scopes" not in rendered


@pytest.mark.parametrize("secret_field", ["tool_call_id", "tool_name", "status"])
def test_synthetic_pending_frame_never_persists_untrusted_identity_or_status_secrets(
    tmp_path: Path,
    secret_field: str,
) -> None:
    secret = f"sk-live-{secret_field}-must-not-leak"
    nested_secret = f"nested-{secret_field}-must-not-leak"
    raw_frame = {
        "tool_call_id": "call-unknown",
        "tool_name": "not_a_registered_tool",
        "status": "executing",
        "arguments": {
            "nested": {
                "permission_profile": {"api_key": nested_secret},
                "authorization_scopes": [nested_secret],
            }
        },
    }
    raw_frame[secret_field] = secret
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(pending_tool_frames=[raw_frame]),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    [frame] = payload["pending_tool_frames"]
    rendered = json.dumps(frame, ensure_ascii=False, sort_keys=True)

    assert secret not in path.read_text(encoding="utf-8")
    assert nested_secret not in path.read_text(encoding="utf-8")
    assert secret not in rendered
    assert nested_secret not in rendered
    assert secret not in frame.values()
    assert frame["tool_call_id"].startswith("unknown-recovery:")
    assert frame["tool_name"] == "unknown_recovered_tool"
    assert "original_status" not in frame
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    restoration_text = restored.to_restoration_text()
    assert secret not in restoration_text
    assert nested_secret not in restoration_text


def test_synthetic_pending_frame_preserves_only_registered_canonical_tool_name(tmp_path: Path) -> None:
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": "untrusted-call-id",
                    "tool_name": "send_email",
                    "status": "terminal-but-untrusted",
                }
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    [frame] = json.loads(path.read_text(encoding="utf-8"))["pending_tool_frames"]

    assert frame["tool_name"] == "send_email"
    assert frame["tool_call_id"].startswith("unknown-recovery:")
    assert "untrusted-call-id" not in json.dumps(frame, sort_keys=True)
    assert "terminal-but-untrusted" not in json.dumps(frame, sort_keys=True)


@pytest.mark.parametrize("entry", ["initial", "post_compact", "restart", "legacy"])
def test_all_recovery_entries_preserve_unknown_frames_as_machine_blockers(
    tmp_path: Path,
    monkeypatch,
    entry: str,
) -> None:
    from app.kernel.engine import _build_restoration_context, _load_and_hydrate_recovery_manifest

    payload = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-1",
        "claim_version": 1,
        "claim_worker_id": "worker-1",
        "pending_tool_frames": [{"tool_call_id": "call-unknown", "tool_name": "send_email", "status": "executing"}],
    }
    canonical = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    if entry == "legacy":
        source = tmp_path / "agent-1" / "runtime_artifacts" / "recovery_manifest.json"
    else:
        source = canonical
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(payload), encoding="utf-8")
    if source == canonical:
        os.chmod(source, 0o600)
    context = SessionContext(session_id="session-1", metadata=_authority_metadata())

    if entry == "initial":
        manifest = _load_and_hydrate_recovery_manifest("agent-1", context, data_root=tmp_path)
    elif entry == "post_compact":
        monkeypatch.setattr(
            "app.config.get_settings",
            lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
        )
        _build_restoration_context("agent-1", session_context=context)
        manifest = load_recovery_manifest("agent-1", session_context=context, data_root=tmp_path)
    else:
        manifest = load_and_hydrate_recovery_manifest("agent-1", context, data_root=tmp_path)

    assert manifest is not None
    [frame] = context.metadata["recovered_pending_tool_frames"]
    assert frame["event_type"] == "unknown_recovered_tool_frame"
    assert frame["status"] == "needs_reconciliation"
    assert context.metadata["recovery_reconciliation_blocked"] is True


@pytest.mark.parametrize(
    "authority_field",
    ["tenant_id", "runtime_task_id", "claim_version", "claim_worker_id"],
)
def test_authority_bearing_writer_rejects_incomplete_identity(tmp_path: Path, authority_field: str) -> None:
    metadata = _authority_metadata(
        pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "send_email", "status": "running"}]
    )
    metadata.pop(authority_field)
    session = SessionContext(session_id="session-1", metadata=metadata)

    assert persist_recovery_manifest("agent-1", session, data_root=tmp_path) == []
    assert not recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id=metadata.get("runtime_task_id"),
        data_root=tmp_path,
    ).exists()
    _assert_final_canonical_writer_guard_rejects(tmp_path, authority_field)


def test_authority_bearing_writer_rejects_missing_session(tmp_path: Path) -> None:
    session = SessionContext(
        session_id=None,
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "send_email", "status": "running"}]
        ),
    )

    assert persist_recovery_manifest("agent-1", session, data_root=tmp_path) == []
    _assert_final_canonical_writer_guard_rejects(tmp_path, "session_id")


def test_authority_bearing_writer_rejects_missing_agent_argument(tmp_path: Path) -> None:
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "send_email", "status": "running"}]
        ),
    )

    assert persist_recovery_manifest(None, session, data_root=tmp_path) == []
    _assert_final_canonical_writer_guard_rejects(tmp_path, "agent_id")


def test_higher_claim_quarantines_but_never_overwrites_incomplete_authority_manifest(tmp_path: Path) -> None:
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "session_id": "session-1",
            "agent_id": "agent-1",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
            "pending_tool_frames": [{"tool_call_id": "call-old", "tool_name": "send_email", "status": "running"}],
        },
        sort_keys=True,
    ).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    newer = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            claim_version=2,
            claim_worker_id="worker-2",
            pending_tool_frames=[{"tool_call_id": "call-new", "tool_name": "read_file", "status": "running"}],
        ),
    )

    assert persist_recovery_manifest("agent-1", newer, data_root=tmp_path) == []
    assert path.read_bytes() == raw
    conflict_files = list((path.parents[1] / "authority_conflicts").glob("*.json"))
    quarantine_files = list((path.parents[1] / "authority_quarantine").glob("*.json"))
    assert len(conflict_files) == 1
    assert len(quarantine_files) == 1
    assert quarantine_files[0].read_bytes() == raw
    assert stat.S_IMODE(quarantine_files[0].stat().st_mode) == 0o600
    conflict = json.loads(conflict_files[0].read_text(encoding="utf-8"))
    assert conflict["state"] == "incomplete_authority_manifest"
    assert conflict["raw_sha256"]
    assert "send_email" not in json.dumps(conflict)


@pytest.mark.parametrize(
    "missing_field",
    [
        "agent_id",
        "tenant_id",
        "session_id",
        "runtime_task_id",
        "claim_version",
        "claim_worker_id",
    ],
)
def test_canonical_consumer_quarantines_and_blocks_incomplete_authority_before_hydration(
    tmp_path: Path,
    missing_field: str,
) -> None:
    identity = {
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "runtime_task_id": "run-1",
        "claim_version": 1,
        "claim_worker_id": "worker-1",
    }
    secret = f"secret-from-{missing_field}-must-stay-out-of-conflict-evidence"
    payload = {
        **identity,
        "pending_tool_frames": [
            {
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "status": "running",
                "arguments": {"path": "workspace/source.md", "secret": secret},
            }
        ],
    }
    payload.pop(missing_field)
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    reader = SessionContext(
        session_id="session-1",
        metadata={
            "agent_id": "agent-1",
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 2,
            "claim_worker_id": "worker-2",
        },
    )

    loaded = load_recovery_manifest("agent-1", session_context=reader, data_root=tmp_path)

    assert loaded is None
    assert path.read_bytes() == raw
    assert reader.metadata["recovery_reconciliation_blocked"] is True
    assert reader.metadata["recovery_reconciliation_reason"] == "incomplete_recovery_manifest_authority"
    assert reader.metadata["recovered_manifest_authority_conflict"]["missing_fields"] == [missing_field]
    [quarantine] = list((path.parents[1] / "authority_quarantine").glob("*.json"))
    [conflict_path] = list((path.parents[1] / "authority_conflicts").glob("*.json"))
    assert quarantine.read_bytes() == raw
    conflict_text = conflict_path.read_text(encoding="utf-8")
    assert secret not in conflict_text
    assert missing_field in conflict_text
    assert stat.S_IMODE(quarantine.stat().st_mode) == 0o600
    assert stat.S_IMODE(conflict_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("entry", ["load", "hydrate", "load_and_hydrate", "inspect"])
def test_every_canonical_consumption_entry_fails_closed_for_incomplete_authority(
    tmp_path: Path,
    entry: str,
) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifest,
        hydrate_session_context_from_recovery_manifest,
        inspect_recovery_manifest_checkpoint,
    )

    payload = {
        "session_id": "session-1",
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-1",
        "claim_version": 1,
        "pending_tool_frames": [
            {
                "tool_call_id": "call-1",
                "tool_name": "read_file",
                "status": "running",
                "arguments": {"path": "workspace/source.md"},
            }
        ],
    }
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    raw = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    reader = SessionContext(
        session_id="session-1",
        metadata={
            "agent_id": "agent-1",
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 2,
            "claim_worker_id": "worker-2",
        },
    )

    if entry == "load":
        result = load_recovery_manifest("agent-1", session_context=reader, data_root=tmp_path)
        assert result is None
    elif entry == "load_and_hydrate":
        result = load_and_hydrate_recovery_manifest("agent-1", reader, data_root=tmp_path)
        assert result is None
    elif entry == "hydrate":
        manifest = RecoveryManifest(
            session_id="session-1",
            agent_id="agent-1",
            tenant_id="tenant-1",
            runtime_task_id="run-1",
            claim_version=1,
            claim_worker_id=None,
            pending_tool_frames=[dict(payload["pending_tool_frames"][0])],
        )
        assert hydrate_session_context_from_recovery_manifest(reader, manifest, agent_id="agent-1") is False
    else:
        result = inspect_recovery_manifest_checkpoint(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-1",
            data_root=tmp_path,
        )
        assert result is not None
        assert result["state"] == "incomplete_authority"
        assert result["missing_fields"] == ["claim_worker_id"]
        assert result["recovery_reconciliation_blocked"] is True

    assert reader.recent_files == []
    assert reader.metadata.get("recovered_pending_tool_frames") in (None, [])
    if entry != "inspect":
        assert reader.metadata["recovery_reconciliation_blocked"] is True
        assert reader.metadata["recovery_reconciliation_reason"] == "incomplete_recovery_manifest_authority"
    assert path.read_bytes() == raw


def test_recovery_writer_rejects_agent_root_symlink_without_writing_outside_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "agents"
    outside = tmp_path / "outside-agent"
    data_root.mkdir()
    outside.mkdir()
    (data_root / "agent-1").symlink_to(outside, target_is_directory=True)
    outside_target = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=data_root,
    ).resolve(strict=False)
    outside_target.parent.mkdir(parents=True)
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "send_email", "status": "running"}]
        ),
    )

    with pytest.raises(
        OSError,
        match="symlink|not a directory|secure recovery",
    ):
        persist_recovery_manifest("agent-1", session, data_root=data_root)

    assert not outside_target.exists()


def test_secure_writer_rejects_toctou_swap_to_agent_root_symlink(tmp_path: Path, monkeypatch) -> None:
    import app.runtime.recovery_manifest as recovery

    data_root = tmp_path / "agents"
    agent_root = data_root / "agent-1"
    outside = tmp_path / "outside-agent"
    agent_root.mkdir(parents=True)
    outside.mkdir()
    outside_target = outside / "runtime_artifacts" / "recovery_manifests"
    outside_target.mkdir(parents=True)
    real_open = os.open
    swapped = False

    def swap_before_component_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "agent-1" and dir_fd is not None and not swapped:
            parked = data_root / "agent-1-parked"
            agent_root.rename(parked)
            agent_root.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(recovery.os, "open", swap_before_component_open)
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "send_email", "status": "running"}]
        ),
    )

    with pytest.raises(OSError, match="Secure recovery path rejects symlink"):
        persist_recovery_manifest("agent-1", session, data_root=data_root)

    assert swapped is True
    assert list(outside_target.rglob("*.json")) == []


def test_incomplete_authority_quarantine_replaces_preoccupied_symlink_with_verified_regular_file(
    tmp_path: Path,
) -> None:
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "session_id": "session-1",
            "agent_id": "agent-1",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-old",
                    "tool_name": "send_email",
                    "status": "running",
                    "arguments": {"nested": {"secret": "must-remain-only-in-quarantine"}},
                }
            ],
        },
        sort_keys=True,
    ).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    digest = hashlib.sha256(raw).hexdigest()
    quarantine = path.parents[1] / "authority_quarantine" / f"incomplete-{digest}.json"
    quarantine.parent.mkdir(parents=True)
    outside = tmp_path / "attacker-controlled.json"
    outside.write_bytes(b"attacker")
    quarantine.symlink_to(outside)
    newer = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            claim_version=2,
            claim_worker_id="worker-2",
            pending_tool_frames=[{"tool_call_id": "call-new", "tool_name": "read_file", "status": "running"}],
        ),
    )

    assert persist_recovery_manifest("agent-1", newer, data_root=tmp_path) == []

    assert quarantine.is_symlink() is False
    assert quarantine.is_file()
    assert quarantine.read_bytes() == raw
    assert stat.S_IMODE(quarantine.stat().st_mode) == 0o600
    assert outside.read_bytes() == b"attacker"
    [sidecar] = list((path.parents[1] / "authority_conflicts").glob("*.json"))
    conflict = json.loads(sidecar.read_text(encoding="utf-8"))
    assert conflict["raw_sha256"] == hashlib.sha256(quarantine.read_bytes()).hexdigest()


def test_uuid_agent_identity_accepts_equivalent_hyphenated_and_hex_forms(tmp_path: Path) -> None:
    agent_id = "c3efa20e-ac8f-4bc9-9dbc-a765105ad1dc"
    session = SessionContext(
        session_id="session-1",
        metadata={
            **_authority_metadata(agent_id=agent_id.replace("-", "")),
            "pending_tool_frames": [{"tool_call_id": "call-1", "tool_name": "write_file", "status": "running"}],
        },
    )

    [path] = persist_recovery_manifest(agent_id, session, data_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["agent_id"] == agent_id


def test_continuity_only_checkpoint_remains_compatible_without_run_claim_authority(tmp_path: Path) -> None:
    session = SessionContext(session_id="session-1", metadata={"agent_id": "agent-1"})
    session.recent_files.append("workspace/source.md")
    session.recent_writes.append("workspace/result.md")
    session.file_snapshots["workspace/source.md"] = {"exists": True, "size": 1, "mtime_ns": 1}

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["recent_reads"] == ["workspace/source.md"]
    assert payload["recent_writes"] == ["workspace/result.md"]
    assert payload["pending_tool_frames"] == []
    assert payload["permission_profile"] == {}


@pytest.mark.asyncio
async def test_unknown_frame_blocker_persists_tombstone_before_any_new_tool(monkeypatch) -> None:
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks

    ordering: list[str] = []
    persisted_frames: list[dict] = []

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    async def renew():
        ordering.append("renew")

    def persist(request, **_kwargs):
        ordering.append("persist")
        persisted_frames.extend(request.session_context.metadata["pending_tool_frames"])
        return {"ref": "runtime_artifacts/recovery_manifests/manifest.json", "sha256": "a" * 64, "bytes": 1}

    async def execute(*_args, **_kwargs):
        ordering.append("execute")
        raise AssertionError("blocked recovery must never execute a new tool")

    async def emit_event(_event):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine._persist_recovery_manifest_checkpoint", persist)
    frame = {
        "schema": "hive.unknown_recovered_tool_frame.v1",
        "event_type": "unknown_recovered_tool_frame",
        "tool_call_id": "call-unknown",
        "tool_name": "send_email",
        "status": "needs_reconciliation",
        "reason": "pending_tool_frame_unknown_status",
        "raw_sha256": "b" * 64,
    }
    context = SessionContext(
        session_id="session-1",
        metadata={
            **_authority_metadata(),
            "pending_tool_frames": [frame],
            "recovered_pending_tool_frames": [frame],
            "recovery_reconciliation_blocked": True,
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[],
        agent_name="Agent",
        role_description="role",
        agent_id="agent-1",
        session_context=context,
        memory_session_id="session-1",
    )

    result, _args, executed = await _execute_tool_with_hooks(
        execute_tool=execute,
        request=request,
        runtime_config=RuntimeConfig(tenant_id="tenant-1", max_tool_rounds=3),
        tool_name="read_file",
        tool_args={"path": "workspace/next.md"},
        tool_call_id="call-next",
        emit_event=emit_event,
        renew_runtime_lease=renew,
    )

    assert executed is False
    assert "reconciliation" in result.lower()
    assert ordering == ["renew", "persist"]
    assert persisted_frames == [frame]


def test_recoverable_status_cannot_bypass_registered_frame_validation_or_recursive_secret_scrub(
    tmp_path: Path,
) -> None:
    secret = "sk-live-nested-bypass-must-not-leak"
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": "untrusted-call-id",
                    "tool_name": "malicious-unregistered-tool",
                    "status": "running",
                    "arguments": {
                        "nested": {
                            "permission_profile": {"api_key": secret},
                            "authorization_scopes": [secret],
                        }
                    },
                }
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    [frame] = payload["pending_tool_frames"]

    assert frame["event_type"] == "unknown_recovered_tool_frame"
    assert frame["tool_name"] == "unknown_recovered_tool"
    assert frame["tool_call_id"].startswith("unknown-recovery:")
    assert secret not in raw
    restored = load_and_hydrate_recovery_manifest("agent-1", session, data_root=tmp_path)
    assert restored is not None
    assert secret not in restored.to_restoration_text()
    assert session.metadata["recovery_reconciliation_blocked"] is True


@pytest.mark.parametrize(
    "invalid_call_id",
    [None, "", "   ", 42, "line\nbreak", "x" * 513],
)
def test_invalid_recovered_tool_call_id_is_always_synthetic(
    tmp_path: Path,
    invalid_call_id,
) -> None:
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": invalid_call_id,
                    "tool_name": "read_file",
                    "status": "running",
                    "arguments": {"path": "workspace/source.md"},
                }
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    [frame] = json.loads(path.read_text(encoding="utf-8"))["pending_tool_frames"]

    assert frame["event_type"] == "unknown_recovered_tool_frame"
    assert frame["tool_call_id"].startswith("unknown-recovery:")


def test_registered_alias_is_canonicalized_and_nested_secret_fields_are_scrubbed(tmp_path: Path) -> None:
    secret = "must-not-reach-disk-or-restoration"
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": "call-alias",
                    "tool_name": "bing_search",
                    "status": "running",
                    "arguments": {
                        "query": "safe",
                        "nested": {
                            "api_key": secret,
                            "credentials": {"token": secret},
                        },
                    },
                }
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    [frame] = json.loads(raw)["pending_tool_frames"]

    assert frame["tool_name"] == "web_search"
    assert frame["arguments"] == {"nested": {}, "query": "safe"}
    assert secret not in raw
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert secret not in restored.to_restoration_text()


def test_unknown_frame_registry_lookup_is_one_snapshot_not_per_frame_reload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.tools.registry import tool_spec_v1
    import app.tools.collector as collector

    assert tool_spec_v1("read_file") is not None
    reload_calls = 0

    def count_reload(*_args, **_kwargs):
        nonlocal reload_calls
        reload_calls += 1

    monkeypatch.setattr(collector, "_import_handler_modules", count_reload)
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": f"call-{index}",
                    "tool_name": f"unknown-tool-{index}",
                    "status": "invalid",
                }
                for index in range(25)
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)

    assert reload_calls == 0
    assert len(json.loads(path.read_text(encoding="utf-8"))["pending_tool_frames"]) == 25


def test_circular_deep_and_oversized_frame_is_bounded_and_secret_free(tmp_path: Path) -> None:
    secret = "cycle-secret-must-not-leak"
    circular: dict = {
        "tool_call_id": "call-cycle",
        "tool_name": "unknown-cycle-tool",
        "status": "invalid",
        "secret": secret,
    }
    circular["arguments"] = circular
    deep: dict = {"api_key": secret}
    for _ in range(200):
        deep = {"nested": deep}
    circular["deep"] = deep
    circular["oversized"] = secret + ("x" * (3 * 1024 * 1024))
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(pending_tool_frames=[circular]),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    [frame] = json.loads(raw)["pending_tool_frames"]

    assert frame["event_type"] == "unknown_recovered_tool_frame"
    assert len(raw.encode("utf-8")) < 64 * 1024
    assert secret not in raw


def test_lexical_data_root_intermediate_symlink_is_rejected_without_outside_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    lexical_data_root = alias / "agents"
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "read_file", "status": "running"}]
        ),
    )

    with pytest.raises(OSError, match="symlink|secure recovery|not a directory"):
        persist_recovery_manifest("agent-1", session, data_root=lexical_data_root)

    assert list(outside.rglob("*.json")) == []


def test_existing_recovery_directory_with_group_or_other_write_is_rejected(tmp_path: Path) -> None:
    data_root = tmp_path / "agents"
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=data_root,
    )
    path.parent.mkdir(parents=True)
    os.chmod(data_root / "agent-1" / "runtime_artifacts", 0o777)
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "read_file", "status": "running"}]
        ),
    )

    with pytest.raises(OSError, match="permissions|secure recovery"):
        persist_recovery_manifest("agent-1", session, data_root=data_root)

    assert not path.exists()


def test_canonical_hardlink_and_non_private_mode_are_never_restored(tmp_path: Path) -> None:
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "read_file", "status": "running"}]
        ),
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    external = tmp_path / "external-authority.json"
    path.replace(external)
    os.link(external, path)
    os.chmod(external, 0o644)

    assert load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path) is None
    assert path.stat().st_nlink == 2
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_existing_lock_hardlink_is_rejected_without_chmod_of_victim(tmp_path: Path) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    recovery._ensure_private_directory(path.parent)
    victim = tmp_path / "victim.txt"
    victim.write_text("victim", encoding="utf-8")
    os.chmod(victim, 0o666)
    os.link(victim, path.with_suffix(".lock"))

    with pytest.raises(OSError, match="hardlink|private|link"):
        with recovery._session_manifest_lock(path):
            raise AssertionError("unsafe lock must not be acquired")

    assert stat.S_IMODE(victim.stat().st_mode) == 0o666
    assert victim.stat().st_nlink == 2


def test_quarantine_swap_after_verified_write_never_commits_a_valid_sidecar_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "session_id": "session-1",
            "agent_id": "agent-1",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
            "pending_tool_frames": [{"tool_call_id": "call-old", "tool_name": "send_email", "status": "running"}],
        },
        sort_keys=True,
    ).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    digest = hashlib.sha256(raw).hexdigest()
    quarantine = path.parents[1] / "authority_quarantine" / f"incomplete-{digest}.json"
    outside = tmp_path / "replacement.json"
    outside.write_bytes(b"replacement")
    real_write_verified = recovery._write_verified_private_bytes

    def swap_after_verified_write(target: Path, payload: bytes):
        verified = real_write_verified(target, payload)
        if target == quarantine:
            target.unlink()
            target.symlink_to(outside)
        return verified

    monkeypatch.setattr(recovery, "_write_verified_private_bytes", swap_after_verified_write)
    newer = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            claim_version=2,
            claim_worker_id="worker-2",
            pending_tool_frames=[{"tool_call_id": "call-new", "tool_name": "read_file", "status": "running"}],
        ),
    )

    with pytest.raises(OSError, match="verification|changed|reference"):
        persist_recovery_manifest("agent-1", newer, data_root=tmp_path)

    assert path.read_bytes() == raw
    assert list((path.parents[1] / "authority_conflicts").glob("*.json")) == []


def test_session_lock_scavenges_only_safe_stale_atomic_temps(tmp_path: Path) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    recovery._ensure_private_directory(path.parent)
    safe = path.parent / f".{path.name}.{'a' * 32}.tmp"
    safe.write_bytes(b"orphan")
    os.chmod(safe, 0o600)
    old = time.time() - 3600
    os.utime(safe, (old, old))
    outside = tmp_path / "outside-temp"
    outside.write_bytes(b"outside")
    symlink = path.parent / f".{path.name}.{'b' * 32}.tmp"
    symlink.symlink_to(outside)
    victim = tmp_path / "hardlink-temp-victim"
    victim.write_bytes(b"victim")
    os.chmod(victim, 0o600)
    hardlink = path.parent / f".{path.name}.{'c' * 32}.tmp"
    os.link(victim, hardlink)

    with recovery._session_manifest_lock(path):
        pass

    assert safe.exists() is False
    assert symlink.is_symlink()
    assert outside.read_bytes() == b"outside"
    assert hardlink.exists()
    assert victim.stat().st_nlink == 2


def test_secure_writer_never_reopens_created_directory_by_lexical_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.runtime.recovery_manifest as recovery

    monkeypatch.setattr(
        recovery,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(AssertionError("lexical directory fsync is forbidden")),
    )
    session = SessionContext(session_id="session-1", metadata=_authority_metadata())
    session.track_pending_item("durable")

    assert persist_recovery_manifest("agent-1", session, data_root=tmp_path)


@pytest.mark.parametrize(
    "malicious_agent_id",
    ["../escaped-agent", "nested/agent", "nested\\agent"],
)
def test_recovery_manifest_rejects_non_component_agent_identity(
    tmp_path: Path,
    malicious_agent_id: str,
) -> None:
    data_root = tmp_path / "agents"
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            agent_id=malicious_agent_id,
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "read_file", "status": "running"}],
        ),
    )

    with pytest.raises(ValueError, match="agent_id"):
        recovery_manifest_path(
            malicious_agent_id,
            session_id="session-1",
            runtime_task_id="run-1",
            data_root=data_root,
        )
    with pytest.raises(ValueError, match="agent_id"):
        persist_recovery_manifest(malicious_agent_id, session, data_root=data_root)

    assert list(tmp_path.rglob("*.json")) == []


def test_recovery_manifest_rejects_absolute_agent_identity_without_writing_outside_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "agents"
    outside_agent = tmp_path / "outside-agent"
    malicious_agent_id = str(outside_agent)
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            agent_id=malicious_agent_id,
            pending_tool_frames=[{"tool_call_id": "call-1", "tool_name": "read_file", "status": "running"}],
        ),
    )

    with pytest.raises(ValueError, match="agent_id"):
        persist_recovery_manifest(malicious_agent_id, session, data_root=data_root)

    assert not (outside_agent / "runtime_artifacts").exists()


def test_permission_profile_and_registered_frame_recursively_scrub_nested_secrets(
    tmp_path: Path,
) -> None:
    secret = "sk-live-permission-profile-must-not-leak"
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            permission_profile={
                "mode": "default",
                "nested": {"api_key": secret},
                "authorization_scopes": [secret],
                "safe": {"label": "preserved"},
            },
            pending_tool_frames=[
                {
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "status": "running",
                    "arguments": {
                        "path": "workspace/source.md",
                        "nested": {"credentials": {"token": secret}},
                    },
                }
            ],
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["permission_profile"] == {
        "mode": "default",
        "nested": {},
        "safe": {"label": "preserved"},
    }
    assert payload["pending_tool_frames"][0]["arguments"] == {"nested": {}, "path": "workspace/source.md"}
    assert secret not in raw
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert secret not in restored.to_restoration_text()


@pytest.mark.parametrize(
    "secret_field",
    [
        "accessToken",
        "ClientSecret",
        "api.Key",
        "authorization-Scopes",
        "runtimePermission.Profile",
        "private-Key",
        "sessionToken",
        "providerSecret",
        "userPassword",
        "cloudCredential",
    ],
)
def test_registered_frame_scrubs_camel_pascal_and_mixed_separator_secret_keys(
    tmp_path: Path,
    caplog,
    secret_field: str,
) -> None:
    secret = f"secret-for-{secret_field}-must-not-leak"
    nested: dict = {secret_field: secret, "safe": "preserved"}
    for depth in range(12):
        nested = {f"level-{depth}": nested}
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            permission_profile={"nested": nested},
            pending_tool_frames=[
                {
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "status": "running",
                    "arguments": {"path": "workspace/source.md", "nested": nested},
                }
            ],
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)

    assert restored is not None
    assert secret not in raw
    assert secret not in restored.to_restoration_text()
    assert secret not in caplog.text
    assert "preserved" in raw


def test_registered_frame_oversized_keys_are_digest_bounded_and_reloadable(tmp_path: Path) -> None:
    import app.runtime.recovery_manifest as recovery

    arguments = {"path": "workspace/source.md"}
    for index in range(10):
        arguments[f"attacker-key-{index}-" + ("x" * 300_000)] = "value"
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "status": "running",
                    "arguments": arguments,
                }
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    [frame] = json.loads(raw)["pending_tool_frames"]
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)

    assert len(raw.encode("utf-8")) < recovery.MAX_RECOVERY_MANIFEST_BYTES
    assert restored is not None
    assert "attacker-key-" not in raw
    assert "x" * 128 not in raw
    assert sum(key.startswith("__bounded_key_sha256__:") for key in frame["arguments"]) == 10


def test_registered_frame_many_keys_emit_one_truncation_marker_and_remain_reloadable(tmp_path: Path) -> None:
    import app.runtime.recovery_manifest as recovery

    arguments = {f"safe-key-{index:05d}": "value" for index in range(6000)}
    session = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frames=[
                {
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "status": "running",
                    "arguments": arguments,
                }
            ]
        ),
    )

    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    [frame] = json.loads(raw)["pending_tool_frames"]
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)

    def count_truncation_markers(value) -> int:
        if isinstance(value, dict):
            own = int(value.get("__bounded__") in {"node_limit", "output_limit"})
            return own + sum(count_truncation_markers(item) for item in value.values())
        if isinstance(value, list):
            return sum(count_truncation_markers(item) for item in value)
        return 0

    assert len(raw.encode("utf-8")) < recovery.MAX_RECOVERY_MANIFEST_BYTES
    assert restored is not None
    assert len(frame["arguments"]) < len(arguments)
    assert count_truncation_markers(frame) == 1


def test_atomic_writer_rejects_oversized_manifest_without_replacing_checkpoint(tmp_path: Path) -> None:
    import app.runtime.recovery_manifest as recovery

    session = SessionContext(session_id="session-1", metadata=_authority_metadata())
    session.track_pending_item("preserved-checkpoint")
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    previous = path.read_bytes()
    session.pending_items[:] = ["x" * (recovery.MAX_RECOVERY_MANIFEST_BYTES + 1)]

    with pytest.raises(ValueError, match="byte limit"):
        persist_recovery_manifest("agent-1", session, data_root=tmp_path)

    assert path.read_bytes() == previous
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert restored.pending_items == ["preserved-checkpoint"]


def test_quarantine_swap_after_sidecar_write_rolls_back_sidecar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    raw = json.dumps(
        {
            "session_id": "session-1",
            "agent_id": "agent-1",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
            "pending_tool_frames": [{"tool_call_id": "call-old", "tool_name": "send_email", "status": "running"}],
        },
        sort_keys=True,
    ).encode()
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    digest = hashlib.sha256(raw).hexdigest()
    quarantine = path.parents[1] / "authority_quarantine" / f"incomplete-{digest}.json"
    conflict = path.parents[1] / "authority_conflicts" / f"incomplete-{digest}.json"
    outside = tmp_path / "replacement-after-sidecar.json"
    outside.write_bytes(b"replacement")
    real_write_verified = recovery._write_verified_private_bytes

    def swap_after_sidecar_write(target: Path, payload: bytes):
        verified = real_write_verified(target, payload)
        if target == conflict:
            quarantine.unlink()
            quarantine.symlink_to(outside)
        return verified

    monkeypatch.setattr(recovery, "_write_verified_private_bytes", swap_after_sidecar_write)
    newer = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            claim_version=2,
            claim_worker_id="worker-2",
            pending_tool_frames=[{"tool_call_id": "call-new", "tool_name": "read_file", "status": "running"}],
        ),
    )

    with pytest.raises(OSError, match="verification|changed|reference"):
        persist_recovery_manifest("agent-1", newer, data_root=tmp_path)

    assert path.read_bytes() == raw
    assert quarantine.is_symlink()
    assert list((path.parents[1] / "authority_conflicts").glob("*.json")) == []


def test_session_lock_retries_transient_enoent_only_until_bounded_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    recovery._ensure_private_directory(path.parent)
    real_open = recovery.os.open
    lock_name = path.with_suffix(".lock").name
    attempts = 0

    def always_missing_lock(filename, flags, mode=0o777, *, dir_fd=None):
        nonlocal attempts
        if filename == lock_name and dir_fd is not None:
            attempts += 1
            raise FileNotFoundError(filename)
        return real_open(filename, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(recovery.os, "open", always_missing_lock)
    monkeypatch.setattr(recovery, "RECOVERY_LOCK_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(recovery, "RECOVERY_LOCK_POLL_SECONDS", 0.005)
    started = time.monotonic()

    with pytest.raises(FileNotFoundError):
        with recovery._session_manifest_lock(path):
            raise AssertionError("an absent lock must never be acquired")

    elapsed = time.monotonic() - started
    assert 2 <= attempts <= 20
    assert elapsed < 0.2

    monkeypatch.setattr(recovery.os, "open", real_open)
    monkeypatch.setattr(recovery, "RECOVERY_LEGACY_LOCK_TIMEOUT_SECONDS", 0.03)
    agent_root = tmp_path / "agent-lock-contention"
    legacy_lock_path = agent_root / recovery.RECOVERY_MANIFESTS_REL_DIR / ".legacy.lock"
    with recovery._manifest_file_lock(legacy_lock_path):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="Timed out acquiring recovery manifest lock"):
            with recovery._legacy_manifest_lock(agent_root):
                raise AssertionError("a held legacy lock must never be acquired")
        elapsed = time.monotonic() - started

    assert 0.02 <= elapsed < 0.2


def test_only_root_owned_macos_private_root_alias_is_normalized(monkeypatch) -> None:
    import app.runtime.recovery_manifest as recovery

    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    monkeypatch.setattr(
        recovery.os,
        "lstat",
        lambda path: (
            SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
            if Path(path) == Path("/var")
            else (_ for _ in ()).throw(FileNotFoundError(path))
        ),
    )
    monkeypatch.setattr(recovery.os, "readlink", lambda path: "private/var")

    assert recovery._normalize_trusted_platform_data_root(Path("/var/folders/hive")) == Path(
        "/private/var/folders/hive"
    )


def test_user_owned_or_unrecognized_root_alias_is_never_normalized(monkeypatch) -> None:
    import app.runtime.recovery_manifest as recovery

    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    monkeypatch.setattr(
        recovery.os,
        "lstat",
        lambda _path: SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=os.geteuid()),
    )
    monkeypatch.setattr(recovery.os, "readlink", lambda _path: "attacker-controlled")

    assert recovery._normalize_trusted_platform_data_root(Path("/var/folders/hive")) == Path("/var/folders/hive")


def test_verified_private_write_closes_descriptor_when_initial_verification_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    captured = []

    def reject(verified):
        captured.append(verified)
        raise OSError("injected initial verification failure")

    monkeypatch.setattr(recovery, "_verify_private_file_reference", reject)

    with pytest.raises(OSError, match="injected initial verification failure"):
        recovery._write_verified_private_bytes(path, b"private evidence")

    assert len(captured) == 1
    assert captured[0].descriptor == -1


def test_reconciliation_closes_quarantine_descriptor_when_post_write_verification_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-corrupt",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"pending_tool_frames":[')
    os.chmod(path, 0o600)
    reviewed_evidence = recovery.reviewed_recovery_manifest_evidence(
        recovery.inspect_recovery_manifest_checkpoint(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-corrupt",
            data_root=tmp_path,
        )
    )
    real_verify = recovery._verify_private_file_reference
    quarantine_guard = None
    quarantine_verifications = 0

    def fail_post_write_verification(verified):
        nonlocal quarantine_guard, quarantine_verifications
        if "reconciliation_quarantine" in verified.path.parts:
            quarantine_guard = verified
            quarantine_verifications += 1
            if quarantine_verifications == 2:
                raise OSError("injected post-write verification failure")
        return real_verify(verified)

    monkeypatch.setattr(recovery, "_verify_private_file_reference", fail_post_write_verification)

    with pytest.raises(OSError, match="injected post-write verification failure"):
        recovery.resolve_recovery_manifest_reconciliation(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-corrupt",
            action="archive",
            reason="operator reviewed corrupt evidence",
            actor_user_id="operator-1",
            expected_manifest_state=reviewed_evidence["expected_manifest_state"],
            expected_manifest_ref=reviewed_evidence["expected_manifest_ref"],
            expected_sha256=reviewed_evidence["expected_sha256"],
            data_root=tmp_path,
        )

    assert quarantine_guard is not None
    assert quarantine_guard.descriptor == -1
