from __future__ import annotations

def _assemble_test_prompt(*, agent_context: str, dynamic_suffix: str, context_window_tokens: int | None = None) -> str:
    from app.runtime.prompt_builder import assemble_runtime_prompt, build_frozen_prompt_prefix

    return assemble_runtime_prompt(
        build_frozen_prompt_prefix(agent_context=agent_context),
        dynamic_suffix,
        context_window_tokens=context_window_tokens,
    )


def test_prompt_builder_merges_prompt_sections_from_runtime_trunk():
    from app.runtime.context_budget import compute_context_budget
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    messages = [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "latest market research status"},
    ]
    prompt = _assemble_test_prompt(
        agent_context="BASE_PROMPT",
        dynamic_suffix=build_dynamic_prompt_suffix(
            memory_snapshot="MEMORY",
            retrieval_context="## Knowledge\nKNOWLEDGE",
            system_prompt_suffix="SUFFIX",
            latest_user_query="latest market research status",
            user_name="Rocky",
            channel="task",
            agent_name="Ops Agent",
            budget_profile=compute_context_budget(
                context_window_tokens=None,
                query="latest market research status",
                messages=messages,
                active_pack_count=0,
            ),
        ),
    )

    assert "BASE_PROMPT" in prompt
    assert "MEMORY" in prompt
    assert "## System" in prompt
    assert "## Doing Tasks" in prompt
    assert "KNOWLEDGE" in prompt
    assert "SUFFIX" in prompt
    assert "__PROMPT_DYNAMIC_BOUNDARY__" in prompt
    assert "## Task Playbook" in prompt
    assert "verify sources" in prompt.lower()


def test_prompt_builder_skips_empty_sections():
    prompt = _assemble_test_prompt(
        agent_context="BASE",
        dynamic_suffix="",
    )

    # With sections injected, prompt starts with BASE but includes System/Tasks/Tools
    assert prompt.startswith("BASE")
    assert "## System" in prompt


def test_prompt_builder_includes_active_packs_section():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    prompt = _assemble_test_prompt(
        agent_context="BASE",
        dynamic_suffix=build_dynamic_prompt_suffix(
            active_packs=[{
                "name": "web_pack",
                "summary": "网页搜索与抓取能力",
                "tools": ["web_search", "firecrawl_fetch"],
            }]
        ),
    )

    assert "## Active Capability Packs" in prompt
    assert "web_pack" in prompt
    assert "web_search, firecrawl_fetch" in prompt


class TestModelAwareBudget:
    """assemble_runtime_prompt scales budget with context window."""

    def test_default_budget_when_no_context_window(self) -> None:
        from app.runtime.context_budget import compute_system_prompt_budget
        budget = compute_system_prompt_budget(None)
        assert budget == 60000

    def test_default_budget_when_zero(self) -> None:
        from app.runtime.context_budget import compute_system_prompt_budget
        assert compute_system_prompt_budget(0) == 60000

    def test_small_model_gets_floor_budget(self) -> None:
        from app.runtime.context_budget import compute_system_prompt_budget
        # 8K context → 8000 * 0.20 * 3.5 = 5600 → clamped to floor 15000
        budget = compute_system_prompt_budget(8000)
        assert budget == 15000

    def test_large_model_gets_scaled_budget(self) -> None:
        from app.runtime.context_budget import compute_system_prompt_budget
        # 200K context → 200000 * 0.20 * 3.5 = 140000 (within 180K ceiling)
        budget = compute_system_prompt_budget(200000)
        assert budget == 140000

    def test_medium_model_proportional(self) -> None:
        from app.runtime.context_budget import compute_system_prompt_budget
        # 64K context → 64000 * 0.20 * 3.5 = 44800
        budget = compute_system_prompt_budget(64000)
        assert budget == 44800

    def test_assemble_trims_frozen_when_over_budget(self) -> None:
        from app.runtime.prompt_builder import PROMPT_CACHE_BOUNDARY, assemble_runtime_prompt
        frozen = "A" * 20000
        dynamic = "B" * 100
        # 8K model → budget 15000 → 20000 + 100 > 15000 → should trim
        result = assemble_runtime_prompt(frozen, dynamic, context_window_tokens=8000)
        assert len(result) <= 15200  # budget + truncation notice
        assert "B" * 100 in result  # dynamic preserved
        assert PROMPT_CACHE_BOUNDARY.strip() in result  # cache split preserved even when trimmed

    def test_assemble_no_trim_when_within_budget(self) -> None:
        from app.runtime.prompt_builder import assemble_runtime_prompt
        frozen = "A" * 1000
        dynamic = "B" * 100
        result = assemble_runtime_prompt(frozen, dynamic, context_window_tokens=200000)
        assert "truncated" not in result


def test_dynamic_suffix_trims_large_retrieval_but_keeps_suffix():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    retrieval = "\n".join(f"- item {i} {'x' * 80}" for i in range(80))
    suffix = build_dynamic_prompt_suffix(
        active_packs=[],
        retrieval_context=retrieval,
        system_prompt_suffix="FINAL_SUFFIX",
    )

    assert "FINAL_SUFFIX" in suffix
    assert len(suffix) < 3200


def test_dynamic_suffix_preserves_presectioned_retrieval_context_without_rewrapping():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    retrieval = (
        "## Runtime Context\n"
        "RUNTIME_HINTS\n\n"
        "## Relevant Memory Recall\n"
        "MEMORY_RECALL\n\n"
        "## Knowledge\n"
        "KNOWLEDGE"
    )

    suffix = build_dynamic_prompt_suffix(
        retrieval_context=retrieval,
        system_prompt_suffix="FINAL_SUFFIX",
    )

    assert suffix.count("## Knowledge") == 1
    assert "## Runtime Context" in suffix
    assert "## Relevant Memory Recall" in suffix
    assert "FINAL_SUFFIX" in suffix
