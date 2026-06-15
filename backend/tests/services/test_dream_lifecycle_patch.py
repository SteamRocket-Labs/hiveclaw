"""Dream lifecycle-patch tests (docs/agent-memory-md-first-spec.md §12 P3).

Acceptance:
- Merge creates superseded edges.
- Contradiction creates contradiction or supersession edges.
- Cap cleanup archives / de-indexes entries instead of silent deletion.
- Hindsight only syncs active entries.

Dream is the Reconsolidator: its decisions become lifecycle patches —
retired lines move to memory/archive.md (reversible, MD-first evidence)
and lifecycle.json records the supersede/archive edge. Physical deletion
of evidence is forbidden.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    stub = lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))  # noqa: E731
    monkeypatch.setattr("app.config.get_settings", stub)
    # auto_dream binds get_settings at module import time — patch both names.
    monkeypatch.setattr("app.services.auto_dream.get_settings", stub)
    mem_dir = tmp_path / str(agent_id) / "memory"
    mem_dir.mkdir(parents=True)
    return agent_id, tmp_path, mem_dir


def _lifecycle_records(mem_dir: Path) -> list[dict]:
    path = mem_dir / "lifecycle.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def test_dream_merge_creates_superseded_edges(agent_env) -> None:
    from app.services.auto_dream import _apply_dream_decisions_unlocked

    agent_id, _root, mem_dir = agent_env
    (mem_dir / "feedback.md").write_text(
        "# Feedback\n\n"
        "- [2026-04-01] User rejected emoji in responses\n"
        "- [2026-04-05] User rejected adding emojis to answer\n"
        "- [2026-04-10] User rejected emoji in responses (3rd confirmation)\n",
        encoding="utf-8",
    )

    report = _apply_dream_decisions_unlocked(
        agent_id,
        {
            "t3_merges": [
                {
                    "file": "feedback.md",
                    "keep": "- [2026-04-10] User rejected emoji in responses (3rd confirmation)",
                    "drop": [
                        "User rejected emoji in responses\n",
                        "User rejected adding emojis to answer",
                    ],
                    "reason": "3 restatements of the same rule",
                }
            ]
        },
    )

    body = (mem_dir / "feedback.md").read_text(encoding="utf-8")
    # The canonical line survives; the merged duplicates left the active file.
    assert "(3rd confirmation)" in body
    assert "- [2026-04-05] User rejected adding emojis to answer" not in body

    # ...but they are archived, not deleted.
    archive = (mem_dir / "archive.md").read_text(encoding="utf-8")
    assert "User rejected adding emojis to answer" in archive
    assert "superseded" in archive

    # Lifecycle records carry the superseded edge.
    records = _lifecycle_records(mem_dir)
    superseded = [r for r in records if r["status"] == "superseded"]
    assert len(superseded) >= 1
    assert report["t3_merges_applied"] == 1


def test_dream_contradiction_creates_supersession_edge(agent_env) -> None:
    from app.services.auto_dream import _apply_dream_decisions_unlocked

    agent_id, _root, mem_dir = agent_env
    (mem_dir / "feedback.md").write_text(
        "# Feedback\n\n"
        "- [2026-02-01] User prefers Japanese for internal messaging\n"
        "- [2026-04-14] User now wants all responses in Chinese going forward\n",
        encoding="utf-8",
    )

    report = _apply_dream_decisions_unlocked(
        agent_id,
        {
            "t3_contradictions": [
                {
                    "file": "feedback.md",
                    "new": "User now wants all responses in Chinese going forward",
                    "old": "User prefers Japanese for internal messaging",
                    "resolution": "kept_new",
                    "reason": "user explicitly superseded the older preference",
                }
            ]
        },
    )

    body = (mem_dir / "feedback.md").read_text(encoding="utf-8")
    assert "Chinese going forward" in body
    assert "Japanese for internal messaging" not in body

    archive = (mem_dir / "archive.md").read_text(encoding="utf-8")
    assert "Japanese for internal messaging" in archive

    records = _lifecycle_records(mem_dir)
    assert any(r["status"] == "superseded" for r in records)
    assert report["contradictions_resolved"] == 1


def test_cap_cleanup_archives_instead_of_deleting(agent_env) -> None:
    from app.services.auto_dream import _T3_MAX_ENTRIES_PER_FILE, _consolidate_t3_files

    agent_id, _root, mem_dir = agent_env
    # Compose genuinely distinct lines so programmatic dedup keeps them all
    # and only the cap eviction lane fires.
    subjects = [
        "postgres rls policies",
        "feishu webhook retries",
        "vite proxy rewrites",
        "redis pubsub channels",
        "alembic migration heads",
        "jwt refresh rotation",
        "onlyoffice jwt secrets",
        "tenant quota ceilings",
        "trigger cron parsing",
        "delegation lease ttl",
        "plaza rate limits",
        "email mime threading",
        "discord gateway intents",
        "slack socket mode",
        "wecom callback crypto",
        "teams adaptive cards",
        "skill frontmatter schema",
        "pack activation events",
        "mcp server health",
        "workspace artifact spill",
        "loop guard thresholds",
        "compaction retry budget",
        "vision payload caps",
        "audit log retention",
        "approval flow states",
        "security zone matrix",
        "capability gate map",
        "objective ledger rows",
        "focus projection sync",
        "heartbeat lease keys",
        "dream gate hours",
        "extractor cursor files",
        "backfill session ids",
        "hindsight bank ids",
        "bm25 tokenizer bigrams",
        "jaccard dedup floors",
        "lifecycle sketch expiry",
        "preservation flag caps",
        "soul append separators",
        "evolution lineage rotation",
        "scorecard counters",
        "blocklist expiry days",
        "runtime task journal",
        "leaf call envelopes",
        "workflow step gates",
        "deep research lanes",
        "synthesis coverage rules",
        "critic refute votes",
        "planner preset bounds",
        "explorer shard digests",
        "reasoner tool masks",
        "invoker prompt cache",
        "kernel round budget",
        "channel stream pacing",
        "ws idle timeouts",
        "upload progress events",
        "i18n key parity",
        "zustand store slices",
        "tanstack query keys",
        "router lazy guards",
    ]
    verbs = ["cap", "rotate", "gate", "checksum", "replay", "throttle", "expire", "fence", "shard", "audit"]
    lines = [f"- [2026-01-{(i % 28) + 1:02d}] {verbs[i % 10]} {subjects[i]} at boundary {i * 7}" for i in range(60)]
    (mem_dir / "knowledge.md").write_text("# Knowledge\n\n" + "\n".join(lines) + "\n", encoding="utf-8")

    stats = _consolidate_t3_files(agent_id)

    body = (mem_dir / "knowledge.md").read_text(encoding="utf-8")
    active_lines = [line for line in body.splitlines() if line.startswith("- [")]
    assert len(active_lines) <= _T3_MAX_ENTRIES_PER_FILE
    assert stats["knowledge.md"] > 0

    # The acceptance core: every line removed from the active file (whether
    # by dedup-supersede or cap eviction) is archived — never silently dropped.
    archive = (mem_dir / "archive.md").read_text(encoding="utf-8")
    surviving = set(active_lines)
    removed = [line for line in lines if line not in surviving]
    assert removed, "test premise: consolidation removed at least one line"
    for line in removed:
        content_part = line.split("] ", 1)[1]
        assert content_part in archive, f"removed line missing from archive: {content_part}"
    assert "dedup_superseded" in archive or "cap_eviction" in archive

    # Both retirement lanes write a terminal lifecycle state — never deletion.
    records = _lifecycle_records(mem_dir)
    assert any(r["status"] in ("archived", "superseded") for r in records)


def test_t3_cap_retention_uses_lifecycle_counters() -> None:
    from app.services.auto_dream import _select_t3_cap_retention

    lines = [
        "- [2026-01-01][entry_id=harmful] outdated proxy guidance",
        "- [2026-01-02][entry_id=hot] verified deploy checklist",
        "- [2026-01-03][entry_id=reinforced] stable user preference",
        "- [2026-01-04][entry_id=cold] cold old note",
    ]
    kept, evicted = _select_t3_cap_retention(
        lines,
        keep_count=2,
        protected_markers=[],
        lifecycle_metadata={
            "harmful": {"harmful_count": "3", "reinforcement_count": "4", "access_count": "0"},
            "hot": {"harmful_count": "0", "reinforcement_count": "1", "access_count": "9"},
            "reinforced": {"harmful_count": "0", "reinforcement_count": "5", "access_count": "1"},
            "cold": {"harmful_count": "0", "reinforcement_count": "0", "access_count": "0"},
        },
    )

    assert kept == [lines[1], lines[2]]
    assert evicted == [lines[0], lines[3]]


def test_archive_file_stays_out_of_active_recall(agent_env) -> None:
    """archive.md must not leak into the manifest, INDEX, or search."""
    from app.memory.md_store import build_t3_entry_manifest, rebuild_index, search_t3_facts
    from app.services.auto_dream import _apply_dream_decisions_unlocked

    agent_id, root, mem_dir = agent_env
    (mem_dir / "feedback.md").write_text(
        "# Feedback\n\n- [2026-04-01] retired preference marker XYZZY\n- [2026-04-02] surviving preference\n",
        encoding="utf-8",
    )
    _apply_dream_decisions_unlocked(
        agent_id,
        {
            "t3_merges": [
                {
                    "file": "feedback.md",
                    "keep": "- [2026-04-02] surviving preference",
                    "drop": ["retired preference marker XYZZY"],
                    "reason": "dedup",
                }
            ]
        },
    )

    manifest = build_t3_entry_manifest(root, agent_id)
    assert all("XYZZY" not in entry.content for entry in manifest)

    index_body = rebuild_index(root, agent_id).read_text(encoding="utf-8")
    assert "XYZZY" not in index_body

    hits = search_t3_facts(root, agent_id, "XYZZY", limit=5)
    assert hits == []


def test_hindsight_only_syncs_active_t3_files() -> None:
    """The hindsight collector reads exactly the five active T3 files —
    archive.md must never be a sync source."""
    from app.memory import hindsight_sync

    assert "archive.md" not in hindsight_sync._T3_FILES
    assert set(hindsight_sync._T3_FILES) == {
        "feedback.md",
        "knowledge.md",
        "strategies.md",
        "blocked.md",
        "user.md",
    }
