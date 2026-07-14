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

    def test_assemble_fails_loudly_when_selected_prompt_exceeds_budget(self) -> None:
        from app.runtime.prompt_builder import PromptBudgetExceededError, assemble_runtime_prompt

        frozen = "A" * 20000
        dynamic = "B" * 100
        # 8K model → budget 15000. The final assembler is not allowed to
        # silently choose which frozen or dynamic bytes survive.
        with pytest.raises(PromptBudgetExceededError) as exc_info:
            assemble_runtime_prompt(frozen, dynamic, context_window_tokens=8000)

        assert exc_info.value.required_chars > exc_info.value.budget_chars

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


def test_dynamic_suffix_preserves_every_deferred_tool_despite_advisory_budget() -> None:
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
    assert "tool_29" in suffix
    assert "more available in manifest" not in suffix


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


def test_dynamic_suffix_never_second_trims_authorized_semantic_inputs() -> None:
    import hashlib

    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    memory_tail = "DECISIVE-MEMORY-TAIL"
    retrieval_tail = "DECISIVE-RETRIEVAL-TAIL"
    suffix_tail = "DECISIVE-SUFFIX-TAIL"
    memory = ("memory-evidence " * 800) + memory_tail
    retrieval = ("retrieval-evidence " * 500) + retrieval_tail
    explicit_suffix = ("request-context " * 500) + suffix_tail
    ledger: list[dict] = []

    rendered = build_dynamic_prompt_suffix(
        memory_snapshot=memory,
        retrieval_context=retrieval,
        system_prompt_suffix=explicit_suffix,
        context_section_ledger=ledger,
    )

    assert memory_tail in rendered
    assert retrieval_tail in rendered
    assert suffix_tail in rendered
    decisions = {item["candidate_id"]: item for item in ledger}
    assert (
        decisions["dynamic:memory:memory_snapshot"]["source_hash"] == hashlib.sha256(memory.encode()).hexdigest()[:16]
    )
    assert (
        decisions["dynamic:knowledge:retrieval_context"]["source_hash"]
        == hashlib.sha256(retrieval.encode()).hexdigest()[:16]
    )
    assert not any(item["decision"] == "selected_trimmed" for item in ledger)


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


def test_dynamic_suffix_preserves_complete_retrieval_and_suffix():
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix

    retrieval = "\n".join(f"- item {i} {'x' * 80}" for i in range(80))
    suffix = build_dynamic_prompt_suffix(
        active_tool_groups=[],
        retrieval_context=retrieval,
        system_prompt_suffix="FINAL_SUFFIX",
    )

    assert "FINAL_SUFFIX" in suffix
    assert retrieval in suffix
    assert "- item 79" in suffix


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

        # Stay below the higher cache advisory threshold so this records only
        # the early warning band.
        bloated = "x" * 43000
        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(bloated)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 0
        assert any("above warn threshold" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records)

    def test_cache_advisory_overrun_bumps_both_counters_without_trimming(self, caplog) -> None:
        import logging

        from app.memory import metrics
        from app.runtime.prompt_builder import _meter_frozen_prefix

        # 16000 tokens × 3.5 chars/token = 56000 chars; pad past the limit.
        oversized = "x" * 60000
        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(oversized)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 1
        assert any("above cache advisory threshold" in rec.message for rec in caplog.records)

    def test_exact_cache_advisory_threshold_stays_early_warning_only(self, caplog) -> None:
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
        assert not any("above cache advisory threshold" in rec.message for rec in caplog.records)

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
        with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
            _meter_frozen_prefix(oversized)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_warn_total"] == 1
        assert snap["frozen_prefix_overrun_total"] == 1
        assert any("above cache advisory threshold" in rec.message for rec in caplog.records)

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


# ── Frozen prefix cache-economics telemetry ───────────────────


class TestFrozenPrefixSemanticPreservation:
    """Cache-economics thresholds observe size but never rewrite context."""

    def setup_method(self) -> None:
        from app.memory import metrics

        metrics.reset_all()

    def test_under_limit_no_trimming(self) -> None:
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        prefix = build_frozen_prompt_prefix(agent_context="ctx", skill_catalog="## Skills\n- a\n- b")
        assert "ctx" in prefix
        assert "## Skills" in prefix  # catalog kept when budget allows

    def test_large_inline_catalog_is_preserved_completely(self) -> None:
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        decisive_tail = "DECISIVE_INLINE_SKILL_TAIL"
        bloated_catalog = "## Skills\n" + "\n".join(f"- skill_{i}: {'x' * 30}" for i in range(1800)) + decisive_tail

        prefix = build_frozen_prompt_prefix(agent_context="tiny ctx", skill_catalog=bloated_catalog)

        assert "tiny ctx" in prefix
        assert bloated_catalog in prefix
        assert decisive_tail in prefix

    def test_partial_catalog_kept_when_leftover_budget_fits(self) -> None:
        """Base small, modest-size catalog — re-fit a trimmed catalog."""
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        catalog = "## Skills\n" + "\n".join(f"- skill_{i}" for i in range(150))
        prefix = build_frozen_prompt_prefix(agent_context="ctx", skill_catalog=catalog)
        assert catalog in prefix

    def test_oversized_identity_tail_is_preserved(self) -> None:
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        decisive_tail = "DECISIVE_OVERSIZED_IDENTITY_TAIL"
        oversize_ctx = ("soul_data " * 7000) + decisive_tail
        prefix = build_frozen_prompt_prefix(agent_context=oversize_ctx)

        assert oversize_ctx in prefix
        assert decisive_tail in prefix
        assert "## System" in prefix
        assert "## Doing Tasks" in prefix
        assert "## Using Your Tools" in prefix
        assert prefix.startswith("soul_data")

    def test_metering_records_complete_size_without_rewriting(self) -> None:
        from app.memory import metrics
        from app.runtime.prompt_builder import build_frozen_prompt_prefix

        bloated_catalog = "## Skills\n" + "\n".join(f"- skill_{i}: {'x' * 30}" for i in range(2000))
        rendered = build_frozen_prompt_prefix(agent_context="ctx", skill_catalog=bloated_catalog)

        snap = metrics.snapshot()
        assert snap["frozen_prefix_chars"]["max"] == len(rendered)
        assert snap["frozen_prefix_overrun_total"] == 1


# ── P1-W2-2: Dynamic suffix per-section caps ──────────────────


class TestDynamicSuffixSemanticPreservation:
    """Advisory section budgets never cut authorized semantic inputs."""

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

    def test_oversized_memory_context_is_preserved_completely(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        decisive_tail = "DECISIVE_MEMORY_TAIL"
        bloated_memory = ("MEMORY-LINE\n" * 5000) + decisive_tail
        suffix = build_dynamic_prompt_suffix(
            memory_snapshot=bloated_memory,
        )

        assert "## Your Memory System" in suffix
        assert bloated_memory in suffix
        assert decisive_tail in suffix
        assert "memory context trimmed" not in suffix

    def test_large_system_prompt_suffix_is_preserved_completely(self) -> None:
        from app.runtime.prompt_builder import build_dynamic_prompt_suffix

        decisive_tail = "DECISIVE_SYSTEM_SUFFIX_TAIL"
        bloated_suffix = ("x " * 10000) + decisive_tail
        suffix = build_dynamic_prompt_suffix(
            system_prompt_suffix=bloated_suffix,
        )

        assert bloated_suffix in suffix
        assert decisive_tail in suffix

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


# ── Legacy frozen-budget compatibility helper ──────────────────


def test_frozen_prefix_budget_helper_preserves_complete_catalog() -> None:
    from app.runtime.prompt_builder import _enforce_frozen_prefix_budget

    base = ["x" * 56000]
    catalog = "| skill-a | does a | a.md |\n" * 50

    result = _enforce_frozen_prefix_budget(base, catalog)

    assert catalog in result
    assert result.startswith(base[0])


def test_frozen_prefix_budget_helper_ignores_cache_advisory_char_limit() -> None:
    from app.runtime.prompt_builder import _enforce_frozen_prefix_budget

    base = ["base"]
    catalog = "\n".join(f"| skill-{i} | description {i} | s{i}.md |" for i in range(400))

    result = _enforce_frozen_prefix_budget(base, catalog, char_limit=100)

    assert catalog in result
    assert "skill-399" in result


def test_trim_block_budget_is_advisory_and_preserves_complete_semantic_input() -> None:
    from app.runtime.prompt_builder import _trim_block

    result = _trim_block("line one\nline two\nline three\n" + "x" * 500, budget_chars=60)

    assert result == "line one\nline two\nline three\n" + "x" * 500


# ── Frozen context byte fidelity ───────────────────────────────


class TestFrozenContextByteFidelity:
    """All authorized frozen sections remain available to the model."""

    def test_long_soul_context_and_tool_contract_all_survive(self) -> None:
        from app.runtime.prompt_builder import _enforce_frozen_prefix_budget

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

        result = _enforce_frozen_prefix_budget(base_parts, "")

        assert agent_context in result
        assert system_body in result
        assert tasks_body in result
        assert tools_body in result

    def test_advisory_char_limit_does_not_cut_static_contract_or_context(self) -> None:
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

        assert agent_context in rendered
        assert "SYSTEM_CONTRACT_MUST_SURVIVE" in rendered
        assert "TASK_CONTRACT_MUST_SURVIVE" in rendered
        assert "TOOL_CONTRACT_MUST_SURVIVE" in rendered
        assert len(rendered) > 3500


def test_final_system_prompt_budget_never_blind_trims_frozen_contract():
    import hashlib

    from app.runtime.prompt_builder import PromptBudgetExceededError, assemble_runtime_prompt

    frozen = "FROZEN_HEAD\n" + ("frozen " * 1200) + "\nDECISIVE_FROZEN_TAIL"
    with pytest.raises(PromptBudgetExceededError) as exc_info:
        assemble_runtime_prompt(
            frozen,
            "DYNAMIC_SUFFIX",
            budget_profile=SimpleNamespace(system_prompt_budget_chars=1200),
        )

    error = exc_info.value
    assert error.frozen_sha256 == hashlib.sha256(frozen.encode("utf-8")).hexdigest()
    assert error.required_chars > error.budget_chars
    assert "immutable frozen prompt contract" in str(error)


def test_frozen_prefix_cache_economics_never_trim_model_identity() -> None:
    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    decisive_tail = "DECISIVE_IDENTITY_TAIL_MUST_REACH_MODEL"
    agent_context = "## Identity & Mission\n\n" + ("identity evidence " * 5_000) + decisive_tail

    rendered = build_frozen_prompt_prefix(agent_context=agent_context)

    assert decisive_tail in rendered
    assert "identity overrun" not in rendered
    assert "agent context omitted" not in rendered


def test_skills_catalog_section_preserves_complete_discovery_index() -> None:
    from app.runtime.prompt_sections.skills_catalog import build_skills_catalog_section

    decisive_tail = "DECISIVE_SKILL_INDEX_TAIL"
    skills_text = ("skill discovery evidence\n" * 400) + decisive_tail

    rendered = build_skills_catalog_section(skills_text, budget_chars=300)

    assert skills_text in rendered
    assert decisive_tail in rendered


def test_agent_skill_catalog_preserves_complete_ranked_descriptions(monkeypatch) -> None:
    from uuid import uuid4

    from app.services import agent_context

    decisive_tail = "DECISIVE_RANKED_SKILL_TAIL"
    skills_text = ("ranked skill description\n" * 400) + decisive_tail
    monkeypatch.setattr(agent_context, "_load_skills_index", lambda *_args, **_kwargs: skills_text)

    rendered = agent_context.build_skill_catalog_section_for_agent(uuid4())

    assert skills_text in rendered
    assert decisive_tail in rendered


def test_context_window_argument_never_changes_frozen_semantic_bytes() -> None:
    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    decisive_tail = "DECISIVE_WINDOW_INDEPENDENT_TAIL"
    soul_text = "## Identity & Mission\n\n" + ("soul content " * 4500) + decisive_tail

    result_no_window = build_frozen_prompt_prefix(agent_context=soul_text)
    result_large_window = build_frozen_prompt_prefix(
        agent_context=soul_text,
        context_window_tokens=256000,
    )

    assert result_no_window == result_large_window
    assert decisive_tail in result_no_window


def test_oversized_soul_is_observed_without_rewriting(caplog) -> None:
    import logging

    from app.runtime.prompt_builder import build_frozen_prompt_prefix

    decisive_tail = "DECISIVE_SOUL_OVER_ADVISORY_TAIL"
    massive_soul = "## Identity & Mission\n\nYou are the soul.\n" + ("soul overflow content " * 4000) + decisive_tail

    with caplog.at_level(logging.WARNING, logger="app.runtime.prompt_builder"):
        result = build_frozen_prompt_prefix(agent_context=massive_soul)

    assert massive_soul in result
    assert decisive_tail in result
    assert "identity overrun" not in result
    assert any("cache advisory threshold" in rec.message for rec in caplog.records)


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
