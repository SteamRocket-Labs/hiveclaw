"""Part E red tests: milestones — selection criteria + retroactive promotion (spec §3.5).

Milestones are chosen, not written: criteria ①②③ ride the T2 labels call
(covered in Part D's prompt tests); criterion ④ (referenced as a narrative
anchor) can only fire during 工序 3, so HEARTBEAT.md must teach retroactive
promotion — and the gate must let one patch create the milestone page and
reference it from a profile entry atomically.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4


def test_heartbeat_template_teaches_retroactive_milestones() -> None:
    template = Path("app/templates/HEARTBEAT.md").read_text(encoding="utf-8")
    assert "memory/milestones/<slug>.md" in template
    assert "追认" in template or "retroactive" in template
    # anchors are optional navigation, evidence must stay immutable t2- refs
    assert "ms-" in template


def test_combined_patch_creates_milestone_and_references_it(tmp_path: Path) -> None:
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch

    agent_id = uuid4()
    milestone_target = "memory/milestones/ms-web3-report.md"
    self_target = "memory/self/self.md"
    empty = hashlib.sha256(b"").hexdigest()

    milestone_page = (
        "---\ntitle: Web3 研报首胜\nstatus: active\n---\n首次独立交付完整深度研报,owner 明确好评。\n证据: t2-a1b2\n"
    )
    self_entry = (
        "### 深度研究 — 熟练\n"
        "<!-- id: cap-deep-research -->\n"
        "拆解、多源检索、交叉验证。\n"
        "- 证据: t2-a1b2\n"
        "- 锚点: [[ms-web3-report]]"
    )
    scores = "\n".join(
        f'<score name="{name}" value="4"><rationale>ok</rationale>'
        f"<source_refs><source_ref>t2://session/s1/segment/seg-1</source_ref></source_refs></score>"
        for name in ("evidence_strength", "scope_clarity", "stability", "future_utility", "conflict_safety")
    )
    patch_md = f"""<t3_consolidation_patch schema_version="t3.consolidation_patch.v1">
  <target_files>
    <target_file path="{milestone_target}"/>
    <target_file path="{self_target}"/>
  </target_files>
  <base_revisions>
    <base_revision path="{milestone_target}" sha256="{empty}"/>
    <base_revision path="{self_target}" sha256="{empty}"/>
  </base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1"/></source_packages>
  <evidence><source_ref>t2://session/s1/segment/seg-1</source_ref></evidence>
  <target_view_labels>
    <target_view>milestones</target_view><consolidation_mode>create</consolidation_mode>
    <source_coverage>single_session</source_coverage><stability>stable</stability>
    <behavior_impact>recall_only</behavior_impact><prompt_priority>p1_dynamic</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <upsert_page target="{milestone_target}"><page_content><![CDATA[{milestone_page}]]></page_content></upsert_page>
    <upsert_entry target="{self_target}" entry_id="cap-deep-research" section="能力"><entry_content><![CDATA[{self_entry}]]></entry_content></upsert_entry>
  </proposed_changes>
</t3_consolidation_patch>"""
    review_md = (
        '<memory_gate_review schema_version="t3.review.v1"><decision>accept</decision>'
        '<memory_gate_rubric schema_version="memory_gate_rubric.v1">'
        f"{scores}<decision>accept_new</decision></memory_gate_rubric></memory_gate_review>"
    )

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="t3job-anchor",
        revised_patch_md=patch_md,
        review_md=review_md,
    )

    assert result.status == "committed", result.issues
    mem = tmp_path / str(agent_id) / "memory"
    assert (mem / "milestones" / "ms-web3-report.md").exists()
    self_text = (mem / "self" / "self.md").read_text(encoding="utf-8")
    assert "[[ms-web3-report]]" in self_text
    assert "## 能力" in self_text
