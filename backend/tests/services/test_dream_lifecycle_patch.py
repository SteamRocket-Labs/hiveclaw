"""Dream/T3 lifecycle boundary tests.

Dream can inspect accepted T3 and propose identity-level `soul.md` promotions.
It must not apply accepted T3 lifecycle patches. T3 merge, retire, dedup, and
conflict resolution now belong to the T3 Consolidator -> Memory Gate -> Platform
Gate lane.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    agent_id = uuid.uuid4()
    stub = lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path))  # noqa: E731
    monkeypatch.setattr("app.config.get_settings", stub)
    monkeypatch.setattr("app.services.auto_dream.get_settings", stub)
    mem_dir = tmp_path / str(agent_id) / "memory"
    (mem_dir / "t3").mkdir(parents=True)
    (tmp_path / str(agent_id) / "soul.md").write_text("# Soul\n\n## Identity\n", encoding="utf-8")
    return agent_id, tmp_path, mem_dir


def test_dream_merge_concern_is_held_not_applied(agent_env) -> None:
    from app.services.auto_dream import _apply_dream_decisions_unlocked

    agent_id, _root, mem_dir = agent_env
    target = mem_dir / "t3" / "user.md"
    target.write_text(
        "# T3 User\n\n"
        "- [2026-04-01] User rejected emoji in responses\n"
        "- [2026-04-05] User rejected adding emojis to answer\n"
        "- [2026-04-10] User rejected emoji in responses (3rd confirmation)\n",
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    report = _apply_dream_decisions_unlocked(
        agent_id,
        {
            "t3_merges": [
                {
                    "file": "t3/user.md",
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

    assert target.read_text(encoding="utf-8") == before
    assert not (mem_dir / "archive.md").exists()
    assert not (mem_dir / "lifecycle.json").exists()
    assert not (mem_dir / "control" / "lifecycle.json").exists()
    assert report["t3_merges_applied"] == 0
    assert report["t3_patch_candidates_held"] == 1


def test_dream_contradiction_concern_is_held_not_applied(agent_env) -> None:
    from app.services.auto_dream import _apply_dream_decisions_unlocked

    agent_id, _root, mem_dir = agent_env
    target = mem_dir / "t3" / "user.md"
    target.write_text(
        "# T3 User\n\n"
        "- [2026-02-01] User prefers Japanese for internal messaging\n"
        "- [2026-04-14] User now wants all responses in Chinese going forward\n",
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    report = _apply_dream_decisions_unlocked(
        agent_id,
        {
            "t3_contradictions": [
                {
                    "file": "t3/user.md",
                    "new": "User now wants all responses in Chinese going forward",
                    "old": "User prefers Japanese for internal messaging",
                    "resolution": "kept_new",
                    "reason": "user explicitly superseded the older preference",
                }
            ]
        },
    )

    assert target.read_text(encoding="utf-8") == before
    assert not (mem_dir / "archive.md").exists()
    assert not (mem_dir / "lifecycle.json").exists()
    assert not (mem_dir / "control" / "lifecycle.json").exists()
    assert report["contradictions_resolved"] == 0
    assert report["t3_patch_candidates_held"] == 1


def test_t3_consolidate_is_noop_for_accepted_files(agent_env) -> None:
    from app.services.auto_dream import _consolidate_t3_files

    agent_id, _root, mem_dir = agent_env
    (mem_dir / "knowledge").mkdir(parents=True, exist_ok=True)
    target = mem_dir / "knowledge" / "capability-notes.md"
    target.write_text(
        "---\ntitle: Capability Notes\nstatus: active\n---\n\n"
        + "\n".join(f"- [2026-01-{(i % 28) + 1:02d}] distinct capability note {i}" for i in range(60))
        + "\n",
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    stats = _consolidate_t3_files(agent_id)

    assert target.read_text(encoding="utf-8") == before
    assert stats["memory/knowledge/capability-notes.md"] == 0
    assert not (mem_dir / "archive.md").exists()


def test_t3_cap_retention_scoring_helper_still_prefers_reinforced_entries() -> None:
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


def test_direct_t3_write_is_refused(agent_env) -> None:
    from app.services.auto_dream import _write_t3_file

    agent_id, _root, _mem_dir = agent_env

    with pytest.raises(RuntimeError, match="direct T3 write refused"):
        _write_t3_file(agent_id, "t3/user.md", "# T3 User\n\n")


def test_native_t3_active_file_set_is_canonical() -> None:
    from app.memory.t3_platform_gate import PROFILE_PLANE_TARGETS, is_accepted_t3_target

    assert PROFILE_PLANE_TARGETS == (
        "memory/self/self.md",
        "memory/profiles/owner.md",
        "memory/profiles/collaborators.md",
        "memory/profiles/domain.md",
    )
    assert is_accepted_t3_target("memory/knowledge/some-page.md")
    assert not is_accepted_t3_target("memory/t3/episodes.md")
