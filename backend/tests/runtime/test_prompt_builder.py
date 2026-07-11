from __future__ import annotations

from types import SimpleNamespace

import pytest


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


def test_dynamic_suffix_renders_active_packs():
    """Closure A4 migration (ex test_prompt_builder_includes_active_packs_section):
    the kernel passes session.active_tool_groups straight to the suffix builder."""
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        active_tool_groups=[
            {
                "name": "web_pack",
                "summary": "网页搜索与抓取能力",
                "tools": ["web_search", "firecrawl_fetch"],
            }
        ],
    )

    assert "## Active Runtime Tool Groups" in suffix
    assert "web_pack" in suffix
    assert "web_search, firecrawl_fetch" in suffix


def test_dynamic_suffix_renders_available_deferred_tools():
    """Turn-1 prompt should enumerate deferred names and the select:<tool> path."""
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        available_deferred_tools=[
            {
                "name": "firecrawl_fetch",
                "group": "web_pack",
                "reason": "advanced crawl needed",
                "selector": "select:firecrawl_fetch",
                "schema_token_cost": 42,
                "risk": "network_read",
            },
            "mcp__github__search",
        ],
    )

    assert "## Available Deferred Tools" in suffix
    assert "select:firecrawl_fetch" in suffix
    assert "select:mcp__github__search" in suffix
    assert "group=web_pack" in suffix
    assert "risk=network_read" in suffix
    assert "advanced crawl needed" in suffix
    assert "schema_tokens=42" in suffix


def test_dynamic_suffix_caps_deferred_tool_index_by_budget() -> None:
    from app.runtime.context_budget import TaskProfile
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    budget_profile = type(
        "Budget",
        (),
        {
            "memory_budget_chars": 8000,
            "active_tool_groups_budget_chars": 360,
            "retrieval_budget_chars": 3000,
            "runtime_triggers_budget_chars": 3000,
            "skill_catalog_budget_chars": 1200,
            "task_profile": TaskProfile(name="general", complexity="medium"),
        },
    )()
    candidates = [
        {
            "name": f"tool_{idx}",
            "group": "bulk",
            "reason": "available",
            "selector": f"select:tool_{idx}",
            "schema_token_cost": 99,
            "risk": "governed_runtime",
        }
        for idx in range(30)
    ]

    suffix = build_dynamic_prompt_suffix(available_deferred_tools=candidates, budget_profile=budget_profile)

    assert "## Available Deferred Tools" in suffix
    assert "tool_0" in suffix
    assert "tool_29" not in suffix
    assert "more available in manifest" in suffix


def test_dynamic_suffix_records_context_candidate_selection_ledger():
    from app.runtime.context_budget import TaskProfile
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    budget_profile = type(
        "Budget",
        (),
        {
            "memory_budget_chars": 8000,
            "active_tool_groups_budget_chars": 120,
            "retrieval_budget_chars": 3000,
            "runtime_triggers_budget_chars": 90,
            "skill_catalog_budget_chars": 1200,
            "task_profile": TaskProfile(name="research", complexity="medium"),
        },
    )()
    ledger: list[dict] = []

    suffix = build_dynamic_prompt_suffix(
        memory_snapshot="important memory\n" * 20,
        runtime_metadata_context="",
        skill_catalog="## Skills\n- load_skill",
        retrieval_context="retrieved evidence\n" * 20,
        budget_profile=budget_profile,
        context_section_ledger=ledger,
    )

    decisions = {item["candidate_id"]: item for item in ledger}

    assert "## Your Memory System" in suffix
    assert "## Knowledge" in suffix
    assert "## Skills" in suffix
    assert decisions["dynamic:memory:memory_snapshot"]["selected"] is True
    assert decisions["dynamic:memory:memory_snapshot"]["decision"].startswith("selected")
    assert decisions["dynamic:runtime:runtime_metadata"]["decision"] == "suppressed_empty"
    assert decisions["dynamic:knowledge:retrieval_context"]["selected"] is True
    assert decisions["dynamic:knowledge:retrieval_context"]["decision"].startswith("selected")
    assert decisions["dynamic:skill:skill_catalog"]["budget_key"] == "skill_catalog_budget_chars"
    assert decisions["dynamic:skill:skill_catalog"]["candidate_ref"]["candidate_id"].startswith("skill:skill_catalog:")
    assert (
        decisions["dynamic:memory:memory_snapshot"]["render_order"]
        < decisions["dynamic:skill:skill_catalog"]["render_order"]
    )


def test_dynamic_suffix_has_no_activation_hints_surface() -> None:
    """QKV retirement: the dynamic suffix must not grow a parallel activation
    hints injection surface — recall ranking feeds retriever/KB ordering only."""
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    ledger: list[dict] = []
    suffix = build_dynamic_prompt_suffix(context_section_ledger=ledger)

    assert "## Activation Hints" not in suffix
    assert not any(item["candidate_id"] == "dynamic:activation:hints" for item in ledger)


def test_hook_additional_context_is_recorded_as_hook_context_candidate() -> None:
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    ledger: list[dict] = []
    suffix = build_dynamic_prompt_suffix(
        system_prompt_suffix="## Hook Additional Context\nPolicy hint from hook.",
        context_section_ledger=ledger,
    )
    hook_entries = [item for item in ledger if item["kind"] == "hook_context"]

    assert "Policy hint from hook." in suffix
    assert len(hook_entries) == 1
    assert hook_entries[0]["candidate_id"].startswith("dynamic:hook:user_prompt_submit")
    assert hook_entries[0]["source_ref"] == "hook:user_prompt_submit"
    assert hook_entries[0]["reason"] == "hook_additional_context"
    assert hook_entries[0]["budget_key"] == "hook_context_chars"


def test_dynamic_suffix_renders_effective_permissions_context():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(
        permissions_context=(
            "# Effective Runtime Permissions\napproval_policy: on_request\nnetwork_access: restricted\n"
        )
    )

    assert "# Effective Runtime Permissions" in suffix
    assert "approval_policy: on_request" in suffix
    assert "network_access: restricted" in suffix


def test_dynamic_suffix_suggests_deferred_tool_groups_not_capability_packs():
    from app.runtime.context_budget import TaskProfile
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    budget_profile = type(
        "Budget",
        (),
        {
            "task_profile": TaskProfile(
                name="research",
                complexity="medium",
                suggested_deferred_tool_group_names=("web",),
            ),
            "active_tool_groups_budget_chars": 2000,
            "retrieval_budget_chars": 3000,
        },
    )()

    suffix = build_dynamic_prompt_suffix(budget_profile=budget_profile)

    assert "## Likely Deferred Tool Groups" in suffix
    assert "tool_search" in suffix
    assert "- web" in suffix
    assert "web_pack" not in suffix
    assert "## Likely Capability Packs" not in suffix
    assert "These packs are likely useful" not in suffix


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

    def test_section_breakdown_counts_cjk_by_text_not_ascii_ratio(self) -> None:
        from app.runtime.prompt_builder import _measure_frozen_prefix_sections

        prefix = "## Identity & Mission\n" + ("中文预算校准" * 300)

        [section] = _measure_frozen_prefix_sections(prefix)

        assert section.name == "identity_mission"
        assert section.tokens > int(section.chars / 3.5) * 3
        assert section.tokens >= int(section.chars * 0.85)

    def test_cjk_frozen_prefix_overrun_is_not_hidden_by_ascii_ratio(self, caplog) -> None:
        import logging

        from app.memory import metrics
        from app.runtime.prompt_builder import _meter_frozen_prefix

        oversized = "中文预算" * 5000
        with caplog.at_level(logging.ERROR, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(oversized)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 1
        assert any("exceeds hard limit" in rec.message for rec in caplog.records)

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
        """Base overflows on its own — Tier4 last-resort trim with observable notice.

        I.3: when agent_context alone overflows the cap (no ## Context Material
        to strip, no tool/task sections), the Tier4 identity-overrun path fires.
        Core contracts: (1) size is bounded, (2) head is preserved,
        (3) an observable marker appears (either the generic trim notice or
        the louder identity-overrun marker introduced by I.3).
        """
        from app.runtime.prompt_builder import (
            _FROZEN_PREFIX_CHAR_LIMIT,
            _FROZEN_IDENTITY_OVERRUN_MARKER,
            _FROZEN_PREFIX_TRIM_NOTICE,
            build_frozen_prompt_prefix,
        )

        # Pump agent_context past the hard cap to force base trimming.
        oversize_ctx = "soul_data " * 7000  # ~70K chars
        prefix = build_frozen_prompt_prefix(agent_context=oversize_ctx)

        assert len(prefix) <= _FROZEN_PREFIX_CHAR_LIMIT
        # The marker now sits before the immutable System/Tasks/Tools tail so
        # safety-critical instructions remain byte-for-byte present.
        assert _FROZEN_PREFIX_TRIM_NOTICE in prefix or _FROZEN_IDENTITY_OVERRUN_MARKER in prefix
        assert "## System" in prefix
        assert "## Doing Tasks" in prefix
        assert "## Using Your Tools" in prefix
        # Head of agent_context must be preserved (highest-value content).
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

    def test_memory_context_within_memory_budget_is_not_second_trimmed_to_ratio(self) -> None:
        from app.runtime.context_budget import TaskProfile
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        budget_profile = type(
            "Budget",
            (),
            {
                "memory_budget_chars": 2000,
                "active_tool_groups_budget_chars": 1200,
                "retrieval_budget_chars": 3000,
                "runtime_triggers_budget_chars": 3000,
                "task_profile": TaskProfile(name="general", complexity="medium"),
            },
        )()
        memory_context = "[Semantic Memory]\n- " + ("ranked memory evidence " * 70) + "\n- SCORE_AWARE_TAIL_SENTINEL"
        assert len(memory_context) < budget_profile.memory_budget_chars

        suffix = build_dynamic_prompt_suffix(memory_snapshot=memory_context, budget_profile=budget_profile)

        assert "SCORE_AWARE_TAIL_SENTINEL" in suffix
        assert "memory context trimmed" not in suffix

    def test_oversized_memory_context_trimmed_to_memory_budget(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        # 50K-char memory context; default profile gives an 8K memory budget.
        # Body must be capped + trim notice present.
        bloated_memory = "MEMORY-LINE\n" * 5000  # ~60K chars
        suffix = build_dynamic_prompt_suffix(
            memory_snapshot=bloated_memory,
        )

        assert "## Your Memory System" in suffix  # template still present
        assert "MEMORY-LINE" in suffix  # body partially preserved
        assert "memory context trimmed" in suffix  # trim notice fired

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
        assert "memory context trimmed" not in suffix

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
    assert "work ledger" in suffix.lower()  # state recording responsibility (CC Work Ledger, not objectives)
    assert "do not invent work" in suffix.lower()  # pacing: clean exit over busy-loop


def test_dynamic_suffix_omits_autonomous_section_for_heartbeat() -> None:
    """Heartbeat is the distiller (librarian), not a wake-to-work run: its
    semantics are fully owned by the identity heartbeat template + the
    HEARTBEAT.md SOP (which explicitly forbids external-facing actions).
    Injecting the generic Autonomous Work section would both duplicate and
    CONTRADICT that SOP ("bias toward action" / "external actions via
    plan/checkpoint" do not apply to curation runs)."""
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    suffix = build_dynamic_prompt_suffix(latest_user_query="tick", source="heartbeat")

    assert "Autonomous Work" not in suffix


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


def test_trim_block_marker_is_observable() -> None:
    """C3: _trim_block's cut marker says it was budget-trimmed, not a bare ellipsis."""
    from app.runtime.prompt_builder import _trim_block

    result = _trim_block("line one\nline two\nline three\n" + "x" * 500, budget_chars=60)

    assert "(trimmed" in result
    assert len(result) <= 60  # marker counts against the budget


# ── I.3 frozen budget inversion fix ────────────────────────────


class TestFrozenBudgetInversionFix:
    """I.3 spec: identity-protected layered trim + window-scaled cap.

    These tests are RED until _enforce_frozen_prefix_budget gains the layered
    trim (Tier0/soul untouched → Tier2/Context Material stripped → Tier3/Tools
    trimmed → Tier4/soul last resort) and build_frozen_prompt_prefix accepts a
    context_window_tokens kwarg that scales the cap.
    """

    def test_long_soul_survives_frozen_trim(self) -> None:
        """Tier0 invariant: ## Identity & Mission survives when Context Material
        and/or Tools sections are the cause of overflow.

        Scenario: agent_context has a large soul block + large ## Context
        Material block; system/tasks/tools sections bring the total above cap.
        After enforcement the identity text must be fully present; Context
        Material must be stripped/trimmed (Tier2) rather than the soul (Tier0),
        proving the inversion bug is fixed.

        Current BROKEN behavior: tail-trim of base_only silently strips
        Tools/Tasks from the end while ## Context Material (mid agent_context)
        survives. The new layered trim must do the OPPOSITE: strip Context
        Material first, keep Tools/Tasks, never touch soul.
        """
        from app.runtime.prompt_builder import (
            _CHARS_PER_TOKEN_ESTIMATE,
            _FROZEN_PREFIX_TOKEN_LIMIT,
            _enforce_frozen_prefix_budget,
        )

        # Build a realistic agent_context: soul at head, large Context Material in middle.
        # Soul is small; Context Material is massive (this is the overflow culprit).
        identity_text = "You are the chief analyst.\n" + ("identity soul content " * 50)
        # Context Material is ~44K chars — the primary cause of overflow
        # (cap = 16000 tokens × 3.5 = 56000 chars; we need total > 56000)
        context_material = (
            "## Context Material\n\n### Company Information\n"
            + ("company boilerplate " * 2200)  # 2200 * 20 = 44000 chars
        )
        agent_context = f"## Identity & Mission\n\n{identity_text}\n\n{context_material}"

        # System / Tasks / Tools are reasonably sized (should survive after Context Material stripped)
        system_body = "## System\n\n" + ("system rule " * 400)  # ~4800 chars
        tasks_body = "## Doing Tasks\n\n" + ("task instruction " * 400)  # ~6800 chars
        tools_body = "## Using Your Tools\n\n" + ("tool guidance " * 400)  # ~6400 chars

        base_parts = [agent_context, system_body, tasks_body, tools_body]

        # Total chars must exceed cap so enforcement fires
        cap_chars = int(_FROZEN_PREFIX_TOKEN_LIMIT * _CHARS_PER_TOKEN_ESTIMATE)
        total_chars = sum(len(p) for p in base_parts)
        assert total_chars > cap_chars, (
            f"test invariant: total ({total_chars}) must exceed cap ({cap_chars}) to exercise trim path"
        )

        result = _enforce_frozen_prefix_budget(base_parts, "")

        # Tier0 invariant: soul identity text must be intact
        assert "You are the chief analyst." in result, "soul identity text must survive trim"

        # Tier3 invariant: tool guidance must survive (currently BROKEN — gets tail-trimmed)
        assert "## Using Your Tools" in result, (
            "Tools section (Tier3) must survive; currently broken because tail-trim drops it "
            "while ## Context Material (Tier2) survives — this is the inversion bug"
        )

        # Tier2: Context Material (company boilerplate) must be stripped/trimmed — it's the
        # lower-priority section that should be sacrificed first
        full_company = "company boilerplate " * 2000
        assert full_company not in result, (
            "## Context Material (Tier2) must be trimmed/stripped; "
            "currently it survives while Tools (Tier3) is lost — inversion bug"
        )

    def test_budget_cut_keeps_static_contract_and_context_recovery_pointer(self) -> None:
        from app.runtime.prompt_builder import _enforce_frozen_prefix_budget

        agent_context = (
            "## Identity & Mission\n\n"
            + ("identity evidence " * 900)
            + "\n\n## Context Material\n\n"
            + ("company evidence " * 900)
            + "\n\n## A2A Collaborators\n\n"
            + ("collaborator evidence " * 900)
        )
        system = "## System\n\nSYSTEM_CONTRACT_MUST_SURVIVE"
        tasks = "## Doing Tasks\n\nTASK_CONTRACT_MUST_SURVIVE"
        tools = "## Using Your Tools\n\nTOOL_CONTRACT_MUST_SURVIVE"

        rendered = _enforce_frozen_prefix_budget(
            [agent_context, system, tasks, tools],
            "",
            char_limit=3500,
        )

        assert len(rendered) <= 3500
        assert "SYSTEM_CONTRACT_MUST_SURVIVE" in rendered
        assert "TASK_CONTRACT_MUST_SURVIVE" in rendered
        assert "TOOL_CONTRACT_MUST_SURVIVE" in rendered
        assert "read_context_resource" in rendered
        assert '"ref":"index"' in rendered


def test_final_system_prompt_truncation_has_recovery_pointer():
    from app.runtime.prompt_builder import assemble_runtime_prompt

    prompt = assemble_runtime_prompt(
        "FROZEN_HEAD\n" + ("frozen " * 1200),
        "DYNAMIC_SUFFIX",
        budget_profile=SimpleNamespace(system_prompt_budget_chars=1200),
    )

    assert len(prompt) <= 1200
    assert "read_context_resource" in prompt
    assert '"ref":"index"' in prompt
    assert "DYNAMIC_SUFFIX" in prompt

    def test_frozen_cap_scales_with_window(self) -> None:
        """Cap formula: max(16000, min(int(0.10 * window), 32000)) tokens.

        A 256K-token window gives cap = 25600 tokens = 89600 chars.
        A prefix that exceeds 16K tokens (56K chars) but fits in 25600 tokens
        must survive un-trimmed when the large window is provided, but be
        trimmed when no window is given (16K-token floor).
        """
        from app.runtime.prompt_builder import (
            _CHARS_PER_TOKEN_ESTIMATE,
            _FROZEN_PREFIX_TOKEN_LIMIT,
            build_frozen_prompt_prefix,
        )

        # 18K tokens ≈ 63000 chars — exceeds 16K floor but fits 25.6K (256K window)
        # Build a plausible prefix: soul + tasks + tools
        soul_text = "## Identity & Mission\n\nYou are a senior assistant.\n" + ("soul content " * 4500)
        # 4500 * 13 + header = ~58560 chars > 56000 chars floor cap

        floor_cap_chars = int(_FROZEN_PREFIX_TOKEN_LIMIT * _CHARS_PER_TOKEN_ESTIMATE)
        assert len(soul_text) > floor_cap_chars, (
            f"soul_text ({len(soul_text)}) must exceed floor cap ({floor_cap_chars}) to test scaling"
        )

        # No window → 16K floor → trim fires
        result_no_window = build_frozen_prompt_prefix(agent_context=soul_text)
        assert len(result_no_window) <= floor_cap_chars + 200  # trimmed to floor

        # Large window (256K) → cap = min(25600, 32000) = 25600 tokens = 89600 chars
        large_window_cap_chars = int(min(int(0.10 * 256000), 32000) * _CHARS_PER_TOKEN_ESTIMATE)
        assert len(soul_text) < large_window_cap_chars, (
            f"soul_text ({len(soul_text)}) must fit in scaled cap ({large_window_cap_chars}) to test no-trim path"
        )
        result_large_window = build_frozen_prompt_prefix(
            agent_context=soul_text,
            context_window_tokens=256000,
        )
        # With 256K window the prefix fits — no trim marker should appear
        assert "frozen prefix trimmed" not in result_large_window
        assert "You are a senior assistant." in result_large_window

    def test_soul_over_budget_is_observable(self, caplog) -> None:
        """Tier4 last-resort: when soul alone exceeds cap, emit a LOUD trim
        marker AND log an explicit WARNING — never silently discard identity.

        'Overrun' here means the enforcement code must emit a WARNING that
        specifically calls out that identity/soul had to be trimmed (not just
        the generic warn-threshold log). The resulting prefix must also contain
        a visible marker.
        """
        import logging

        from app.runtime.prompt_builder import (
            _CHARS_PER_TOKEN_ESTIMATE,
            _FROZEN_PREFIX_TOKEN_LIMIT,
            build_frozen_prompt_prefix,
        )

        # Soul that clearly exceeds the floor cap (16K tokens = 56K chars)
        cap_chars = int(_FROZEN_PREFIX_TOKEN_LIMIT * _CHARS_PER_TOKEN_ESTIMATE)
        massive_soul = "## Identity & Mission\n\nYou are the soul.\n" + ("soul overflow content " * 4000)
        # ~88K chars — well over the 56K floor cap
        assert len(massive_soul) > cap_chars, f"soul ({len(massive_soul)}) must exceed floor cap ({cap_chars})"

        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            result = build_frozen_prompt_prefix(agent_context=massive_soul)

        # Result must be bounded (even after Tier4 trim, prefix stays under cap)
        assert len(result) <= cap_chars + 500

        # An observable loud marker SPECIFIC to soul/identity trim must appear in
        # the output — not just the generic "frozen prefix trimmed to stay under cache budget" tail notice.
        # The new code must emit _IDENTITY_OVERRUN_MARKER (or equivalent) when Tier4 fires.
        _GENERIC_TRIM_NOTICE = "frozen prefix trimmed to stay under cache budget"
        assert _GENERIC_TRIM_NOTICE not in result or (
            "soul" in result.lower()
            or "identity overrun" in result.lower()
            or "[identity" in result.lower()
            or "tier4" in result.lower()
        ), "Tier4 must produce a distinct marker beyond the generic tail-trim notice"
        # The key requirement: a soul/identity-specific loud marker exists in the output
        has_loud_identity_marker = (
            "identity overrun" in result.lower()
            or "soul overrun" in result.lower()
            or "[identity trimmed" in result.lower()
            or "tier-4" in result.lower()
            or "tier4" in result.lower()
        )
        assert has_loud_identity_marker, (
            "Tier4 soul overrun must produce a LOUD DISTINCT marker in the output "
            "(e.g. 'identity overrun', 'soul overrun', '[identity trimmed'), not just the generic notice. "
            f"Got result ending: {result[-200:]!r}"
        )

        # Log must contain an explicit ERROR that calls out soul/identity being cut —
        # the generic warn-threshold message is not enough
        has_soul_trim_error_log = any(
            rec.levelno >= logging.ERROR
            and (
                "soul" in rec.message.lower()
                or "identity" in rec.message.lower()
                or "tier 4" in rec.message.lower()
                or "tier4" in rec.message.lower()
            )
            for rec in caplog.records
        )
        assert has_soul_trim_error_log, (
            "Tier4 soul overrun must log an ERROR explicitly mentioning soul/identity trim; "
            f"got: {[(r.levelno, r.message) for r in caplog.records]}"
        )


class TestSkillCatalogInDynamicSuffix:
    """Step 9 (CC parity): the skill catalog lives in the dynamic suffix, not
    the frozen prefix — adding/distilling a skill must never bust the
    prompt-cache boundary.
    """

    def test_dynamic_suffix_renders_skill_catalog(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        suffix = build_dynamic_prompt_suffix(skill_catalog="## Skills\n- web_search\n- write_file")
        assert "## Skills" in suffix
        assert "web_search" in suffix

    def test_dynamic_suffix_omits_empty_catalog(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        suffix = build_dynamic_prompt_suffix(skill_catalog="")
        assert "## Skills" not in suffix

    def test_catalog_in_dynamic_not_frozen(self) -> None:
        """The cache-bust fix, end to end: on the invoker's primary path the
        frozen prefix carries no catalog, while the dynamic suffix does."""
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix, build_frozen_prompt_prefix

        catalog = "## Skills\n- SKILL_MARKER_XYZ"
        # Invoker builds the frozen prefix WITHOUT passing skill_catalog.
        frozen = build_frozen_prompt_prefix(agent_context="AGENT_CONTEXT_BODY")
        dynamic = build_dynamic_prompt_suffix(skill_catalog=catalog)
        assert "SKILL_MARKER_XYZ" not in frozen
        assert "SKILL_MARKER_XYZ" in dynamic

    def test_catalog_section_for_none_agent_is_empty(self) -> None:
        from app.services.agent_context import build_skill_catalog_section_for_agent

        assert build_skill_catalog_section_for_agent(None) == ""

    def test_request_carries_skill_catalog_field(self) -> None:
        """InvocationRequest must expose skill_catalog so the invoker can thread
        the catalog to the kernel's dynamic suffix."""
        from app.kernel.contracts import InvocationRequest

        req = InvocationRequest(
            model=None,
            messages=[],
            agent_name="a",
            role_description="r",
            skill_catalog="## Skills\n- x",
        )
        assert req.skill_catalog == "## Skills\n- x"
