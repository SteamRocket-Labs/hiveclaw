from __future__ import annotations


EXPECTED_CC_NATIVE_ATTACHMENT_TYPES = frozenset(
    {
        "agent_listing_delta",
        "agent_mention",
        "already_read_file",
        "async_hook_response",
        "auto_mode",
        "auto_mode_exit",
        "bagel_console",
        "budget_usd",
        "command_permissions",
        "compact_file_reference",
        "compaction_reminder",
        "companion_intro",
        "context_efficiency",
        "critical_system_reminder",
        "current_session_memory",
        "date_change",
        "deferred_tools_delta",
        "diagnostics",
        "directory",
        "dynamic_skill",
        "edited_image_file",
        "edited_text_file",
        "file",
        "hook_additional_context",
        "hook_blocking_error",
        "hook_cancelled",
        "hook_error_during_execution",
        "hook_non_blocking_error",
        "hook_permission_decision",
        "hook_stopped_continuation",
        "hook_success",
        "hook_system_message",
        "invoked_skills",
        "max_turns_reached",
        "mcp_instructions_delta",
        "mcp_resource",
        "nested_memory",
        "opened_file_in_ide",
        "output_style",
        "output_token_usage",
        "pdf_reference",
        "plan_file_reference",
        "plan_mode",
        "plan_mode_exit",
        "plan_mode_reentry",
        "queued_command",
        "relevant_memories",
        "selected_lines_in_ide",
        "skill_discovery",
        "skill_listing",
        "structured_output",
        "task_reminder",
        "task_status",
        "team_context",
        "teammate_mailbox",
        "teammate_shutdown_batch",
        "todo_reminder",
        "token_usage",
        "ultrathink_effort",
        "verify_plan_reminder",
    }
)

EXPECTED_HIVE_TRANSIENT_TYPES = frozenset(
    {
        "work_ledger_reminder",
        "plan_mode_full",
        "plan_mode_sparse",
        "round_pressure",
        "loop_guard_warning",
    }
)


def test_tg3_cc_native_catalog_matches_cc_attachment_union_exactly() -> None:
    """T-G3.1: freeze CC native names instead of accepting len>=40 false coverage."""
    from app.kernel.runtime_guidance_catalog import ATTACHMENT_ALIGNMENT_CATALOG, CC_NATIVE_ATTACHMENT_CATALOG

    names = {entry.cc_type for entry in CC_NATIVE_ATTACHMENT_CATALOG}
    legacy_names = {entry.cc_type for entry in ATTACHMENT_ALIGNMENT_CATALOG}

    assert len(CC_NATIVE_ATTACHMENT_CATALOG) == 60
    assert names == EXPECTED_CC_NATIVE_ATTACHMENT_TYPES
    assert legacy_names == EXPECTED_CC_NATIVE_ATTACHMENT_TYPES


def test_tg3_cc_native_entries_have_complete_decisions() -> None:
    from app.kernel.runtime_guidance_catalog import CC_NATIVE_ATTACHMENT_CATALOG, AlignmentStatus

    valid_statuses = {status.value for status in AlignmentStatus}
    for entry in CC_NATIVE_ATTACHMENT_CATALOG:
        assert entry.status in valid_statuses, entry.cc_type
        assert entry.hive_surface, entry.cc_type
        assert entry.single_ingress, entry.cc_type
        assert entry.persistence_policy, entry.cc_type
        assert entry.decision, entry.cc_type
        assert not hasattr(entry, "hive_type"), entry.cc_type


def test_tg3_hive_native_guidance_catalog_is_a_separate_namespace() -> None:
    from app.kernel.runtime_guidance_catalog import HIVE_NATIVE_GUIDANCE_CATALOG, AlignmentStatus

    valid_statuses = {status.value for status in AlignmentStatus}
    hive_names = {entry.hive_type for entry in HIVE_NATIVE_GUIDANCE_CATALOG}

    assert hive_names
    assert not hive_names & EXPECTED_CC_NATIVE_ATTACHMENT_TYPES
    for entry in HIVE_NATIVE_GUIDANCE_CATALOG:
        assert entry.status in valid_statuses, entry.hive_type
        assert entry.hive_surface, entry.hive_type
        assert entry.single_ingress, entry.hive_type
        assert entry.persistence_policy, entry.hive_type
        assert entry.decision, entry.hive_type
        assert not hasattr(entry, "cc_type"), entry.hive_type


def test_runtime_transient_prompt_entries_are_scheduler_owned_hive_guidance() -> None:
    from app.kernel.runtime_guidance_catalog import HIVE_NATIVE_GUIDANCE_CATALOG, runtime_transient_prompt_entries

    entries = runtime_transient_prompt_entries()
    entry_names = {entry.hive_type for entry in entries}
    hive_names = {entry.hive_type for entry in HIVE_NATIVE_GUIDANCE_CATALOG}

    assert EXPECTED_HIVE_TRANSIENT_TYPES <= entry_names
    assert entry_names <= hive_names
    for entry in entries:
        assert entry.single_ingress == "kernel/reminder_scheduler.py::ReminderScheduler", entry.hive_type
        assert entry.persistence_policy == "transient_request_only", entry.hive_type
        assert entry.status == "have", entry.hive_type
        assert not hasattr(entry, "cc_type"), entry.hive_type


def test_catalog_groups_tg3_outcomes_by_namespace_for_docs_and_reviews() -> None:
    from app.kernel.runtime_guidance_catalog import catalog_by_status

    cc_grouped = catalog_by_status("cc_native")
    hive_grouped = catalog_by_status("hive_native")

    assert any(entry.cc_type == "diagnostics" for entry in cc_grouped["planned"])
    assert any(entry.cc_type == "nested_memory" for entry in cc_grouped["n/a"])
    assert any(entry.hive_type == "loop_guard_warning" for entry in hive_grouped["have"])
    assert all(not hasattr(entry, "hive_type") for entries in cc_grouped.values() for entry in entries)
    assert all(not hasattr(entry, "cc_type") for entries in hive_grouped.values() for entry in entries)
