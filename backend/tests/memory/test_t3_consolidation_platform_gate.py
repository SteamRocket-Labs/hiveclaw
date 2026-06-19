from __future__ import annotations

import json
import uuid
from pathlib import Path


def _write_reviewed_t2_package(root: Path, agent_id: uuid.UUID, session_id: str = "s1", segment_id: str = "seg-1") -> Path:
    package_dir = root / str(agent_id) / "memory" / "sessions" / session_id / "segments" / segment_id
    package_dir.mkdir(parents=True)
    source_ref = f"t0://session/{session_id}/segment/{segment_id}#seq=1..3"
    (package_dir / "summary.md").write_text(
        f"""# Summary

<t2_summary id="sum-1" status="closed">
  <summary>用户要求 T3 通过 batch 收敛，并保留场景召回线索。</summary>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
</t2_summary>
""",
        encoding="utf-8",
    )
    (package_dir / "labels.md").write_text(
        f"""# Labels

<t2_labels id="lbl-1">
  <package_status>closed</package_status>
  <event_labels><event_label>memory_architecture_decision</event_label></event_labels>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
</t2_labels>
""",
        encoding="utf-8",
    )
    (package_dir / "review.md").write_text(
        f"""# Review

<t2_review id="rev-1">
  <decision>approved</decision>
  <allowed_next>t3_intake</allowed_next>
  <review_rubric schema_version="t2.review_rubric.v1">
    <score name="summary_fidelity" value="0.95"/>
    <score name="source_ref_coverage" value="0.95"/>
    <score name="label_alignment" value="0.90"/>
    <score name="safety_scope" value="0.95"/>
    <score name="package_closure" value="0.90"/>
    <review_score>0.95</review_score>
  </review_rubric>
  <source_refs><source_ref uri="{source_ref}"/></source_refs>
</t2_review>
""",
        encoding="utf-8",
    )
    (package_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "t2.segment-package.manifest.v1",
                "package_id": "pkg-1",
                "package_status": "reviewed",
                "source_refs": [source_ref],
                "files": {
                    "summary.md": {},
                    "labels.md": {},
                    "review.md": {},
                    "manifest.json": {},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return package_dir


def _accepted_review() -> str:
    return """# T3 Memory Gate Review

<memory_gate_review id="t3r_1" schema_version="t3.review.v1">
  <decision>accept</decision>
  <memory_gate_rubric schema_version="memory_gate_rubric.v1">
    <score name="evidence_strength" value="4"><rationale>Reviewed T2 package plus source refs support the claim.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs></score>
    <score name="scope_clarity" value="4"><rationale>Target is clearly an episode recall anchor.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#labels</source_ref></source_refs></score>
    <score name="stability" value="3"><rationale>Architecture principle is stable enough for T3.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#review</source_ref></source_refs></score>
    <score name="future_utility" value="4"><rationale>Improves future recall and memory-system work.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs></score>
    <score name="conflict_safety" value="4"><rationale>No conflicting T3 block in neighborhood.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs></score>
    <decision>accept_new</decision>
    <decision_rationale>Meets T3 acceptance threshold.</decision_rationale>
    <required_followup>commit</required_followup>
  </memory_gate_rubric>
</memory_gate_review>
"""


def _accepted_review_for(*refs: str, decision: str = "accept_new") -> str:
    source_refs = "".join(f"<source_ref>{ref}</source_ref>" for ref in refs)
    return f"""# T3 Memory Gate Review

<memory_gate_review id="t3r_1" schema_version="t3.review.v1">
  <decision>accept</decision>
  <memory_gate_rubric schema_version="memory_gate_rubric.v1">
    <score name="evidence_strength" value="4"><rationale>Evidence directly supports the claim.</rationale><source_refs>{source_refs}</source_refs></score>
    <score name="scope_clarity" value="4"><rationale>The target memory scope is narrow and explicit.</rationale><source_refs>{source_refs}</source_refs></score>
    <score name="stability" value="3"><rationale>The memory is stable enough for T3.</rationale><source_refs>{source_refs}</source_refs></score>
    <score name="future_utility" value="4"><rationale>The memory improves future behavior.</rationale><source_refs>{source_refs}</source_refs></score>
    <score name="conflict_safety" value="4"><rationale>No conflicting accepted T3 block exists.</rationale><source_refs>{source_refs}</source_refs></score>
    <decision>{decision}</decision>
    <decision_rationale>Meets T3 acceptance threshold.</decision_rationale>
    <required_followup>commit</required_followup>
  </memory_gate_rubric>
</memory_gate_review>
"""


def _write_explicit_overlay_entry(root: Path, agent_id: uuid.UUID, entry_id: str = "explicit_keep_discussing") -> str:
    overlay_dir = root / str(agent_id) / "memory" / "explicit"
    entries_dir = overlay_dir / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    entry_path = entries_dir / f"{entry_id}.md"
    entry_path.write_text(
        f"""---
id: {entry_id}
origin: explicit_user_request
category: feedback
principal_scope: user
status: active
target_hint: user
sensitivity: PL1_public
created_at: 2026-06-18T00:00:00+00:00
source_refs:
  - tool:save_memory
---

<explicit_memory id="{entry_id}" status="active">
  <user_words>用户要求架构改造前必须先讨论并落文档。</user_words>
  <normalized_memory>用户要求架构改造前必须先讨论并落文档。</normalized_memory>
  <source_refs><source_ref>tool:save_memory</source_ref></source_refs>
</explicit_memory>
""",
        encoding="utf-8",
    )
    (overlay_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "id": entry_id,
                "status": "active",
                "category": "feedback",
                "target_hint": "user",
                "sensitivity": "PL1_public",
                "created_at": "2026-06-18T00:00:00+00:00",
                "source_refs": "tool:save_memory",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return entry_id


def test_build_t3_consolidation_batch_stages_source_bundle_and_neighborhood(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_consolidation import build_t3_consolidation_batch

    agent_id = uuid.uuid4()
    package_dir = _write_reviewed_t2_package(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    (mem_dir / "t3" / "capabilities.md").write_text(
        "# T3 Capabilities\n\n"
        "<t3_capability id=\"cap_existing\" status=\"active\"><name>Memory batch flow</name></t3_capability>\n",
        encoding="utf-8",
    )

    result = build_t3_consolidation_batch(
        agent_id=agent_id,
        data_root=tmp_path,
        package_dirs=[package_dir],
        job_id="job-t3-1",
    )

    assert result.status == "staged"
    assert (result.job_dir / "source_bundle.json").exists()
    assert (result.job_dir / "t3_neighborhood.md").exists()
    assert (result.job_dir / "consolidation_pitch.md").exists()
    assert (result.job_dir / "review.md").exists()
    assert (result.job_dir / "revised_patch.md").exists()
    assert (result.job_dir / "manifest.json").exists()

    bundle = json.loads((result.job_dir / "source_bundle.json").read_text(encoding="utf-8"))
    assert bundle["schema_version"] == "t3.source_bundle.v1"
    assert bundle["source_packages"][0]["ref"] == "t2://session/s1/segment/seg-1"
    assert "memory/t3/episodes.md" in bundle["allowed_target_files"]

    neighborhood = (result.job_dir / "t3_neighborhood.md").read_text(encoding="utf-8")
    assert "cap_existing" in neighborhood
    assert "base_revision" in neighborhood


def test_build_t3_consolidation_batch_embeds_explicit_overlay_details(tmp_path: Path) -> None:
    from app.memory.t3_consolidation import build_t3_consolidation_batch

    agent_id = uuid.uuid4()
    entry_id = _write_explicit_overlay_entry(tmp_path, agent_id)

    result = build_t3_consolidation_batch(
        agent_id=agent_id,
        data_root=tmp_path,
        package_dirs=[],
        explicit_entry_ids=[entry_id],
        job_id="job-explicit-1",
    )

    assert result.status == "staged"
    bundle = json.loads((result.job_dir / "source_bundle.json").read_text(encoding="utf-8"))
    assert bundle["source_packages"] == []
    assert bundle["explicit_overlay_entries"][0]["id"] == entry_id
    assert bundle["explicit_overlay_entries"][0]["ref"] == f"explicit://memory/{entry_id}"
    assert bundle["explicit_overlay_entries"][0]["status"] == "active"
    assert bundle["explicit_overlay_entries"][0]["target_hint"] == "user"
    assert "架构改造前必须先讨论" in bundle["explicit_overlay_entries"][0]["content"]
    assert "tool:save_memory" in bundle["explicit_overlay_entries"][0]["source_refs"]


def test_platform_gate_applies_llm_authored_append_block_atomically(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid.uuid4()
    _write_reviewed_t2_package(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    target = mem_dir / "t3" / "episodes.md"
    base_sha = file_sha256(target)
    patch = f"""# Revised Patch

<t3_consolidation_patch id="t3p_1" schema_version="t3.consolidation_patch.v1">
  <base_revisions>
    <base_revision path="memory/t3/episodes.md" sha256="{base_sha}"/>
  </base_revisions>
  <source_packages>
    <source_package ref="t2://session/s1/segment/seg-1" status="reviewed"/>
  </source_packages>
  <target_files>
    <target_file path="memory/t3/episodes.md"/>
  </target_files>
  <target_view_labels>
    <target_view>episodes</target_view>
    <consolidation_mode>create</consolidation_mode>
    <source_coverage>single_session</source_coverage>
    <cue_strength>0.80</cue_strength>
    <stability>stable</stability>
    <behavior_impact>recall_only</behavior_impact>
    <prompt_priority>p1_dynamic</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <append_block target="memory/t3/episodes.md" block_id="ep_memory_t3_batch" insert_policy="append_to_active_blocks">
      <block_content><![CDATA[
<t3_episode id="ep_memory_t3_batch" status="active" confidence="0.95" prompt_priority="p1_dynamic">
  <title>T3 通过 Consolidation Batch 从多个 Segment Package 收敛</title>
  <source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs>
</t3_episode>
      ]]></block_content>
    </append_block>
  </proposed_changes>
  <evidence>
    <source_ref>t2://session/s1/segment/seg-1#summary</source_ref>
  </evidence>
</t3_consolidation_patch>
"""

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-t3-commit",
        revised_patch_md=patch,
        review_md=_accepted_review(),
    )

    assert result.status == "committed"
    body = target.read_text(encoding="utf-8")
    assert "ep_memory_t3_batch" in body
    assert "T3 通过 Consolidation Batch" in body
    assert "consolidation_patch" not in body
    assert (mem_dir / ".staging" / "t3_jobs" / "job-t3-commit" / "manifest.json").exists()


def test_platform_gate_marks_t2_and_explicit_overlay_absorbed_after_commit(tmp_path: Path) -> None:
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid.uuid4()
    package_dir = _write_reviewed_t2_package(tmp_path, agent_id)
    explicit_id = _write_explicit_overlay_entry(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    target = mem_dir / "t3" / "user.md"
    base_sha = file_sha256(target)
    explicit_ref = f"explicit://memory/{explicit_id}"
    t2_ref = "t2://session/s1/segment/seg-1"
    patch = f"""# Revised Patch

<t3_consolidation_patch id="t3p_explicit" schema_version="t3.consolidation_patch.v1">
  <base_revisions><base_revision path="memory/t3/user.md" sha256="{base_sha}"/></base_revisions>
  <source_packages>
    <source_package ref="{t2_ref}" status="reviewed"/>
    <source_package ref="{explicit_ref}" status="active"/>
  </source_packages>
  <target_files><target_file path="memory/t3/user.md"/></target_files>
  <target_view_labels>
    <target_view>user</target_view>
    <consolidation_mode>create</consolidation_mode>
    <source_coverage>explicit_user</source_coverage>
    <cue_strength>0.90</cue_strength>
    <stability>stable</stability>
    <behavior_impact>response_style</behavior_impact>
    <prompt_priority>p1_dynamic</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <append_block target="memory/t3/user.md" block_id="usr_discuss_first">
      <block_content><![CDATA[
<t3_user_memory id="usr_discuss_first" status="active" confidence="0.95" prompt_priority="p1_dynamic">
  <claim>用户要求架构改造前必须先讨论并落文档。</claim>
  <source_refs>
    <source_ref>{explicit_ref}</source_ref>
    <source_ref>t2://session/s1/segment/seg-1#summary</source_ref>
  </source_refs>
</t3_user_memory>
      ]]></block_content>
    </append_block>
  </proposed_changes>
  <evidence>
    <source_ref>{explicit_ref}</source_ref>
    <source_ref>t2://session/s1/segment/seg-1#summary</source_ref>
  </evidence>
</t3_consolidation_patch>
"""

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-explicit-commit",
        revised_patch_md=patch,
        review_md=_accepted_review_for(explicit_ref, "t2://session/s1/segment/seg-1#summary"),
    )

    assert result.status == "committed"
    assert "usr_discuss_first" in target.read_text(encoding="utf-8")

    overlay_entry = load_explicit_overlay_entries(tmp_path, agent_id)[0]
    assert overlay_entry.status == "absorbed"
    assert overlay_entry.metadata["accepted_blocks"] == "usr_discuss_first"
    assert overlay_entry.metadata["status_reason"] == "t3_commit:job-explicit-commit"

    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_status"] == "absorbed"
    assert manifest["t3_absorbed_by"] == "job-explicit-commit"
    assert manifest["t3_committed_blocks"] == ["usr_discuss_first"]


def test_platform_gate_accepts_reinforce_mode_and_marks_sources_reinforced(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid.uuid4()
    package_dir = _write_reviewed_t2_package(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    target = mem_dir / "t3" / "user.md"
    target.write_text(
        "# T3 User\n\n"
        "<t3_user_memory id=\"usr_existing_rule\" status=\"active\" confidence=\"0.90\" prompt_priority=\"p1_dynamic\">\n"
        "  <claim>用户要求架构改造前必须先讨论。</claim>\n"
        "  <source_refs><source_ref>t2://session/s0/segment/seg-0</source_ref></source_refs>\n"
        "</t3_user_memory>\n",
        encoding="utf-8",
    )
    base_sha = file_sha256(target)
    t2_ref = "t2://session/s1/segment/seg-1"
    patch = f"""# Revised Patch

<t3_consolidation_patch id="t3p_reinforce" schema_version="t3.consolidation_patch.v1">
  <base_revisions><base_revision path="memory/t3/user.md" sha256="{base_sha}"/></base_revisions>
  <source_packages><source_package ref="{t2_ref}" status="reviewed"/></source_packages>
  <target_files><target_file path="memory/t3/user.md"/></target_files>
  <target_view_labels>
    <target_view>user</target_view>
    <consolidation_mode>reinforce</consolidation_mode>
    <source_coverage>single_session</source_coverage>
    <cue_strength>0.80</cue_strength>
    <stability>stable</stability>
    <behavior_impact>response_style</behavior_impact>
    <prompt_priority>p1_dynamic</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <reinforce_block target="memory/t3/user.md" block_id="usr_existing_rule"/>
  </proposed_changes>
  <evidence><source_ref>{t2_ref}#summary</source_ref></evidence>
</t3_consolidation_patch>
"""

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-reinforce",
        revised_patch_md=patch,
        review_md=_accepted_review_for(f"{t2_ref}#summary", decision="reinforced"),
    )

    assert result.status == "committed"
    assert result.committed_blocks == ("usr_existing_rule",)
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_status"] == "reinforced"
    assert manifest["t3_lifecycle_status"] == "reinforced"
    assert "absorbed_at" not in manifest


def test_platform_gate_rejects_low_score_reinforced_review(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid.uuid4()
    _write_reviewed_t2_package(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    target = mem_dir / "t3" / "user.md"
    base_sha = file_sha256(target)
    t2_ref = "t2://session/s1/segment/seg-1"
    patch = f"""# Revised Patch

<t3_consolidation_patch id="t3p_bad_reinforce" schema_version="t3.consolidation_patch.v1">
  <base_revisions><base_revision path="memory/t3/user.md" sha256="{base_sha}"/></base_revisions>
  <source_packages><source_package ref="{t2_ref}" status="reviewed"/></source_packages>
  <target_files><target_file path="memory/t3/user.md"/></target_files>
  <target_view_labels>
    <target_view>user</target_view>
    <consolidation_mode>reinforce</consolidation_mode>
    <source_coverage>single_session</source_coverage>
    <cue_strength>0.80</cue_strength>
    <stability>stable</stability>
    <behavior_impact>response_style</behavior_impact>
    <prompt_priority>p1_dynamic</prompt_priority>
  </target_view_labels>
  <proposed_changes>
    <reinforce_block target="memory/t3/user.md" block_id="usr_existing_rule"/>
  </proposed_changes>
  <evidence><source_ref>{t2_ref}#summary</source_ref></evidence>
</t3_consolidation_patch>
"""
    low_review = _accepted_review_for(f"{t2_ref}#summary", decision="reinforced")
    low_review = low_review.replace('value="4"', 'value="1"')

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-low-reinforce",
        revised_patch_md=patch,
        review_md=low_review,
    )

    assert result.status == "held"
    assert "evidence_strength below accepted threshold" in result.issues


def test_stage_pending_t3_consolidation_job_discovers_reviewed_packages_and_active_overlay(tmp_path: Path) -> None:
    from app.memory.t3_consolidation import stage_pending_t3_consolidation_job

    agent_id = uuid.uuid4()
    _write_reviewed_t2_package(tmp_path, agent_id)
    explicit_id = _write_explicit_overlay_entry(tmp_path, agent_id)

    result = stage_pending_t3_consolidation_job(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-pending-1",
    )

    assert result.status == "staged"
    bundle = json.loads((result.job_dir / "source_bundle.json").read_text(encoding="utf-8"))
    assert bundle["source_packages"][0]["ref"] == "t2://session/s1/segment/seg-1"
    assert bundle["explicit_overlay_entries"][0]["id"] == explicit_id


def test_stage_pending_t3_consolidation_job_reuses_existing_staged_job(tmp_path: Path) -> None:
    from app.memory.t3_consolidation import stage_pending_t3_consolidation_job

    agent_id = uuid.uuid4()
    _write_reviewed_t2_package(tmp_path, agent_id)
    _write_explicit_overlay_entry(tmp_path, agent_id)

    first = stage_pending_t3_consolidation_job(agent_id=agent_id, data_root=tmp_path)
    second = stage_pending_t3_consolidation_job(agent_id=agent_id, data_root=tmp_path)

    assert first.status == "staged"
    assert second.status == "staged"
    assert second.job_id == first.job_id
    job_dirs = list((tmp_path / str(agent_id) / "memory" / ".staging" / "t3_jobs").glob("*"))
    assert len(job_dirs) == 1


def test_platform_gate_holds_patch_with_legacy_target(tmp_path: Path) -> None:
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch

    agent_id = uuid.uuid4()
    patch = """<t3_consolidation_patch id="t3p_bad" schema_version="t3.consolidation_patch.v1">
  <source_packages><source_package ref="t2://session/s1/segment/seg-1" status="reviewed"/></source_packages>
  <target_files><target_file path="memory/feedback.md"/></target_files>
  <target_view_labels><target_view>user</target_view></target_view_labels>
  <proposed_changes><append_block target="memory/feedback.md" block_id="bad"><block_content><![CDATA[<t3_user_memory id="bad"/>]]></block_content></append_block></proposed_changes>
  <evidence><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></evidence>
</t3_consolidation_patch>"""

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-bad-target",
        revised_patch_md=patch,
        review_md=_accepted_review(),
    )

    assert result.status == "held"
    assert any("non-canonical target" in issue for issue in result.issues)
    assert not (tmp_path / str(agent_id) / "memory" / "feedback.md").exists()


def test_platform_gate_returns_rebase_required_when_base_revision_changed(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    target = mem_dir / "t3" / "user.md"
    old_sha = file_sha256(target)
    target.write_text(target.read_text(encoding="utf-8") + "\n<t3_user_memory id=\"usr_existing\"/>\n", encoding="utf-8")
    patch = f"""<t3_consolidation_patch id="t3p_rebase" schema_version="t3.consolidation_patch.v1">
  <base_revisions><base_revision path="memory/t3/user.md" sha256="{old_sha}"/></base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1" status="reviewed"/></source_packages>
  <target_files><target_file path="memory/t3/user.md"/></target_files>
  <target_view_labels><target_view>user</target_view><consolidation_mode>create</consolidation_mode><source_coverage>single_session</source_coverage><cue_strength>0.80</cue_strength><stability>stable</stability><behavior_impact>response_style</behavior_impact><prompt_priority>p1_dynamic</prompt_priority></target_view_labels>
  <proposed_changes><append_block target="memory/t3/user.md" block_id="usr_new"><block_content><![CDATA[<t3_user_memory id="usr_new" status="active"><claim>用户偏好先讨论。</claim></t3_user_memory>]]></block_content></append_block></proposed_changes>
  <evidence><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></evidence>
</t3_consolidation_patch>"""

    result = apply_t3_consolidation_patch(
        agent_id=agent_id,
        data_root=tmp_path,
        job_id="job-rebase",
        revised_patch_md=patch,
        review_md=_accepted_review(),
    )

    assert result.status == "rebase_required"
    assert (mem_dir / ".staging" / "t3_jobs" / "job-rebase" / "conflict_bundle.json").exists()
