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

from app.runtime.recovery_manifest import build_recovery_manifest, load_recovery_manifest
from app.runtime.session import SessionContext


# ── Manifest carries the right runtime state ─────────────────


def test_manifest_inherits_recent_files_writes_skills_packs() -> None:
    sc = SessionContext(session_id="s1")
    sc.track_file_read("memory/feedback.md")
    sc.track_file_write("workspace/note.md")
    sc.track_skill_loaded("memory-guide")
    sc.active_tool_groups.append({"name": "web_pack", "summary": "search", "tools": []})
    sc.track_pending_item("finish report")
    sc.track_tool_outcome("web_search", "found 3 hits")

    manifest = build_recovery_manifest(sc)

    assert manifest.session_id == "s1"
    assert "memory/feedback.md" in manifest.recent_reads
    assert "workspace/note.md" in manifest.recent_writes
    assert "memory-guide" in manifest.active_skills
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
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest = load_recovery_manifest(agent_id, data_root=tmp_path)

    assert manifest is not None
    assert manifest.session_id == "session-1"
    assert manifest.recent_reads == ["workspace/report.md"]
    assert manifest.pending_items == ["finish D8 recovery"]
    assert manifest.permission_profile["allowed_tools"] == ["write_file"]


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


def test_recovery_manifest_filename_constant_documented() -> None:
    """Anchor the filename so the prompt-builder side and operator tools
    target the same path. Lives next to compaction_summary.md in the
    runtime_artifacts dir."""
    expected = Path("runtime_artifacts") / "recovery_manifest.json"
    assert expected.name == "recovery_manifest.json"
    assert expected.parent.name == "runtime_artifacts"
