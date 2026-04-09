from __future__ import annotations

import uuid
from pathlib import Path


def test_migrate_legacy_t2_files_moves_entries_into_weighted_files(tmp_path: Path) -> None:
    from app.memory.legacy_migration import migrate_legacy_memory_tree

    agent_id = uuid.uuid4()
    learnings_dir = tmp_path / str(agent_id) / "memory" / "learnings"
    learnings_dir.mkdir(parents=True)
    (learnings_dir / "LEARNINGS.md").write_text(
        "# Learnings\n\n- Always confirm the user's desired output format.\n",
        encoding="utf-8",
    )
    (learnings_dir / "ERRORS.md").write_text(
        "# Errors\n\n- Do not rely on stale code state.\n",
        encoding="utf-8",
    )
    (learnings_dir / "FEATURE_REQUESTS.md").write_text(
        "# Feature Requests\n\n- Add true cross-session recall.\n",
        encoding="utf-8",
    )

    report = migrate_legacy_memory_tree(tmp_path, agent_id)

    insights = (learnings_dir / "insights.md").read_text(encoding="utf-8")
    errors = (learnings_dir / "errors.md").read_text(encoding="utf-8")
    requests = (learnings_dir / "requests.md").read_text(encoding="utf-8")

    assert report["migrated_t2_entries"] == 3
    assert "Always confirm the user's desired output format." in insights
    assert "Do not rely on stale code state." in errors
    assert "Add true cross-session recall." in requests
    names = {path.name for path in learnings_dir.iterdir()}
    assert "LEARNINGS.md" not in names
    assert "ERRORS.md" not in names
    assert "FEATURE_REQUESTS.md" not in names


def test_migrate_legacy_memory_md_imports_lines_into_knowledge(tmp_path: Path) -> None:
    from app.memory.legacy_migration import migrate_legacy_memory_tree

    agent_id = uuid.uuid4()
    memory_dir = tmp_path / str(agent_id) / "memory"
    memory_dir.mkdir(parents=True)
    legacy_path = memory_dir / "memory.md"
    legacy_path.write_text(
        "# Memory\n\n- 系统现在采用 md-first 记忆架构\n- dream 只负责 T3 consolidate\n",
        encoding="utf-8",
    )

    report = migrate_legacy_memory_tree(tmp_path, agent_id)

    knowledge = (memory_dir / "knowledge.md").read_text(encoding="utf-8")

    assert report["migrated_t3_entries"] == 2
    assert "系统现在采用 md-first 记忆架构" in knowledge
    assert "dream 只负责 T3 consolidate" in knowledge
    assert not legacy_path.exists()
