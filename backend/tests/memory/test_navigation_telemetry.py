"""Navigation & access telemetry tests (docs/agent-memory-md-first-spec.md §12 P6).

Acceptance:
- `memory/INDEX.md` or manifest has consumer in prompt assembly.
- Entry-level recall counters and last-recalled timestamps are updated
  (engine fields: access_count / last_accessed, bumped by the retriever).
- Heat drives navigation order and retirement candidates.
- Activated memory includes activation reasons.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _seed(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    mem_dir = tmp_path / str(agent_id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    (mem_dir / "knowledge.md").write_text(
        "# Knowledge\n\n"
        f"- [2026-05-01][entry_id=mem_hot1][access_count=9][last_accessed={recent}] hot fact about deploy pipeline\n"
        f"- [2026-01-01][entry_id=mem_cold1][access_count=0][last_accessed={old}] cold fact about legacy proxy\n"
        "- [2026-03-01][entry_id=mem_mid1][access_count=3][last_accessed=never] mid fact about quota ceilings\n",
        encoding="utf-8",
    )
    return mem_dir


# ── Heat ──


def test_compute_entry_heat_orders_hot_above_cold() -> None:
    from app.memory.md_store import compute_entry_heat

    recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()

    hot = compute_entry_heat({"access_count": "9", "last_accessed": recent})
    mid = compute_entry_heat({"access_count": "3", "last_accessed": "never"})
    cold = compute_entry_heat({"access_count": "0", "last_accessed": old})

    assert hot > mid > cold


# ── Navigation section (manifest consumer in prompt assembly) ──


def test_memory_navigation_section_renders_heat_ordered_rows(tmp_path: Path) -> None:
    from app.runtime.prompt_sections import build_memory_navigation_section

    agent_id = uuid.uuid4()
    _seed(tmp_path, agent_id)

    section = build_memory_navigation_section(tmp_path, agent_id)

    assert "## Memory Navigation" in section
    assert "mem_hot1" in section
    assert "load_memory" in section
    # Heat ordering: hot entry listed before the cold one.
    assert section.index("mem_hot1") < section.index("mem_cold1")
    # Navigation exposes spec-named telemetry.
    assert "recall" in section.lower()


def test_memory_navigation_section_empty_without_memory(tmp_path: Path) -> None:
    from app.runtime.prompt_sections import build_memory_navigation_section

    section = build_memory_navigation_section(tmp_path, uuid.uuid4())
    assert section == ""


def test_dynamic_suffix_consumes_navigation_section() -> None:
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        memory_snapshot="[Semantic Memory]\n- [feedback] user prefers brevity",
        memory_navigation="## Memory Navigation\n| mem_hot1 | knowledge.md | ... |",
    )
    assert "## Memory Navigation" in suffix
    assert "mem_hot1" in suffix


# ── Retirement candidates ──


def test_list_retirement_candidates_orders_cold_first(tmp_path: Path) -> None:
    from app.memory.md_store import list_retirement_candidates

    agent_id = uuid.uuid4()
    _seed(tmp_path, agent_id)

    candidates = list_retirement_candidates(tmp_path, agent_id, limit=2)
    assert candidates
    assert candidates[0]["entry_id"] == "mem_cold1"
    assert all(c["entry_id"] != "mem_hot1" for c in candidates)


def test_list_retirement_candidates_excludes_protected(tmp_path: Path) -> None:
    from app.memory.md_store import list_retirement_candidates

    agent_id = uuid.uuid4()
    _seed(tmp_path, agent_id)

    candidates = list_retirement_candidates(
        tmp_path,
        agent_id,
        limit=5,
        protected_markers=["legacy proxy"],
    )
    assert all("legacy proxy" not in c["content"] for c in candidates)


def test_dream_prompt_includes_retirement_candidates() -> None:
    from app.services.auto_dream import _build_dream_consolidation_user_prompt

    prompt = _build_dream_consolidation_user_prompt(
        "Agent",
        "soul body",
        {"knowledge.md": "- [2026-01-01] cold fact"},
        retirement_candidates=[
            {
                "entry_id": "mem_cold1",
                "content": "cold fact about legacy proxy",
                "heat": 0.0,
                "filename": "knowledge.md",
            }
        ],
    )
    assert "retirement" in prompt.lower()
    assert "mem_cold1" in prompt
