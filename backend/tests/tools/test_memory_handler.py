from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


async def _safe_memory_threat_classifier(**_kwargs):
    from app.memory.write_gate import MemoryThreatAssessment

    return MemoryThreatAssessment(
        rejected=False,
        labels=[],
        method="llm_classifier",
        confidence=0.99,
        rationale="No memory-write threat detected.",
    )


def _write_t3_ready_package(root: Path, agent_id: uuid.UUID) -> Path:
    package_dir = root / str(agent_id) / "memory" / "sessions" / "s1" / "segments" / "seg-1"
    package_dir.mkdir(parents=True, exist_ok=True)
    source_ref = "t0://session/s1/segment/seg-1#seq=1..2"
    (package_dir / "summary.md").write_text(
        f'<t2_summary id="sum-1" status="closed"><summary>User prefers concise answers.</summary><source_refs><source_ref uri="{source_ref}"/></source_refs></t2_summary>',
        encoding="utf-8",
    )
    (package_dir / "labels.md").write_text(
        f'<t2_labels id="lbl-1"><package_status>closed</package_status><source_refs><source_ref uri="{source_ref}"/></source_refs></t2_labels>',
        encoding="utf-8",
    )
    (package_dir / "review.md").write_text(
        f'<t2_review id="rev-1"><decision>approved</decision><allowed_next>t3_intake</allowed_next><review_rubric schema_version="t2.review_rubric.v1"><score name="summary_fidelity" value="0.95"/><score name="source_ref_coverage" value="0.95"/><score name="label_alignment" value="0.90"/><score name="safety_scope" value="0.95"/><score name="package_closure" value="0.90"/><review_score>0.95</review_score></review_rubric><source_refs><source_ref uri="{source_ref}"/></source_refs></t2_review>',
        encoding="utf-8",
    )
    (package_dir / "manifest.json").write_text(
        '{"schema_version":"t2.segment-package.manifest.v1","package_status":"reviewed","source_refs":["t0://session/s1/segment/seg-1#seq=1..2"]}\n',
        encoding="utf-8",
    )
    return package_dir


def _accepted_t3_review() -> str:
    return """<memory_gate_review id="r1" schema_version="t3.review.v1">
  <decision>accept</decision>
  <memory_gate_rubric schema_version="memory_gate_rubric.v1">
    <score name="evidence_strength" value="4"><rationale>Source-backed.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs></score>
    <score name="scope_clarity" value="4"><rationale>Narrow preference.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#labels</source_ref></source_refs></score>
    <score name="stability" value="3"><rationale>Stable enough.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#review</source_ref></source_refs></score>
    <score name="future_utility" value="4"><rationale>Useful for response style.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs></score>
    <score name="conflict_safety" value="4"><rationale>No conflict.</rationale><source_refs><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></source_refs></score>
    <decision>accept_new</decision>
    <decision_rationale>Commit.</decision_rationale>
    <required_followup>commit</required_followup>
  </memory_gate_rubric>
</memory_gate_review>"""


@pytest.mark.asyncio
async def test_save_memory_writes_explicit_overlay_not_accepted_t3(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr(
            "app.memory.write_gate.classify_memory_write_threat_with_llm",
            _safe_memory_threat_classifier,
        )
        result = await save_memory(agent_id, {"content": "User prefers concise answers", "category": "feedback"})

    memory_dir = tmp_path / str(agent_id) / "memory"
    overlay_index = memory_dir / "explicit" / "MEMORY.md"

    assert "Saved to explicit memory overlay [feedback]" in result
    assert overlay_index.exists()
    assert "User prefers concise answers" in overlay_index.read_text(encoding="utf-8")
    assert not (memory_dir / "feedback.md").exists()
    assert not (memory_dir / "t3" / "user.md").exists()


@pytest.mark.asyncio
async def test_save_memory_holds_when_semantic_reviewer_is_unavailable(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(agent_id, {"content": "User prefers concise answers", "category": "feedback"})

    assert result.startswith("[Held]")
    assert "semantic_review_unavailable" in result
    assert not (tmp_path / str(agent_id) / "memory" / "explicit" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_save_memory_result_preserves_committed_decisive_tail(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()
    tail = "EXPLICIT_MEMORY_COMMITTED_DECISIVE_TAIL"
    content = "Permanent user preference with complete rationale: " + ("context " * 80) + tail

    async def write_overlay(*_args, **_kwargs):
        return SimpleNamespace(
            status="saved",
            category="feedback",
            target_hint="user",
            content=content,
            entry_id="memory-tail",
            sensitivity="PL1_public",
            reason="",
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr("app.memory.explicit_overlay.write_explicit_memory_overlay", write_overlay)
        result = await save_memory(agent_id, {"content": content, "category": "feedback"})

    assert tail in result


@pytest.mark.asyncio
async def test_save_memory_never_uses_regex_to_reject_episodic_semantics(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()
    content = "巡检没有更新，但用户明确要求长期记住该事实"
    captured: dict = {}

    async def write_overlay(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="active",
            category="reference",
            target_hint="capabilities",
            content=content,
            entry_id="explicit-episodic-review",
            sensitivity="PL1_public",
            reason="",
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr("app.memory.explicit_overlay.write_explicit_memory_overlay", write_overlay)
        result = await save_memory(agent_id, {"content": content, "category": "reference"})

    assert captured["content"] == content
    assert result.startswith("Saved to explicit memory overlay")
    assert "[Skipped]" not in result


@pytest.mark.asyncio
async def test_save_memory_rejects_pl4_credentials_before_overlay_write(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {"content": "Production API key is sk-live-abcdef1234567890abcdef", "category": "reference"},
        )

    assert result.startswith("[Rejected]")
    assert not (tmp_path / str(agent_id) / "memory" / "explicit" / "MEMORY.md").exists()


@pytest.mark.asyncio
async def test_search_and_load_memory_include_explicit_overlay(tmp_path: Path) -> None:
    from app.tools.handlers.memory import load_memory, save_memory, search_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr(
            "app.memory.write_gate.classify_memory_write_threat_with_llm",
            _safe_memory_threat_classifier,
        )
        await save_memory(agent_id, {"content": "Use snake_case for Python variable names", "category": "feedback"})
        result = await search_memory(agent_id, {"query": "snake_case", "scope": "facts"})

        assert "## Explicit Memory Overlay" in result
        assert "snake_case" in result
        match = re.search(r"id=([a-zA-Z0-9_-]+)", result)
        assert match

        loaded = load_memory(agent_id, {"ids": [match.group(1)]})

    assert "## Loaded Explicit Memory Overlay" in loaded
    assert "Use snake_case for Python variable names" in loaded
    assert "source=memory/explicit/entries/" in loaded


@pytest.mark.asyncio
async def test_search_memory_has_no_hidden_default_result_cap(tmp_path: Path) -> None:
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()
    decisive_tail = "MEMORY_SEARCH_LAST_AUTHORIZED_CANDIDATE"

    def fake_search_overlay(_root, _agent_id, _query, *, limit):
        assert limit is None
        return [
            {
                "id": f"memory-{index}",
                "category": "feedback",
                "content": decisive_tail if index == 24 else f"authorized candidate {index}",
                "preview": f"preview {index}",
                "target_hint": "user",
                "sensitivity": "PL1_public",
            }
            for index in range(25)
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr("app.memory.explicit_overlay.search_explicit_overlay_entries", fake_search_overlay)
        mp.setattr("app.memory.plane_read.search_plane_facts", lambda *_args, **_kwargs: [])
        result = await search_memory(agent_id, {"query": "candidate", "scope": "facts"})

    assert decisive_tail in result
    assert "memory-24" in result


@pytest.mark.asyncio
async def test_search_memory_returns_complete_recalled_session_transcript(tmp_path: Path) -> None:
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()
    decisive_tail = "DECISIVE_SESSION_TRANSCRIPT_TAIL"

    async def fake_search_session_history(*_args, **_kwargs):
        return [
            {
                "started_at": "2026-07-13T00:00:00Z",
                "source": "web",
                "headline": "Relevant prior session",
                "focused_recap": "Short recap without the decisive tail.",
                "summary": "Short summary without the decisive tail.",
                "evidence_lines": ["User: initial evidence"],
                "transcript_window": "User: nearby context only",
                "context_snippets": ["nearby context only"],
                "transcript": "User: initial evidence\nAssistant: complete reasoning\n" + decisive_tail,
            }
        ]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr("app.tools.handlers.memory.search_session_history", fake_search_session_history)
        result = await search_memory(agent_id, {"query": "relevant", "scope": "sessions"})

    assert "Complete transcript:" in result
    assert decisive_tail in result


@pytest.mark.asyncio
async def test_update_memory_supersedes_explicit_overlay_entry(tmp_path: Path) -> None:
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.tools.handlers.memory import save_memory, update_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr(
            "app.memory.write_gate.classify_memory_write_threat_with_llm",
            _safe_memory_threat_classifier,
        )
        await save_memory(agent_id, {"content": "User prefers short replies", "category": "feedback"})
        old_entry = load_explicit_overlay_entries(tmp_path, agent_id)[0]

        result = await update_memory(
            agent_id,
            {
                "memory_id": old_entry.entry_id,
                "content": "User prefers short replies, but include concrete examples when useful",
                "category": "feedback",
            },
        )
        entries = load_explicit_overlay_entries(tmp_path, agent_id)

    assert result.startswith(f"Updated explicit memory {old_entry.entry_id} ->")
    assert {entry.status for entry in entries} == {"retired", "active"}
    assert any("include concrete examples" in entry.content and entry.status == "active" for entry in entries)


@pytest.mark.asyncio
async def test_retire_memory_deactivates_explicit_overlay_entry(tmp_path: Path) -> None:
    from app.memory.explicit_overlay import load_explicit_overlay_entries
    from app.tools.handlers.memory import retire_memory, save_memory, search_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr(
            "app.memory.write_gate.classify_memory_write_threat_with_llm",
            _safe_memory_threat_classifier,
        )
        await save_memory(
            agent_id,
            {"content": "Temporary preference for blue buttons during prototype review", "category": "feedback"},
        )
        entry = load_explicit_overlay_entries(tmp_path, agent_id)[0]
        result = await retire_memory(agent_id, {"memory_id": entry.entry_id, "reason": "obsolete"})
        search_result = await search_memory(agent_id, {"query": "blue buttons", "scope": "facts"})
        entries = load_explicit_overlay_entries(tmp_path, agent_id)

    assert result == f"Retired explicit memory {entry.entry_id}: obsolete"
    assert entries[0].status == "retired"
    assert "No memory found" in search_result


def test_load_memory_reads_profile_plane_entries(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.tools.handlers.memory import load_memory

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    (mem_dir / "self" / "self.md").write_text(
        "## 能力\n\n### 部署经验 — 熟练\n<!-- id: cap-deploy -->\npublic deployment note\n- 证据: t2-a1b2\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = load_memory(agent_id, {"ids": ["cap-deploy", "cap-missing"]})

    assert "public deployment note" in result
    assert "cap-missing" not in result or "No memory" not in result


@pytest.mark.asyncio
async def test_accepted_t3_update_and_retire_require_t3_patch(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.tools.handlers.memory import retire_memory, update_memory

    agent_id = uuid.uuid4()
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    (mem_dir / "profiles" / "owner.md").write_text(
        "## 偏好\n\n### 回复风格 — 已确认\n<!-- id: u-style -->\nUser prefers concise answers.\n- 证据: t2-a1b2\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        update_result = await update_memory(
            agent_id, {"memory_id": "u-style", "content": "User prefers detailed answers"}
        )
        retire_result = await retire_memory(agent_id, {"memory_id": "u-style", "reason": "obsolete"})

    assert update_result.startswith("[Needs T3 Patch]")
    assert "Platform Gate" in update_result
    assert retire_result.startswith("[Needs T3 Patch]")
    assert "Platform Gate" in retire_result


@pytest.mark.asyncio
async def test_search_memory_suppresses_sensitive_wiki_page_even_when_preview_is_safe(tmp_path: Path) -> None:
    from app.tools.handlers.memory import search_memory

    agent_id = uuid.uuid4()
    wiki_dir = tmp_path / str(agent_id) / "memory" / "wiki"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "comp-plan.md").write_text(
        "---\ntitle: Comp Plan\ntype: concept\nstatus: active\n---\n\n"
        "## Current Claim\n\n"
        f"{'public context ' * 20}\n\nsalary planning is confidential\n",
        encoding="utf-8",
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await search_memory(agent_id, {"query": "public context", "scope": "facts"})

    assert "Comp Plan" not in result
    assert "salary planning is confidential" not in result


def test_submit_t3_revised_patch_after_existing_review_requires_fresh_review(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_consolidation import build_t3_consolidation_batch
    from app.memory.t3_platform_gate import file_sha256
    from app.tools.handlers.memory import submit_t3_memory_gate_review, submit_t3_revised_patch

    agent_id = uuid.uuid4()
    package_dir = _write_t3_ready_package(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    result = build_t3_consolidation_batch(
        agent_id=agent_id,
        data_root=tmp_path,
        package_dirs=[package_dir],
        job_id="job-tool-commit",
    )
    target = mem_dir / "profiles" / "owner.md"
    base_sha = file_sha256(target)
    patch = f"""<t3_consolidation_patch id="p1" schema_version="t3.consolidation_patch.v1">
  <base_revisions><base_revision path="memory/profiles/owner.md" sha256="{base_sha}"/></base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1" status="reviewed"/></source_packages>
  <target_files><target_file path="memory/profiles/owner.md"/></target_files>
  <target_view_labels><target_view>profiles</target_view><consolidation_mode>create</consolidation_mode><source_coverage>single_session</source_coverage><cue_strength>0.90</cue_strength><stability>stable</stability><behavior_impact>response_style</behavior_impact><prompt_priority>p1_dynamic</prompt_priority></target_view_labels>
  <proposed_changes><upsert_entry target="memory/profiles/owner.md" entry_id="usr_concise" section="偏好"><entry_content><![CDATA[### 回复风格 — 已确认
<!-- id: usr_concise -->
User prefers concise answers.
- 证据: t2-a1b2]]></entry_content></upsert_entry></proposed_changes>
  <evidence><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></evidence>
</t3_consolidation_patch>"""

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        review_result = submit_t3_memory_gate_review(
            agent_id, {"job_id": result.job_id, "content": _accepted_t3_review()}
        )
        patch_result = submit_t3_revised_patch(agent_id, {"job_id": result.job_id, "content": patch})

    assert "Submitted T3 job artifact" in review_result
    assert "waiting for fresh Memory Gate review" in patch_result
    assert not target.exists() or "usr_concise" not in target.read_text(encoding="utf-8")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        final_review_result = submit_t3_memory_gate_review(
            agent_id,
            {"job_id": result.job_id, "content": _accepted_t3_review()},
        )

    assert "Platform Gate committed" in final_review_result
    assert "usr_concise" in target.read_text(encoding="utf-8")


def test_submit_t3_memory_gate_review_triggers_platform_gate_when_patch_ready(tmp_path: Path) -> None:
    from app.memory.md_store import ensure_t3_layout
    from app.memory.t3_consolidation import build_t3_consolidation_batch
    from app.memory.t3_platform_gate import file_sha256
    from app.tools.handlers.memory import submit_t3_memory_gate_review, submit_t3_revised_patch

    agent_id = uuid.uuid4()
    package_dir = _write_t3_ready_package(tmp_path, agent_id)
    mem_dir = ensure_t3_layout(tmp_path, agent_id)
    result = build_t3_consolidation_batch(
        agent_id=agent_id,
        data_root=tmp_path,
        package_dirs=[package_dir],
        job_id="job-tool-review-last",
    )
    target = mem_dir / "self" / "self.md"
    base_sha = file_sha256(target)
    patch = f"""<t3_consolidation_patch id="p2" schema_version="t3.consolidation_patch.v1">
  <base_revisions><base_revision path="memory/self/self.md" sha256="{base_sha}"/></base_revisions>
  <source_packages><source_package ref="t2://session/s1/segment/seg-1" status="reviewed"/></source_packages>
  <target_files><target_file path="memory/self/self.md"/></target_files>
  <target_view_labels><target_view>self</target_view><consolidation_mode>create</consolidation_mode><source_coverage>single_session</source_coverage><cue_strength>0.90</cue_strength><stability>stable</stability><behavior_impact>tool_policy</behavior_impact><prompt_priority>p1_dynamic</prompt_priority></target_view_labels>
  <proposed_changes><upsert_entry target="memory/self/self.md" entry_id="wrk_discuss_first" section="方法"><entry_content><![CDATA[### 架构改造先讨论 — 已确认
<!-- id: wrk_discuss_first -->
Discuss memory architecture changes before implementation.
- 证据: t2-a1b2]]></entry_content></upsert_entry></proposed_changes>
  <evidence><source_ref>t2://session/s1/segment/seg-1#summary</source_ref></evidence>
</t3_consolidation_patch>"""

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        patch_result = submit_t3_revised_patch(agent_id, {"job_id": result.job_id, "content": patch})
        review_result = submit_t3_memory_gate_review(
            agent_id, {"job_id": result.job_id, "content": _accepted_t3_review()}
        )

    assert "Submitted T3 job artifact" in patch_result
    assert "Platform Gate committed" in review_result
    assert "wrk_discuss_first" in target.read_text(encoding="utf-8")
