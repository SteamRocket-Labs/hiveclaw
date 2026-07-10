from __future__ import annotations

import inspect
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


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


def test_dream_template_requires_source_refs_without_owning_t2_retention() -> None:
    dream_template = _normalized((PROJECT_ROOT / "backend" / "app" / "templates" / "DREAM.md").read_text(encoding="utf-8"))

    assert "source references are precise enough" in dream_template
    assert "soul_candidate" in dream_template
    assert "source_refs" in dream_template
    assert "soul.md.next" in dream_template
    assert "the next t3 consolidation batch" in dream_template
    assert "status=absorbed" not in dream_template
    assert "archive referenced t2" not in dream_template


def test_dream_template_does_not_instruct_direct_writes_to_platform_managed_evolution_files() -> None:
    dream_template = (PROJECT_ROOT / "backend" / "app" / "templates" / "DREAM.md").read_text(encoding="utf-8")

    forbidden_phrases = [
        "Log this dream cycle to `evolution/",
        "Append to `evolution/",
        "Update `evolution/",
        "write_file` / `edit_file` under `evolution/",
    ]
    for phrase in forbidden_phrases:
        assert phrase not in dream_template
    assert "Do not write `memory/t3/**` directly" in dream_template


def test_skill_distiller_prompt_rejects_goal_and_wake_policy_as_skills() -> None:
    from app.services import skill_distiller

    source = inspect.getsource(skill_distiller._draft_skill_with_llm)
    prompt = _normalized(source)

    assert "trigger is wake policy" in prompt
    assert "do not convert wake policies" in prompt
    # The retired objective/focus-projection concepts must not be reintroduced.
    assert "objective ledger" not in prompt
    assert "focus.md" not in prompt
