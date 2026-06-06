from __future__ import annotations


def test_tg3_catalog_covers_cc_attachment_inventory() -> None:
    """T-G3: CC attachment alignment is a code-owned catalog, not prose only."""
    from app.kernel.runtime_guidance_catalog import ATTACHMENT_ALIGNMENT_CATALOG

    names = {entry.cc_type for entry in ATTACHMENT_ALIGNMENT_CATALOG}

    assert len(ATTACHMENT_ALIGNMENT_CATALOG) >= 40
    assert len(names) == len(ATTACHMENT_ALIGNMENT_CATALOG)
    for required in {
        "todo_reminder",
        "task_reminder",
        "plan_mode",
        "plan_mode_reentry",
        "plan_mode_exit",
        "critical_system_reminder",
        "task_status",
        "skill_listing",
        "skill_discovery",
        "nested_memory",
        "relevant_memories",
        "diagnostics",
        "edited_text_file",
        "hook_additional_context",
    }:
        assert required in names


def test_tg3_catalog_entries_have_status_persistence_and_single_ingress() -> None:
    from app.kernel.runtime_guidance_catalog import ATTACHMENT_ALIGNMENT_CATALOG, AlignmentStatus

    valid_statuses = {status.value for status in AlignmentStatus}
    for entry in ATTACHMENT_ALIGNMENT_CATALOG:
        assert entry.status in valid_statuses, entry.cc_type
        assert entry.hive_surface, entry.cc_type
        assert entry.single_ingress, entry.cc_type
        assert entry.persistence_policy, entry.cc_type
        assert entry.decision, entry.cc_type


def test_runtime_transient_prompt_entries_use_scheduler_as_only_ingress() -> None:
    from app.kernel.runtime_guidance_catalog import runtime_transient_prompt_entries

    entries = runtime_transient_prompt_entries()

    assert entries
    for entry in entries:
        assert entry.single_ingress == "kernel/reminder_scheduler.py::ReminderScheduler", entry.cc_type
        assert entry.persistence_policy == "transient_request_only", entry.cc_type


def test_catalog_groups_tg3_outcomes_for_docs_and_reviews() -> None:
    from app.kernel.runtime_guidance_catalog import catalog_by_status

    grouped = catalog_by_status()

    assert grouped["have"]
    assert grouped["planned"]
    assert grouped["n/a"]
    assert any(entry.cc_type == "diagnostics" for entry in grouped["planned"])
    assert any(entry.cc_type == "nested_memory" for entry in grouped["n/a"])
