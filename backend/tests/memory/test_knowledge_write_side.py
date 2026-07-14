"""Part C red tests: knowledge write side — curator visibility + network care (spec §3.4).

The T3 Consolidator can only judge update-vs-create if the platform shows it
the current knowledge network (L1: complete input visibility), and the gate
must mechanically protect the "conflicts go to Contradictions, never delete
the old view" rule when a page is updated.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4


def _mem_dir(tmp_path: Path, agent_id) -> Path:
    return tmp_path / str(agent_id) / "memory"


def _write_page(tmp_path: Path, agent_id, *, subdir: str, slug: str, body: str) -> Path:
    page_dir = _mem_dir(tmp_path, agent_id) / subdir
    page_dir.mkdir(parents=True, exist_ok=True)
    path = page_dir / f"{slug}.md"
    path.write_text(body, encoding="utf-8")
    return path


KNOWLEDGE_WITH_CONTRADICTIONS = """---
title: L2 Rollup
status: active
---
## Current Claim
L2 通过链下计算扩容。

## Evidence
t2-a1b2

## Contradictions
- 早期以为 Optimistic 主流,t2-i9j0 显示 ZK 增长更快 → 已更新
- t2-k1l2 曾质疑数据可用性成本 → 部分成立

## Relations
- is_a [[k:Scaling Solution]]

## Decisive Tail Context
这一段位于页面末尾，决定新证据应更新现有页面而不是机械新建重复页面。
"""


def test_neighborhood_lists_two_plane_state(tmp_path: Path) -> None:
    from app.memory.t3_consolidation import build_t3_neighborhood

    agent_id = uuid4()
    _write_page(tmp_path, agent_id, subdir="knowledge", slug="l2-rollup", body=KNOWLEDGE_WITH_CONTRADICTIONS)
    _write_page(
        tmp_path,
        agent_id,
        subdir="milestones",
        slug="ms-web3-report",
        body="---\ntitle: Web3 研报首胜\nstatus: active\n---\n首次交付完整研报。\n",
    )
    self_dir = _mem_dir(tmp_path, agent_id) / "self"
    self_dir.mkdir(parents=True, exist_ok=True)
    (self_dir / "self.md").write_text(
        "## 失败模式\n\n### 爱猜 — active\n<!-- id: fm-guessing -->\n- 状态: active\n\n"
        "## 完整上下文\n这条尾部说明决定该失败模式仍然有效，不能只凭标题判断。\n",
        encoding="utf-8",
    )

    neighborhood = build_t3_neighborhood(data_root=tmp_path, agent_id=agent_id)

    assert "knowledge/l2-rollup" in neighborhood
    assert "L2 Rollup" in neighborhood
    assert "is_a [[k:Scaling Solution]]" in neighborhood  # relations visible for update-vs-create
    assert "milestones/ms-web3-report" in neighborhood
    assert "fm-guessing" in neighborhood  # profile-plane entry ids visible
    assert "memory/self/self.md" in neighborhood  # base revision line for the fixed files
    assert "决定新证据应更新现有页面" in neighborhood
    assert "决定该失败模式仍然有效" in neighborhood


def test_source_bundle_allows_two_plane_targets(tmp_path: Path) -> None:
    from app.memory.t3_consolidation import build_t3_consolidation_batch

    agent_id = uuid4()
    overlay = _mem_dir(tmp_path, agent_id) / "explicit"
    (overlay / "entries").mkdir(parents=True, exist_ok=True)
    (overlay / "manifest.jsonl").write_text(
        json.dumps({"id": "ex-1", "status": "active", "category": "general"}) + "\n", encoding="utf-8"
    )
    (overlay / "entries" / "ex-1.md").write_text("<normalized_memory>记住。</normalized_memory>", encoding="utf-8")

    result = build_t3_consolidation_batch(
        agent_id=agent_id, data_root=tmp_path, package_dirs=[], explicit_entry_ids=["ex-1"]
    )

    bundle = json.loads((result.job_dir / "source_bundle.json").read_text(encoding="utf-8"))
    allowed = bundle["allowed_target_files"]
    assert "memory/self/self.md" in allowed
    assert "memory/profiles/owner.md" in allowed
    assert any("memory/knowledge/<slug>.md" in item for item in allowed)
    assert any("memory/milestones/<slug>.md" in item for item in allowed)


def test_updating_knowledge_page_must_preserve_contradictions(tmp_path: Path) -> None:
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid4()
    page_path = _write_page(
        tmp_path, agent_id, subdir="knowledge", slug="l2-rollup", body=KNOWLEDGE_WITH_CONTRADICTIONS
    )
    target = "memory/knowledge/l2-rollup.md"
    current_sha = file_sha256(page_path)

    dropped = KNOWLEDGE_WITH_CONTRADICTIONS.replace("- t2-k1l2 曾质疑数据可用性成本 → 部分成立\n", "").replace(
        "L2 通过链下计算扩容。", "L2 通过链下计算+证明上链扩容。"
    )
    kept = KNOWLEDGE_WITH_CONTRADICTIONS.replace("L2 通过链下计算扩容。", "L2 通过链下计算+证明上链扩容。")

    def _patch(page_content: str, sha: str) -> tuple[str, str]:
        scores = "\n".join(
            f'<score name="{name}" value="4"><rationale>ok</rationale>'
            f"<source_refs><source_ref>t2://session/s1/segment/seg-1</source_ref></source_refs></score>"
            for name in ("evidence_strength", "scope_clarity", "stability", "future_utility", "conflict_safety")
        )
        return (
            f'<t3_consolidation_patch schema_version="t3.consolidation_patch.v1">'
            f'<target_files><target_file path="{target}"/></target_files>'
            f'<base_revisions><base_revision path="{target}" sha256="{sha}"/></base_revisions>'
            f'<source_packages><source_package ref="t2://session/s1/segment/seg-1"/></source_packages>'
            f"<evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence>"
            f"<target_view_labels><target_view>knowledge</target_view>"
            f"<consolidation_mode>merge</consolidation_mode><source_coverage>single_session</source_coverage>"
            f"<stability>stable</stability><behavior_impact>recall_only</behavior_impact>"
            f"<prompt_priority>p1_dynamic</prompt_priority></target_view_labels>"
            f'<proposed_changes><upsert_page target="{target}">'
            f"<page_content><![CDATA[{page_content}]]></page_content></upsert_page></proposed_changes>"
            f"</t3_consolidation_patch>"
        ), (
            '<memory_gate_review schema_version="t3.review.v1"><decision>accept</decision>'
            '<memory_gate_rubric schema_version="memory_gate_rubric.v1">'
            f"{scores}<decision>merge_required</decision></memory_gate_rubric></memory_gate_review>"
        )

    patch_md, review_md = _patch(dropped, current_sha)
    held = apply_t3_consolidation_patch(
        agent_id=agent_id, data_root=tmp_path, job_id="t3job-drop", revised_patch_md=patch_md, review_md=review_md
    )
    assert held.status == "held"
    assert any("Contradictions" in issue for issue in held.issues)
    assert "曾质疑数据可用性成本" in page_path.read_text(encoding="utf-8")  # old page intact

    current_sha = file_sha256(page_path)
    patch_md, review_md = _patch(kept, current_sha)
    committed = apply_t3_consolidation_patch(
        agent_id=agent_id, data_root=tmp_path, job_id="t3job-keep", revised_patch_md=patch_md, review_md=review_md
    )
    assert committed.status == "committed", committed.issues
    updated = page_path.read_text(encoding="utf-8")
    assert "证明上链扩容" in updated
    assert "曾质疑数据可用性成本" in updated


def test_heartbeat_template_teaches_knowledge_curation() -> None:
    template = Path("app/templates/HEARTBEAT.md").read_text(encoding="utf-8")
    assert "memory/knowledge/<slug>.md" in template
    assert "upsert_page" in template
    assert "Relations" in template  # mandatory network edge teaching
    assert "Contradictions" in template  # conflicts recorded, old view never deleted
