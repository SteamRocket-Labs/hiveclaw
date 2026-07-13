"""P1-W3-9 — RecoveryManifest is now actually persisted on compaction.

The dataclass existed but only `app/evals/run.py` ever built one. This
test pins the new path:
  - build_recovery_manifest pulls the structured state from SessionContext
  - the in-process JSON shape matches what the kernel writes to
    `runtime_artifacts/recovery_manifest.json` on PRE_COMPACTION

Driving the kernel through a real compaction is heavy; instead we
exercise the construction + serialization that the kernel inlines, and
add a smoke test for the round-trip JSON write so future contributors
who change the shape have a forcing function.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.runtime.recovery_manifest import (
    build_recovery_manifest,
    hydrate_session_context_from_recovery_manifest,
    load_and_hydrate_recovery_manifest,
    load_recovery_manifest,
    persist_recovery_manifest,
    recovery_manifest_path,
)
from app.runtime.session import SessionContext


def _authority_metadata(
    *,
    runtime_task_id: str = "run-1",
    claim_version: int = 1,
    claim_worker_id: str | None = None,
    **extra,
) -> dict:
    return {
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "runtime_task_id": runtime_task_id,
        "claim_version": claim_version,
        "claim_worker_id": claim_worker_id or f"worker-{claim_version}",
        **extra,
    }


def _reviewed_manifest_evidence(
    *,
    agent_id: str,
    tenant_id: str,
    session_id: str,
    runtime_task_id: str,
    data_root: Path,
) -> dict[str, str | None]:
    from app.runtime.recovery_manifest import (
        inspect_recovery_manifest_checkpoint,
        reviewed_recovery_manifest_evidence,
    )

    return reviewed_recovery_manifest_evidence(
        inspect_recovery_manifest_checkpoint(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            data_root=data_root,
        )
    )


# ── Manifest carries the right runtime state ─────────────────


def test_manifest_inherits_recent_files_writes_skills_packs() -> None:
    sc = SessionContext(session_id="s1")
    sc.track_file_read("memory/feedback.md")
    sc.track_file_write("workspace/note.md")
    sc.track_skill_loaded("web-research")
    sc.active_tool_groups.append({"name": "web_pack", "summary": "search", "tools": []})
    sc.track_pending_item("finish report")
    sc.track_tool_outcome("web_search", "found 3 hits")

    manifest = build_recovery_manifest(sc)

    assert manifest.session_id == "s1"
    assert "memory/feedback.md" in manifest.recent_reads
    assert "workspace/note.md" in manifest.recent_writes
    assert manifest.current_turn_writes == ["workspace/note.md"]
    assert "web-research" in manifest.active_skills
    assert "web_pack" in manifest.active_tool_groups
    assert "finish report" in manifest.pending_items
    assert any(o.get("tool") == "web_search" for o in manifest.recent_tool_outcomes)


def test_empty_session_context_yields_empty_manifest() -> None:
    """No tracked state → is_empty() True so the kernel skips persistence."""
    manifest = build_recovery_manifest(SessionContext())
    assert manifest.is_empty()


# ── Persistence shape (mirrors the kernel write) ─────────────


def test_manifest_round_trips_through_json(tmp_path) -> None:
    """The kernel writes the same field-by-field shape; round-trip
    through json.loads must preserve every key the rehydrator looks at."""
    sc = SessionContext(session_id="abc")
    sc.track_file_read("a.md")
    sc.track_file_write("b.md")
    sc.track_skill_loaded("k")
    sc.active_tool_groups.append({"name": "p", "summary": "", "tools": []})
    sc.track_pending_item("x")
    sc.track_external_ref("https://example.com")
    sc.track_tool_outcome("read_file", "ok")

    manifest = build_recovery_manifest(sc)

    payload = {
        "session_id": manifest.session_id,
        "recent_reads": manifest.recent_reads,
        "recent_writes": manifest.recent_writes,
        "current_turn_writes": manifest.current_turn_writes,
        "recent_tool_outcomes": manifest.recent_tool_outcomes,
        "active_skills": manifest.active_skills,
        "active_tool_groups": manifest.active_tool_groups,
        "recent_external_refs": manifest.recent_external_refs,
        "pending_items": manifest.pending_items,
        "blocked_patterns": manifest.blocked_patterns,
    }

    target = tmp_path / "recovery_manifest.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    decoded = json.loads(target.read_text(encoding="utf-8"))
    assert decoded["session_id"] == "abc"
    assert decoded["recent_reads"] == ["a.md"]
    assert decoded["recent_writes"] == ["b.md"]
    assert decoded["current_turn_writes"] == ["b.md"]
    assert decoded["active_skills"] == ["k"]
    assert decoded["active_tool_groups"] == ["p"]
    assert decoded["pending_items"] == ["x"]
    assert decoded["recent_external_refs"] == ["https://example.com"]
    assert decoded["recent_tool_outcomes"][0]["tool"] == "read_file"


def test_load_recovery_manifest_reads_runtime_artifacts(tmp_path) -> None:
    agent_id = "agent-1"
    manifest_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                **_authority_metadata(),
                "recent_reads": ["workspace/report.md"],
                "recent_writes": ["workspace/output.md"],
                "current_turn_writes": ["workspace/output.md"],
                "recent_tool_outcomes": [{"tool": "web_search", "summary": "found source"}],
                "active_skills": ["research"],
                "active_tool_groups": ["web_pack"],
                "recent_external_refs": ["https://example.com"],
                "pending_items": ["finish D8 recovery"],
                "blocked_patterns": ["do not retry stale tool"],
                "discovered_tools": ["web_search"],
                "pending_tool_frames": [{"tool_name": "write_file", "status": "pending"}],
                "permission_checkpoints": [{"permission_request_id": "perm-1", "decision": "allow_once"}],
                "hook_lifecycle_records": [{"hook": "pre_tool_use", "status": "ok"}],
                "compaction_lifecycle_records": [{"phase": "post", "status": "ok"}],
                "permission_profile": {"mode": "default", "allowed_tools": ["write_file"]},
                "mcp_assignments": [{"server": "docs", "tool": "search"}],
                "truth_evidence_refs": ["truth://policy/email-confirmation"],
                "truth_evidence": [{"evidence_id": "truth://policy/email-confirmation"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest = load_recovery_manifest(
        agent_id,
        session_context=SessionContext(session_id="session-1", metadata=_authority_metadata()),
        data_root=tmp_path,
    )

    assert manifest is not None
    assert manifest.session_id == "session-1"
    assert manifest.recent_reads == ["workspace/report.md"]
    assert manifest.current_turn_writes == ["workspace/output.md"]
    assert manifest.pending_items == ["finish D8 recovery"]
    assert manifest.permission_profile["allowed_tools"] == ["write_file"]
    assert manifest.mcp_assignments == [{"server": "docs", "tool": "search"}]
    assert manifest.truth_evidence_refs == ["truth://policy/email-confirmation"]
    assert manifest.truth_evidence == [{"evidence_id": "truth://policy/email-confirmation"}]


def test_recovery_manifest_hydrates_session_context_runtime_state(tmp_path) -> None:
    agent_id = "agent-1"
    manifest_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                **_authority_metadata(),
                "recent_reads": ["workspace/report.md"],
                "recent_writes": ["workspace/output.md"],
                "current_turn_writes": ["workspace/output.md"],
                "recent_tool_outcomes": [{"tool": "web_search", "summary": "found source"}],
                "active_skills": ["research"],
                "active_tool_groups": ["web_pack"],
                "recent_external_refs": ["https://example.com"],
                "pending_items": ["finish D10 hydrate"],
                "discovered_tools": ["exa_search"],
                "pending_tool_frames": [{"tool_name": "write_file", "tool_call_id": "call-1", "status": "running"}],
                "permission_checkpoints": [{"permission_request_id": "perm-1", "decision": "allow_once"}],
                "hook_lifecycle_records": [{"event": "PRE_TOOL_USE", "status": "ok"}],
                "compaction_lifecycle_records": [{"phase": "post", "status": "ok"}],
                "permission_profile": {"mode": "default", "allowed_tools": ["write_file"]},
                "mcp_assignments": [{"server": "docs", "tools": ["search"]}],
                "truth_evidence_refs": ["truth://policy/email-confirmation"],
                "truth_evidence": [{"evidence_id": "truth://policy/email-confirmation"}],
                "pending_skill_handoffs": [{"skill_slug": "research", "execution_tool": "spawn_subagent"}],
                "continuation_records": [{"source": "session_permission_resume", "origin_channel": "feishu"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = load_recovery_manifest(
        agent_id,
        session_context=SessionContext(session_id="session-1", metadata=_authority_metadata()),
        data_root=tmp_path,
    )
    session = SessionContext(
        session_id="session-1",
        metadata={**_authority_metadata(), "pending_tool_frames": [{"tool_call_id": "old"}]},
    )

    assert hydrate_session_context_from_recovery_manifest(session, manifest, agent_id=agent_id) is True

    assert session.recent_files == ["workspace/report.md"]
    assert session.recent_writes == ["workspace/output.md"]
    assert session.current_turn_writes == ["workspace/output.md"]
    assert session.recent_tool_outcomes == [{"tool": "web_search", "summary": "found source"}]
    assert session.active_skills == ["research"]
    assert session.active_tool_groups == [{"name": "web_pack", "summary": "", "tools": []}]
    assert session.recent_external_refs == ["https://example.com"]
    assert session.pending_items == ["finish D10 hydrate"]
    assert session.discovered_tools == ["exa_search"]
    assert session.metadata["pending_tool_frames"] == [
        {"tool_call_id": "old"},
        {"tool_name": "write_file", "tool_call_id": "call-1", "status": "running"},
    ]
    assert "permission_profile" not in session.metadata
    assert session.metadata["recovered_permission_checkpoint_evidence"] == [
        {"permission_request_id": "perm-1", "decision": "allow_once"}
    ]
    assert session.metadata["truth_evidence_refs"] == ["truth://policy/email-confirmation"]
    assert session.metadata["mcp_server_refs"] == ["docs"]
    assert session.metadata["pending_skill_handoffs"] == [
        {"skill_slug": "research", "execution_tool": "spawn_subagent"}
    ]
    assert session.metadata["recovered_from_manifest"] is True


def test_recovery_manifest_never_leaks_runtime_state_into_another_session() -> None:
    from app.runtime.recovery_manifest import RecoveryManifest

    manifest = RecoveryManifest(
        session_id="session-a",
        recent_writes=["workspace/plan-a.md"],
        current_turn_writes=["workspace/plan-a.md"],
        discovered_tools=["private_session_tool"],
        permission_profile={"mode": "full_access"},
    )
    session = SessionContext(session_id="session-b")

    hydrated = hydrate_session_context_from_recovery_manifest(session, manifest)

    assert hydrated is False
    assert session.recent_writes == []
    assert session.current_turn_writes == []
    assert session.discovered_tools == []
    assert "permission_profile" not in session.metadata
    assert "recovered_from_manifest" not in session.metadata


def test_recovery_manifest_never_restores_revoked_permission_authority() -> None:
    from app.runtime.recovery_manifest import RecoveryManifest

    manifest = RecoveryManifest(
        session_id="session-1",
        agent_id="agent-1",
        tenant_id="tenant-1",
        runtime_task_id="run-1",
        claim_version=1,
        claim_worker_id="worker-1",
        permission_profile={"mode": "bypassPermissions", "allowed_tools": ["send_email"]},
    )
    current_profile = {"mode": "default", "allowed_tools": []}
    session = SessionContext(
        session_id="session-1",
        metadata={**_authority_metadata(), "permission_profile": dict(current_profile)},
    )

    assert hydrate_session_context_from_recovery_manifest(session, manifest) is True

    assert session.metadata["permission_profile"] == current_profile
    restoration_text = manifest.to_restoration_text()
    assert "Permission Profile" not in restoration_text
    assert "bypassPermissions" not in restoration_text
    assert "send_email" not in restoration_text


def test_recovered_skill_handoff_cannot_restore_embedded_permission_authority() -> None:
    from app.runtime.recovery_manifest import RecoveryManifest

    manifest = RecoveryManifest(
        session_id="session-1",
        agent_id="agent-1",
        tenant_id="tenant-1",
        runtime_task_id="run-1",
        claim_version=1,
        claim_worker_id="worker-1",
        permission_checkpoints=[
            {
                "decision": "allow",
                "pending_frame": {
                    "tool_name": "send_email",
                    "permission_profile": {
                        "mode": "bypassPermissions",
                        "allowed_tools": ["send_email"],
                    },
                    "arguments": {
                        "to": "user@example.com",
                        "permission_profile": {"mode": "bypassPermissions"},
                        "authorization_scopes": [{"capability": "send_email"}],
                    },
                },
            }
        ],
        pending_tool_frames=[
            {
                "tool_call_id": "call-spawn",
                "tool_name": "spawn_subagent",
                "permission_profile": {"mode": "bypassPermissions"},
                "arguments": {
                    "task": "continue",
                    "permission_profile": {"mode": "auto", "allowed_tools": ["send_email"]},
                    "authorization_scopes": [{"capability": "send_email"}],
                },
            }
        ],
        pending_skill_handoffs=[
            {
                "skill_slug": "research",
                "execution_tool": "spawn_subagent",
                "permission_profile": {"mode": "bypassPermissions"},
                "tool_arguments": {
                    "task": "continue",
                    "permission_profile": {"mode": "auto", "allowed_tools": ["send_email"]},
                    "authorization_scopes": [{"capability": "send_email"}],
                },
            }
        ],
    )
    session = SessionContext(
        session_id="session-1",
        metadata={
            **_authority_metadata(),
            "permission_profile": {"mode": "default", "allowed_tools": []},
            "permission_checkpoints": [{"decision": "deny", "tool_name": "send_email"}],
        },
    )

    assert hydrate_session_context_from_recovery_manifest(session, manifest) is True

    recovered = session.metadata["pending_skill_handoffs"][0]
    assert "permission_profile" not in recovered
    assert "permission_profile" not in recovered["tool_arguments"]
    assert "authorization_scopes" not in recovered["tool_arguments"]
    recovered_frame = session.metadata["recovered_pending_tool_frames"][0]
    assert "permission_profile" not in recovered_frame
    assert "permission_profile" not in recovered_frame["arguments"]
    assert "authorization_scopes" not in recovered_frame["arguments"]
    assert session.metadata["permission_checkpoints"] == [{"decision": "deny", "tool_name": "send_email"}]
    assert session.metadata["recovered_permission_checkpoint_evidence"] == [
        {
            "decision": "allow",
            "pending_frame": {"tool_name": "send_email", "arguments": {"to": "user@example.com"}},
        }
    ]
    restoration_text = manifest.to_restoration_text()
    assert "bypassPermissions" not in restoration_text
    assert "allowed_tools" not in restoration_text


def test_persist_recovery_manifest_deletes_stale_empty_checkpoint(tmp_path) -> None:
    agent_id = "agent-1"
    sc = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(
            pending_tool_frame={
                "tool_call_id": "call-running",
                "tool_name": "write_file",
                "status": "running",
            },
        ),
    )

    written = persist_recovery_manifest(agent_id, sc, data_root=tmp_path)
    manifest_path = recovery_manifest_path(
        agent_id,
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )

    assert written == [manifest_path]
    assert load_recovery_manifest(agent_id, session_context=sc, data_root=tmp_path) is not None

    resumed = SessionContext(session_id="session-1", metadata=_authority_metadata())
    manifest = load_recovery_manifest(agent_id, session_context=resumed, data_root=tmp_path)
    assert hydrate_session_context_from_recovery_manifest(resumed, manifest, agent_id=agent_id)
    for key in (
        "pending_tool_frame",
        "pending_tool_frames",
        "recovered_pending_tool_frames",
        "recovered_tool_frame_reconciliation",
    ):
        resumed.metadata.pop(key, None)
    persist_recovery_manifest(agent_id, resumed, data_root=tmp_path, delete_if_empty=True)

    assert load_recovery_manifest(agent_id, session_context=resumed, data_root=tmp_path) is None
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["pending_tool_frames"] == []


def test_manifest_preserves_mcp_assignments_and_truth_evidence_from_metadata() -> None:
    sc = SessionContext(
        session_id="session-evidence",
        metadata={
            "mcp_assignments": [{"server": "docs", "tools": ["search"]}],
            "evidence_refs": "truth://policy/email-confirmation",
            "truth_evidence": json.dumps(
                [{"evidence_id": "truth://policy/email-confirmation", "citations": ["policy/email"]}],
                ensure_ascii=False,
            ),
        },
    )

    manifest = build_recovery_manifest(sc)

    assert manifest.mcp_assignments == [{"server": "docs", "tools": ["search"]}]
    assert manifest.truth_evidence_refs == ["truth://policy/email-confirmation"]
    assert manifest.truth_evidence == [
        {"evidence_id": "truth://policy/email-confirmation", "citations": ["policy/email"]}
    ]


def test_manifest_to_restoration_text_includes_each_section() -> None:
    """The render path that prompt_builder will eventually consume must
    surface every populated bucket — pin it now so future tweaks don't
    silently drop a section."""
    sc = SessionContext()
    sc.track_file_read("a.md")
    sc.track_file_write("b.md")
    sc.track_skill_loaded("k1")
    sc.active_tool_groups.append({"name": "web", "summary": "", "tools": []})
    sc.track_pending_item("ship feature")
    sc.track_external_ref("https://x.com")

    text = build_recovery_manifest(sc).to_restoration_text()

    assert "Recent Reads" in text
    assert "a.md" in text
    assert "Recent Writes" in text
    assert "b.md" in text
    assert "Active Skills" in text
    assert "k1" in text
    assert "Active Runtime Tool Groups" in text
    assert "web" in text
    assert "Pending Work" in text
    assert "ship feature" in text
    assert "External References" in text


def test_manifest_to_restoration_text_includes_mcp_and_truth_sections() -> None:
    sc = SessionContext(
        metadata={
            "mcp_assignments": [{"server": "docs", "tools": ["search"]}],
            "truth_evidence_refs": ["truth://policy/email-confirmation"],
            "truth_evidence": [{"evidence_id": "truth://policy/email-confirmation"}],
        }
    )

    text = build_recovery_manifest(sc).to_restoration_text()

    assert "MCP Assignments" in text
    assert "docs" in text
    assert "Truth Evidence Refs" in text
    assert "truth://policy/email-confirmation" in text
    assert "Truth Evidence" in text


def test_recovery_manifest_directory_contract_documented() -> None:
    """Every session owns one hashed checkpoint under runtime artifacts."""

    expected = Path("runtime_artifacts") / "recovery_manifests"
    assert expected.name == "recovery_manifests"
    assert expected.parent.name == "runtime_artifacts"


def test_recovery_manifest_paths_are_session_scoped_and_path_safe(tmp_path: Path) -> None:
    agent_id = "agent-1"
    run_id = uuid4()

    path_a = recovery_manifest_path(agent_id, session_id="session/a", data_root=tmp_path)
    path_b = recovery_manifest_path(agent_id, session_id="session-b", data_root=tmp_path)
    path_run_hex = recovery_manifest_path(
        agent_id,
        session_id="session-a",
        runtime_task_id=run_id.hex,
        data_root=tmp_path,
    )
    path_run_str = recovery_manifest_path(
        agent_id,
        session_id="session-a",
        runtime_task_id=str(run_id),
        data_root=tmp_path,
    )

    assert path_a != path_b
    assert path_run_hex == path_run_str
    assert path_a.parent.parent == tmp_path / agent_id / "runtime_artifacts" / "recovery_manifests"
    assert path_a.name.endswith(".json")
    assert "session/a" not in str(path_a)


def test_persisted_recovery_manifests_do_not_overwrite_another_session(tmp_path: Path) -> None:
    agent_id = "agent-1"
    session_a = SessionContext(
        session_id="session-a",
        metadata=_authority_metadata(runtime_task_id="run-a", claim_worker_id="worker-a"),
    )
    session_a.track_file_write("workspace/private-a.md")
    session_b = SessionContext(
        session_id="session-b",
        metadata=_authority_metadata(runtime_task_id="run-b", claim_worker_id="worker-b"),
    )
    session_b.track_file_write("workspace/private-b.md")

    written_a = persist_recovery_manifest(agent_id, session_a, data_root=tmp_path)
    written_b = persist_recovery_manifest(agent_id, session_b, data_root=tmp_path)

    assert written_a == [
        recovery_manifest_path(agent_id, session_id="session-a", runtime_task_id="run-a", data_root=tmp_path)
    ]
    assert written_b == [
        recovery_manifest_path(agent_id, session_id="session-b", runtime_task_id="run-b", data_root=tmp_path)
    ]
    loaded_a = load_recovery_manifest(agent_id, session_context=session_a, data_root=tmp_path)
    loaded_b = load_recovery_manifest(agent_id, session_context=session_b, data_root=tmp_path)
    assert loaded_a is not None and loaded_a.current_turn_writes == ["workspace/private-a.md"]
    assert loaded_b is not None and loaded_b.current_turn_writes == ["workspace/private-b.md"]


def test_legacy_manifest_without_session_identity_fails_closed(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "current_turn_writes": ["workspace/private-a.md"],
                "pending_tool_frames": [{"tool_name": "send_email", "status": "running"}],
                "permission_profile": {"mode": "full_access"},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_recovery_manifest(
        agent_id,
        session_context=SessionContext(session_id="session-b"),
        data_root=tmp_path,
    )

    assert loaded is None

    # Writer inventory: a trusted legacy file with authority-bearing state may
    # not be materialized canonically when the current reader lacks one of the
    # six trusted authority fields.
    incomplete_agent_id = "agent-2"
    incomplete_legacy = tmp_path / incomplete_agent_id / "runtime_artifacts" / "recovery_manifest.json"
    incomplete_legacy.parent.mkdir(parents=True)
    incomplete_raw = json.dumps(
        {
            "session_id": "session-2",
            "agent_id": incomplete_agent_id,
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-2",
            "claim_version": 1,
            "pending_tool_frames": [{"tool_call_id": "call-2", "tool_name": "read_file", "status": "running"}],
        },
        sort_keys=True,
    ).encode()
    incomplete_legacy.write_bytes(incomplete_raw)
    incomplete_reader = SessionContext(
        session_id="session-2",
        metadata={
            "agent_id": incomplete_agent_id,
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-2",
            "claim_version": 2,
        },
    )
    incomplete_canonical = recovery_manifest_path(
        incomplete_agent_id,
        session_id="session-2",
        runtime_task_id="run-2",
        data_root=tmp_path,
    )

    assert (
        load_recovery_manifest(
            incomplete_agent_id,
            session_context=incomplete_reader,
            data_root=tmp_path,
        )
        is None
    )
    assert not incomplete_canonical.exists()
    [quarantined] = list(
        (tmp_path / incomplete_agent_id / "runtime_artifacts" / "recovery_manifests" / "legacy_quarantine").glob(
            "runtime-*.json"
        )
    )
    assert quarantined.read_bytes() == incomplete_raw

    conflict_agent_id = "agent-3"
    conflict_legacy = tmp_path / conflict_agent_id / "runtime_artifacts" / "recovery_manifest.json"
    conflict_legacy.parent.mkdir(parents=True)
    conflict_raw = json.dumps(
        {
            "session_id": "session-3",
            "runtime_task_id": "run-3",
            "legacy_conflict": {"reason": "legacy sources diverged"},
        },
        sort_keys=True,
    ).encode()
    conflict_legacy.write_bytes(conflict_raw)
    trigger = SessionContext(
        session_id="trigger-3",
        metadata=_authority_metadata(
            agent_id=conflict_agent_id,
            runtime_task_id="trigger-run-3",
            pending_items=["trigger cleanup"],
        ),
    )

    persist_recovery_manifest(conflict_agent_id, trigger, data_root=tmp_path)
    conflict_canonical = recovery_manifest_path(
        conflict_agent_id,
        session_id="session-3",
        runtime_task_id="run-3",
        data_root=tmp_path,
    )
    assert not conflict_canonical.exists()
    [conflict_quarantine] = list(
        (tmp_path / conflict_agent_id / "runtime_artifacts" / "recovery_manifests" / "legacy_quarantine").glob(
            "runtime-*.json"
        )
    )
    assert conflict_quarantine.read_bytes() == conflict_raw


def test_stale_claim_cannot_overwrite_newer_session_checkpoint(tmp_path: Path) -> None:
    agent_id = "agent-1"
    shared_identity = {
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-1",
    }
    newer = SessionContext(
        session_id="session-1",
        metadata={**shared_identity, "claim_version": 2, "claim_worker_id": "worker-2"},
    )
    newer.track_file_write("workspace/newer.md")
    stale = SessionContext(
        session_id="session-1",
        metadata={**shared_identity, "claim_version": 1, "claim_worker_id": "worker-1"},
    )
    stale.track_file_write("workspace/stale.md")

    assert persist_recovery_manifest(agent_id, newer, data_root=tmp_path)
    assert persist_recovery_manifest(agent_id, stale, data_root=tmp_path) == []

    loaded = load_recovery_manifest(agent_id, session_context=newer, data_root=tmp_path)
    assert loaded is not None
    assert loaded.claim_version == 2
    assert loaded.current_turn_writes == ["workspace/newer.md"]


def test_same_claim_stale_checkpoint_sequence_cannot_overwrite_newer_state(tmp_path: Path) -> None:
    agent_id = "agent-1"
    identity = {
        "runtime_task_id": "run-1",
        "claim_version": 7,
        "claim_worker_id": "worker-1",
    }
    active = SessionContext(session_id="session-1", metadata=dict(identity))
    active.track_pending_item("checkpoint-1")
    assert persist_recovery_manifest(agent_id, active, data_root=tmp_path)
    active.pending_items[:] = ["checkpoint-2"]
    assert persist_recovery_manifest(agent_id, active, data_root=tmp_path)

    stale_same_claim = SessionContext(session_id="session-1", metadata=dict(identity))
    stale_same_claim.track_pending_item("stale-state")
    assert persist_recovery_manifest(agent_id, stale_same_claim, data_root=tmp_path) == []

    loaded = load_recovery_manifest(agent_id, session_context=active, data_root=tmp_path)
    assert loaded is not None
    assert loaded.checkpoint_seq == 2
    assert loaded.pending_items == ["checkpoint-2"]


def test_same_claim_different_worker_cannot_replace_checkpoint(tmp_path: Path) -> None:
    agent_id = "agent-1"
    first = SessionContext(
        session_id="session-1",
        metadata={
            "runtime_task_id": "run-1",
            "claim_version": 7,
            "claim_worker_id": "worker-1",
        },
    )
    first.track_pending_item("worker-1-state")
    assert persist_recovery_manifest(agent_id, first, data_root=tmp_path)
    competing = SessionContext(
        session_id="session-1",
        metadata={
            "runtime_task_id": "run-1",
            "claim_version": 7,
            "claim_worker_id": "worker-2",
        },
    )
    competing.track_pending_item("worker-2-state")

    assert persist_recovery_manifest(agent_id, competing, data_root=tmp_path) == []
    loaded = load_recovery_manifest(agent_id, session_context=first, data_root=tmp_path)
    assert loaded is not None
    assert loaded.claim_worker_id == "worker-1"
    assert loaded.pending_items == ["worker-1-state"]


def test_two_process_same_claim_checkpoint_race_has_one_valid_winner(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    child = tmp_path / "persist_recovery_racer.py"
    gate = tmp_path / "start"
    child.write_text(
        """
import json
import sys
import time
from pathlib import Path

from app.runtime.recovery_manifest import persist_recovery_manifest
from app.runtime.session import SessionContext

root = Path(sys.argv[1])
gate = Path(sys.argv[2])
label = sys.argv[3]
result_path = Path(sys.argv[4])
while not gate.exists():
    time.sleep(0.005)
session = SessionContext(
    session_id="session-race",
    metadata={
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-race",
        "claim_version": 7,
        "claim_worker_id": "worker-race",
    },
)
session.track_pending_item(label)
written = persist_recovery_manifest("agent-1", session, data_root=root)
result_path.write_text(json.dumps({"written": [str(path) for path in written]}), encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )
    result_a = tmp_path / "result-a.json"
    result_b = tmp_path / "result-b.json"
    processes = [
        subprocess.Popen(
            [sys.executable, str(child), str(tmp_path), str(gate), label, str(result_path)],
            cwd=backend_root,
        )
        for label, result_path in (("state-a", result_a), ("state-b", result_b))
    ]
    gate.write_text("go", encoding="utf-8")
    for process in processes:
        assert process.wait(timeout=10) == 0

    outcomes = [json.loads(path.read_text(encoding="utf-8"))["written"] for path in (result_a, result_b)]
    assert sorted(bool(outcome) for outcome in outcomes) == [False, True]
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-race",
        runtime_task_id="run-race",
        data_root=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["checkpoint_seq"] == 1
    assert payload["pending_items"] in (["state-a"], ["state-b"])


def test_two_process_claim_aba_race_always_keeps_newest_claim(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    child = tmp_path / "persist_recovery_claim_racer.py"
    gate = tmp_path / "start-claim-race"
    child.write_text(
        """
import json
import sys
import time
from pathlib import Path

from app.runtime.recovery_manifest import persist_recovery_manifest
from app.runtime.session import SessionContext

root = Path(sys.argv[1])
gate = Path(sys.argv[2])
claim = int(sys.argv[3])
result_path = Path(sys.argv[4])
while not gate.exists():
    time.sleep(0.005)
session = SessionContext(
    session_id="session-claim-race",
    metadata={
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-claim-race",
        "claim_version": claim,
        "claim_worker_id": f"worker-{claim}",
    },
)
session.track_pending_item(f"claim-{claim}")
written = persist_recovery_manifest("agent-1", session, data_root=root)
result_path.write_text(json.dumps({"written": bool(written)}), encoding="utf-8")
""".lstrip(),
        encoding="utf-8",
    )
    results = [tmp_path / "claim-1.json", tmp_path / "claim-2.json"]
    processes = [
        subprocess.Popen(
            [sys.executable, str(child), str(tmp_path), str(gate), str(claim), str(result)],
            cwd=backend_root,
        )
        for claim, result in zip((1, 2), results, strict=True)
    ]
    gate.write_text("go", encoding="utf-8")
    for process in processes:
        assert process.wait(timeout=10) == 0

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-claim-race",
        runtime_task_id="run-claim-race",
        data_root=tmp_path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["claim_version"] == 2
    assert payload["claim_worker_id"] == "worker-2"
    assert payload["pending_items"] == ["claim-2"]
    assert all(result.exists() for result in results)


def test_claim_tombstone_prevents_stale_worker_aba_after_checkpoint_clear(tmp_path: Path) -> None:
    agent_id = "agent-1"
    newer = SessionContext(
        session_id="session-1",
        metadata={"runtime_task_id": "run-1", "claim_version": 2},
    )
    newer.track_pending_item("new claim work")
    [path] = persist_recovery_manifest(agent_id, newer, data_root=tmp_path)
    newer.pending_items.clear()
    persist_recovery_manifest(agent_id, newer, data_root=tmp_path, delete_if_empty=True)

    stale = SessionContext(
        session_id="session-1",
        metadata={"runtime_task_id": "run-1", "claim_version": 1},
    )
    stale.track_pending_item("stale work")

    assert persist_recovery_manifest(agent_id, stale, data_root=tmp_path) == []
    tombstone = json.loads(path.read_text(encoding="utf-8"))
    assert tombstone["claim_version"] == 2
    assert tombstone["pending_items"] == []
    assert load_recovery_manifest(agent_id, session_context=newer, data_root=tmp_path) is None


def test_next_runtime_task_owns_a_distinct_checkpoint_for_the_same_session(tmp_path: Path) -> None:
    agent_id = "agent-1"
    first_run = SessionContext(
        session_id="session-1",
        metadata={"tenant_id": "tenant-1", "runtime_task_id": "run-1", "claim_version": 1},
    )
    first_run.track_pending_item("old turn")
    next_run = SessionContext(
        session_id="session-1",
        metadata={"tenant_id": "tenant-1", "runtime_task_id": "run-2", "claim_version": 1},
    )
    next_run.track_pending_item("new turn")

    [first_path] = persist_recovery_manifest(agent_id, first_run, data_root=tmp_path)
    [next_path] = persist_recovery_manifest(agent_id, next_run, data_root=tmp_path)

    assert first_path != next_path
    loaded_next = load_recovery_manifest(agent_id, session_context=next_run, data_root=tmp_path)
    assert loaded_next is not None
    assert loaded_next.runtime_task_id == "run-2"
    assert loaded_next.pending_items == ["new turn"]


def test_runless_legacy_import_strips_tool_and_permission_authority(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "recent_reads": ["workspace/source.md"],
                "recent_writes": ["workspace/result.md"],
                "current_turn_writes": ["workspace/result.md"],
                "active_skills": ["private-skill"],
                "discovered_tools": ["send_email"],
                "pending_tool_frames": [{"tool_name": "send_email", "status": "running"}],
                "permission_profile": {"mode": "full_access"},
            }
        ),
        encoding="utf-8",
    )
    current_run = SessionContext(
        session_id="session-1",
        metadata={"tenant_id": "tenant-1", "runtime_task_id": "run-new", "claim_version": 1},
    )

    imported = load_recovery_manifest(agent_id, session_context=current_run, data_root=tmp_path)

    assert imported is not None
    assert imported.recent_reads == ["workspace/source.md"]
    assert imported.recent_writes == ["workspace/result.md"]
    assert imported.current_turn_writes == []
    assert imported.active_skills == []
    assert imported.discovered_tools == []
    assert imported.pending_tool_frames == []
    assert imported.permission_profile == {}
    assert legacy_path.exists() is False
    canonical = load_recovery_manifest(agent_id, session_context=current_run, data_root=tmp_path)
    assert canonical is not None and canonical.runtime_task_id == "run-new"


def test_deleted_canonical_checkpoint_cannot_resurrect_quarantined_legacy_state(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "runtime_task_id": "run-1",
                "pending_tool_frames": [{"tool_call_id": "old", "tool_name": "send_email"}],
                "permission_profile": {"mode": "full_access"},
            }
        ),
        encoding="utf-8",
    )
    current_run = SessionContext(
        session_id="session-1",
        metadata=_authority_metadata(),
    )
    assert load_recovery_manifest(agent_id, session_context=current_run, data_root=tmp_path) is not None

    current_run.metadata.clear()
    current_run.metadata.update(_authority_metadata())
    persist_recovery_manifest(agent_id, current_run, data_root=tmp_path, delete_if_empty=True)

    assert legacy_path.exists() is False
    assert load_recovery_manifest(agent_id, session_context=current_run, data_root=tmp_path) is None


def test_new_writer_preserves_owned_legacy_run_for_later_resume(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "session_id": "session-a",
                "agent_id": agent_id,
                "tenant_id": "tenant-1",
                "runtime_task_id": "run-a",
                "claim_version": 1,
                "claim_worker_id": "worker-1",
                "recent_reads": ["workspace/a.md"],
                "pending_tool_frames": [{"tool_call_id": "call-a", "tool_name": "read_file"}],
            }
        ),
        encoding="utf-8",
    )
    session_b = SessionContext(
        session_id="session-b",
        metadata={"runtime_task_id": "run-b", "claim_version": 1},
    )
    session_b.track_pending_item("run b")

    assert persist_recovery_manifest(agent_id, session_b, data_root=tmp_path)

    resumed_a = load_recovery_manifest(
        agent_id,
        session_context=SessionContext(
            session_id="session-a",
            metadata=_authority_metadata(runtime_task_id="run-a"),
        ),
        data_root=tmp_path,
    )
    assert resumed_a is not None
    assert resumed_a.recent_reads == ["workspace/a.md"]
    assert resumed_a.pending_tool_frames == [{"tool_call_id": "call-a", "tool_name": "read_file"}]


def test_unrelated_writer_does_not_cut_over_incomplete_continuity_legacy_owner(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "session_id": "session-a",
                "runtime_task_id": "run-a",
                "recent_reads": ["workspace/a.md"],
            }
        ),
        encoding="utf-8",
    )
    session_b = SessionContext(
        session_id="session-b",
        metadata=_authority_metadata(runtime_task_id="run-b"),
    )
    session_b.track_pending_item("run b")

    assert persist_recovery_manifest(agent_id, session_b, data_root=tmp_path)
    assert legacy_path.exists()

    owner = SessionContext(
        session_id="session-a",
        metadata=_authority_metadata(runtime_task_id="run-a"),
    )
    resumed_a = load_recovery_manifest(agent_id, session_context=owner, data_root=tmp_path)

    assert resumed_a is not None
    assert resumed_a.runtime_task_id == "run-a"
    assert resumed_a.tenant_id == "tenant-1"
    assert resumed_a.claim_worker_id == "worker-1"
    assert resumed_a.recent_reads == ["workspace/a.md"]

    deployed_agent_id = "agent-continuity-backfill"
    deployed_path = recovery_manifest_path(
        deployed_agent_id,
        session_id="session-deployed",
        runtime_task_id="run-deployed",
        data_root=tmp_path,
    )
    deployed_path.parent.mkdir(parents=True)
    deployed_path.write_text(
        json.dumps(
            {
                "session_id": "session-deployed",
                "runtime_task_id": "run-deployed",
                "recent_reads": ["workspace/deployed.md"],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(deployed_path, 0o600)
    deployed_owner = SessionContext(
        session_id="session-deployed",
        metadata=_authority_metadata(
            agent_id=deployed_agent_id,
            runtime_task_id="run-deployed",
        ),
    )

    deployed = load_recovery_manifest(
        deployed_agent_id,
        session_context=deployed_owner,
        data_root=tmp_path,
    )
    deployed_payload = json.loads(deployed_path.read_text(encoding="utf-8"))
    assert deployed is not None and deployed.recent_reads == ["workspace/deployed.md"]
    assert all(
        deployed_payload[field] is not None
        for field in (
            "agent_id",
            "tenant_id",
            "session_id",
            "runtime_task_id",
            "claim_version",
            "claim_worker_id",
        )
    )


def test_owner_canonical_is_committed_before_legacy_retirement(tmp_path: Path, monkeypatch) -> None:
    import app.runtime.recovery_manifest as recovery

    agent_id = "agent-1"
    legacy_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps({"session_id": "session-a", "recent_reads": ["workspace/a.md"]}),
        encoding="utf-8",
    )
    owner = SessionContext(
        session_id="session-a",
        metadata={"runtime_task_id": "run-a", "claim_version": 1},
    )

    def crash_during_retirement(*_args, **_kwargs):
        raise OSError("simulated crash during legacy retirement")

    monkeypatch.setattr(recovery, "_retire_legacy_manifests", crash_during_retirement)
    try:
        recovery.load_recovery_manifest(agent_id, session_context=owner, data_root=tmp_path)
    except OSError as exc:
        assert "legacy retirement" in str(exc)
    else:
        raise AssertionError("legacy retirement crash was not injected")

    canonical = recovery_manifest_path(
        agent_id,
        session_id="session-a",
        runtime_task_id="run-a",
        data_root=tmp_path,
    )
    assert canonical.exists()
    assert json.loads(canonical.read_text(encoding="utf-8"))["recent_reads"] == ["workspace/a.md"]


def test_agent_writable_workspace_legacy_is_never_imported(tmp_path: Path) -> None:
    agent_id = "agent-1"
    current = SessionContext(
        session_id="session-1",
        metadata={
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-a",
        },
    )
    current.track_pending_item("trusted")
    [canonical] = persist_recovery_manifest(agent_id, current, data_root=tmp_path)
    trusted_bytes = canonical.read_bytes()

    untrusted = tmp_path / agent_id / "workspace" / "recovery_manifest.json"
    untrusted.parent.mkdir(parents=True)
    untrusted.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "runtime_task_id": "run-1",
                "claim_version": 999,
                "pending_tool_frames": [{"tool_call_id": "evil", "tool_name": "send_email", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )

    another = SessionContext(session_id="session-2", metadata={"runtime_task_id": "run-2"})
    another.track_pending_item("trigger cleanup")
    assert persist_recovery_manifest(agent_id, another, data_root=tmp_path)

    assert canonical.read_bytes() == trusted_bytes
    loaded = load_recovery_manifest(agent_id, session_context=current, data_root=tmp_path)
    assert loaded is not None
    assert loaded.pending_items == ["trusted"]
    assert loaded.claim_version == 1


def test_legacy_symlink_is_never_followed_or_imported(tmp_path: Path) -> None:
    agent_id = "agent-1"
    external = tmp_path / "external.json"
    external.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "runtime_task_id": "run-1",
                "claim_version": 999,
                "pending_items": ["external poison"],
            }
        ),
        encoding="utf-8",
    )
    legacy = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy.parent.mkdir(parents=True)
    legacy.symlink_to(external)
    session = SessionContext(session_id="session-1", metadata={"runtime_task_id": "run-1"})

    assert load_recovery_manifest(agent_id, session_context=session, data_root=tmp_path) is None
    assert external.exists()
    assert json.loads(external.read_text(encoding="utf-8"))["claim_version"] == 999


def test_legacy_fifo_fails_closed_without_blocking_worker(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"
    legacy.parent.mkdir(parents=True)
    os.mkfifo(legacy)
    session = SessionContext(session_id="session-1", metadata={"runtime_task_id": "run-1"})

    started = time.monotonic()
    loaded = load_recovery_manifest(agent_id, session_context=session, data_root=tmp_path)
    elapsed = time.monotonic() - started

    assert loaded is None
    assert elapsed < 0.5


def test_recovery_file_lock_times_out_instead_of_blocking_forever(tmp_path: Path) -> None:
    import app.runtime.recovery_manifest as recovery

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    recovery._ensure_private_directory(path.parent)
    lock_path = path.with_suffix(".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="Timed out acquiring recovery manifest lock"):
            with recovery._session_manifest_lock(path):
                raise AssertionError("contended lock must not be acquired")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert time.monotonic() - started < 1.0


def test_agent_writable_legacy_cannot_conflict_with_trusted_runtime_legacy(tmp_path: Path) -> None:
    agent_id = "agent-1"
    agent_root = tmp_path / agent_id
    runtime_legacy = agent_root / "runtime_artifacts" / "recovery_manifest.json"
    workspace_legacy = agent_root / "workspace" / "recovery_manifest.json"
    runtime_legacy.parent.mkdir(parents=True)
    workspace_legacy.parent.mkdir(parents=True)
    common = {
        "session_id": "session-1",
        "agent_id": agent_id,
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-1",
        "claim_version": 1,
        "claim_worker_id": "worker-1",
    }
    runtime_legacy.write_text(
        json.dumps(
            {
                **common,
                "pending_tool_frames": [{"tool_call_id": "call-read", "tool_name": "read_file", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )
    workspace_legacy.write_text(
        json.dumps(
            {
                **common,
                "pending_tool_frames": [{"tool_call_id": "call-write", "tool_name": "write_file", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )
    runtime_digest = hashlib.sha256(runtime_legacy.read_bytes()).hexdigest()
    workspace_digest = hashlib.sha256(workspace_legacy.read_bytes()).hexdigest()
    current = SessionContext(
        session_id="session-1",
        metadata={
            "agent_id": agent_id,
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
        },
    )

    loaded = load_recovery_manifest(agent_id, session_context=current, data_root=tmp_path)

    assert loaded is not None
    assert loaded.pending_tool_frames == [{"tool_call_id": "call-read", "tool_name": "read_file", "status": "running"}]
    canonical = recovery_manifest_path(
        agent_id,
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert payload["pending_tool_frames"][0]["tool_call_id"] == "call-read"
    assert payload["legacy_conflict"] == {}
    cutover = json.loads(
        (agent_root / "runtime_artifacts" / "recovery_manifests" / "legacy_cutover.json").read_text(encoding="utf-8")
    )
    assert cutover["legacy_conflicts"] == []
    assert {record["sha256"] for record in cutover["records"]} == {runtime_digest, workspace_digest}
    workspace_record = next(record for record in cutover["records"] if record["source"].startswith("workspace/"))
    assert workspace_record["status"] == "rejected_untrusted_agent_writable_source"
    assert runtime_legacy.exists() is False
    assert workspace_legacy.exists() is False


def test_legacy_conflict_never_overwrites_newer_valid_canonical(tmp_path: Path) -> None:
    agent_id = "agent-1"
    current = SessionContext(
        session_id="session-1",
        metadata={"runtime_task_id": "run-1", "claim_version": 5},
    )
    current.track_pending_item("valid-current")
    [canonical] = persist_recovery_manifest(agent_id, current, data_root=tmp_path)
    canonical_bytes = canonical.read_bytes()

    agent_root = tmp_path / agent_id
    for rel_path, call_id in (
        ("runtime_artifacts/recovery_manifest.json", "legacy-runtime"),
        ("workspace/recovery_manifest.json", "legacy-workspace"),
    ):
        legacy = agent_root / rel_path
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            json.dumps(
                {
                    "session_id": "session-1",
                    "runtime_task_id": "run-1",
                    "claim_version": 1,
                    "pending_tool_frames": [{"tool_call_id": call_id, "tool_name": "write_file", "status": "running"}],
                }
            ),
            encoding="utf-8",
        )

    another_session = SessionContext(session_id="session-2", metadata={"runtime_task_id": "run-2"})
    another_session.track_pending_item("trigger cutover")
    assert persist_recovery_manifest(agent_id, another_session, data_root=tmp_path)

    assert canonical.read_bytes() == canonical_bytes
    loaded = load_recovery_manifest(agent_id, session_context=current, data_root=tmp_path)
    assert loaded is not None
    assert loaded.claim_version == 5
    assert loaded.pending_items == ["valid-current"]

    incomplete_agent_id = "agent-incomplete"
    incomplete_path = recovery_manifest_path(
        incomplete_agent_id,
        session_id="session-incomplete",
        runtime_task_id="run-incomplete",
        data_root=tmp_path,
    )
    incomplete_path.parent.mkdir(parents=True)
    secret = "incomplete-canonical-secret"
    incomplete_raw = json.dumps(
        {
            "session_id": "session-incomplete",
            "agent_id": incomplete_agent_id,
            "runtime_task_id": "run-incomplete",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-incomplete",
                    "tool_name": "send_email",
                    "status": "running",
                    "arguments": {"secret": secret},
                }
            ],
        },
        sort_keys=True,
    ).encode()
    incomplete_path.write_bytes(incomplete_raw)
    os.chmod(incomplete_path, 0o600)
    incoming_legacy = tmp_path / incomplete_agent_id / "runtime_artifacts" / "recovery_manifest.json"
    incoming_legacy.write_text(
        json.dumps(
            {
                "session_id": "session-incomplete",
                "agent_id": incomplete_agent_id,
                "tenant_id": "tenant-1",
                "runtime_task_id": "run-incomplete",
                "claim_version": 2,
                "claim_worker_id": "worker-2",
                "pending_tool_frames": [{"tool_call_id": "call-legacy", "tool_name": "read_file", "status": "running"}],
            }
        ),
        encoding="utf-8",
    )
    trigger = SessionContext(
        session_id="trigger-incomplete",
        metadata=_authority_metadata(
            agent_id=incomplete_agent_id,
            runtime_task_id="trigger-incomplete",
            pending_items=["trigger cleanup"],
        ),
    )

    persist_recovery_manifest(incomplete_agent_id, trigger, data_root=tmp_path)
    assert incomplete_path.read_bytes() == incomplete_raw
    [raw_quarantine] = list((incomplete_path.parents[1] / "authority_quarantine").glob("*.json"))
    [conflict_sidecar] = list((incomplete_path.parents[1] / "authority_conflicts").glob("*.json"))
    assert raw_quarantine.read_bytes() == incomplete_raw
    assert secret not in conflict_sidecar.read_text(encoding="utf-8")
    cutover = json.loads(
        (tmp_path / incomplete_agent_id / "runtime_artifacts" / "recovery_manifests" / "legacy_cutover.json").read_text(
            encoding="utf-8"
        )
    )
    assert cutover["records"][0]["status"] == "preserved_canonical_incomplete_authority"


def test_atomic_checkpoint_failure_preserves_previous_valid_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent_id = "agent-1"
    session = SessionContext(session_id="session-1", metadata=_authority_metadata())
    session.track_file_write("workspace/before.md")
    [path] = persist_recovery_manifest(agent_id, session, data_root=tmp_path)
    before = path.read_bytes()
    real_replace = os.replace

    def crash_before_replace(_source, _target, **_kwargs) -> None:
        raise OSError("simulated kill before atomic replace")

    session.metadata["claim_version"] = 2
    session.track_file_write("workspace/after.md")
    monkeypatch.setattr(os, "replace", crash_before_replace)
    try:
        try:
            persist_recovery_manifest(agent_id, session, data_root=tmp_path)
        except OSError as exc:
            assert "simulated kill" in str(exc)
        else:
            raise AssertionError("checkpoint write did not use atomic replace")
    finally:
        monkeypatch.setattr(os, "replace", real_replace)

    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["current_turn_writes"] == ["workspace/before.md"]
    assert list(path.parent.glob("*.tmp")) == []


def test_sigkill_during_atomic_replace_preserves_previous_manifest_bytes(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    agent_id = "agent-1"
    session = SessionContext(
        session_id="session-kill",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-kill",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
        },
    )
    session.track_pending_item("before-kill")
    [path] = persist_recovery_manifest(agent_id, session, data_root=tmp_path)
    before = path.read_bytes()
    marker = tmp_path / "replace-ready"
    child = tmp_path / "persist_recovery_killed_writer.py"
    child.write_text(
        """
import os
import sys
import time
from pathlib import Path

import app.runtime.recovery_manifest as recovery
from app.runtime.session import SessionContext

root = Path(sys.argv[1])
target = Path(sys.argv[2])
marker = Path(sys.argv[3])
real_replace = recovery.os.replace

def stop_before_replace(source, destination, **kwargs):
    if Path(destination).name == target.name:
        marker.write_text(str(source), encoding="utf-8")
        while True:
            time.sleep(1)
    real_replace(source, destination, **kwargs)

recovery.os.replace = stop_before_replace
session = SessionContext(
    session_id="session-kill",
    metadata={
        "tenant_id": "tenant-1",
        "runtime_task_id": "run-kill",
        "claim_version": 2,
        "claim_worker_id": "worker-2",
    },
)
session.track_pending_item("after-kill")
recovery.persist_recovery_manifest("agent-1", session, data_root=root)
""".lstrip(),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(child), str(tmp_path), str(path), str(marker)],
        cwd=backend_root,
    )
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "child writer never reached the atomic replace boundary"
    temporary = path.parent / marker.read_text(encoding="utf-8")
    process.kill()
    assert process.wait(timeout=10) != 0

    assert path.read_bytes() == before
    assert json.loads(path.read_text(encoding="utf-8"))["pending_items"] == ["before-kill"]
    assert temporary.exists()
    assert temporary.stat().st_mode & 0o777 == 0o600


def test_checkpoint_creation_fsyncs_new_directory_chain(tmp_path: Path, monkeypatch) -> None:
    import app.runtime.recovery_manifest as recovery

    fsynced_directories: set[tuple[int, int]] = set()
    real_fsync = recovery.os.fsync

    def observe(descriptor: int) -> None:
        descriptor_stat = os.fstat(descriptor)
        if stat.S_ISDIR(descriptor_stat.st_mode):
            fsynced_directories.add((descriptor_stat.st_dev, descriptor_stat.st_ino))
        real_fsync(descriptor)

    monkeypatch.setattr(recovery.os, "fsync", observe)
    session = SessionContext(session_id="session-1", metadata={"runtime_task_id": "run-1"})
    session.track_pending_item("durable")

    [checkpoint] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)

    assert checkpoint.exists()
    assert len(fsynced_directories) >= 4


def test_checkpoint_and_legacy_quarantine_files_are_mode_0600(tmp_path: Path) -> None:
    agent_id = "agent-1"
    legacy = tmp_path / agent_id / "workspace" / "recovery_manifest.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"session_id":"session-untrusted"}', encoding="utf-8")
    os.chmod(legacy, 0o644)
    session = SessionContext(session_id="session-1", metadata={"runtime_task_id": "run-1"})
    session.track_pending_item("private")

    [checkpoint] = persist_recovery_manifest(agent_id, session, data_root=tmp_path)

    quarantined = list(
        (tmp_path / agent_id / "runtime_artifacts" / "recovery_manifests" / "legacy_quarantine").glob("*.json")
    )
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert quarantined
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in quarantined)


def test_corrupt_canonical_checkpoint_is_never_overwritten_by_a_new_writer(tmp_path: Path) -> None:
    agent_id = "agent-1"
    session = SessionContext(
        session_id="session-1",
        metadata={"runtime_task_id": "run-1", "claim_version": 2},
    )
    path = recovery_manifest_path(
        agent_id,
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    corrupt_bytes = b'{"session_id":"session-1","pending_tool_frames":['
    path.write_bytes(corrupt_bytes)
    session.track_pending_item("new writer must stop")

    written = persist_recovery_manifest(agent_id, session, data_root=tmp_path)

    assert written == []
    assert path.read_bytes() == corrupt_bytes
    assert load_recovery_manifest(agent_id, session_context=session, data_root=tmp_path) is None


def test_tenant_bound_session_rejects_tenantless_checkpoint(tmp_path: Path) -> None:
    agent_id = "agent-1"
    tenantless_writer = SessionContext(
        session_id="session-1",
        metadata={"runtime_task_id": "run-1", "claim_version": 1, "claim_worker_id": "worker-a"},
    )
    tenantless_writer.track_pending_item("must never cross tenant authority")
    assert persist_recovery_manifest(agent_id, tenantless_writer, data_root=tmp_path)

    tenant_bound_reader = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-b",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-a",
        },
    )

    assert (
        load_recovery_manifest(
            agent_id,
            session_context=tenant_bound_reader,
            data_root=tmp_path,
        )
        is None
    )


def test_operator_reconciliation_resolution_clears_durable_unknown_frame(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import resolve_recovery_manifest_reconciliation

    agent_id = "agent-1"
    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 1,
            "claim_worker_id": "worker-a",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-email",
                    "tool_name": "send_email",
                    "status": "needs_reconciliation",
                }
            ],
        },
    )
    assert persist_recovery_manifest(agent_id, session, data_root=tmp_path)

    receipt = resolve_recovery_manifest_reconciliation(
        agent_id=agent_id,
        tenant_id="tenant-1",
        session_id="session-1",
        runtime_task_id="run-1",
        action="mark_resolved",
        reason="operator verified the remote send exactly once",
        actor_user_id="operator-1",
        **_reviewed_manifest_evidence(
            agent_id=agent_id,
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-1",
            data_root=tmp_path,
        ),
        data_root=tmp_path,
    )

    assert receipt["sha256"]
    resolved = load_recovery_manifest(agent_id, session_context=session, data_root=tmp_path)
    assert resolved is not None
    assert resolved.pending_tool_frames == []
    assert resolved.continuation_records[-1]["source"] == "runtime_reconciliation"
    assert resolved.continuation_records[-1]["action"] == "mark_resolved"


def test_inspect_recovery_manifest_checkpoint_returns_reviewable_cas_evidence(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import inspect_recovery_manifest_checkpoint

    session = SessionContext(
        session_id="business-task-run-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 4,
            "claim_worker_id": "worker-4",
            "pending_tool_frames": [
                {"tool_call_id": "call-send", "tool_name": "send_email", "status": "needs_reconciliation"}
            ],
            "recovery_reconciliation_blocked": True,
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)

    snapshot = inspect_recovery_manifest_checkpoint(
        agent_id="agent-1",
        tenant_id="tenant-1",
        session_id="business-task-run-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )

    assert snapshot is not None
    assert snapshot["state"] == "valid"
    assert snapshot["receipt"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert snapshot["expected_checkpoint_seq"] == session.metadata["recovery_checkpoint_seq"]
    assert snapshot["expected_claim_version"] == 4
    assert snapshot["expected_claim_worker_id"] == "worker-4"
    assert snapshot["pending_tool_frames"][0]["tool_call_id"] == "call-send"
    assert snapshot["recovery_reconciliation_blocked"] is True
    assert snapshot["reconciliation_resolution"] == {}


def test_prior_run_reconciliation_block_survives_manifest_restart(tmp_path: Path) -> None:
    agent_id = "agent-1"
    prior = {
        "source_runtime_task_id": "run-previous",
        "status": "needs_reconciliation",
        "frames": [
            {
                "tool_call_id": "call-email",
                "tool_name": "send_email",
                "status": "needs_reconciliation",
            }
        ],
    }
    writer = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-current",
            "claim_version": 1,
            "claim_worker_id": "worker-current",
            "recovery_reconciliation_blocked": True,
            "prior_run_recovery_reconciliations": [prior],
        },
    )

    assert persist_recovery_manifest(agent_id, writer, data_root=tmp_path)

    restarted = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-current",
            "claim_version": 2,
            "claim_worker_id": "worker-restarted",
        },
    )
    restored = load_and_hydrate_recovery_manifest(agent_id, restarted, data_root=tmp_path)

    assert restored is not None
    assert restored.recovery_reconciliation_blocked is True
    assert restored.prior_run_recovery_reconciliations == [prior]
    assert restarted.metadata["recovery_reconciliation_blocked"] is True
    assert restarted.metadata["prior_run_recovery_reconciliations"] == [prior]


def test_operator_reconciliation_creates_durable_tombstone_when_manifest_is_missing(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import resolve_recovery_manifest_reconciliation

    receipt = resolve_recovery_manifest_reconciliation(
        agent_id="agent-1",
        tenant_id="tenant-1",
        session_id="session-1",
        runtime_task_id="run-missing",
        action="mark_resolved",
        reason="operator verified the prior run from transactional evidence",
        actor_user_id="operator-1",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-missing",
            data_root=tmp_path,
        ),
        data_root=tmp_path,
    )

    assert receipt["ephemeral"] is False
    payload = json.loads(Path(receipt["path"]).read_text(encoding="utf-8"))
    assert {
        field: payload[field]
        for field in (
            "agent_id",
            "tenant_id",
            "session_id",
            "runtime_task_id",
            "claim_version",
            "claim_worker_id",
        )
    } == {
        "agent_id": "agent-1",
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "runtime_task_id": "run-missing",
        "claim_version": 0,
        "claim_worker_id": "operator-reconciliation:operator-1",
    }
    reader = SessionContext(
        session_id="session-1",
        metadata={
            "agent_id": "agent-1",
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-missing",
            "claim_version": 0,
            "claim_worker_id": "operator-reconciliation:operator-1",
        },
    )
    resolved = load_recovery_manifest("agent-1", session_context=reader, data_root=tmp_path)
    assert resolved is not None
    assert resolved.pending_tool_frames == []
    assert resolved.continuation_records[-1]["source"] == "runtime_reconciliation"
    assert resolved.continuation_records[-1]["action"] == "mark_resolved"


def test_operator_reconciliation_quarantines_corrupt_manifest_before_tombstone(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestReconciliationError,
        inspect_recovery_manifest_checkpoint,
        resolve_recovery_manifest_reconciliation,
    )

    path = recovery_manifest_path(
        "agent-1",
        session_id="session-1",
        runtime_task_id="run-corrupt",
        data_root=tmp_path,
    )
    path.parent.mkdir(parents=True)
    corrupt = b'{"pending_tool_frames":['
    path.write_bytes(corrupt)
    os.chmod(path, 0o600)

    resolve_recovery_manifest_reconciliation(
        agent_id="agent-1",
        tenant_id="tenant-1",
        session_id="session-1",
        runtime_task_id="run-corrupt",
        action="archive",
        reason="remote evidence cannot prove completion",
        actor_user_id="operator-1",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-corrupt",
            data_root=tmp_path,
        ),
        data_root=tmp_path,
    )

    quarantine = tmp_path / "agent-1" / "runtime_artifacts" / "recovery_manifests" / "reconciliation_quarantine"
    quarantined = list(quarantine.glob("corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupt
    assert quarantined[0].stat().st_mode & 0o777 == 0o600

    incomplete_path = recovery_manifest_path(
        "agent-2",
        session_id="session-incomplete",
        runtime_task_id="run-incomplete",
        data_root=tmp_path,
    )
    incomplete_path.parent.mkdir(parents=True)
    secret = "operator-must-not-leak-this"
    incomplete_raw = json.dumps(
        {
            "session_id": "session-incomplete",
            "agent_id": "agent-2",
            "runtime_task_id": "run-incomplete",
            "claim_version": 1,
            "claim_worker_id": "worker-1",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-incomplete",
                    "tool_name": "send_email",
                    "status": "running",
                    "arguments": {"secret": secret},
                }
            ],
        },
        sort_keys=True,
    ).encode()
    incomplete_path.write_bytes(incomplete_raw)
    os.chmod(incomplete_path, 0o600)

    inspection = inspect_recovery_manifest_checkpoint(
        agent_id="agent-2",
        tenant_id="tenant-1",
        session_id="session-incomplete",
        runtime_task_id="run-incomplete",
        data_root=tmp_path,
    )
    assert inspection is not None and inspection["state"] == "incomplete_authority"

    with pytest.raises(RecoveryManifestReconciliationError, match="requires exact ref and sha256"):
        resolve_recovery_manifest_reconciliation(
            agent_id="agent-2",
            tenant_id="tenant-1",
            session_id="session-incomplete",
            runtime_task_id="run-incomplete",
            action="archive",
            reason="operator reviewed incomplete authority evidence",
            actor_user_id="operator-1",
            expected_manifest_state="incomplete_authority",
            data_root=tmp_path,
        )

    assert incomplete_path.read_bytes() == incomplete_raw
    receipt = inspection["receipt"]
    resolved_receipt = resolve_recovery_manifest_reconciliation(
        agent_id="agent-2",
        tenant_id="tenant-1",
        session_id="session-incomplete",
        runtime_task_id="run-incomplete",
        action="archive",
        reason="operator reviewed incomplete authority evidence",
        actor_user_id="operator-1",
        expected_manifest_state="incomplete_authority",
        expected_manifest_ref=receipt["ref"],
        expected_sha256=receipt["sha256"],
        data_root=tmp_path,
    )
    resolved_payload = json.loads(incomplete_path.read_text(encoding="utf-8"))
    assert resolved_receipt["source_state"] == "incomplete_authority"
    assert resolved_payload["tenant_id"] == "tenant-1"
    assert resolved_payload["pending_tool_frames"] == []
    assert resolved_payload["reconciliation_resolution"]["action"] == "archive"
    [authority_quarantine] = list((incomplete_path.parents[1] / "authority_quarantine").glob("*.json"))
    [authority_conflict] = list((incomplete_path.parents[1] / "authority_conflicts").glob("*.json"))
    assert authority_quarantine.read_bytes() == incomplete_raw
    assert secret not in authority_conflict.read_text(encoding="utf-8")


def test_reconciliation_batch_preflights_every_target_before_mutating_any_manifest(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestReconciliationError,
        resolve_recovery_manifest_reconciliations,
    )

    sessions = []
    paths = []
    for run_id in ("run-a", "run-b"):
        session = SessionContext(
            session_id="session-1",
            metadata={
                "tenant_id": "tenant-1",
                "runtime_task_id": run_id,
                "claim_version": 3,
                "claim_worker_id": "worker-3",
                "pending_tool_frames": [
                    {
                        "tool_call_id": f"call-{run_id}",
                        "tool_name": "send_email",
                        "status": "needs_reconciliation",
                    }
                ],
            },
        )
        [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
        sessions.append(session)
        paths.append(path)
    before_a = paths[0].read_bytes()
    external = tmp_path / "external.json"
    external.write_text("outside", encoding="utf-8")
    paths[1].unlink()
    paths[1].symlink_to(external)

    with pytest.raises(RecoveryManifestReconciliationError, match="not a regular file"):
        resolve_recovery_manifest_reconciliations(
            targets=[
                {
                    "agent_id": "agent-1",
                    "session_id": "session-1",
                    "runtime_task_id": run_id,
                    **_reviewed_manifest_evidence(
                        agent_id="agent-1",
                        tenant_id="tenant-1",
                        session_id="session-1",
                        runtime_task_id=run_id,
                        data_root=tmp_path,
                    ),
                    "expected_checkpoint_seq": session.metadata["recovery_checkpoint_seq"],
                    "expected_claim_version": 3,
                    "expected_claim_worker_id": "worker-3",
                }
                for run_id, session in zip(("run-a", "run-b"), sessions, strict=True)
            ],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="verified both sends",
            actor_user_id="operator-1",
            operation_id="operation-1",
            data_root=tmp_path,
        )

    assert paths[0].read_bytes() == before_a
    assert external.read_text(encoding="utf-8") == "outside"


def test_reconciliation_batch_rejects_stale_manifest_cas_without_clearing_new_frame(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestReconciliationError,
        resolve_recovery_manifest_reconciliations,
    )

    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 4,
            "claim_worker_id": "worker-4",
            "pending_tool_frames": [
                {"tool_call_id": "call-old", "tool_name": "send_email", "status": "needs_reconciliation"}
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    old_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    old_review = _reviewed_manifest_evidence(
        agent_id="agent-1",
        tenant_id="tenant-1",
        session_id="session-1",
        runtime_task_id="run-1",
        data_root=tmp_path,
    )
    old_seq = session.metadata["recovery_checkpoint_seq"]
    session.metadata["pending_tool_frames"].append(
        {"tool_call_id": "call-new", "tool_name": "write_file", "status": "needs_reconciliation"}
    )
    assert persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    latest = path.read_bytes()

    with pytest.raises(RecoveryManifestReconciliationError, match="changed since operator review"):
        resolve_recovery_manifest_reconciliations(
            targets=[
                {
                    "agent_id": "agent-1",
                    "session_id": "session-1",
                    "runtime_task_id": "run-1",
                    **old_review,
                    "expected_sha256": old_sha,
                    "expected_checkpoint_seq": old_seq,
                    "expected_claim_version": 4,
                    "expected_claim_worker_id": "worker-4",
                }
            ],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="reviewed only the old frame",
            actor_user_id="operator-1",
            operation_id="operation-stale",
            data_root=tmp_path,
        )

    assert path.read_bytes() == latest
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert [frame["tool_call_id"] for frame in restored.pending_tool_frames] == ["call-old", "call-new"]


def test_reconciliation_rejects_reviewed_manifest_reference_mismatch_without_mutation(
    tmp_path: Path,
) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestEvidenceDriftError,
        inspect_recovery_manifest_checkpoint,
        resolve_recovery_manifest_reconciliations,
    )

    session = SessionContext(
        session_id="session-ref-cas",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-ref-cas",
            "claim_version": 4,
            "claim_worker_id": "worker-ref-cas",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-ref-cas",
                    "tool_name": "send_email",
                    "status": "needs_reconciliation",
                }
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    inspection = inspect_recovery_manifest_checkpoint(
        agent_id="agent-1",
        tenant_id="tenant-1",
        session_id="session-ref-cas",
        runtime_task_id="run-ref-cas",
        data_root=tmp_path,
    )
    assert inspection is not None
    before = path.read_bytes()

    with pytest.raises(RecoveryManifestEvidenceDriftError, match="reference mismatch"):
        resolve_recovery_manifest_reconciliations(
            targets=[
                {
                    "agent_id": "agent-1",
                    "session_id": "session-ref-cas",
                    "runtime_task_id": "run-ref-cas",
                    "expected_manifest_state": "present",
                    "expected_manifest_ref": "runtime_artifacts/recovery_manifests/wrong.json",
                    "expected_sha256": inspection["receipt"]["sha256"],
                    "expected_checkpoint_seq": inspection["expected_checkpoint_seq"],
                    "expected_claim_version": 4,
                    "expected_claim_worker_id": "worker-ref-cas",
                }
            ],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="reviewed a byte-identical manifest at another authority path",
            actor_user_id="operator-1",
            operation_id="operation-ref-cas",
            data_root=tmp_path,
        )

    assert path.read_bytes() == before


def test_missing_review_state_rejects_manifest_created_before_resolution_without_clearing_frame(
    tmp_path: Path,
) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestEvidenceDriftError,
        resolve_recovery_manifest_reconciliations,
    )

    target = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "runtime_task_id": "run-created-after-review",
        "expected_manifest_state": "missing",
        "expected_manifest_ref": None,
        "expected_sha256": None,
    }
    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-created-after-review",
            "claim_version": 8,
            "claim_worker_id": "worker-8",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-created-after-review",
                    "tool_name": "send_email",
                    "status": "needs_reconciliation",
                }
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    bytes_with_new_side_effect_evidence = path.read_bytes()

    with pytest.raises(RecoveryManifestEvidenceDriftError, match="state changed since operator review"):
        resolve_recovery_manifest_reconciliations(
            targets=[target],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="reviewed a missing manifest before the peer side effect arrived",
            actor_user_id="operator-1",
            operation_id="operation-missing-state-cas",
            data_root=tmp_path,
        )

    assert path.read_bytes() == bytes_with_new_side_effect_evidence
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert [frame["tool_call_id"] for frame in restored.pending_tool_frames] == ["call-created-after-review"]


def test_unreviewed_target_cannot_archive_manifest_created_after_operator_review(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestReconciliationError,
        resolve_recovery_manifest_reconciliations,
    )

    target = {
        "agent_id": "agent-1",
        "session_id": "business-task-session",
        "runtime_task_id": "business-task-run",
        "source": "business_task",
    }
    session = SessionContext(
        session_id="business-task-session",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "business-task-run",
            "claim_version": 1,
            "claim_worker_id": "business-task:worker",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-business-write",
                    "tool_name": "write_file",
                    "status": "running",
                }
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    original_raw = path.read_bytes()

    with pytest.raises(RecoveryManifestReconciliationError, match="reviewed manifest state"):
        resolve_recovery_manifest_reconciliations(
            targets=[target],
            tenant_id="tenant-1",
            action="archive",
            reason="operator reviewed the original business task evidence",
            actor_user_id="operator-1",
            operation_id="operation-unreviewed-business-task",
            data_root=tmp_path,
        )

    assert path.read_bytes() == original_raw
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert [frame["tool_call_id"] for frame in restored.pending_tool_frames] == ["call-business-write"]


@pytest.mark.parametrize(
    ("expected_state", "actual_kind"),
    [
        ("corrupt", "missing"),
        ("nonregular", "missing"),
        ("identity_mismatch", "present"),
    ],
)
def test_reviewed_manifest_state_is_a_required_resolution_cas(
    tmp_path: Path,
    expected_state: str,
    actual_kind: str,
) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestEvidenceDriftError,
        resolve_recovery_manifest_reconciliations,
    )

    target = {
        "agent_id": "agent-1",
        "session_id": "session-state-cas",
        "runtime_task_id": f"run-{expected_state}",
        "expected_manifest_state": expected_state,
        "expected_manifest_ref": (
            None if expected_state == "nonregular" else "runtime_artifacts/recovery_manifests/reviewed.json"
        ),
        "expected_sha256": None if expected_state == "nonregular" else "a" * 64,
    }
    path = recovery_manifest_path(
        "agent-1",
        session_id="session-state-cas",
        runtime_task_id=f"run-{expected_state}",
        data_root=tmp_path,
    )
    if actual_kind == "present":
        session = SessionContext(
            session_id="session-state-cas",
            metadata={
                "tenant_id": "tenant-1",
                "runtime_task_id": f"run-{expected_state}",
                "claim_version": 1,
                "claim_worker_id": "worker-state-cas",
                "pending_tool_frames": [
                    {
                        "tool_call_id": f"call-{expected_state}",
                        "tool_name": "send_email",
                        "status": "needs_reconciliation",
                    }
                ],
            },
        )
        assert persist_recovery_manifest("agent-1", session, data_root=tmp_path) == [path]

    before = path.read_bytes() if path.exists() else None
    with pytest.raises(RecoveryManifestEvidenceDriftError, match="state changed since operator review"):
        resolve_recovery_manifest_reconciliations(
            targets=[target],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="state CAS",
            actor_user_id="operator-1",
            operation_id=f"operation-{expected_state}",
            data_root=tmp_path,
        )
    assert (path.read_bytes() if path.exists() else None) == before


def test_missing_manifest_resolution_fence_prevents_stale_claim_resurrection(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import resolve_recovery_manifest_reconciliation

    resolve_recovery_manifest_reconciliation(
        agent_id="agent-1",
        tenant_id="tenant-1",
        session_id="session-1",
        runtime_task_id="run-missing",
        action="mark_resolved",
        reason="operator verified transactional evidence",
        actor_user_id="operator-1",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-missing",
            data_root=tmp_path,
        ),
        expected_checkpoint_seq=0,
        expected_claim_version=7,
        expected_claim_worker_id="worker-7",
        operation_id="operation-fence",
        data_root=tmp_path,
    )
    stale = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-missing",
            "claim_version": 7,
            "claim_worker_id": "worker-7",
            "pending_tool_frames": [
                {"tool_call_id": "call-stale", "tool_name": "send_email", "status": "needs_reconciliation"}
            ],
        },
    )

    assert persist_recovery_manifest("agent-1", stale, data_root=tmp_path) == []
    resolved = load_recovery_manifest("agent-1", session_context=stale, data_root=tmp_path)
    assert resolved is not None
    assert resolved.pending_tool_frames == []
    assert resolved.reconciliation_resolution["operation_id"] == "operation-fence"


def test_retry_resolution_allows_only_a_newer_claim_to_checkpoint_again(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import resolve_recovery_manifest_reconciliations

    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-retry",
            "claim_version": 7,
            "claim_worker_id": "worker-7",
            "pending_tool_frames": [
                {"tool_call_id": "call-old", "tool_name": "read_file", "status": "needs_reconciliation"}
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    target = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "runtime_task_id": "run-retry",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-retry",
            data_root=tmp_path,
        ),
        "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_checkpoint_seq": session.metadata["recovery_checkpoint_seq"],
        "expected_claim_version": 7,
        "expected_claim_worker_id": "worker-7",
    }
    resolve_recovery_manifest_reconciliations(
        targets=[target],
        tenant_id="tenant-1",
        action="retry",
        reason="read-only tool is safe to retry",
        actor_user_id="operator-1",
        operation_id="operation-retry",
        data_root=tmp_path,
    )

    session.metadata.update(
        {
            "claim_version": 8,
            "claim_worker_id": "worker-8",
            "pending_tool_frames": [{"tool_call_id": "call-new", "tool_name": "read_file", "status": "running"}],
        }
    )
    written = persist_recovery_manifest("agent-1", session, data_root=tmp_path)

    assert written == [path]
    restored = load_recovery_manifest("agent-1", session_context=session, data_root=tmp_path)
    assert restored is not None
    assert restored.claim_version == 8
    assert restored.claim_worker_id == "worker-8"
    assert [frame["tool_call_id"] for frame in restored.pending_tool_frames] == ["call-new"]
    assert restored.reconciliation_resolution == {}


def test_reconciliation_rejects_reviewed_manifest_deleted_before_resolution(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestReconciliationError,
        resolve_recovery_manifest_reconciliations,
    )

    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-deleted",
            "claim_version": 3,
            "claim_worker_id": "worker-3",
            "pending_tool_frames": [
                {"tool_call_id": "call-reviewed", "tool_name": "send_email", "status": "needs_reconciliation"}
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    target = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "runtime_task_id": "run-deleted",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-deleted",
            data_root=tmp_path,
        ),
        "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_checkpoint_seq": session.metadata["recovery_checkpoint_seq"],
        "expected_claim_version": 3,
        "expected_claim_worker_id": "worker-3",
    }
    path.unlink()

    with pytest.raises(RecoveryManifestReconciliationError, match="changed since operator review"):
        resolve_recovery_manifest_reconciliations(
            targets=[target],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="reviewed the original manifest",
            actor_user_id="operator-1",
            operation_id="operation-deleted",
            data_root=tmp_path,
        )

    assert not path.exists()


def test_same_reconciliation_operation_resumes_idempotently_after_db_finalize_failure(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import resolve_recovery_manifest_reconciliations

    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-1",
            "claim_version": 5,
            "claim_worker_id": "worker-5",
            "pending_tool_frames": [
                {"tool_call_id": "call-1", "tool_name": "send_email", "status": "needs_reconciliation"}
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    target = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "runtime_task_id": "run-1",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-1",
            data_root=tmp_path,
        ),
        "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_checkpoint_seq": session.metadata["recovery_checkpoint_seq"],
        "expected_claim_version": 5,
        "expected_claim_worker_id": "worker-5",
    }

    first = resolve_recovery_manifest_reconciliations(
        targets=[target],
        tenant_id="tenant-1",
        action="mark_resolved",
        reason="verified exactly once",
        actor_user_id="operator-1",
        operation_id="operation-resume",
        data_root=tmp_path,
    )
    second = resolve_recovery_manifest_reconciliations(
        targets=[target],
        tenant_id="tenant-1",
        action="mark_resolved",
        reason="verified exactly once",
        actor_user_id="operator-1",
        operation_id="operation-resume",
        data_root=tmp_path,
    )

    assert second[0]["sha256"] == first[0]["sha256"]
    assert second[0]["source_state"] == "already_resolved"


def test_same_reconciliation_operation_rejects_a_different_actor(tmp_path: Path) -> None:
    from app.runtime.recovery_manifest import (
        RecoveryManifestReconciliationError,
        resolve_recovery_manifest_reconciliations,
    )

    session = SessionContext(
        session_id="session-1",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-actor",
            "claim_version": 5,
            "claim_worker_id": "worker-5",
            "pending_tool_frames": [
                {"tool_call_id": "call-1", "tool_name": "send_email", "status": "needs_reconciliation"}
            ],
        },
    )
    [path] = persist_recovery_manifest("agent-1", session, data_root=tmp_path)
    target = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "runtime_task_id": "run-actor",
        **_reviewed_manifest_evidence(
            agent_id="agent-1",
            tenant_id="tenant-1",
            session_id="session-1",
            runtime_task_id="run-actor",
            data_root=tmp_path,
        ),
        "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected_checkpoint_seq": session.metadata["recovery_checkpoint_seq"],
        "expected_claim_version": 5,
        "expected_claim_worker_id": "worker-5",
    }
    resolve_recovery_manifest_reconciliations(
        targets=[target],
        tenant_id="tenant-1",
        action="mark_resolved",
        reason="verified exactly once",
        actor_user_id="operator-a",
        operation_id="operation-actor",
        data_root=tmp_path,
    )

    with pytest.raises(RecoveryManifestReconciliationError, match="different authority"):
        resolve_recovery_manifest_reconciliations(
            targets=[target],
            tenant_id="tenant-1",
            action="mark_resolved",
            reason="verified exactly once",
            actor_user_id="operator-b",
            operation_id="operation-actor",
            data_root=tmp_path,
        )


def test_fork_and_rewind_start_with_distinct_recovery_authority(tmp_path: Path) -> None:
    agent_id = "agent-1"
    source = SessionContext(
        session_id="session-source",
        metadata={
            "tenant_id": "tenant-1",
            "runtime_task_id": "run-source",
            "claim_version": 1,
            "claim_worker_id": "worker-source",
            "permission_profile": {"mode": "bypassPermissions", "allowed_tools": ["send_email"]},
            "pending_tool_frames": [{"tool_call_id": "call-source", "tool_name": "send_email", "status": "running"}],
        },
    )
    source.track_file_write("workspace/source-private.md")
    [source_path] = persist_recovery_manifest(agent_id, source, data_root=tmp_path)

    fork = SessionContext(
        session_id="session-fork",
        metadata={"tenant_id": "tenant-1", "runtime_task_id": "run-fork", "claim_version": 1},
    )
    rewind = SessionContext(
        session_id="session-source",
        metadata={"tenant_id": "tenant-1", "runtime_task_id": "run-rewind", "claim_version": 1},
    )

    assert load_recovery_manifest(agent_id, session_context=fork, data_root=tmp_path) is None
    assert load_recovery_manifest(agent_id, session_context=rewind, data_root=tmp_path) is None

    fork.track_pending_item("fork-only")
    rewind.track_pending_item("rewind-only")
    [fork_path] = persist_recovery_manifest(agent_id, fork, data_root=tmp_path)
    [rewind_path] = persist_recovery_manifest(agent_id, rewind, data_root=tmp_path)

    assert len({source_path, fork_path, rewind_path}) == 3
    loaded_source = load_recovery_manifest(agent_id, session_context=source, data_root=tmp_path)
    loaded_fork = load_recovery_manifest(agent_id, session_context=fork, data_root=tmp_path)
    loaded_rewind = load_recovery_manifest(agent_id, session_context=rewind, data_root=tmp_path)
    assert loaded_source is not None and loaded_source.pending_tool_frames[0]["tool_name"] == "send_email"
    assert loaded_fork is not None and loaded_fork.pending_items == ["fork-only"]
    assert loaded_rewind is not None and loaded_rewind.pending_items == ["rewind-only"]
    assert loaded_fork.permission_profile == {}
    assert loaded_fork.pending_tool_frames == []
    assert loaded_rewind.permission_profile == {}
    assert loaded_rewind.pending_tool_frames == []


def test_deleting_one_session_checkpoint_preserves_other_sessions(tmp_path: Path) -> None:
    agent_id = "agent-1"
    session_a = SessionContext(session_id="session-a")
    session_a.track_pending_item("a")
    session_b = SessionContext(session_id="session-b")
    session_b.track_pending_item("b")
    persist_recovery_manifest(agent_id, session_a, data_root=tmp_path)
    persist_recovery_manifest(agent_id, session_b, data_root=tmp_path)

    session_a.pending_items.clear()
    persist_recovery_manifest(agent_id, session_a, data_root=tmp_path, delete_if_empty=True)

    assert load_recovery_manifest(agent_id, session_context=session_a, data_root=tmp_path) is None
    loaded_b = load_recovery_manifest(agent_id, session_context=session_b, data_root=tmp_path)
    assert loaded_b is not None and loaded_b.pending_items == ["b"]


def test_default_persistence_has_one_durable_authority_root(tmp_path: Path, monkeypatch) -> None:
    agent_id = f"agent-{uuid4().hex}"
    durable_root = tmp_path / "durable-agents"
    transient_agent_root = Path("/tmp/hive_workspaces") / agent_id
    transient_agent_root.mkdir(parents=True)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: type("Settings", (), {"AGENT_DATA_DIR": str(durable_root)})(),
    )
    session = SessionContext(session_id="session-1")
    session.track_pending_item("resume")

    try:
        written = persist_recovery_manifest(agent_id, session)

        assert written == [recovery_manifest_path(agent_id, session_id="session-1", data_root=durable_root)]
        transient_checkpoint = transient_agent_root / "runtime_artifacts" / "recovery_manifests" / written[0].name
        assert transient_checkpoint.exists() is False
    finally:
        shutil.rmtree(transient_agent_root, ignore_errors=True)
