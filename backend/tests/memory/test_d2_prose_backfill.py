"""D2 (purity debt): T3 `.md` prose carries only `[date][entry_id]`; every other
field (sensitivity/status/version/refs + D1 telemetry) lives in the lifecycle
sidecar. Existing dirty lines (the 05-25 format drift) are backfilled to the
clean prose, their metadata migrated into the sidecar, the originals archived.

Owner decision (2026-06-08): aggressive strip to `[date][entry_id] content`.
The one hard safety invariant: the migration must NEVER lose `sensitivity` —
access control reads it, so a stripped entry that loses its PL2/PL3 marker would
silently downgrade to public. Tests pin that the sensitivity survives the round
trip (write → strip → sidecar → manifest join).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.memory.md_store import append_t3_entry, build_t3_entry_manifest
from app.memory.lifecycle_store import MemoryLifecycleStore, lifecycle_path
from app.memory.t3_store import append_t3_memory_candidate, backfill_t3_prose


@pytest.mark.asyncio
async def test_write_keeps_only_date_and_entry_id_in_prose(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()
    result = await append_t3_memory_candidate(
        agent_id,
        category="feedback",
        content="User requires plain-text answers, no emoji",
        proposed_by="agent_tool",
        data_root=tmp_path,
    )
    assert result.status == "accepted"

    line = next(
        ln
        for ln in (tmp_path / str(agent_id) / "memory" / "feedback.md").read_text(encoding="utf-8").splitlines()
        if ln.startswith("- [")
    )
    # Prose carries the date + the entry_id join key, nothing else inline.
    assert f"[entry_id={result.entry_id}]" in line
    assert "[sensitivity=" not in line
    assert "[status=" not in line
    assert "[version=" not in line
    assert "[access_count" not in line
    assert "[last_accessed" not in line

    # The stripped metadata is in the sidecar instead.
    lifecycle = json.loads((tmp_path / str(agent_id) / "memory" / "lifecycle.json").read_text(encoding="utf-8"))
    record = next(r for r in lifecycle if r["id"] == result.entry_id)
    assert record["metadata"].get("sensitivity")
    assert record["status"] == "active"


def test_manifest_joins_metadata_from_sidecar_when_prose_is_bare(tmp_path: Path) -> None:
    """Read side: sensitivity/status come from the sidecar even when prose is bare."""
    agent_id = uuid.uuid4()
    # Bare prose line — only date + entry_id, no inline metadata.
    append_t3_entry(
        tmp_path,
        agent_id,
        category="knowledge",
        content="vendor escalation contact lives in the ops runbook",
        timestamp="2026-05-10",
        metadata={"entry_id": "bare1", "sensitivity": "PL2_pii", "status": "active", "version": "1"},
    )
    # Rewrite the prose to the bare D2 target form (strip everything but entry_id).
    kpath = tmp_path / str(agent_id) / "memory" / "knowledge.md"
    kpath.write_text(
        "# Knowledge\n\n- [2026-05-10][entry_id=bare1] vendor escalation contact lives in the ops runbook\n",
        encoding="utf-8",
    )

    manifest = build_t3_entry_manifest(tmp_path, agent_id)
    entry = next(e for e in manifest if e.entry_id == "bare1")
    assert entry.metadata.get("sensitivity") == "PL2_pii"
    assert entry.metadata.get("status") == "active"


def test_backfill_strips_inline_metadata_and_preserves_sensitivity(tmp_path: Path) -> None:
    """The safety-critical test: a dirty PL3 line keeps PL3 after backfill."""
    agent_id = uuid.uuid4()
    mem = tmp_path / str(agent_id) / "memory"
    mem.mkdir(parents=True)
    # 05-25 dirty format with inline telemetry + a sensitive marker.
    (mem / "knowledge.md").write_text(
        "# Knowledge\n\n"
        "- [2026-06-04][entry_id=k1][sensitivity=PL3_confidential][status=active][version=1]"
        "[access_count=97][last_accessed=2026-06-04T17:00] acquisition target shortlist\n",
        encoding="utf-8",
    )

    before = build_t3_entry_manifest(tmp_path, agent_id)
    assert next(e for e in before if e.entry_id == "k1").metadata.get("sensitivity") == "PL3_confidential"

    report = backfill_t3_prose(tmp_path, agent_id, dry_run=False)
    assert report["files_changed"] >= 1
    assert report["entries_migrated"] >= 1

    # Prose is now clean.
    line = next(ln for ln in (mem / "knowledge.md").read_text(encoding="utf-8").splitlines() if ln.startswith("- ["))
    assert line.strip() == "- [2026-06-04][entry_id=k1] acquisition target shortlist"

    # SAFETY: sensitivity survived — no silent access-control downgrade.
    after = build_t3_entry_manifest(tmp_path, agent_id)
    assert next(e for e in after if e.entry_id == "k1").metadata.get("sensitivity") == "PL3_confidential"

    # D1 safety: legacy inline telemetry must move to the sidecar's dedicated
    # telemetry fields, not survive as inert metadata or reset to zero.
    lifecycle = MemoryLifecycleStore(lifecycle_path(tmp_path, agent_id))
    lifecycle_entry = lifecycle.get("k1")
    assert lifecycle_entry.access_count == 97
    assert lifecycle_entry.last_accessed is not None
    assert lifecycle_entry.last_accessed.isoformat().startswith("2026-06-04T17:00")

    # Original dirty line preserved in the archive (reversible evidence).
    archive = (mem / "archive.md").read_text(encoding="utf-8")
    assert "[access_count=97]" in archive


def test_backfill_dry_run_does_not_write(tmp_path: Path) -> None:
    agent_id = uuid.uuid4()
    mem = tmp_path / str(agent_id) / "memory"
    mem.mkdir(parents=True)
    dirty = "# Strategies\n\n- [2026-06-04][entry_id=s1][sensitivity=PL1_public][access_count=5] scan cadence works\n"
    (mem / "strategies.md").write_text(dirty, encoding="utf-8")

    report = backfill_t3_prose(tmp_path, agent_id, dry_run=True)

    # Nothing on disk changed.
    assert (mem / "strategies.md").read_text(encoding="utf-8") == dirty
    assert not (mem / "archive.md").exists()
    # The dry-run still reports what WOULD change, with a visible diff.
    assert report["entries_migrated"] >= 1
    assert report["dry_run"] is True
    assert any("s1" in d for d in report["diff"])


def test_backfill_is_idempotent_on_clean_prose(tmp_path: Path) -> None:
    """Already-clean lines are a no-op — backfill never double-archives."""
    agent_id = uuid.uuid4()
    mem = tmp_path / str(agent_id) / "memory"
    mem.mkdir(parents=True)
    (mem / "feedback.md").write_text(
        "# Feedback\n\n- [2026-06-04][entry_id=f1] user prefers terse replies\n", encoding="utf-8"
    )

    report = backfill_t3_prose(tmp_path, agent_id, dry_run=False)
    assert report["entries_migrated"] == 0
    assert not (mem / "archive.md").exists()
