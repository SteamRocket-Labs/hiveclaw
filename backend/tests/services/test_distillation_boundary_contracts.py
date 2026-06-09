from __future__ import annotations

import inspect
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def test_extractor_prompt_keeps_autonomy_state_out_of_t2_memory() -> None:
    from app.services.extract_agent import EXTRACT_PROMPT

    prompt = _normalized(EXTRACT_PROMPT)

    assert "trigger is wake policy" in prompt
    assert "do not extract trigger schedules" in prompt
    assert "do not extract runtime task" in prompt
    # The retired objective/focus-projection concepts must not be reintroduced.
    assert "objective ledger" not in prompt
    assert "focus.md" not in prompt
    assert "workspace artifacts" in prompt


def test_extractor_filters_operational_autonomy_instance_state() -> None:
    from app.services.extract_agent import _parse_extractions, _pattern_extract

    messages = [
        {
            "role": "assistant",
            "content": (
                "Must continue objective_id=8d3f5b9e-0000-4000-8000-000000000001 "
                "with trigger_id=8d3f5b9e-0000-4000-8000-000000000002 and "
                "runtime_task_id=8d3f5b9e-0000-4000-8000-000000000003."
            ),
        }
    ]

    assert _pattern_extract(messages) == []

    parsed = _parse_extractions(
        "\n".join(
            [
                "[project] trigger_id=8d3f5b9e-0000-4000-8000-000000000002 is currently running",
                "[feedback] User confirmed concise output is the preferred reporting style",
            ]
        )
    )

    # The operational-autonomy line must be filtered out; only the feedback
    # survives. Feedback entries now carry decision-calibration metadata
    # (reaction/polarity/source), so assert the autonomy-filtering contract this
    # test exists to protect rather than the exact feedback dict shape.
    assert len(parsed) == 1
    assert parsed[0]["category"] == "feedback"
    assert parsed[0]["content"] == "User confirmed concise output is the preferred reporting style"


def test_conversation_summarizer_preserves_autonomy_run_state_without_distilling_it() -> None:
    from app.services.conversation_summarizer import _SUMMARIZE_SYSTEM_PROMPT

    prompt = _normalized(_SUMMARIZE_SYSTEM_PROMPT)

    assert "trigger is wake policy" in prompt
    assert "preserve runtime task" in prompt
    assert "preserve attempt evidence" in prompt
    assert "do not rewrite autonomous run state as long-term memory" in prompt
    assert "objective ledger" not in prompt
    assert "focus.md" not in prompt


def test_dream_prompts_do_not_promote_operational_autonomy_state_to_soul() -> None:
    from app.services.auto_dream import _AUTO_DREAM_SYSTEM_PROMPT, _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE

    dream_template = (PROJECT_ROOT / "backend" / "app" / "templates" / "DREAM.md").read_text(encoding="utf-8")
    combined = _normalized(
        "\n".join(
            [
                _AUTO_DREAM_SYSTEM_PROMPT,
                _DREAM_CONSOLIDATION_USER_PROMPT_TEMPLATE,
                dream_template,
            ]
        )
    )

    assert "trigger is wake policy" in combined
    assert "do not promote wake policies" in combined
    assert "do not promote runtime task" in combined
    assert "workspace artifacts" in combined
    # The retired objective/focus-projection concepts must not be reintroduced.
    assert "objective ledger" not in combined
    assert "focus.md projection" not in combined


def test_dream_template_preserves_t2_retention_provenance() -> None:
    dream_template = _normalized((PROJECT_ROOT / "backend" / "app" / "templates" / "DREAM.md").read_text(encoding="utf-8"))

    assert "status=absorbed" in dream_template
    assert "memory/archive.md" in dream_template
    assert "original t2 line" in dream_template
    assert "entry id" in dream_template
    assert "recoverable" in dream_template
    assert "do not archive referenced t2" not in dream_template


def test_skill_distiller_prompt_rejects_goal_and_wake_policy_as_skills() -> None:
    from app.services import skill_distiller

    source = inspect.getsource(skill_distiller._draft_skill_with_llm)
    prompt = _normalized(source)

    assert "trigger is wake policy" in prompt
    assert "do not convert wake policies" in prompt
    # The retired objective/focus-projection concepts must not be reintroduced.
    assert "objective ledger" not in prompt
    assert "focus.md" not in prompt
