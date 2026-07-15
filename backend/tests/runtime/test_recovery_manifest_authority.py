from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.ccplus_contracts import permission_profile_snapshot_hash
from app.runtime.recovery_manifest_store import (
    RecoveryAuthorityFrame,
    load_recovery_manifest,
    persist_recovery_manifest,
    recovery_manifest_path,
    resolve_recovery_authority,
)
from app.runtime.session import SessionContext


def _authority(
    *,
    agent_id=None,
    tenant_id=None,
    requester_user_id=None,
    session_id: str = "session-a",
    root_runtime_task_id: str | None = "root-task-a",
    principal_snapshot_hash: str = "principal-hash-a",
    policy_snapshot_hash: str | None = None,
    config_snapshot_hash: str = "config-hash-a",
    base_transcript_sequence: int | None = 41,
) -> RecoveryAuthorityFrame:
    agent_id = agent_id or uuid4()
    tenant_id = tenant_id or uuid4()
    requester_user_id = requester_user_id or uuid4()
    if policy_snapshot_hash is None:
        policy_snapshot_hash = permission_profile_snapshot_hash({"mode": "default"})
    return RecoveryAuthorityFrame(
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        requester_user_id=str(requester_user_id),
        session_id=session_id,
        root_session_id="root-session-a",
        root_runtime_task_id=root_runtime_task_id,
        principal_type="delegated_user",
        principal_id=str(requester_user_id),
        principal_snapshot_hash=principal_snapshot_hash,
        policy_snapshot_hash=policy_snapshot_hash,
        config_snapshot_hash=config_snapshot_hash,
        base_transcript_sequence=base_transcript_sequence,
    )


def _session(authority: RecoveryAuthorityFrame, sentinel: str) -> SessionContext:
    return SessionContext(
        session_id=authority.session_id,
        metadata={
            "permission_profile": {"mode": "default"},
            "base_transcript_sequence": authority.base_transcript_sequence,
            "pending_tool_frames": [
                {
                    "tool_call_id": f"call-{sentinel}",
                    "tool_name": "read_file",
                    "arguments": {"path": f"workspace/{sentinel}.md"},
                    "status": "running",
                }
            ],
        },
        pending_items=[sentinel],
    )


def test_concurrent_sessions_use_distinct_authority_scoped_paths(tmp_path) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    requester_id = uuid4()
    first = _authority(
        agent_id=agent_id,
        tenant_id=tenant_id,
        requester_user_id=requester_id,
        session_id="session-a",
        root_runtime_task_id="root-a",
    )
    second = replace(first, session_id="session-b", root_session_id="session-b", root_runtime_task_id="root-b")

    first_write = persist_recovery_manifest(first, _session(first, "FIRST"), data_root=tmp_path)
    second_write = persist_recovery_manifest(second, _session(second, "SECOND"), data_root=tmp_path)

    assert first_write.status == "written"
    assert second_write.status == "written"
    assert first_write.paths != second_write.paths
    assert recovery_manifest_path(first, data_root=tmp_path) != recovery_manifest_path(second, data_root=tmp_path)

    first_load = load_recovery_manifest(first, data_root=tmp_path)
    second_load = load_recovery_manifest(second, data_root=tmp_path)
    assert first_load.status == "loaded"
    assert second_load.status == "loaded"
    assert first_load.manifest is not None and first_load.manifest.pending_items == ["FIRST"]
    assert second_load.manifest is not None and second_load.manifest.pending_items == ["SECOND"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("tenant_id", "tenant-b", "tenant_id_mismatch"),
        ("requester_user_id", "user-b", "requester_user_id_mismatch"),
        ("principal_snapshot_hash", "principal-b", "principal_snapshot_hash_mismatch"),
        ("policy_snapshot_hash", "policy-b", "policy_snapshot_hash_mismatch"),
        ("config_snapshot_hash", "config-b", "config_snapshot_hash_mismatch"),
        ("base_transcript_sequence", 42, "base_transcript_sequence_mismatch"),
    ],
)
def test_authority_or_policy_drift_holds_recovery_without_hydration(
    tmp_path,
    field: str,
    value: object,
    reason: str,
) -> None:
    authority = _authority()
    persist_recovery_manifest(authority, _session(authority, "PRIVATE"), data_root=tmp_path)
    drifted = replace(authority, **{field: value})

    result = load_recovery_manifest(drifted, data_root=tmp_path)
    target = SessionContext(session_id=authority.session_id)

    assert result.status == "held"
    assert result.reason == reason
    assert result.manifest is None
    assert result.hydrate(target) is False
    assert target.pending_items == []
    assert "recovered_pending_tool_frames" not in target.metadata
    status_payload = result.status_payload()
    assert status_payload is not None
    assert "manifest_ref" not in status_payload
    assert "quarantine_ref" not in status_payload


def test_different_root_runtime_task_never_consumes_prior_run_manifest(tmp_path) -> None:
    authority = _authority(root_runtime_task_id="root-a")
    persist_recovery_manifest(authority, _session(authority, "PRIVATE"), data_root=tmp_path)

    result = load_recovery_manifest(
        replace(authority, root_runtime_task_id="root-b"),
        data_root=tmp_path,
    )

    assert result.status == "absent"
    assert result.reason == "different_root_runtime_task"
    assert result.manifest is None


def test_forked_session_never_inherits_parent_manifest_without_explicit_projection(tmp_path) -> None:
    parent = _authority(session_id="parent-session", root_runtime_task_id="parent-root-task")
    persist_recovery_manifest(parent, _session(parent, "PARENT_PRIVATE"), data_root=tmp_path)
    fork = replace(
        parent,
        session_id="fork-session",
        root_session_id="fork-session",
        root_runtime_task_id="fork-root-task",
    )

    result = load_recovery_manifest(fork, data_root=tmp_path)

    assert result.status == "absent"
    assert result.manifest is None


def test_forged_cross_agent_manifest_is_held(tmp_path) -> None:
    source = _authority()
    persist_recovery_manifest(source, _session(source, "PRIVATE"), data_root=tmp_path)
    source_path = recovery_manifest_path(source, data_root=tmp_path)
    target = replace(source, agent_id=str(uuid4()))
    target_path = recovery_manifest_path(target, data_root=tmp_path)
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(source_path.read_bytes())

    result = load_recovery_manifest(target, data_root=tmp_path)

    assert result.status == "held"
    assert result.reason == "agent_id_mismatch"
    assert result.manifest is None


def test_unsigned_legacy_manifest_is_quarantined_with_exact_bytes(tmp_path) -> None:
    authority = _authority()
    legacy_path = tmp_path / authority.agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    original = json.dumps(
        {"pending_items": ["UNVERIFIABLE_LEGACY"], "permission_profile": {"mode": "full_access"}}
    ).encode()
    legacy_path.write_bytes(original)

    result = load_recovery_manifest(authority, data_root=tmp_path)

    assert result.status == "quarantined"
    assert result.reason == "legacy_authority_unverifiable"
    assert result.manifest is None
    assert not legacy_path.exists()
    assert result.quarantine_path is not None
    assert result.quarantine_path.read_bytes() == original
    status_payload = result.status_payload()
    assert status_payload is not None
    assert "manifest_ref" not in status_payload
    assert "quarantine_ref" not in status_payload


def test_signed_legacy_envelope_migrates_only_after_exact_authority_verification(tmp_path) -> None:
    authority = _authority()
    persist_recovery_manifest(authority, _session(authority, "MIGRATED"), data_root=tmp_path)
    canonical = recovery_manifest_path(authority, data_root=tmp_path)
    original = canonical.read_bytes()
    legacy_path = tmp_path / authority.agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.write_bytes(original)
    canonical.unlink()

    result = load_recovery_manifest(authority, data_root=tmp_path)

    assert result.status == "loaded_migrated"
    assert result.manifest is not None and result.manifest.pending_items == ["MIGRATED"]
    assert canonical.read_bytes() == original
    assert result.quarantine_path is not None
    assert result.quarantine_path.read_bytes() == original
    assert not legacy_path.exists()


def test_corrupt_current_manifest_is_quarantined_and_never_falls_back(tmp_path) -> None:
    authority = _authority()
    path = recovery_manifest_path(authority, data_root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"partial":')

    result = load_recovery_manifest(authority, data_root=tmp_path)

    assert result.status == "quarantined"
    assert result.reason == "corrupt_json"
    assert result.manifest is None
    assert result.quarantine_path is not None
    assert result.quarantine_path.read_bytes() == b'{"partial":'


def test_hmac_rejects_agent_forged_manifest_content(tmp_path) -> None:
    authority = _authority()
    persist_recovery_manifest(authority, _session(authority, "ORIGINAL"), data_root=tmp_path)
    path = recovery_manifest_path(authority, data_root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["body"]["manifest"]["pending_items"] = ["FORGED_EFFECT"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = load_recovery_manifest(authority, data_root=tmp_path)

    assert result.status == "quarantined"
    assert result.reason == "integrity_mismatch"
    assert result.manifest is None


def test_signed_envelope_normalizes_uuid_and_datetime_runtime_metadata(tmp_path) -> None:
    authority = _authority()
    value_id = uuid4()
    value_time = datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc)
    session = SessionContext(
        session_id=authority.session_id,
        metadata={
            "permission_profile": {"mode": "default"},
            "base_transcript_sequence": authority.base_transcript_sequence,
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-json-native",
                    "tool_name": "read_file",
                    "status": "running",
                    "arguments": {"opaque_id": value_id, "observed_at": value_time},
                }
            ],
        },
    )

    written = persist_recovery_manifest(authority, session, data_root=tmp_path)
    loaded = load_recovery_manifest(authority, data_root=tmp_path)

    assert written.status == "written"
    assert loaded.status == "loaded"
    assert loaded.manifest is not None
    arguments = loaded.manifest.pending_tool_frames[0]["arguments"]
    assert arguments == {
        "opaque_id": str(value_id),
        "observed_at": str(value_time),
    }


def test_atomic_replace_failure_preserves_previous_valid_checkpoint(tmp_path, monkeypatch) -> None:
    import app.runtime.recovery_manifest_store as store

    authority = _authority()
    session = _session(authority, "ORIGINAL")
    persist_recovery_manifest(authority, session, data_root=tmp_path)
    path = recovery_manifest_path(authority, data_root=tmp_path)
    original = path.read_bytes()
    session.pending_items[:] = ["NEWER"]
    real_replace = store.os.replace

    def fail_destination_replace(source, destination):
        if str(destination) == str(path):
            raise OSError("injected atomic replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(store.os, "replace", fail_destination_replace)

    with pytest.raises(OSError, match="injected atomic replace failure"):
        persist_recovery_manifest(authority, session, data_root=tmp_path)

    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_empty_checkpoint_deletes_only_current_authority_path(tmp_path) -> None:
    first = _authority(session_id="session-a", root_runtime_task_id="root-a")
    second = replace(first, session_id="session-b", root_session_id="session-b", root_runtime_task_id="root-b")
    persist_recovery_manifest(first, _session(first, "FIRST"), data_root=tmp_path)
    persist_recovery_manifest(second, _session(second, "SECOND"), data_root=tmp_path)

    deleted = persist_recovery_manifest(
        first,
        SessionContext(session_id=first.session_id),
        data_root=tmp_path,
        delete_if_empty=True,
    )

    assert deleted.status == "deleted"
    assert not recovery_manifest_path(first, data_root=tmp_path).exists()
    assert recovery_manifest_path(second, data_root=tmp_path).exists()


def test_restoration_pointer_names_exact_scoped_envelope_and_hash(tmp_path) -> None:
    authority = _authority()
    persist_recovery_manifest(authority, _session(authority, "指针POINTER"), data_root=tmp_path)
    result = load_recovery_manifest(authority, data_root=tmp_path)

    text = result.render_restoration_text(budget_chars=700)

    assert result.manifest_ref is not None and result.manifest_ref in text
    assert result.envelope_sha256 is not None and result.envelope_sha256 in text
    assert result.resource_path is not None
    assert result.resource_path.exists()
    raw = result.resource_path.read_bytes()
    assert f'"manifest_bytes":{len(raw)}' in text
    assert f'"manifest_chars":{len(raw.decode("utf-8"))}' in text
    assert len(raw) > len(raw.decode("utf-8"))
    assert '"reader_tool":"read_context_resource"' in text
    assert "omitted_fields" in text


def test_rendered_recovery_pointer_remains_byte_exact_after_later_checkpoint(tmp_path) -> None:
    authority = _authority()
    first_session = _session(authority, "FIRST_POINTER")
    persist_recovery_manifest(authority, first_session, data_root=tmp_path)
    first = load_recovery_manifest(authority, data_root=tmp_path)
    first_text = first.render_restoration_text(budget_chars=700)

    assert first.resource_path is not None
    first_bytes = first.resource_path.read_bytes()
    first_ref = first.manifest_ref

    second_session = _session(authority, "SECOND_POINTER")
    persist_recovery_manifest(authority, second_session, data_root=tmp_path)
    second = load_recovery_manifest(authority, data_root=tmp_path)
    second_text = second.render_restoration_text(budget_chars=700)

    assert first.resource_path.read_bytes() == first_bytes
    assert first_ref in first_text
    assert second.manifest_ref in second_text
    assert first.manifest_ref != second.manifest_ref
    assert first.resource_path != second.resource_path


def test_path_is_hash_scoped_and_cannot_escape_with_hostile_session_id(tmp_path) -> None:
    authority = _authority(session_id="../../other-agent/../session")

    path = recovery_manifest_path(authority, data_root=tmp_path).resolve()
    agent_root = (tmp_path / authority.agent_id).resolve()

    assert path.is_relative_to(agent_root)
    assert ".." not in path.parts
    assert authority.session_id not in path.as_posix()


def test_authority_rejects_agent_path_traversal_before_storage_resolution() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        _authority(agent_id="../../other-agent")


@pytest.mark.parametrize("symlink_level", ["agent", "runtime_artifacts"])
def test_store_rejects_symlink_escape_from_agent_root(tmp_path, symlink_level: str) -> None:
    authority = _authority()
    outside = tmp_path.parent / f"{tmp_path.name}-{symlink_level}-outside"
    outside.mkdir()
    agent_root = tmp_path / authority.agent_id
    if symlink_level == "agent":
        agent_root.symlink_to(outside, target_is_directory=True)
    else:
        agent_root.mkdir()
        (agent_root / "runtime_artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink|escapes"):
        recovery_manifest_path(authority, data_root=tmp_path)


def test_render_rechecks_snapshot_path_after_symlink_swap(tmp_path) -> None:
    authority = _authority()
    persist_recovery_manifest(authority, _session(authority, "SAFE"), data_root=tmp_path)
    loaded = load_recovery_manifest(authority, data_root=tmp_path)
    assert loaded.resource_path is not None
    snapshots_dir = loaded.resource_path.parent.parent
    outside = tmp_path.parent / f"{tmp_path.name}-snapshot-swap-outside"
    outside.mkdir()
    snapshots_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="snapshot.*escapes"):
        loaded.render_restoration_text(budget_chars=700)

    assert not list(outside.rglob("*.json"))


def test_runtime_authority_resolution_binds_principal_policy_config_root_and_transcript() -> None:
    from app.kernel.contracts import ExecutionIdentityRef, InvocationRequest, RuntimeConfig
    from app.runtime.ccplus_contracts import permission_profile_snapshot_hash

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    source_agent_id = uuid4()
    permission_profile = {"mode": "accept_edits", "allowed_tools": ["read_file"]}
    session = SessionContext(
        session_id="child-session",
        metadata={
            "permission_profile": permission_profile,
            "runtime_task_id": "child-task",
            "root_runtime_task_id": "root-task",
            "base_transcript_sequence": 77,
            "execution_principal": {
                "schema": "hive.execution_principal.v1",
                "tenant_id": str(tenant_id),
                "source_agent_id": str(source_agent_id),
                "requester_user_id": str(requester_id),
                "root_session_id": "root-session",
                "root_runtime_task_id": "root-task",
                "origin": "agent_tool",
                "delegation_chain": ["hop-a", "hop-b"],
            },
            "a2a_authority_required": True,
            "a2a_authority_policy_hash": permission_profile_snapshot_hash(permission_profile),
            "a2a_authority_snapshot_hash": "delegation-authority-hash",
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(),
        messages=[],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=requester_id,
        execution_identity=ExecutionIdentityRef(
            identity_type="delegated_user",
            identity_id=requester_id,
            label="requester",
        ),
        session_context=session,
        memory_session_id="child-session",
        allowed_tool_names=("read_file",),
        excluded_tool_names=("write_file",),
    )

    resolution = resolve_recovery_authority(
        request,
        RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=8, execution_mode="default"),
    )

    assert resolution.status == "bound"
    assert resolution.reason is None
    assert resolution.frame is not None
    assert resolution.frame.tenant_id == str(tenant_id)
    assert resolution.frame.agent_id == str(agent_id)
    assert resolution.frame.requester_user_id == str(requester_id)
    assert resolution.frame.root_session_id == "root-session"
    assert resolution.frame.root_runtime_task_id == "root-task"
    assert resolution.frame.base_transcript_sequence == 77
    assert resolution.frame.delegation_authority_hash == "delegation-authority-hash"
    assert resolution.frame.policy_snapshot_hash == permission_profile_snapshot_hash(permission_profile)
    assert resolution.frame.config_snapshot_hash
    assert resolution.frame.principal_snapshot_hash

    model_changed = resolve_recovery_authority(
        replace(
            request,
            model=SimpleNamespace(
                provider="anthropic",
                model="claude-test",
                max_input_tokens=256_000,
            ),
        ),
        RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=8, execution_mode="default"),
    )
    assert model_changed.frame is not None
    assert model_changed.frame.config_snapshot_hash != resolution.frame.config_snapshot_hash


def test_zero_base_transcript_sequence_is_preserved_as_authority(tmp_path) -> None:
    from app.kernel.contracts import InvocationRequest, RuntimeConfig

    tenant_id = uuid4()
    agent_id = uuid4()
    requester_id = uuid4()
    permission_profile = {"mode": "default"}
    session = SessionContext(
        session_id="zero-sequence-session",
        metadata={
            "permission_profile": permission_profile,
            "runtime_task_id": "zero-sequence-task",
            "base_transcript_sequence": 0,
            "initial_user_message_t0_sequence": 99,
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(),
        messages=[],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=requester_id,
        session_context=session,
        memory_session_id="zero-sequence-session",
    )

    resolution = resolve_recovery_authority(
        request,
        RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=8),
    )

    assert resolution.frame is not None
    assert resolution.frame.base_transcript_sequence == 0
    session.pending_items.append("ZERO_SEQUENCE")
    persisted = persist_recovery_manifest(resolution.frame, session, data_root=tmp_path)
    assert persisted.status == "written"


def test_runtime_authority_resolution_is_typed_unavailable_for_missing_session_or_a2a_policy_drift() -> None:
    from app.kernel.contracts import InvocationRequest, RuntimeConfig

    tenant_id = uuid4()
    agent_id = uuid4()
    missing_session = InvocationRequest(
        model=SimpleNamespace(),
        messages=[],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        session_context=SessionContext(session_id=None),
    )
    missing = resolve_recovery_authority(
        missing_session,
        RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=8),
    )
    assert missing.status == "unavailable"
    assert missing.reason == "session_id_unavailable"
    assert missing.frame is None

    missing_root_session = SessionContext(session_id="session-without-root", metadata={})
    missing_root = resolve_recovery_authority(
        replace(
            missing_session,
            session_context=missing_root_session,
            memory_session_id="session-without-root",
        ),
        RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=8),
    )
    assert missing_root.status == "unavailable"
    assert missing_root.reason == "root_runtime_task_id_unavailable"
    assert missing_root.frame is None

    drift_session = SessionContext(
        session_id="session-a",
        metadata={
            "permission_profile": {"mode": "default"},
            "a2a_authority_required": True,
            "a2a_authority_policy_hash": "stale-policy-hash",
        },
    )
    drift = resolve_recovery_authority(
        replace(missing_session, session_context=drift_session, memory_session_id="session-a"),
        RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=8),
    )
    assert drift.status == "unavailable"
    assert drift.reason == "a2a_policy_snapshot_drift"
    assert drift.frame is None
