"""Codex additive optimization ledger.

CC/FreeCode remains Hive's semantic baseline. Codex is useful as an
engineering-control reference, but this ledger makes that relationship explicit
so approval, sandboxing, telemetry, compaction hooks, and reconciliation can be
absorbed without redefining tool discovery, skills, subagents, or context
composition semantics.
"""

from __future__ import annotations

from typing import Any


_FORBIDDEN_SEMANTIC_OVERRIDES = (
    "tool_discovery",
    "skill_progressive_disclosure",
    "subagent_context_semantics",
    "context_composition_semantics",
)


def _entry(
    *,
    capability: str,
    codex_sources: list[str],
    hive_surfaces: list[str],
    adoption_rule: str,
    semantic_boundary: str,
) -> dict[str, Any]:
    return {
        "capability": capability,
        "layer": "control_plane",
        "codex_sources": codex_sources,
        "hive_surfaces": hive_surfaces,
        "adoption_rule": adoption_rule,
        "semantic_boundary": semantic_boundary,
    }


def build_codex_optimization_ledger() -> dict[str, Any]:
    return {
        "schema": "hive.ccplus.codex_optimization_ledger.v1",
        "semantic_baseline": "freecode_cc",
        "codex_role": "additive_control_plane",
        "source_snapshot": {
            "schema_rs": "/Users/rocky243/Context Engineering/codex/codex-rs/hooks/src/schema.rs",
            "compact_hooks": "/Users/rocky243/Context Engineering/codex/codex-rs/hooks/src/events/compact.rs",
            "analytics_events": "/Users/rocky243/Context Engineering/codex/codex-rs/analytics/src/events.rs",
            "exec_policy": "/Users/rocky243/Context Engineering/codex/codex-rs/execpolicy/src/decision.rs",
            "codex_api_common": "/Users/rocky243/Context Engineering/codex/codex-rs/codex-api/src/common.rs",
        },
        "forbidden_semantic_overrides": list(_FORBIDDEN_SEMANTIC_OVERRIDES),
        "adoptable_control_plane": [
            _entry(
                capability="approval_sandbox_decision_enum",
                codex_sources=[
                    "execpolicy/src/decision.rs::Decision(allow,prompt,forbidden)",
                    "analytics/src/events.rs::GuardianReviewedAction(sandbox_permissions)",
                ],
                hive_surfaces=[
                    "runtime/authorization_decision.py::AuthorizationDecisionEntry",
                    "tools/service.py::ToolRuntimeService",
                    "tools/governance.py::PermissionProfileV1",
                ],
                adoption_rule="normalize result telemetry into allow|prompt|forbidden without changing Hive policy gates",
                semantic_boundary="does_not_change_tool_availability_or_cc_permission_semantics",
            ),
            _entry(
                capability="compaction_lifecycle_hooks",
                codex_sources=[
                    "hooks/src/schema.rs::HookEventNameWire(PreCompact,PostCompact)",
                    "hooks/src/events/compact.rs::PreCompactRequest/PostCompactRequest",
                ],
                hive_surfaces=[
                    "kernel/engine.py::_compress_messages_with_lifecycle_hooks",
                    "runtime/compaction_trace.py::CompactionTraceContext",
                    "runtime/cache_decision_ledger.py",
                ],
                adoption_rule="record pre/post compact lifecycle and stop_reason as observability",
                semantic_boundary="does_not_replace_hive_compaction_prompt_or_memory_truth_source",
            ),
            _entry(
                capability="turn_thread_telemetry",
                codex_sources=[
                    "analytics/src/events.rs::ThreadInitializedEventParams(thread_id,session_id)",
                    "analytics/src/events.rs::CodexAcceptedLineFingerprintsEventParams(turn_id,thread_id)",
                    "codex-api/src/common.rs::ResponsesApiRequest(client_metadata,prompt_cache_key)",
                ],
                hive_surfaces=[
                    "runtime/turn_envelope.py",
                    "services/session_index.py",
                    "models/invocation_span.py",
                ],
                adoption_rule="preserve turn_id/thread_id/prompt_cache_key as telemetry join keys",
                semantic_boundary="does_not_change_session_or_t0_truth_semantics",
            ),
            _entry(
                capability="resume_reconciliation",
                codex_sources=[
                    "analytics/src/events.rs::TurnStatus",
                    "hooks/src/events/compact.rs::serialization_failure_hook_events",
                ],
                hive_surfaces=[
                    "runtime/recovery_manifest.py",
                    "runtime/subagent_decision_entry.py",
                    "services/subagent_run_service.py::resume_persisted_subagent_runs",
                ],
                adoption_rule="make resume failure/safe-retry state machine-visible",
                semantic_boundary="does_not_auto_replay_unsafe_child_tool_frames",
            ),
            _entry(
                capability="memory_consolidation_worker",
                codex_sources=[
                    "codex-api/src/common.rs::MemorySummarizeInput",
                    "codex-api/src/common.rs::MemorySummarizeOutput",
                ],
                hive_surfaces=[
                    "services/auto_dream.py",
                    "memory/t0",
                    "memory/t2",
                    "memory/t3",
                ],
                adoption_rule="treat consolidation worker shape as background control-plane discipline",
                semantic_boundary="does_not_replace_t0_t2_t3_markdown_memory_authority",
            ),
        ],
    }


def codex_delta_can_override_semantics(_surface: str) -> bool:
    return False


def append_codex_optimization_ledger(session_context: Any | None) -> dict[str, Any]:
    ledger = build_codex_optimization_ledger()
    if session_context is None:
        return ledger

    from app.runtime.context import ensure_runtime_assembly_state

    ensure_runtime_assembly_state(session_context).record_codex_optimization_ledger(ledger)
    return ledger


__all__ = [
    "append_codex_optimization_ledger",
    "build_codex_optimization_ledger",
    "codex_delta_can_override_semantics",
]
