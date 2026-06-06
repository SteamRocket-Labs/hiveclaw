"""T-G3 runtime guidance alignment catalog.

This module is the code-owned source of truth for the CC attachment/reminder
alignment inventory described in docs/runtime-guidance-cc-alignment.md. It does
not execute reminders; it prevents the alignment decision from living only in
prose by recording each CC attachment family, Hive's mapped surface, the single
ingress that owns it, and whether the data is transient, persisted, internal, or
not applicable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AlignmentStatus(str, Enum):
    HAVE = "have"
    PLANNED = "planned"
    NOT_APPLICABLE = "n/a"


@dataclass(frozen=True, slots=True)
class RuntimeGuidanceCatalogEntry:
    cc_type: str
    family: str
    status: str
    hive_surface: str
    single_ingress: str
    persistence_policy: str
    decision: str


_SCHEDULER = "kernel/reminder_scheduler.py::ReminderScheduler"


def _entry(
    cc_type: str,
    family: str,
    status: AlignmentStatus,
    hive_surface: str,
    single_ingress: str,
    persistence_policy: str,
    decision: str,
) -> RuntimeGuidanceCatalogEntry:
    return RuntimeGuidanceCatalogEntry(
        cc_type=cc_type,
        family=family,
        status=status.value,
        hive_surface=hive_surface,
        single_ingress=single_ingress,
        persistence_policy=persistence_policy,
        decision=decision,
    )


ATTACHMENT_ALIGNMENT_CATALOG: tuple[RuntimeGuidanceCatalogEntry, ...] = (
    _entry(
        "todo_reminder",
        "task",
        AlignmentStatus.HAVE,
        "Work Ledger reminder with persisted todo snapshot",
        _SCHEDULER,
        "transient_request_only",
        "Mapped to track_todo/record_finding/read_ledger; idle10+cooldown10.",
    ),
    _entry(
        "task_reminder",
        "task",
        AlignmentStatus.HAVE,
        "Work Ledger reminder with gentle guard",
        _SCHEDULER,
        "transient_request_only",
        "Same Hive surface as todo_reminder; task writes are cognitive, not execution.",
    ),
    _entry(
        "task_status",
        "task",
        AlignmentStatus.NOT_APPLICABLE,
        "Private Work Ledger/read_ledger view",
        "services/agent_work_ledger.py",
        "persisted_work_ledger_json",
        "CC exposes task status as prompt attachment; Hive keeps it private unless reminder/read_ledger surfaces it.",
    ),
    _entry(
        "plan_mode",
        "plan",
        AlignmentStatus.HAVE,
        "Plan Mode FULL reminder",
        _SCHEDULER,
        "transient_request_only",
        "Read-only policy and approval path live in one main-loop Plan Mode runtime.",
    ),
    _entry(
        "plan_mode_reentry",
        "plan",
        AlignmentStatus.HAVE,
        "Plan Mode FULL re-arm after compaction",
        _SCHEDULER,
        "transient_request_only",
        "Compaction reset re-arms fire-once plan reminder without losing queued events.",
    ),
    _entry(
        "plan_mode_exit",
        "plan",
        AlignmentStatus.HAVE,
        "exit_plan_mode approval request/result",
        "tools/handlers/plan_mode.py::exit_plan_mode",
        "tool_result_and_plan_row",
        "Plan submission persists in agent_plan_requests; no runtime reminder path needed.",
    ),
    _entry(
        "critical_system_reminder",
        "safety",
        AlignmentStatus.PLANNED,
        "Critical invariant reminder",
        _SCHEDULER,
        "future_transient_request_only",
        "T-G2 added internal guards; a dedicated critical-reminder spec remains a separate policy cut.",
    ),
    _entry(
        "token_budget_warning",
        "budget",
        AlignmentStatus.HAVE,
        "Round-pressure warning",
        _SCHEDULER,
        "transient_request_only",
        "80% and final-2 round budget reminders carry real tool/context counts.",
    ),
    _entry(
        "loop_guard_warning",
        "safety",
        AlignmentStatus.HAVE,
        "Loop guard warn-before-abort diagnostic",
        _SCHEDULER,
        "transient_request_only",
        "LoopGuard creates the decision; scheduler is the only prompt-ingress path.",
    ),
    _entry(
        "permission_denied",
        "governance",
        AlignmentStatus.HAVE,
        "Permission/approval tool result",
        "services/approval_service.py + kernel permission events",
        "tool_result_and_event",
        "Governance failure is returned as tool result/event, not a periodic reminder.",
    ),
    _entry(
        "approval_required",
        "governance",
        AlignmentStatus.HAVE,
        "Plan gate / approval checkpoint",
        "services/plan_mode_gate.py",
        "persisted_plan_or_checkpoint",
        "External-visible or irreversible actions route through gate/checkpoint records.",
    ),
    _entry(
        "skill_listing",
        "skill",
        AlignmentStatus.HAVE,
        "Static/deferred skill catalog",
        "tools/handlers/skills.py::tool_search",
        "static_prompt_or_tool_result",
        "Hive keeps skill discovery in static guidance/tool_search rather than repeated attachments.",
    ),
    _entry(
        "skill_discovery",
        "skill",
        AlignmentStatus.HAVE,
        "tool_search -> load_skill flow",
        "tools/handlers/skills.py::tool_search",
        "tool_result",
        "Matches Hive's current small-cut pack semantics.",
    ),
    _entry(
        "skill_loaded",
        "skill",
        AlignmentStatus.HAVE,
        "load_skill result",
        "tools/handlers/skills.py::load_skill",
        "tool_result",
        "Loaded skill knowledge is returned through the tool result path.",
    ),
    _entry(
        "mcp_tool_listing",
        "tooling",
        AlignmentStatus.HAVE,
        "MCP resource/server tools",
        "tools/handlers/mcp.py",
        "tool_result",
        "MCP discovery stays in tool APIs and static/deferred tool descriptions.",
    ),
    _entry(
        "subagent_listing",
        "coordination",
        AlignmentStatus.HAVE,
        "spawn_subagent/delegate tools",
        "prompt_sections/executing_actions.py + tool schemas",
        "static_prompt",
        "Core tool visibility plus tool descriptions own subagent discoverability.",
    ),
    _entry(
        "workflow_listing",
        "workflow",
        AlignmentStatus.HAVE,
        "preview_workflow/start_workflow tools",
        "prompt_sections/executing_actions.py + workflow tool schemas",
        "static_prompt",
        "Workflow discoverability is static/tool-schema driven; runtime admission is in workflow services.",
    ),
    _entry(
        "relevant_memories",
        "memory",
        AlignmentStatus.HAVE,
        "Memory Control Plane dynamic suffix",
        "runtime/prompt_builder.py",
        "dynamic_prompt_suffix",
        "Tenant-scoped governed memory is injected through the existing memory suffix.",
    ),
    _entry(
        "nested_memory",
        "memory",
        AlignmentStatus.NOT_APPLICABLE,
        "Governed owner/company memory scopes",
        "memory/activation.py",
        "persisted_memory_control_plane",
        "Hive models nested memory as governed scopes, not a separate CC-style prompt attachment.",
    ),
    _entry(
        "memory_compaction_summary",
        "memory",
        AlignmentStatus.HAVE,
        "Compaction summary reinjection",
        "kernel/engine.py::_build_restoration_context",
        "transient_after_compaction",
        "Post-compaction restoration includes identity/focus/ledger context.",
    ),
    _entry(
        "compact_reboot_context",
        "memory",
        AlignmentStatus.HAVE,
        "Work Ledger reboot block",
        "services/agent_work_ledger.py::render_work_ledger_resume_block",
        "transient_after_compaction",
        "Answers the five reboot questions from persisted ledger state.",
    ),
    _entry(
        "diagnostics",
        "ide",
        AlignmentStatus.PLANNED,
        "LSP/test diagnostics attachment",
        "future diagnostics watcher",
        "future_transient_or_artifact",
        "Requires IDE/LSP/file-watch substrate; tracked as an independent cut.",
    ),
    _entry(
        "edited_text_file",
        "ide",
        AlignmentStatus.PLANNED,
        "External file edit watcher",
        "future workspace file-watch service",
        "future_transient_or_artifact",
        "Needs file-watch/live editor integration before prompt injection is meaningful.",
    ),
    _entry(
        "workspace_file_snapshot",
        "workspace",
        AlignmentStatus.HAVE,
        "read_file/list_files tool results",
        "tools/handlers/filesystem.py",
        "tool_result",
        "File state is explicit tool output; runtime reminders do not duplicate it.",
    ),
    _entry(
        "tool_result_eviction_notice",
        "budget",
        AlignmentStatus.HAVE,
        "Large tool result eviction notice",
        "kernel/engine.py::_maybe_evict_tool_result",
        "tool_result",
        "Evicted outputs are stored as artifacts with a tool-result pointer.",
    ),
    _entry(
        "prompt_too_long_retry_notice",
        "budget",
        AlignmentStatus.HAVE,
        "PTL retry/compression path",
        "kernel/engine.py prompt-too-long retry",
        "internal_event",
        "Retry mechanics are internal; summaries re-enter through compaction/restoration.",
    ),
    _entry(
        "environment_context",
        "runtime",
        AlignmentStatus.HAVE,
        "Runtime/session/source context",
        "runtime/prompt_builder.py",
        "static_or_dynamic_prompt",
        "Agent source, channel, HR/channel context live in prompt sections, not reminder spam.",
    ),
    _entry(
        "model_limit_notice",
        "budget",
        AlignmentStatus.HAVE,
        "RuntimeConfig/model budget",
        "kernel/engine.py RuntimeConfig",
        "internal_policy",
        "Budgets constrain execution; user-visible nudge is round_pressure.",
    ),
    _entry(
        "safety_policy_notice",
        "governance",
        AlignmentStatus.HAVE,
        "Capability/preflight/approval policy",
        "services/action_preflight.py + tools/service.py",
        "governed_tool_execution",
        "Safety is enforced at tool runtime, not by optional prompt text.",
    ),
    _entry(
        "web_fetch_domain_reminder",
        "web",
        AlignmentStatus.PLANNED,
        "Source-quality/domain reminder",
        "future search/fetch guidance spec",
        "future_transient_request_only",
        "Worth a separate cut after web-search quality policy is defined.",
    ),
    _entry(
        "image_attachment",
        "multimodal",
        AlignmentStatus.HAVE,
        "Vision message transform",
        "kernel/dependencies.apply_vision_transform",
        "request_message",
        "Images are native message content, not reminders.",
    ),
    _entry(
        "agent_delegation_status",
        "coordination",
        AlignmentStatus.HAVE,
        "Lease/Signal/Checkpoint + ledger_todo_id writeback",
        "agents/orchestrator.py + agents/subagent.py",
        "persisted_coordination_and_ledger",
        "A2A progress is persisted in coordination records and optional ledger mirrors.",
    ),
    _entry(
        "async_task_status",
        "runtime",
        AlignmentStatus.HAVE,
        "RuntimeTask/list_async_tasks status",
        "services/task_executor.py + tools async task handlers",
        "persisted_runtime_task",
        "Async status is explicit API/tool state, not repeated prompt attachment.",
    ),
    _entry(
        "deep_research_routing_reminder",
        "deep_research",
        AlignmentStatus.HAVE,
        "DR routing reminder",
        "kernel/engine.py::_maybe_inject_routing_reminder",
        "tool_result",
        "Legacy DR-specific reminder remains isolated until DR revamp.",
    ),
    _entry(
        "hook_additional_context",
        "hook",
        AlignmentStatus.HAVE,
        "Tool result next_action and handler context",
        "tool handlers returning next_action/tool result text",
        "tool_result",
        "Hive's equivalent lives in governed tool results rather than a global attachment bus.",
    ),
    _entry(
        "hook_pre_tool_use",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Action preflight/capability gate",
        "services/action_preflight.py + services/capability_gate.py",
        "internal_governance_event",
        "Pre-tool hooks are enforcement/audit, not prompt reminders.",
    ),
    _entry(
        "hook_post_tool_use",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "runtime/hooks.py POST_TOOL_USE",
        "runtime/hooks.py",
        "internal_hook_event",
        "Memory/audit hook, intentionally not prompt injection.",
    ),
    _entry(
        "hook_notification",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Channel delivery notifications",
        "services/channel_delivery_service.py",
        "internal_or_external_event",
        "Delivery events are not model guidance.",
    ),
    _entry(
        "hook_stop",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Run completion hooks",
        "runtime/hooks.py RESPONSE_COMPLETE",
        "internal_hook_event",
        "Used for extraction/learning, not prompt injection.",
    ),
    _entry(
        "hook_subagent_stop",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Subagent completion coordination",
        "agents/subagent.py + runtime/hooks.py",
        "internal_coordination_event",
        "Subagent completion is coordination state, not a parent prompt attachment.",
    ),
    _entry(
        "hook_user_prompt_submit",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Invocation request construction",
        "runtime/invoker.py",
        "request_input",
        "Hive routes prompt-submit concerns through invoker/session context.",
    ),
    _entry(
        "hook_session_start",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Session context initialization",
        "runtime/session_key.py",
        "internal_session_state",
        "Session start is state setup, not dynamic reminder.",
    ),
    _entry(
        "hook_session_end",
        "hook",
        AlignmentStatus.NOT_APPLICABLE,
        "Session close/consolidation",
        "runtime/hooks.py SESSION_CLOSE",
        "internal_hook_event",
        "Close events feed memory consolidation, not the active prompt.",
    ),
    _entry(
        "hook_pre_compact",
        "hook",
        AlignmentStatus.HAVE,
        "PRE_COMPACTION extraction hook",
        "runtime/hooks.py PRE_COMPACTION",
        "internal_hook_event",
        "Extraction/audit before compaction; prompt restoration is owned by engine.",
    ),
    _entry(
        "hook_post_compact",
        "hook",
        AlignmentStatus.HAVE,
        "POST_COMPACTION summary hook",
        "runtime/hooks.py POST_COMPACTION",
        "internal_hook_event",
        "Post-compact hook persists summary metadata; prompt restoration remains engine-owned.",
    ),
)


def runtime_transient_prompt_entries() -> tuple[RuntimeGuidanceCatalogEntry, ...]:
    """Entries that may inject transient prompt text at runtime today."""

    return tuple(
        entry
        for entry in ATTACHMENT_ALIGNMENT_CATALOG
        if entry.status == AlignmentStatus.HAVE.value and entry.persistence_policy == "transient_request_only"
    )


def catalog_by_status() -> dict[str, list[RuntimeGuidanceCatalogEntry]]:
    grouped: dict[str, list[RuntimeGuidanceCatalogEntry]] = {status.value: [] for status in AlignmentStatus}
    for entry in ATTACHMENT_ALIGNMENT_CATALOG:
        grouped.setdefault(entry.status, []).append(entry)
    return grouped


__all__ = [
    "ATTACHMENT_ALIGNMENT_CATALOG",
    "AlignmentStatus",
    "RuntimeGuidanceCatalogEntry",
    "catalog_by_status",
    "runtime_transient_prompt_entries",
]
