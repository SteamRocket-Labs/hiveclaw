from __future__ import annotations

import uuid
from pathlib import Path


def test_t3_layout_creates_only_four_canonical_accepted_files(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import ACCEPTED_T3_TARGETS

    agent_id = uuid.uuid4()

    mem_dir = ensure_t3_layout(tmp_path, agent_id)

    expected = {
        "memory/t3/episodes.md",
        "memory/t3/user.md",
        "memory/t3/worker.md",
        "memory/t3/capabilities.md",
    }
    assert set(ACCEPTED_T3_TARGETS) == expected
    for rel_path in expected:
        assert (tmp_path / str(agent_id) / rel_path).exists()

    for legacy in ("feedback.md", "knowledge.md", "strategies.md", "blocked.md"):
        assert not (mem_dir / legacy).exists()
    for forbidden in ("t3/index.md", "t3/relations.md", "t3/contradictions.md", "t3/chapters"):
        assert not (mem_dir / forbidden).exists()


def test_legacy_categories_route_to_canonical_t3_files() -> None:
    from app.memory.md_store import t3_spec_for_category

    assert t3_spec_for_category("feedback")["filename"] == "t3/user.md"
    assert t3_spec_for_category("user")["filename"] == "t3/user.md"
    assert t3_spec_for_category("constraint")["filename"] == "t3/worker.md"
    assert t3_spec_for_category("blocked_pattern")["filename"] == "t3/worker.md"
    assert t3_spec_for_category("strategy")["filename"] == "t3/capabilities.md"
    assert t3_spec_for_category("project")["filename"] == "t3/capabilities.md"
    assert t3_spec_for_category("reference")["filename"] == "t3/capabilities.md"
    assert t3_spec_for_category("episode")["filename"] == "t3/episodes.md"


def test_platform_gate_rejects_non_canonical_t3_targets() -> None:
    from app.memory.t3_platform_gate import is_accepted_t3_target

    assert is_accepted_t3_target("memory/t3/episodes.md")
    assert is_accepted_t3_target("memory/t3/user.md")
    assert is_accepted_t3_target("memory/t3/worker.md")
    assert is_accepted_t3_target("memory/t3/capabilities.md")

    assert not is_accepted_t3_target("memory/feedback.md")
    assert not is_accepted_t3_target("memory/t3/index.md")
    assert not is_accepted_t3_target("memory/t3/chapters/chapter-1/synthesis.md")
    assert not is_accepted_t3_target("soul.md")
