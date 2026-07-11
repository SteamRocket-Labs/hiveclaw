"""Part F red tests: growth mechanisms — convergence loop + nominations (spec §4.3).

工序 4 convergence: writes are incremental operation patches; convergence is
the OPPOSITE — a full-file rewrite that merges duplicates, resolves
contradictions, and clears retired entries. The platform measures dirtiness
mechanically and surfaces it in the curator's neighborhood; the LLM performs
the rewrite through the gate (full input, old version archived, refs unioned).
工序 5 (self→soul nomination via owner-gated dream) and 工序 6 (self→Skill
candidate handoff) ride existing gates — the teaching lives in the SOP
templates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


DIRTY_SELF = "\n\n".join(
    [
        "## 能力",
        *[
            f"### 能力条目 {i} — 一般\n<!-- id: cap-{i} -->\n重复啰嗦的描述内容，彼此高度重叠。\n- 证据: t2-x{i}"
            for i in range(8)
        ],
        "## 失败模式",
        "### 已退役条目 A — active\n<!-- id: fm-a -->\n<!-- retired: by t3job-1 at 2026-07-01 -->\n旧内容。",
        "### 已退役条目 B — active\n<!-- id: fm-b -->\n<!-- retired: by t3job-2 at 2026-07-01 -->\n旧内容。",
    ]
)


def _write_self(tmp_path: Path, agent_id, content: str) -> Path:
    self_dir = _mem_dir(tmp_path, agent_id) / "self"
    self_dir.mkdir(parents=True, exist_ok=True)
    path = self_dir / "self.md"
    path.write_text(content, encoding="utf-8")
    return path


# --- mechanical dirtiness measurement ---


def test_dirtiness_flags_retired_entries_and_bloat(tmp_path: Path) -> None:
    from app.memory.convergence import assess_convergence_dirtiness

    agent_id = uuid4()
    _write_self(tmp_path, agent_id, DIRTY_SELF)

    report = assess_convergence_dirtiness(
        agent_id=agent_id, data_root=tmp_path, max_chars_per_file=200, max_retired_entries=1
    )

    dirty = {item["target"]: item for item in report.dirty_files}
    assert "memory/self/self.md" in dirty
    reasons = dirty["memory/self/self.md"]["reasons"]
    assert any("retired" in reason for reason in reasons)
    assert any("chars" in reason for reason in reasons)


def test_dirtiness_clean_for_small_files(tmp_path: Path) -> None:
    from app.memory.convergence import assess_convergence_dirtiness

    agent_id = uuid4()
    _write_self(tmp_path, agent_id, "## 能力\n\n### 深度研究 — 熟练\n<!-- id: cap-1 -->\n简洁。\n")

    report = assess_convergence_dirtiness(agent_id=agent_id, data_root=tmp_path)

    assert report.dirty_files == ()


def test_dirtiness_report_persisted_to_control(tmp_path: Path) -> None:
    from app.memory.convergence import refresh_convergence_dirtiness

    agent_id = uuid4()
    _write_self(tmp_path, agent_id, DIRTY_SELF)

    refresh_convergence_dirtiness(agent_id=agent_id, data_root=tmp_path, max_chars_per_file=200)

    payload = json.loads(
        (_mem_dir(tmp_path, agent_id) / "control" / "convergence_dirtiness.json").read_text(encoding="utf-8")
    )
    assert payload["schema"] == "convergence_dirtiness.v1"
    assert payload["dirty_files"]


def test_neighborhood_surfaces_convergence_warning(tmp_path: Path) -> None:
    from app.memory.convergence import refresh_convergence_dirtiness
    from app.memory.t3_consolidation import build_t3_neighborhood

    agent_id = uuid4()
    _write_self(tmp_path, agent_id, DIRTY_SELF)
    refresh_convergence_dirtiness(agent_id=agent_id, data_root=tmp_path, max_chars_per_file=200)

    neighborhood = build_t3_neighborhood(data_root=tmp_path, agent_id=agent_id)

    assert "CONVERGENCE NEEDED" in neighborhood
    assert "memory/self/self.md" in neighborhood


# --- full-file rewrite through the gate (工序 4 execution) ---


def _scores() -> str:
    return "\n".join(
        f'<score name="{name}" value="4"><rationale>ok</rationale>'
        f"<source_refs><source_ref>t2://session/s1/segment/seg-1</source_ref></source_refs></score>"
        for name in ("evidence_strength", "scope_clarity", "stability", "future_utility", "conflict_safety")
    )


def _review_md() -> str:
    return (
        '<memory_gate_review schema_version="t3.review.v1"><decision>accept</decision>'
        '<memory_gate_rubric schema_version="memory_gate_rubric.v1">'
        f"{_scores()}<decision>supersede_existing</decision></memory_gate_rubric></memory_gate_review>"
    )


def _rewrite_patch(target: str, sha: str, new_content: str, *, note: str | None) -> str:
    note_attr = f' convergence_note="{note}"' if note else ""
    return f"""<t3_consolidation_patch schema_version="t3.consolidation_patch.v1">
  <target_files><target_file path="{target}"/></target_files>
  <base_revisions><base_revision path="{target}" sha256="{sha}"/></base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1"/></source_packages>
  <evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence>
  <target_view_labels>
    <target_view>self</target_view><consolidation_mode>supersede</consolidation_mode>
    <source_coverage>multi_session</source_coverage><stability>stable</stability>
    <behavior_impact>memory_policy</behavior_impact><prompt_priority>p0_if_relevant</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <rewrite_file target="{target}"{note_attr}><file_content><![CDATA[{new_content}]]></file_content></rewrite_file>
  </proposed_changes>
</t3_consolidation_patch>"""


CONVERGED_SELF = """## 能力

### 深度研究 — 熟练
<!-- id: cap-merged -->
八条重复条目收敛为一条母题;证据取并集。
- 证据: t2-x0 · t2-x1 · t2-x7

## 失败模式

(已退役条目已在本次收敛中清除)
"""


def test_rewrite_file_converges_profile_file_with_archive(tmp_path: Path) -> None:
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid4()
    self_path = _write_self(tmp_path, agent_id, DIRTY_SELF)
    target = "memory/self/self.md"
    sha = file_sha256(self_path)

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="t3job-converge",
        revised_patch_md=_rewrite_patch(target, sha, CONVERGED_SELF, note="消重8→1;清除retired×2;refs并集"),
        review_md=_review_md(),
    )

    assert result.status == "committed", result.issues
    content = self_path.read_text(encoding="utf-8")
    assert "cap-merged" in content
    assert "retired:" not in content  # retired entries physically removed by convergence
    # The shared AgentAssetTransaction journal is the single rollback
    # authority for T3/Soul/Skill writes; the prior file lives in its backup.
    revision = json.loads((tmp_path / str(agent_id) / "runtime_artifacts/asset_transactions/revision.json").read_text())
    journal_dir = (
        tmp_path / str(agent_id) / "runtime_artifacts/asset_transactions/transactions" / revision["last_transaction_id"]
    )
    journal = json.loads((journal_dir / "journal.json").read_text())
    operation = next(item for item in journal["operations"] if item["path"] == target)
    backup = journal_dir / operation["backup_file"]
    assert backup.is_file(), "convergence rewrite must archive the previous version"
    assert "cap-0" in backup.read_text(encoding="utf-8")


def test_rewrite_file_without_convergence_note_is_held(tmp_path: Path) -> None:
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid4()
    self_path = _write_self(tmp_path, agent_id, DIRTY_SELF)
    sha = file_sha256(self_path)

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="t3job-no-note",
        revised_patch_md=_rewrite_patch("memory/self/self.md", sha, CONVERGED_SELF, note=None),
        review_md=_review_md(),
    )

    assert result.status == "held"
    assert "cap-0" in self_path.read_text(encoding="utf-8")  # untouched


def test_rewrite_file_with_stale_sha_requires_rebase(tmp_path: Path) -> None:
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch

    agent_id = uuid4()
    _write_self(tmp_path, agent_id, DIRTY_SELF)
    stale = hashlib.sha256(b"").hexdigest()

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="t3job-stale",
        revised_patch_md=_rewrite_patch("memory/self/self.md", stale, CONVERGED_SELF, note="收敛"),
        review_md=_review_md(),
    )

    assert result.status == "rebase_required"


def test_rewrite_file_rejects_emptying_a_populated_file(tmp_path: Path) -> None:
    """Anti-deletion guard: convergence must converge, not wipe."""
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid4()
    self_path = _write_self(tmp_path, agent_id, DIRTY_SELF)
    sha = file_sha256(self_path)

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="t3job-wipe",
        revised_patch_md=_rewrite_patch("memory/self/self.md", sha, "  ", note="清空"),
        review_md=_review_md(),
    )

    assert result.status == "held"
    assert "cap-0" in self_path.read_text(encoding="utf-8")


# --- production wiring + SOP teaching ---


@pytest.mark.asyncio
async def test_heartbeat_maintenance_refreshes_convergence_dirtiness(tmp_path: Path, monkeypatch) -> None:
    from app.services import heartbeat

    agent_id = uuid4()
    _write_self(tmp_path, agent_id, DIRTY_SELF)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: type(
            "S",
            (),
            {
                "AGENT_DATA_DIR": str(tmp_path),
                "MEMORY_CONVERGENCE_MAX_CHARS_PER_FILE": 200.0,
                "MEMORY_CONVERGENCE_MAX_RETIRED_ENTRIES": 1,
            },
        )(),
    )

    report = await heartbeat._run_convergence_dirtiness_refresh(agent_id)

    assert report is not None
    assert report.dirty_files
    assert (_mem_dir(tmp_path, agent_id) / "control" / "convergence_dirtiness.json").exists()


def test_execute_heartbeat_wires_convergence_refresh() -> None:
    import inspect

    from app.services import heartbeat

    source = inspect.getsource(heartbeat._execute_heartbeat)
    assert "_run_convergence_dirtiness_refresh(" in source


def test_heartbeat_template_teaches_convergence_loop() -> None:
    template = Path("app/templates/HEARTBEAT.md").read_text(encoding="utf-8")
    assert "rewrite_file" in template
    assert "CONVERGENCE NEEDED" in template
    assert "convergence_note" in template
    # profile plane converges; knowledge plane is tended (never squashed) — opposite disciplines
    assert "织网" in template or "network care" in template


def test_dream_template_teaches_soul_nomination_and_skill_handoff() -> None:
    template = Path("app/templates/DREAM.md").read_text(encoding="utf-8")
    # 工序 5: self→soul nomination, owner-gated, never self-written
    assert "self/self.md" in template
    assert "提名" in template or "nominat" in template
    # nomination standard: long-term stable + high confidence + no counter-example demotion
    assert "反例" in template or "counter-example" in template
    # 工序 6: self→Skill candidate handoff with the two-way link convention
    assert "skill" in template.lower()
    assert "已固化" in template or "[[skill-" in template


def test_convergence_thresholds_come_from_settings() -> None:
    from app.config import get_settings

    settings = get_settings()
    assert settings.MEMORY_CONVERGENCE_MAX_CHARS_PER_FILE > 0
    assert settings.MEMORY_CONVERGENCE_MAX_RETIRED_ENTRIES >= 1
