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
                active_tool_groups=[
                    {
                        "name": "web_pack",
                        "summary": "网页搜索与抓取能力",
                        "tools": ["web_search", "firecrawl_fetch"],
                    }
                ]
            )
        ),
    )

    assert "## Active Runtime Tool Groups" in prompt
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
        active_tool_groups=[],
        retrieval_context=retrieval,
        system_prompt_suffix="FINAL_SUFFIX",
    )

    assert "FINAL_SUFFIX" in suffix
    assert len(suffix) < 3200


def test_dynamic_suffix_includes_runtime_metadata_before_environment():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        runtime_metadata_context="## Runtime Metadata\nACTIVE_TRIGGER\nCurrent Conversation: Rocky",
        user_name="Rocky",
        channel="web",
    )

    assert "## Runtime Metadata" in suffix
    assert "ACTIVE_TRIGGER" in suffix
    assert suffix.index("## Runtime Metadata") < suffix.index("## Environment")


@pytest.mark.asyncio
async def test_runtime_metadata_lives_after_cache_boundary(monkeypatch):
    from app.runtime.context import RuntimeContext
    from app.runtime.prompt_builder import PROMPT_CACHE_BOUNDARY, build_runtime_prompt
    from app.runtime.session import SessionContext

    agent_id = uuid4()

    async def fake_build_agent_context(
        _agent_id,
        _agent_name,
        _role_description,
        *,
        current_user_name=None,
        include_runtime_metadata=True,
        budget_profile=None,
    ):
        del budget_profile
        assert current_user_name == "Rocky"
        assert include_runtime_metadata is False
        return "FROZEN_AGENT_CONTEXT"

    async def fake_runtime_context(_agent_id, *, current_user_name=None, budget_profile=None):
        del budget_profile
        assert _agent_id == agent_id
        assert current_user_name == "Rocky"
        return "## Runtime Metadata\nACTIVE_TRIGGER\nCurrent Conversation: Rocky"

    async def fake_fetch_relevant_knowledge(query, tenant_id=None, **_kwargs):
        assert query == "check status"
        assert tenant_id == agent_id
        return ""

    monkeypatch.setattr("app.runtime.prompt_builder.build_agent_runtime_context", fake_runtime_context)
    monkeypatch.setattr("app.runtime.prompt_builder.fetch_relevant_knowledge", fake_fetch_relevant_knowledge)

    prompt = await build_runtime_prompt(
        agent_id=agent_id,
        agent_name="Ops Agent",
        role_description="Operations",
        messages=[{"role": "user", "content": "check status"}],
        tenant_id=agent_id,
        current_user_name="Rocky",
        memory_context="",
        system_prompt_suffix="",
        runtime_context=RuntimeContext(
            session=SessionContext(session_id="s-runtime", source="task", channel="web"),
        ),
        build_agent_context_fn=fake_build_agent_context,
    )

    frozen, dynamic = prompt.split(PROMPT_CACHE_BOUNDARY, 1)
    assert "FROZEN_AGENT_CONTEXT" in frozen
    assert "ACTIVE_TRIGGER" not in frozen
    assert "Current Conversation: Rocky" not in frozen
    assert "ACTIVE_TRIGGER" in dynamic
    assert "Current Conversation: Rocky" in dynamic


# ── P1-1b: Frozen prefix metering ──────────────────────────────


class TestFrozenPrefixMetering:
    """build_frozen_prompt_prefix records every build and warns over 12K tokens."""

    def setup_method(self) -> None:
        from app.memory import metrics

        metrics.reset_all()

    def test_small_prefix_records_sample_no_warn(self) -> None:
        from app.memory import metrics
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        prefix = build_frozen_prompt_prefix(agent_context="tiny")
        snap = metrics.snapshot()

        assert snap["frozen_prefix_chars"]["count"] == 1
        assert snap["frozen_prefix_tokens"]["count"] == 1
        assert snap["frozen_prefix_warn_total"] == 0
        assert snap["frozen_prefix_overrun_total"] == 0
        # tiny + section bodies stays under the 12K-token warn threshold
        assert snap["frozen_prefix_tokens"]["max"] < 12000
        assert "tiny" in prefix

    def test_warn_threshold_bumps_warn_counter_only(self, caplog) -> None:
        import logging

        from app.memory import metrics
        from app.runtime.prompt_builder import _meter_frozen_prefix

        # 12000 tokens × 3.5 chars/token = 42000 chars. Stay below the
        # 16000-token hard limit (56000 chars) so this warns but does not overrun.
        bloated = "x" * 43000
        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(bloated)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 0
        assert any("above warn threshold" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records)

    def test_hard_limit_bumps_both_counters_logs_error(self, caplog) -> None:
        import logging

        from app.memory import metrics
        from app.runtime.prompt_builder import _meter_frozen_prefix

        # 16000 tokens × 3.5 chars/token = 56000 chars; pad past the limit.
        oversized = "x" * 60000
        with caplog.at_level(logging.ERROR, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(oversized)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 1
        assert any("exceeds hard limit" in rec.message and rec.levelno == logging.ERROR for rec in caplog.records)

    def test_exact_hard_limit_stays_warning_only(self, caplog) -> None:
        import logging

        from app.memory import metrics
        from app.runtime.prompt_builder import _meter_frozen_prefix

        # Exactly 16000 tokens × 3.5 chars/token = 56000 chars.
        at_limit = "x" * 56000
        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(at_limit)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 0
        assert any("above warn threshold" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records)
        assert not any("exceeds hard limit" in rec.message for rec in caplog.records)

    def test_section_breakdown_attributes_frozen_prefix_growth(self) -> None:
        from app.runtime.prompt_builder import _measure_frozen_prefix_sections

        prefix = "\n\n".join(
            [
                "## Identity & Mission\n" + ("identity " * 3000),
                "## System\n" + ("system " * 1600),
                "## Skills\n" + ("skill " * 1000),
            ]
        )

        sections = _measure_frozen_prefix_sections(prefix)

        assert [section.name for section in sections] == ["identity_mission", "system", "skills"]
        assert sections[0].chars > sections[1].chars > sections[2].chars
        assert sections[0].tokens == int(sections[0].chars / 3.5)

    def test_warn_log_includes_top_section_diagnostics(self, caplog) -> None:
        import logging

        from app.runtime.prompt_builder import _meter_frozen_prefix

        bloated = "\n\n".join(
            [
                "## Identity & Mission\n" + ("identity " * 3000),
                "## System\n" + ("system " * 1600),
                "## Skills\n" + ("skill " * 1000),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(bloated)

        warning = next(rec for rec in caplog.records if "above warn threshold" in rec.message)
        assert "top_sections=" in warning.message
        assert "identity_mission=" in warning.message
        assert warning.section_tokens["identity_mission"] > warning.section_tokens["system"]
        assert warning.section_tokens["system"] > warning.section_tokens["skills"]

    def test_repeated_calls_accumulate_in_window(self) -> None:
        from app.memory import metrics
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        for _ in range(5):
            build_frozen_prompt_prefix(agent_context="hello")

        snap = metrics.snapshot()
        assert snap["frozen_prefix_chars"]["count"] == 5
        assert snap["frozen_prefix_tokens"]["count"] == 5

    def test_output_efficiency_section_no_longer_imported(self) -> None:
        """Deprecated section was deleted; importing it must fail.

        Catches accidental re-introduction via copy/paste from old branches.
        """
        with pytest.raises(ImportError):
            from app.runtime.prompt_sections import (  # noqa: F401
                build_output_efficiency_section,
            )

    def test_output_efficiency_module_file_removed(self) -> None:
        """The module file itself must be gone."""
        from pathlib import Path

        from app.runtime import prompt_sections

        sections_dir = Path(prompt_sections.__file__).parent
        assert not (sections_dir / "output_efficiency.py").exists()

    def test_frozen_prefix_does_not_include_output_efficiency_marker(self) -> None:
        """Sanity: frozen prefix must not carry the deprecated section header."""
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        prefix = build_frozen_prompt_prefix(agent_context="ctx")
        # the only place an "Output Efficiency" header could appear is the
        # deleted section — tone_style does not use that label.
        assert "Output Efficiency" not in prefix


# ── P1-W2-1: Frozen prefix hard cap ────────────────────────────


class TestFrozenPrefixHardCap:
    """build_frozen_prompt_prefix must stay within `_FROZEN_PREFIX_CHAR_LIMIT`.

    Skill catalog is dropped first because `load_skill` can hydrate any body
    on demand — base sections drive behavior and trim only as last resort.
    """

    def setup_method(self) -> None:
        from app.memory import metrics

        metrics.reset_all()

    def test_under_limit_no_trimming(self) -> None:
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        prefix = build_frozen_prompt_prefix(agent_context="ctx", skill_catalog="## Skills\n- a\n- b")
        assert "ctx" in prefix
        assert "## Skills" in prefix  # catalog kept when budget allows

    def test_skill_catalog_dropped_when_only_catalog_pushes_over_limit(self) -> None:
        """Base fits, catalog is huge — drop it (load_skill replaces it)."""
        from app.runtime.prompt_builder import (
            _FROZEN_PREFIX_CHAR_LIMIT,
            build_frozen_prompt_prefix,
        )

        # Catalog alone blows past the 56K char hard cap.
        bloated_catalog = "## Skills\n" + "\n".join(f"- skill_{i}: {'x' * 30}" for i in range(1800))
        assert len(bloated_catalog) > _FROZEN_PREFIX_CHAR_LIMIT

        prefix = build_frozen_prompt_prefix(agent_context="tiny ctx", skill_catalog=bloated_catalog)

        assert len(prefix) <= _FROZEN_PREFIX_CHAR_LIMIT
        assert "tiny ctx" in prefix  # base preserved
        # Catalog body must not appear in full — accept either fully dropped
        # or trimmed to fit leftover budget.
        assert bloated_catalog not in prefix

    def test_partial_catalog_kept_when_leftover_budget_fits(self) -> None:
        """Base small, modest-size catalog — re-fit a trimmed catalog."""
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        # Modest catalog fits leftover budget.
        catalog = "## Skills\n" + "\n".join(f"- skill_{i}" for i in range(150))
        prefix = build_frozen_prompt_prefix(agent_context="ctx", skill_catalog=catalog)
        # Base is small; catalog fits — should appear (full or trimmed).
        assert "## Skills" in prefix

    def test_tail_trim_when_base_alone_overflows(self) -> None:
        """Base overflows on its own — last-resort tail trim with notice."""
        from app.runtime.prompt_builder import (
            _FROZEN_PREFIX_CHAR_LIMIT,
            _FROZEN_PREFIX_TRIM_NOTICE,
            build_frozen_prompt_prefix,
        )

        # Pump agent_context past the hard cap to force base trimming.
        oversize_ctx = "soul_data " * 7000  # ~70K chars
        prefix = build_frozen_prompt_prefix(agent_context=oversize_ctx)

        assert len(prefix) <= _FROZEN_PREFIX_CHAR_LIMIT
        assert prefix.endswith(_FROZEN_PREFIX_TRIM_NOTICE)
        # Head of agent_context must be preserved (highest-value section).
        assert prefix.startswith("soul_data")

    def test_metering_records_post_trim_size(self) -> None:
        """Metric snapshot reflects the trimmed size, not the pre-trim size.

        Otherwise overrun_total stays elevated forever after a single big
        prompt, masking subsequent regressions.
        """
        from app.memory import metrics
        from app.runtime.prompt_builder import (
            _FROZEN_PREFIX_CHAR_LIMIT,
            build_frozen_prompt_prefix,
        )

        bloated_catalog = "## Skills\n" + "\n".join(f"- skill_{i}: {'x' * 30}" for i in range(2000))
        assert len(bloated_catalog) > _FROZEN_PREFIX_CHAR_LIMIT
        build_frozen_prompt_prefix(agent_context="ctx", skill_catalog=bloated_catalog)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_chars"]["max"] <= _FROZEN_PREFIX_CHAR_LIMIT


# ── P1-W2-2: Dynamic suffix per-section caps ──────────────────


class TestDynamicSuffixCaps:
    """Memory snapshot, system_prompt_suffix, and pack/knowledge sections all
    enforce per-section budgets so a runaway upstream caller can't push the
    dynamic block past round-trip-cost-sensible size."""

    def test_memory_snapshot_trimmed_to_60pct_of_memory_budget(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        # 50K-char memory snapshot; default profile gives ~12K memory budget,
        # so 60% = ~7200 chars. Body must be capped + trim notice present.
        bloated_memory = "MEMORY-LINE\n" * 5000  # ~60K chars
        suffix = build_dynamic_prompt_suffix(
            memory_snapshot=bloated_memory,
        )

        assert "## Your Memory System" in suffix  # template still present
        assert "MEMORY-LINE" in suffix  # body partially preserved
        assert "memory snapshot trimmed" in suffix  # trim notice fired

    def test_system_prompt_suffix_trimmed_to_5k_cap(self) -> None:
        from app.runtime.prompt_builder import (
            _SYSTEM_PROMPT_SUFFIX_CHAR_CAP,
            build_dynamic_prompt_suffix,
        )

        # 20K-char rogue suffix.
        bloated_suffix = "x " * 10000  # 20K chars
        suffix = build_dynamic_prompt_suffix(
            system_prompt_suffix=bloated_suffix,
        )

        # Final dynamic block must not contain the full pre-trim suffix.
        # Allow some slack for the trim notice + headers.
        assert len(suffix) < _SYSTEM_PROMPT_SUFFIX_CHAR_CAP + 500

    def test_short_memory_snapshot_passes_through_unchanged(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        suffix = build_dynamic_prompt_suffix(memory_snapshot="just one line")

        assert "just one line" in suffix
        assert "memory snapshot trimmed" not in suffix

    def test_short_system_suffix_passes_through_unchanged(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        suffix = build_dynamic_prompt_suffix(system_prompt_suffix="FINAL_SUFFIX")

        assert "FINAL_SUFFIX" in suffix


# ── B4: unified autonomous-work semantics (docs/agent-lifecycle-cc-alignment.md 主题 B) ──
# CC injects a single "# Autonomous work" section (pacing / first wake-up /
# bias toward action / state recording); Hive's trigger & heartbeat runs get
# the same unified section via the dynamic suffix.


def test_dynamic_suffix_injects_autonomous_section_for_trigger() -> None:
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(latest_user_query="wake", source="trigger")

    assert "Autonomous Work" in suffix
    assert "no live user" in suffix.lower()
    assert "bias toward action" in suffix.lower()  # CC: prefer doing over asking
    assert "objective ledger" in suffix.lower()  # state recording responsibility
    assert "do not invent work" in suffix.lower()  # pacing: clean exit over busy-loop


def test_dynamic_suffix_injects_autonomous_section_for_heartbeat() -> None:
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(latest_user_query="tick", source="heartbeat")

    assert "Autonomous Work" in suffix


def test_dynamic_suffix_omits_autonomous_section_for_live_chat() -> None:
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(latest_user_query="hello", source="web")

    assert "Autonomous Work" not in suffix


# ── C2: catalog over-budget degradation keeps minimum visibility ──
# (docs/agent-lifecycle-cc-alignment.md 主题 C)


def test_frozen_prefix_omitted_catalog_keeps_minimum_visibility() -> None:
    """When the leftover budget is too small for even a trimmed catalog, the
    model must still learn that skills exist and how to reach them — never
    silently blind."""
    from app.runtime.prompt_builder import _FROZEN_PREFIX_CHAR_LIMIT, _enforce_frozen_prefix_budget

    # Base fills the budget to within <200 chars of the limit
    base = ["x" * (_FROZEN_PREFIX_CHAR_LIMIT - 150)]
    catalog = "| skill-a | does a | a.md |\n" * 50

    result = _enforce_frozen_prefix_budget(base, catalog)

    assert "load_skill" in result  # the path back to the skills
    assert len(result) <= _FROZEN_PREFIX_CHAR_LIMIT + 200  # notice itself stays bounded


def test_frozen_prefix_trimmed_catalog_signposts_remaining_skills() -> None:
    """A trimmed catalog must say MORE skills exist and how to discover them."""
    from app.runtime.prompt_builder import _FROZEN_PREFIX_CHAR_LIMIT, _enforce_frozen_prefix_budget

    base = ["x" * (_FROZEN_PREFIX_CHAR_LIMIT - 2_000)]  # leftover ≈2K — enough to trim into
    catalog = "\n".join(f"| skill-{i} | description {i} | s{i}.md |" for i in range(400))
    assert len(catalog) > 4_000  # sanity: catalog genuinely overflows the leftover

    result = _enforce_frozen_prefix_budget(base, catalog)

    assert "skill-0" in result  # head of the catalog survives
    assert "more skills" in result.lower()  # signpost
    assert "load_skill" in result
