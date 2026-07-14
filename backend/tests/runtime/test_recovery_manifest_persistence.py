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

import json
from pathlib import Path

from app.runtime.recovery_manifest import (
    build_recovery_manifest,
    hydrate_session_context_from_recovery_manifest,
    load_recovery_manifest,
    persist_recovery_manifest,
)
from app.runtime.session import SessionContext


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

    manifest = load_recovery_manifest(agent_id, data_root=tmp_path)

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
    manifest = load_recovery_manifest(agent_id, data_root=tmp_path)
    session = SessionContext(session_id="session-1", metadata={"pending_tool_frames": [{"tool_call_id": "old"}]})

    hydrate_session_context_from_recovery_manifest(session, manifest)

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
    assert session.metadata["permission_profile"]["allowed_tools"] == ["write_file"]
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


def test_persist_recovery_manifest_deletes_stale_empty_checkpoint(tmp_path) -> None:
    agent_id = "agent-1"
    sc = SessionContext(
        session_id="session-1",
        metadata={
            "pending_tool_frame": {
                "tool_call_id": "call-running",
                "tool_name": "write_file",
                "status": "running",
            }
        },
    )

    written = persist_recovery_manifest(agent_id, sc, data_root=tmp_path)
    manifest_path = tmp_path / agent_id / "runtime_artifacts" / "recovery_manifest.json"

    assert written == [manifest_path]
    assert load_recovery_manifest(agent_id, data_root=tmp_path) is not None

    sc.metadata.clear()
    persist_recovery_manifest(agent_id, sc, data_root=tmp_path, delete_if_empty=True)

    assert load_recovery_manifest(agent_id, data_root=tmp_path) is None
    assert not manifest_path.exists()


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


def test_recovery_manifest_filename_constant_documented() -> None:
    """Anchor the filename so the prompt-builder side and operator tools
    target the same path. Lives next to compaction_summary.md in the
    runtime_artifacts dir."""
    expected = Path("runtime_artifacts") / "recovery_manifest.json"
    assert expected.name == "recovery_manifest.json"
    assert expected.parent.name == "runtime_artifacts"


def test_restoration_budget_omission_keeps_hash_pinned_full_manifest_pointer() -> None:
    import hashlib

    sc = SessionContext(session_id="large-manifest")
    for index in range(40):
        sc.track_file_read(f"workspace/very-long-file-{index}.md")
        sc.track_pending_item(f"pending-work-{index}")

    manifest = build_recovery_manifest(sc)
    canonical = json.dumps(manifest.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    text = manifest.to_restoration_text(budget_chars=700)

    assert "runtime_artifacts/recovery_manifest.json" in text
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() in text
    assert "omitted_fields" in text
    assert len(text) <= 700


def test_session_recovery_tracking_never_discards_older_items_or_semantic_tails() -> None:
    sc = SessionContext(session_id="complete-recovery")
    outcome_tail = "DECISIVE_RECOVERY_OUTCOME_TAIL"
    for index in range(40):
        sc.track_file_read(f"workspace/read-{index}.md")
        sc.track_file_write(f"workspace/write-{index}.md")
        sc.track_external_ref(f"https://example.com/source/{index}")
        sc.track_pending_item(f"pending-{index}")
    sc.track_tool_outcome("execute_code", ("evidence " * 100) + outcome_tail)

    manifest = build_recovery_manifest(sc)

    assert len(manifest.recent_reads) == 40
    assert len(manifest.recent_writes) == 40
    assert len(manifest.recent_external_refs) == 40
    assert len(manifest.pending_items) == 40
    assert outcome_tail in manifest.recent_tool_outcomes[-1]["summary"]
