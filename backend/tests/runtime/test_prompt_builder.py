from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_prompt_builder_merges_agent_context_knowledge_memory_and_suffix(monkeypatch):
    from app.runtime.context import RuntimeContext
    from app.runtime.prompt_builder import build_runtime_prompt
    from app.runtime.session import SessionContext

    agent_id = uuid4()

    async def fake_build_agent_context(_agent_id, _agent_name, _role_description, current_user_name=None):
        assert current_user_name == "Rocky"
        return "BASE_PROMPT"

    async def fake_fetch_relevant_knowledge(query, tenant_id=None):
        assert query == "latest market research status"
        assert tenant_id == agent_id
        return "KNOWLEDGE"

    monkeypatch.setattr("app.runtime.prompt_builder.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.prompt_builder.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    prompt = await build_runtime_prompt(
        agent_id=agent_id,
        agent_name="Ops Agent",
        role_description="Operations",
        messages=[
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "latest market research status"},
        ],
        tenant_id=agent_id,
        current_user_name="Rocky",
        memory_context="MEMORY",
        system_prompt_suffix="SUFFIX",
        runtime_context=RuntimeContext(
            session=SessionContext(session_id="s-1", source="task", channel="task"),
        ),
    )

    # Frozen prefix = agent_context + sections + memory; dynamic suffix = knowledge + env + suffix
    assert "BASE_PROMPT" in prompt
    assert "MEMORY" in prompt
    assert "## System" in prompt
    assert "## Doing Tasks" in prompt
    assert "KNOWLEDGE" in prompt
    assert "SUFFIX" in prompt
    assert "__PROMPT_DYNAMIC_BOUNDARY__" in prompt
    assert "## Task Playbook" in prompt
    assert "verify sources" in prompt.lower()


@pytest.mark.asyncio
async def test_prompt_builder_skips_empty_sections(monkeypatch):
    from app.runtime.prompt_builder import build_runtime_prompt

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    monkeypatch.setattr("app.runtime.prompt_builder.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.prompt_builder.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    prompt = await build_runtime_prompt(
        agent_id=None,
        agent_name="Agent",
        role_description="",
        messages=[{"role": "assistant", "content": "noop"}],
        tenant_id=None,
        current_user_name=None,
        memory_context="",
        system_prompt_suffix="",
    )

    # With sections injected, prompt starts with BASE but includes System/Tasks/Tools
    assert prompt.startswith("BASE")
    assert "## System" in prompt


@pytest.mark.asyncio
async def test_prompt_builder_includes_active_packs_section(monkeypatch):
    from app.runtime.context import RuntimeContext
    from app.runtime.prompt_builder import build_runtime_prompt
    from app.runtime.session import SessionContext

    async def fake_build_agent_context(*args, **kwargs):
        return "BASE"

    async def fake_fetch_relevant_knowledge(*args, **kwargs):
        return ""

    monkeypatch.setattr("app.runtime.prompt_builder.build_agent_context", fake_build_agent_context)
    monkeypatch.setattr("app.runtime.prompt_builder.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    prompt = await build_runtime_prompt(
        agent_id=None,
        agent_name="Agent",
        role_description="",
        messages=[{"role": "assistant", "content": "noop"}],
        tenant_id=None,
        current_user_name=None,
        memory_context="",
        system_prompt_suffix="",
        runtime_context=RuntimeContext(
            session=SessionContext(
                active_packs=[{
                    "name": "web_pack",
                    "summary": "网页搜索与抓取能力",
                    "tools": ["web_search", "firecrawl_fetch"],
                }]
            )
        ),
    )

    assert "## Active Capability Packs" in prompt
    assert "web_pack" in prompt
    assert "web_search, firecrawl_fetch" in prompt


class TestModelAwareBudget:
    """assemble_runtime_prompt scales budget with context window."""

    def test_default_budget_when_no_context_window(self) -> None:
        from app.runtime.prompt_builder import _compute_system_prompt_budget
        budget = _compute_system_prompt_budget(None)
        assert budget == 60000

    def test_default_budget_when_zero(self) -> None:
        from app.runtime.prompt_builder import _compute_system_prompt_budget
        assert _compute_system_prompt_budget(0) == 60000

    def test_small_model_gets_floor_budget(self) -> None:
        from app.runtime.prompt_builder import _compute_system_prompt_budget
        # 8K context → 8000 * 0.20 * 3.5 = 5600 → clamped to floor 15000
        budget = _compute_system_prompt_budget(8000)
        assert budget == 15000

    def test_large_model_gets_scaled_budget(self) -> None:
        from app.runtime.prompt_builder import _compute_system_prompt_budget
        # 200K context → 200000 * 0.20 * 3.5 = 140000 (within 180K ceiling)
        budget = _compute_system_prompt_budget(200000)
        assert budget == 140000

    def test_medium_model_proportional(self) -> None:
        from app.runtime.prompt_builder import _compute_system_prompt_budget
        # 64K context → 64000 * 0.20 * 3.5 = 44800
        budget = _compute_system_prompt_budget(64000)
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
