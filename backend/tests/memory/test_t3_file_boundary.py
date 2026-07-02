"""Two-plane accepted-memory boundary (Part H cutover).

The legacy flat-T3 four-file layout is retired: bootstrap creates the plane
directories only, and the gate accepts profile-plane files plus dynamic
knowledge/milestone pages — nothing else.
"""

from __future__ import annotations

import uuid
from pathlib import Path


def test_t3_layout_creates_two_plane_dirs_and_no_legacy_files(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)

    for subdir in ("self", "profiles", "knowledge", "milestones"):
        assert (mem_dir / subdir).is_dir()
    for legacy in ("t3/episodes.md", "t3/user.md", "t3/worker.md", "t3/capabilities.md"):
        assert not (mem_dir / legacy).exists()


def test_gate_accepts_only_two_plane_targets() -> None:
    from app.memory.t3_platform_gate import is_accepted_t3_target

    assert is_accepted_t3_target("memory/self/self.md")
    assert is_accepted_t3_target("memory/profiles/owner.md")
    assert is_accepted_t3_target("memory/knowledge/l2-rollup.md")
    assert is_accepted_t3_target("memory/milestones/ms-first-win.md")
    for rejected in (
        "memory/t3/user.md",
        "memory/t3/capabilities.md",
        "memory/knowledge/../escape.md",
        "memory/anything.md",
        "soul.md",
    ):
        assert not is_accepted_t3_target(rejected), rejected
