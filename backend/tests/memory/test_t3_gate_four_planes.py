"""Part B red tests: T3 Platform Gate four-plane targets (spec §1.1/§3.2/§3.4).

The gate grows from the flat legacy four files to the two-plane layout:

- Fixed convergent files: ``memory/self/self.md`` +
  ``memory/profiles/{owner,collaborators,domain}.md`` — written as ``###``
  markdown entries anchored by ``<!-- id: ... -->`` (``upsert_entry`` /
  ``retire_entry``; retire marks mechanically, the convergence loop deletes).
- Dynamic network pages: ``memory/knowledge/<slug>.md`` +
  ``memory/milestones/<slug>.md`` — whole-page ``upsert_page``; a NEW
  knowledge page MUST carry ≥1 ``## Relations`` edge (forward references
  count, spec §3.4) or the gate holds it.
- Legacy XML-block targets keep working until the Part H cutover.

Patch schema stays ``t3.consolidation_patch.v1`` with the new operations —
evidence/base-revision discipline is unchanged.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _empty_sha() -> str:
    return hashlib.sha256(b"").hexdigest()


def _review_xml() -> str:
    scores = "\n".join(
        f'<score name="{name}" value="4"><rationale>充分。</rationale>'
        f"<source_refs><source_ref>t2://session/s1/segment/seg-1</source_ref></source_refs></score>"
        for name in ("evidence_strength", "scope_clarity", "stability", "future_utility", "conflict_safety")
    )
    return f"""<memory_gate_review schema_version="t3.review.v1">
  <decision>accept</decision>
  <memory_gate_rubric schema_version="memory_gate_rubric.v1">
    {scores}
    <decision>accept_new</decision>
  </memory_gate_rubric>
</memory_gate_review>"""


def _patch_xml(*, targets: list[tuple[str, str]], changes: str, labels_view: str = "capabilities") -> str:
    target_files = "\n".join(f'<target_file path="{path}"/>' for path, _sha in targets)
    base_revisions = "\n".join(f'<base_revision path="{path}" sha256="{sha}"/>' for path, sha in targets)
    return f"""<t3_consolidation_patch schema_version="t3.consolidation_patch.v1">
  <target_files>{target_files}</target_files>
  <base_revisions>{base_revisions}</base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1"/></source_packages>
  <evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence>
  <target_view_labels>
    <target_view>{labels_view}</target_view>
    <consolidation_mode>create</consolidation_mode>
    <source_coverage>single_session</source_coverage>
    <stability>stable</stability>
    <behavior_impact>recall_only</behavior_impact>
    <prompt_priority>p1_dynamic</prompt_priority>
  </target_view_labels>
  <proposed_changes>{changes}</proposed_changes>
</t3_consolidation_patch>"""


def _apply(tmp_path: Path, agent_id, patch_md: str):
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch

    return apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id=f"t3job-{uuid4().hex[:8]}",
        revised_patch_md=patch_md,
        review_md=_review_xml(),
    )


KNOWLEDGE_PAGE = """---
title: L2 Rollup
status: active
---
## Current Claim
L2 通过把计算移到链下、证明上链来扩容。

## Scope
以太坊扩容;不含 L1 分片。

## Evidence
t2-a1b2

## Relations
- is_a [[k:Scaling Solution]]
"""

KNOWLEDGE_PAGE_NO_RELATIONS = """---
title: Orphan Concept
status: active
---
## Current Claim
一个没有任何关系边的孤儿概念。

## Evidence
t2-a1b2
"""


# --- dynamic pages: knowledge / milestones ---


def test_upsert_new_knowledge_page_commits(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/knowledge/l2-rollup.md"
    changes = f'<upsert_page target="{target}"><page_content><![CDATA[{KNOWLEDGE_PAGE}]]></page_content></upsert_page>'
    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=changes))

    assert result.status == "committed", result.issues
    page = _mem_dir(tmp_path, agent_id) / "knowledge" / "l2-rollup.md"
    assert page.exists()
    assert "L2 通过把计算移到链下" in page.read_text(encoding="utf-8")


def test_new_knowledge_page_without_relations_is_held(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/knowledge/orphan-concept.md"
    changes = (
        f'<upsert_page target="{target}">'
        f"<page_content><![CDATA[{KNOWLEDGE_PAGE_NO_RELATIONS}]]></page_content>"
        f"</upsert_page>"
    )
    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=changes))

    assert result.status == "held"
    assert any("Relations" in issue for issue in result.issues)
    assert not (_mem_dir(tmp_path, agent_id) / "knowledge" / "orphan-concept.md").exists()


def test_forward_reference_satisfies_network_requirement(tmp_path: Path) -> None:
    """[[k:未存在页]] counts — forward refs mark pages worth writing (spec §3.4)."""
    agent_id = uuid4()
    target = "memory/knowledge/zk-proof.md"
    page = KNOWLEDGE_PAGE_NO_RELATIONS.replace(
        "## Evidence\nt2-a1b2\n",
        "## Evidence\nt2-a1b2\n\n## Relations\n- contrasts_with [[k:一个尚不存在的概念]]\n",
    ).replace("Orphan Concept", "ZK Proof")
    changes = f'<upsert_page target="{target}"><page_content><![CDATA[{page}]]></page_content></upsert_page>'

    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=changes))

    assert result.status == "committed", result.issues


def test_upsert_existing_page_with_stale_sha_requires_rebase(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/knowledge/l2-rollup.md"
    page_path = _mem_dir(tmp_path, agent_id) / "knowledge" / "l2-rollup.md"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(KNOWLEDGE_PAGE, encoding="utf-8")

    changes = (
        f'<upsert_page target="{target}">'
        f"<page_content><![CDATA[{KNOWLEDGE_PAGE}\n更新内容。]]></page_content>"
        f"</upsert_page>"
    )
    stale_sha = _empty_sha()  # file exists, so empty-sha is stale
    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, stale_sha)], changes=changes))

    assert result.status == "rebase_required"


def test_milestone_page_commits_without_relations(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/milestones/ms-web3-report.md"
    page = "---\ntitle: Web3 研报首胜\nstatus: active\n---\n首次交付完整深度研报,owner 明确好评。\n证据: t2-a1b2\n"
    changes = f'<upsert_page target="{target}"><page_content><![CDATA[{page}]]></page_content></upsert_page>'

    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=changes))

    assert result.status == "committed", result.issues
    assert (_mem_dir(tmp_path, agent_id) / "milestones" / "ms-web3-report.md").exists()


def test_dynamic_page_slug_is_validated(tmp_path: Path) -> None:
    agent_id = uuid4()
    for bad_target in (
        "memory/knowledge/../escape.md",
        "memory/knowledge/UPPER.md",
        "memory/knowledge/white space.md",
    ):
        changes = (
            f'<upsert_page target="{bad_target}">'
            f"<page_content><![CDATA[{KNOWLEDGE_PAGE}]]></page_content></upsert_page>"
        )
        result = _apply(tmp_path, agent_id, _patch_xml(targets=[(bad_target, _empty_sha())], changes=changes))
        assert result.status == "held", bad_target


# --- fixed convergent files: self / profiles entries ---


SELF_ENTRY = """### 需求含糊时爱自己猜 — active
<!-- id: fm-guessing -->
触发: 目标不明确。 规避: 先问一个澄清问题。
- 证据: t2-a1b2
- 状态: active(2026-07-02 起)"""


def test_upsert_entry_creates_self_entry_under_section(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/self/self.md"
    changes = (
        f'<upsert_entry target="{target}" entry_id="fm-guessing" section="失败模式">'
        f"<entry_content><![CDATA[{SELF_ENTRY}]]></entry_content>"
        f"</upsert_entry>"
    )
    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=changes))

    assert result.status == "committed", result.issues
    content = (_mem_dir(tmp_path, agent_id) / "self" / "self.md").read_text(encoding="utf-8")
    assert "## 失败模式" in content
    assert "<!-- id: fm-guessing -->" in content
    assert content.index("## 失败模式") < content.index("fm-guessing")


def test_upsert_entry_updates_existing_entry_in_place(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/self/self.md"
    create = (
        f'<upsert_entry target="{target}" entry_id="fm-guessing" section="失败模式">'
        f"<entry_content><![CDATA[{SELF_ENTRY}]]></entry_content></upsert_entry>"
    )
    first = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=create))
    assert first.status == "committed", first.issues

    self_path = _mem_dir(tmp_path, agent_id) / "self" / "self.md"
    current_sha = hashlib.sha256(self_path.read_bytes()).hexdigest()
    updated_entry = SELF_ENTRY.replace("active(2026-07-02 起)", "规避中(2026-07-02 起,规避 1 次有效)")
    update = (
        f'<upsert_entry target="{target}" entry_id="fm-guessing" section="失败模式">'
        f"<entry_content><![CDATA[{updated_entry}]]></entry_content></upsert_entry>"
    )
    second = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, current_sha)], changes=update))

    assert second.status == "committed", second.issues
    content = self_path.read_text(encoding="utf-8")
    assert content.count("fm-guessing") == 1
    assert "规避中(2026-07-02 起" in content
    assert "active(2026-07-02 起)" not in content


def test_retire_entry_marks_without_deleting(tmp_path: Path) -> None:
    """Mechanical retire only MARKS; the convergence loop (工序 4) deletes."""
    agent_id = uuid4()
    target = "memory/profiles/owner.md"
    entry = "### 偏好周报形式 — 已确认\n<!-- id: pref-weekly -->\n每周五要周报。\n- 证据: t2-a1b2"
    create = (
        f'<upsert_entry target="{target}" entry_id="pref-weekly" section="偏好">'
        f"<entry_content><![CDATA[{entry}]]></entry_content></upsert_entry>"
    )
    first = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=create))
    assert first.status == "committed", first.issues

    owner_path = _mem_dir(tmp_path, agent_id) / "profiles" / "owner.md"
    current_sha = hashlib.sha256(owner_path.read_bytes()).hexdigest()
    retire = f'<retire_entry target="{target}" entry_id="pref-weekly" reason="owner 改为月报"/>'
    second = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, current_sha)], changes=retire))

    assert second.status == "committed", second.issues
    content = owner_path.read_text(encoding="utf-8")
    assert "每周五要周报" in content  # not deleted
    assert "retired" in content  # marked for the convergence loop


def test_upsert_entry_without_id_comment_is_held(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/self/self.md"
    bad_entry = "### 没有 id 注释的条目\n内容。"
    changes = (
        f'<upsert_entry target="{target}" entry_id="fm-x" section="能力">'
        f"<entry_content><![CDATA[{bad_entry}]]></entry_content></upsert_entry>"
    )
    result = _apply(tmp_path, agent_id, _patch_xml(targets=[(target, _empty_sha())], changes=changes))

    assert result.status == "held"


# --- discipline carried over from v1 ---


def test_patch_without_t2_or_explicit_evidence_is_held(tmp_path: Path) -> None:
    agent_id = uuid4()
    target = "memory/knowledge/l2-rollup.md"
    patch = (
        _patch_xml(targets=[(target, _empty_sha())], changes="")
        .replace('ref="t2://session/s1/segment/seg-1"', 'ref="workspace://notes.md"')
        .replace(
            "<source_ref>t2://session/s1/segment/seg-1</source_ref>", "<source_ref>workspace://notes.md</source_ref>"
        )
    )
    result = _apply(tmp_path, agent_id, patch)

    assert result.status == "held"


def test_legacy_xml_block_targets_still_work(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import file_sha256

    agent_id = uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    target = "memory/t3/user.md"
    current_sha = file_sha256(mem_dir.parent / target)
    block = '<t3_user_memory id="um-1"><claim>偏好中文汇报。</claim><evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence></t3_user_memory>'
    changes = (
        f'<append_block target="{target}" block_id="um-1">'
        f"<block_content><![CDATA[{block}]]></block_content></append_block>"
    )
    result = _apply(
        tmp_path, agent_id, _patch_xml(targets=[(target, current_sha)], changes=changes, labels_view="user")
    )

    assert result.status == "committed", result.issues


def test_layout_bootstrap_creates_two_plane_dirs(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout

    agent_id = uuid4()
    ensure_t3_layout(tmp_path, agent_id)

    mem = _mem_dir(tmp_path, agent_id)
    for rel in ("self", "profiles", "knowledge", "milestones"):
        assert (mem / rel).is_dir(), rel
