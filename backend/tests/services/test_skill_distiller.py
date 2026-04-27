from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_build_workflow_signature_filters_noise_and_consecutive_duplicates() -> None:
    from app.services.skill_distiller import _build_workflow_signature

    signature = _build_workflow_signature(
        [
            "read_file",
            "web_search",
            "web_search",
            "get_current_time",
            "web_fetch",
            "web_fetch",
            "write_file",
        ]
    )

    assert signature.normalized_tools == ("web_search", "web_fetch", "write_file")
    assert signature.workflow_signature == "web_search -> web_fetch -> write_file"
    assert signature.blocker is None


def test_build_workflow_signature_blocks_external_action_workflows() -> None:
    from app.services.skill_distiller import _build_workflow_signature

    signature = _build_workflow_signature(["web_search", "send_email", "write_file"])

    assert signature.workflow_signature is None
    assert signature.blocker == "external_action_workflow"


def test_validate_skill_draft_rejects_unknown_tools_and_sensitive_content(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _render_skill_markdown
    from app.services.skill_distiller import DistilledSkillDraft, validate_distilled_skill

    draft = DistilledSkillDraft(
        decision="promote",
        confidence=0.91,
        name="Research Sprint",
        description="Collect sources and send them to user_id=abc123.",
        instructions_markdown="First do the work.\nThen use private_token=secret-value.\n",
        declared_tools=("web_search", "tool_that_does_not_exist"),
        declared_packs=("web_pack",),
        reason="Repeated successful workflow.",
    )

    errors = validate_distilled_skill(
        workspace=tmp_path,
        draft=draft,
        rendered_markdown=_render_skill_markdown(
            name=draft.name,
            description=draft.description,
            instructions=draft.instructions_markdown,
            declared_tools=draft.declared_tools,
            declared_packs=draft.declared_packs,
        ),
    )

    assert any("unknown tool" in error for error in errors)
    assert any("sensitive" in error for error in errors)


def test_resolve_existing_skill_as_patch_recommendation(tmp_path: Path) -> None:
    from app.services.agent_tool_domains.workspace import _save_skill
    from app.services.skill_distiller import DistilledSkillDraft, resolve_existing_skill_conflict

    workspace = tmp_path / "agent"
    workspace.mkdir(parents=True)
    _save_skill(
        workspace,
        name="Web Research",
        description="Run repeated web research workflows.",
        instructions="Search first, then fetch, then summarize.",
        declared_tools=("web_search", "web_fetch"),
        declared_packs=("web_pack",),
    )

    resolution = resolve_existing_skill_conflict(
        workspace=workspace,
        draft=DistilledSkillDraft(
            decision="promote",
            confidence=0.93,
            name="Web Research",
            description="A better web research loop.",
            instructions_markdown="Search, fetch, and summarize findings.",
            declared_tools=("web_search", "web_fetch"),
            declared_packs=("web_pack",),
            reason="Repeated workflow matched an existing skill.",
        ),
    )

    assert resolution.final_decision == "patch"
    assert resolution.existing_skill_name == "Web Research"


@pytest.mark.asyncio
async def test_run_skill_distillation_cycle_promotes_high_confidence_candidate(monkeypatch, tmp_path: Path) -> None:
    from app.services.skill_distiller import DistilledSkillDraft, SessionWorkflowEvidence, run_skill_distillation_cycle

    workspace = tmp_path / "agent"
    workspace.mkdir(parents=True)

    async def fake_load_internal_session_evidence(*, agent_id, since_days, state, current_session_id):
        del agent_id, since_days, state, current_session_id
        return [
            SessionWorkflowEvidence(
                session_id="s-1",
                source="heartbeat",
                occurred_at="2026-04-01T10:00:00Z",
                status="success",
                used_skill=False,
                summary="Searched and summarized the market.",
                assistant_reply="[OUTCOME:action_taken] [SCORE:7] wrote market summary",
                tool_names=("web_search", "web_fetch", "write_file"),
            ),
            SessionWorkflowEvidence(
                session_id="s-2",
                source="trigger",
                occurred_at="2026-04-04T10:00:00Z",
                status="success",
                used_skill=False,
                summary="Repeated the same market research workflow.",
                assistant_reply="[OUTCOME:action_taken] [SCORE:7] wrote market summary",
                tool_names=("web_search", "web_fetch", "write_file"),
            ),
            SessionWorkflowEvidence(
                session_id="s-3",
                source="heartbeat",
                occurred_at="2026-04-08T10:00:00Z",
                status="success",
                used_skill=False,
                summary="Third repeat of the market research workflow.",
                assistant_reply="[OUTCOME:action_taken] [SCORE:8] wrote market summary",
                tool_names=("web_search", "web_fetch", "write_file"),
            ),
        ]

    async def fake_draft_skill(**kwargs):
        del kwargs
        return DistilledSkillDraft(
            decision="promote",
            confidence=0.92,
            name="Market Research Loop",
            description="Run the internal market research workflow and save findings.",
            instructions_markdown="1. Search reputable sources.\n2. Fetch the best pages.\n3. Write a concise summary file.\n",
            declared_tools=("web_search", "web_fetch", "write_file"),
            declared_packs=("web_pack",),
            reason="Repeated successful internal workflow.",
        )

    monkeypatch.setattr(
        "app.services.skill_distiller._load_internal_session_evidence",
        fake_load_internal_session_evidence,
    )
    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", fake_draft_skill)

    result = await run_skill_distillation_cycle(
        agent_id=uuid4(),
        workspace=workspace,
        tenant_id=None,
        runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test-key", base_url=None),
    )

    skill_path = workspace / "skills" / "market-research-loop" / "SKILL.md"
    state_path = workspace / "evolution" / "skill_distiller_state.json"
    review = (workspace / "evolution" / "skill_review.md").read_text(encoding="utf-8")

    assert result["status"] == "promoted"
    assert skill_path.exists()
    assert state_path.exists()
    assert "Market Research Loop" in review
    assert "[promoted]" in review


# ──────────────────────────────────────────────────────────────────────────
# PR-20: Skill distiller system prompt structural invariants
#
# _draft_skill_with_llm() builds an inline system prompt that drives the
# promote/patch/defer/reject decision. We lock down best-practice structure:
# XML tags, pipeline context, decision matrix, anti-patterns for
# time-sensitive content, and JSON output contract.
# ──────────────────────────────────────────────────────────────────────────

def _extract_system_prompt_literal() -> str:
    """Read the system_prompt literal out of _draft_skill_with_llm source."""
    from app.services import skill_distiller

    source = inspect.getsource(skill_distiller._draft_skill_with_llm)
    # The system_prompt is built as a parenthesized string-concatenation block.
    # Find the 'system_prompt = (' start and collect until the closing paren.
    start_marker = "system_prompt = ("
    start = source.index(start_marker) + len(start_marker)
    depth = 1
    i = start
    while i < len(source) and depth > 0:
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
        i += 1
    return source[start : i - 1]


class TestSkillDistillerPromptStructure:
    def test_xml_tags_present(self) -> None:
        prompt = _extract_system_prompt_literal()
        for tag in [
            "<role>",
            "</role>",
            "<pipeline_context>",
            "<decision_matrix>",
            "<anti_patterns>",
            "<output_contract>",
        ]:
            assert tag in prompt, f"missing tag: {tag}"

    def test_role_is_conservative(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "conservative skill distiller" in prompt

    def test_pipeline_context_names_four_decisions(self) -> None:
        prompt = _extract_system_prompt_literal()
        for decision in ["promote", "patch", "defer", "reject"]:
            assert decision in prompt, f"missing decision: {decision}"

    def test_pipeline_context_warns_json_parsing(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "parses your JSON directly" in prompt
        assert "markdown fences" in prompt


class TestSkillDistillerDecisionMatrix:
    def test_promote_requires_high_confidence(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "Confidence ≥ 0.75" in prompt or "0.75" in prompt

    def test_defer_is_default_when_uncertain(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "Default if uncertain" in prompt

    def test_reject_for_time_sensitive_or_session_specific(self) -> None:
        prompt = _extract_system_prompt_literal()
        lowered = prompt.lower()
        assert "time-sensitive" in lowered
        assert "session-specific" in lowered


class TestSkillDistillerAntiPatterns:
    def test_rejects_specific_dates(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "Specific dates" in prompt
        assert "2026" in prompt or "yesterday" in prompt.lower()

    def test_rejects_session_bound_ids(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "message_id" in prompt
        assert "task_id" in prompt
        assert "UUIDs" in prompt

    def test_rejects_user_specific_content(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "User-specific names" in prompt or "email addresses" in prompt

    def test_rejects_credentials(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "Credentials" in prompt or "tokens" in prompt.lower()


class TestSkillDistillerOutputContract:
    def test_raw_json_no_fences(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "Return raw JSON only" in prompt
        assert "No markdown fences" in prompt
        assert "No prose outside" in prompt

    def test_all_keys_required_with_empty_defaults(self) -> None:
        prompt = _extract_system_prompt_literal()
        assert "All keys must be present" in prompt
        assert "empty strings" in prompt or "empty arrays" in prompt
