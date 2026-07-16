"""Session V2 canonical envelope validation and deterministic item reduction.

The contract is deliberately mechanical.  It validates exact protocol facts
and never inspects natural-language bytes to infer phase, intent, outcome, or
completion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import copy
import json
from typing import Any, Iterable, Mapping


SESSION_EVENT_SCHEMA = "hive.session_event"
SESSION_EVENT_SCHEMA_VERSION = 2
SESSION_EVENT_COMPATIBILITY_SCHEMA = "hive.session_event_compatibility"


class SessionEventContractError(ValueError):
    """A canonical Session V2 envelope violates the exact protocol matrix."""


def _words(value: str) -> frozenset[str]:
    return frozenset(value.split())


@dataclass(frozen=True, slots=True)
class EventKindRule:
    lifecycles: frozenset[str]
    scopes: frozenset[str]
    terminal: frozenset[str]


_CONTENT_LIFECYCLES = _words("started delta snapshot completed failed cancelled")
_CONTENT_TERMINAL = _words("completed failed cancelled")
_EXEC_LIFECYCLES = _words(
    "queued started progress waiting completed failed denied unavailable cancelled needs_reconciliation reconciled"
)
_EXEC_TERMINAL = _words("completed failed denied unavailable cancelled")


EVENT_KIND_MATRIX: dict[str, EventKindRule] = {
    "session": EventKindRule(_words("created resumed forked"), _words("session"), frozenset()),
    "turn": EventKindRule(
        _words("accepted queued started waiting completed failed cancelled needs_reconciliation reconciled"),
        _words("turn"),
        _words("completed failed cancelled"),
    ),
    "run": EventKindRule(
        _words("queued starting running waiting cancelling completed failed cancelled needs_reconciliation reconciled"),
        _words("run"),
        _words("completed failed cancelled"),
    ),
    "human_input": EventKindRule(
        _words("accepted revised queued bound applied rolled_over rejected cancelled needs_reconciliation reconciled"),
        _words("session"),
        _words("applied rolled_over rejected cancelled"),
    ),
    "input_admission": EventKindRule(
        _words("prepared started sealed admitted rejected cancelled needs_reconciliation reconciled"),
        _words("session"),
        _words("admitted rejected cancelled"),
    ),
    "control_input": EventKindRule(
        _words("accepted started applied rejected failed needs_reconciliation reconciled"),
        # Unknown/stale targets still need a canonical rejected receipt, but a
        # rejected attempt must not forge a run authority that does not exist.
        _words("session run"),
        _words("applied rejected failed"),
    ),
    "turn_replacement": EventKindRule(
        _words("requested cancelling fenced queued admitted completed failed needs_reconciliation reconciled"),
        _words("session"),
        _words("completed failed"),
    ),
    "carry_forward": EventKindRule(
        _words("claimed bound consumed needs_reconciliation reconciled"),
        _words("round"),
        _words("consumed"),
    ),
    "assistant_text": EventKindRule(_CONTENT_LIFECYCLES, _words("round"), _CONTENT_TERMINAL),
    "assistant_commentary": EventKindRule(_CONTENT_LIFECYCLES, _words("round"), _CONTENT_TERMINAL),
    "assistant_reasoning_summary": EventKindRule(_CONTENT_LIFECYCLES, _words("round"), _CONTENT_TERMINAL),
    "assistant_reasoning_private": EventKindRule(_CONTENT_LIFECYCLES, _words("round"), _CONTENT_TERMINAL),
    "assistant_final": EventKindRule(_CONTENT_LIFECYCLES, _words("round"), _CONTENT_TERMINAL),
    "assistant_plan": EventKindRule(_CONTENT_LIFECYCLES, _words("round"), _CONTENT_TERMINAL),
    "tool_search": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "tool_call": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "tool_permission": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "mcp_call": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "web_search": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "image_view": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "code_execution": EventKindRule(_EXEC_LIFECYCLES, _words("round"), _EXEC_TERMINAL),
    "tool_result": EventKindRule(_words("completed"), _words("round"), _words("completed")),
    "file_read": EventKindRule(
        _words("started progress completed failed denied unavailable cancelled"),
        _words("round"),
        _words("completed failed denied unavailable cancelled"),
    ),
    "file_change": EventKindRule(
        _words("started progress completed failed denied unavailable cancelled"),
        _words("round"),
        _words("completed failed denied unavailable cancelled"),
    ),
    "file_preview": EventKindRule(
        _words("started progress completed failed denied unavailable cancelled"),
        _words("round"),
        _words("completed failed denied unavailable cancelled"),
    ),
    "artifact": EventKindRule(
        _words("created updated delivered failed"), _words("run round"), _words("delivered failed")
    ),
    "context_source": EventKindRule(
        _words("started completed failed denied unavailable"),
        _words("round"),
        _words("completed failed denied unavailable"),
    ),
    "context_compaction": EventKindRule(
        _words("started progress completed failed needs_reconciliation reconciled"),
        _words("run round"),
        _words("completed failed"),
    ),
    "memory_search": EventKindRule(
        _EXEC_LIFECYCLES - {"queued", "waiting", "needs_reconciliation", "reconciled"}, _words("round"), _EXEC_TERMINAL
    ),
    "memory_load": EventKindRule(
        _EXEC_LIFECYCLES - {"queued", "waiting", "needs_reconciliation", "reconciled"}, _words("round"), _EXEC_TERMINAL
    ),
    "memory_write_proposal": EventKindRule(
        _EXEC_LIFECYCLES - {"queued", "waiting", "needs_reconciliation", "reconciled"}, _words("round"), _EXEC_TERMINAL
    ),
    "memory_commit": EventKindRule(
        _EXEC_LIFECYCLES - {"queued", "waiting", "needs_reconciliation", "reconciled"}, _words("round"), _EXEC_TERMINAL
    ),
    "skill_search": EventKindRule(
        _EXEC_LIFECYCLES - {"queued", "waiting", "needs_reconciliation", "reconciled"}, _words("round"), _EXEC_TERMINAL
    ),
    "skill_load": EventKindRule(
        _EXEC_LIFECYCLES - {"queued", "waiting", "needs_reconciliation", "reconciled"}, _words("round"), _EXEC_TERMINAL
    ),
    "subagent": EventKindRule(
        _words("queued started progress snapshot waiting completed failed denied unavailable cancelled"),
        _words("run round"),
        _words("completed failed denied unavailable cancelled"),
    ),
    "a2a_delegation": EventKindRule(
        _words("queued started progress snapshot waiting completed failed denied unavailable cancelled"),
        _words("run round"),
        _words("completed failed denied unavailable cancelled"),
    ),
    "a2a_receipt": EventKindRule(
        _words("queued started progress snapshot waiting completed failed denied unavailable cancelled"),
        _words("run round"),
        _words("completed failed denied unavailable cancelled"),
    ),
    "workflow_run": EventKindRule(
        _words("queued started progress waiting completed failed denied cancelled needs_reconciliation reconciled"),
        _words("run round"),
        _words("completed failed denied cancelled"),
    ),
    "workflow_step": EventKindRule(
        _words("queued started progress waiting completed failed denied cancelled needs_reconciliation reconciled"),
        _words("run round"),
        _words("completed failed denied cancelled"),
    ),
    "workflow_gate": EventKindRule(
        _words("queued started progress waiting completed failed denied cancelled needs_reconciliation reconciled"),
        _words("run round"),
        _words("completed failed denied cancelled"),
    ),
    "hook": EventKindRule(
        _words("started waiting completed failed blocked prevented denied cancelled"),
        _words("session run round"),
        _words("completed failed blocked prevented denied cancelled"),
    ),
    "approval": EventKindRule(
        _words("created waiting completed denied expired cancelled"),
        _words("run round"),
        _words("completed denied expired cancelled"),
    ),
    "user_question": EventKindRule(
        _words("created waiting completed denied expired cancelled"),
        _words("run round"),
        _words("completed denied expired cancelled"),
    ),
    "result_commit": EventKindRule(
        _words("prepared streaming sealed round_committed failed needs_reconciliation reconciled"),
        _words("round"),
        _words("round_committed failed"),
    ),
    "run_outcome": EventKindRule(
        _words("prepared sealed terminal_committed failed needs_reconciliation reconciled"),
        _words("run"),
        _words("terminal_committed failed"),
    ),
    "runtime_failure": EventKindRule(_words("recorded"), _words("session turn run round"), _words("recorded")),
    "recovery_action": EventKindRule(
        _words("requested started completed failed reconciled"),
        _words("session turn run"),
        _words("completed failed reconciled"),
    ),
    "evaluation_feedback_mutation": EventKindRule(
        _words("recorded updated withdrawn"), _words("session"), _words("recorded updated withdrawn")
    ),
}


@dataclass(frozen=True, slots=True)
class HookBoundaryRule:
    scopes: frozenset[str]
    lifecycles: frozenset[str]
    sources: frozenset[str] | None = None


HOOK_BOUNDARY_MATRIX: dict[str, HookBoundaryRule] = {
    "SessionStart": HookBoundaryRule(
        _words("session"), _words("started completed failed"), _words("startup resume clear compact")
    ),
    "UserPromptSubmit": HookBoundaryRule(
        _words("session"), _words("started completed blocked prevented failed cancelled")
    ),
    "PreToolUse": HookBoundaryRule(
        _words("round"), _words("started waiting completed blocked prevented denied failed cancelled")
    ),
    "Stop": HookBoundaryRule(_words("run"), _words("started completed blocked prevented failed cancelled")),
    "SubagentStop": HookBoundaryRule(_words("run"), _words("started completed blocked prevented failed cancelled")),
    "PreCompact": HookBoundaryRule(
        _words("run"), _words("started completed failed cancelled"), _words("manual auto reactive")
    ),
    "PostCompact": HookBoundaryRule(
        _words("run"), _words("started completed failed cancelled"), _words("manual auto reactive")
    ),
}

SCOPE_FIELDS: dict[str, frozenset[str]] = {
    "session": frozenset({"level", "session_id", "thread_id"}),
    "turn": frozenset({"level", "session_id", "thread_id", "turn_id"}),
    "run": frozenset({"level", "session_id", "thread_id", "turn_id", "run_id"}),
    "round": frozenset({"level", "session_id", "thread_id", "turn_id", "run_id", "round_id"}),
}
SCOPE_REQUIRED_IDS: dict[str, tuple[str, ...]] = {
    "session": ("session_id", "thread_id"),
    "turn": ("session_id", "thread_id", "turn_id"),
    "run": ("session_id", "thread_id", "turn_id", "run_id"),
    "round": ("session_id", "thread_id", "turn_id", "run_id", "round_id"),
}
ASSISTANT_PHASES = {
    "assistant_text": "unknown",
    "assistant_commentary": "commentary",
    "assistant_reasoning_summary": "reasoning_summary",
    "assistant_reasoning_private": "reasoning_private",
    "assistant_final": "final",
}
ACTOR_TYPES = _words("user assistant runtime tool hook workflow agent system")
AUDIENCES = _words("direct_user participants operator private_provider")


def session_event_contract_manifest() -> dict[str, Any]:
    """Return the language-neutral contract consumed by generated TypeScript."""

    return {
        "schema": SESSION_EVENT_SCHEMA,
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "event_rules": {
            item_kind: {
                "lifecycles": sorted(rule.lifecycles),
                "scopes": sorted(rule.scopes),
                "terminal": sorted(rule.terminal),
            }
            for item_kind, rule in sorted(EVENT_KIND_MATRIX.items())
        },
        "hook_rules": {
            boundary: {
                "lifecycles": sorted(rule.lifecycles),
                "scopes": sorted(rule.scopes),
                "sources": sorted(rule.sources or ()),
                "source_required": rule.sources is not None,
            }
            for boundary, rule in sorted(HOOK_BOUNDARY_MATRIX.items())
        },
        "scope_fields": {level: sorted(fields) for level, fields in sorted(SCOPE_FIELDS.items())},
        "scope_required_ids": {level: list(fields) for level, fields in sorted(SCOPE_REQUIRED_IDS.items())},
        "assistant_phases": dict(sorted(ASSISTANT_PHASES.items())),
        "actor_types": sorted(ACTOR_TYPES),
        "audiences": sorted(AUDIENCES),
    }


def render_session_event_contract_typescript() -> str:
    """Render the checked-in TypeScript artifact; drift is a test failure."""

    manifest = json.dumps(
        session_event_contract_manifest(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "/* Generated by app.services.session_event_contract; do not edit. */\n"
        f"export const SESSION_EVENT_CONTRACT = {manifest} as const;\n"
    )


SESSION_V2_TRIGGER_FUNCTION_SIGNATURES = (
    "public.enforce_session_event_v2_contract()",
    "public.enforce_session_writer_epoch()",
    "public.enforce_session_v2_tenant_binding()",
)


def build_session_event_contract_function_sql() -> str:
    """Generate the PostgreSQL guard from the same matrix as Python/TypeScript.

    Alembic upgrade and fresh ``create_all`` bootstrap both install these exact
    bytes, so the database cannot grow an independently maintained CASE table.
    """

    event_matrix = {
        item_kind: {
            "lifecycles": sorted(rule.lifecycles),
            "scopes": sorted(rule.scopes),
        }
        for item_kind, rule in EVENT_KIND_MATRIX.items()
    }
    hook_matrix = {
        boundary: {
            "lifecycles": sorted(rule.lifecycles),
            "scopes": sorted(rule.scopes),
            "sources": sorted(rule.sources or ()),
        }
        for boundary, rule in HOOK_BOUNDARY_MATRIX.items()
    }
    event_json = json.dumps(event_matrix, sort_keys=True, separators=(",", ":")).replace("'", "''")
    hook_json = json.dumps(hook_matrix, sort_keys=True, separators=(",", ":")).replace("'", "''")
    return f"""
        CREATE OR REPLACE FUNCTION public.enforce_session_event_v2_contract()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
          event_matrix jsonb := '{event_json}'::jsonb;
          hook_matrix jsonb := '{hook_json}'::jsonb;
          rule jsonb;
          hook_rule jsonb;
          level text;
          boundary text;
          source_value text;
          allowed_scope_keys text[];
          bound_tenant_id uuid;
          bound_agent_id uuid;
          epoch public.session_writer_epochs%ROWTYPE;
          run_generation integer;
        BEGIN
          SELECT tenant_id,agent_id INTO bound_tenant_id,bound_agent_id
          FROM public.chat_sessions WHERE id=NEW.session_id;
          IF NOT FOUND OR NEW.tenant_id IS DISTINCT FROM bound_tenant_id
             OR NEW.agent_id IS DISTINCT FROM bound_agent_id THEN
            RAISE EXCEPTION 'session_event_authority_binding_mismatch' USING ERRCODE='23514';
          END IF;
          IF TG_OP='UPDATE'
             AND (to_jsonb(NEW) - ARRAY[
                    'metadata_json','projection_status','projection_attempts',
                    'projection_error','projected_at'
                 ]::text[])
                 = (to_jsonb(OLD) - ARRAY[
                    'metadata_json','projection_status','projection_attempts',
                    'projection_error','projected_at'
                 ]::text[])
             AND (COALESCE(NEW.metadata_json,'{{}}'::jsonb) - ARRAY[
                    't0_bridge_pending','t0_bridge_last_error','t0_bridge_attempts',
                    't0_bridge_relayed_at','t0_bridge_relay_source',
                    't0_bridge_segment_id','t0_bridge_event_id','t0_bridge_sequence'
                 ]::text[])
                 = (COALESCE(OLD.metadata_json,'{{}}'::jsonb) - ARRAY[
                    't0_bridge_pending','t0_bridge_last_error','t0_bridge_attempts',
                    't0_bridge_relayed_at','t0_bridge_relay_source',
                    't0_bridge_segment_id','t0_bridge_event_id','t0_bridge_sequence'
                 ]::text[])
             AND NEW.projection_status IN ('pending','projecting','projected','failed','not_requested')
             AND NEW.projection_attempts >= OLD.projection_attempts
             AND (OLD.projection_status <> 'projected' OR NEW.projection_status='projected') THEN
            -- Projection is a current derived-evidence transition, not a late
            -- semantic writer. Canonical event bytes stay immutable while a
            -- drained legacy generation can still finish its T0 sidecar.
            RETURN NEW;
          END IF;
          SELECT * INTO epoch FROM public.session_writer_epochs WHERE id='global';
          IF epoch.id IS NOT NULL AND epoch.enforcement_mode='enforce' THEN
            IF NEW.schema_version <> 2 THEN
              IF epoch.state <> 'v1_draining' OR NEW.run_id IS NULL THEN
                RAISE EXCEPTION 'writer_epoch_rejected legacy transcript mutation' USING ERRCODE='23514';
              END IF;
              SELECT writer_generation INTO run_generation FROM public.runtime_tasks WHERE id=NEW.run_id;
              IF NOT FOUND OR run_generation <> 1
                 OR NOT epoch.allowed_existing_generations_json @> to_jsonb(ARRAY[run_generation]) THEN
                RAISE EXCEPTION 'writer_epoch_rejected legacy run generation' USING ERRCODE='23514';
              END IF;
            ELSIF NEW.run_id IS NOT NULL THEN
              SELECT writer_generation INTO run_generation FROM public.runtime_tasks WHERE id=NEW.run_id;
              IF NOT FOUND OR run_generation <> 2
                 OR NOT epoch.allowed_existing_generations_json @> to_jsonb(ARRAY[run_generation]) THEN
                RAISE EXCEPTION 'writer_epoch_rejected V2 run generation' USING ERRCODE='23514';
              END IF;
            END IF;
          END IF;
          IF NEW.schema_version <> 2 AND NEW.run_id IS NOT NULL AND (
            epoch.id IS NULL OR NOT EXISTS (
              SELECT 1 FROM public.runtime_tasks AS legacy_run
              WHERE legacy_run.id=NEW.run_id
                AND legacy_run.tenant_id=NEW.tenant_id
                AND legacy_run.parent_session_id=NEW.session_id::text
                AND legacy_run.parent_agent_id=NEW.agent_id
                AND epoch.allowed_existing_generations_json
                    @> to_jsonb(ARRAY[legacy_run.writer_generation])
            )
          ) THEN
            RAISE EXCEPTION 'writer_epoch_rejected legacy run authority'
              USING ERRCODE='23514';
          END IF;
          IF NEW.schema_version <> 2 THEN RETURN NEW; END IF;
          IF NEW.item_id IS NULL OR NEW.item_kind IS NULL OR NEW.lifecycle IS NULL
             OR NEW.payload_schema IS NULL OR NEW.scope_json IS NULL
             OR jsonb_typeof(NEW.scope_json) <> 'object' THEN
            RAISE EXCEPTION 'incomplete SessionEventV2 envelope' USING ERRCODE='23514';
          END IF;
          IF NEW.event_type <> NEW.item_kind || '.' || NEW.lifecycle
             OR NEW.payload_schema <> 'hive.session.payload.' || NEW.item_kind || '.' || NEW.lifecycle || '.v2' THEN
            RAISE EXCEPTION 'SessionEventV2 kind/schema mismatch' USING ERRCODE='23514';
          END IF;
          rule := event_matrix->NEW.item_kind;
          level := NEW.scope_json->>'level';
          IF rule IS NULL OR NOT (rule->'lifecycles' ? NEW.lifecycle)
             OR NOT (rule->'scopes' ? level) THEN
            RAISE EXCEPTION 'illegal SessionEventV2 kind/lifecycle/scope tuple' USING ERRCODE='23514';
          END IF;
          allowed_scope_keys := CASE level
            WHEN 'session' THEN ARRAY['level','session_id','thread_id']
            WHEN 'turn' THEN ARRAY['level','session_id','thread_id','turn_id']
            WHEN 'run' THEN ARRAY['level','session_id','thread_id','turn_id','run_id']
            WHEN 'round' THEN ARRAY['level','session_id','thread_id','turn_id','run_id','round_id']
            ELSE ARRAY[]::text[] END;
          IF EXISTS (
            SELECT 1 FROM jsonb_object_keys(NEW.scope_json) AS scope_key
            WHERE NOT (scope_key = ANY(allowed_scope_keys))
          ) THEN
            RAISE EXCEPTION 'SessionEventV2 scope contains forbidden fields' USING ERRCODE='23514';
          END IF;
          IF NULLIF(NEW.scope_json->>'session_id','') IS NULL
             OR NULLIF(NEW.scope_json->>'thread_id','') IS NULL
             OR NEW.scope_json->>'session_id' <> NEW.scope_json->>'thread_id'
             OR (NEW.scope_json->>'session_id')::uuid <> NEW.session_id THEN
            RAISE EXCEPTION 'invalid SessionEventV2 session scope' USING ERRCODE='23514';
          END IF;
          IF level IN ('turn','run','round') AND NULLIF(NEW.scope_json->>'turn_id','') IS NULL THEN
            RAISE EXCEPTION 'turn_id required by SessionEventV2 scope' USING ERRCODE='23514';
          END IF;
          IF level IN ('run','round') AND NULLIF(NEW.scope_json->>'run_id','') IS NULL THEN
            RAISE EXCEPTION 'run_id required by SessionEventV2 scope' USING ERRCODE='23514';
          END IF;
          IF level='round' AND NULLIF(NEW.scope_json->>'round_id','') IS NULL THEN
            RAISE EXCEPTION 'round_id required by SessionEventV2 scope' USING ERRCODE='23514';
          END IF;
          IF (level IN ('session','turn') AND NEW.run_id IS NOT NULL)
             OR (level IN ('run','round') AND (
                  NEW.run_id IS NULL
                  OR NEW.run_id::text <> NEW.scope_json->>'run_id'
             )) THEN
            RAISE EXCEPTION 'session_event_run_scope_mismatch' USING ERRCODE='23514';
          END IF;
          IF NEW.run_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.runtime_tasks AS run
            WHERE run.id=NEW.run_id
              AND run.tenant_id=NEW.tenant_id
              AND run.parent_session_id=NEW.session_id::text
              AND run.parent_agent_id=NEW.agent_id
          ) THEN
            RAISE EXCEPTION 'session_event_run_authority_mismatch' USING ERRCODE='23514';
          END IF;
          IF NEW.command_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.session_commands AS command
            WHERE command.id=NEW.command_id
              AND command.tenant_id=NEW.tenant_id
              AND command.session_id=NEW.session_id
          ) THEN
            RAISE EXCEPTION 'session_event_command_authority_mismatch' USING ERRCODE='23514';
          END IF;
          IF NEW.input_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.session_turn_inputs AS input
            WHERE input.id=NEW.input_id
              AND input.tenant_id=NEW.tenant_id
              AND input.session_id=NEW.session_id
              AND (
                NEW.command_id IS NULL
                OR input.command_id=NEW.command_id
                OR EXISTS (
                  SELECT 1 FROM public.session_commands AS linked_command
                  WHERE linked_command.id=NEW.command_id
                    AND linked_command.namespace='turn_replacement'
                    AND linked_command.causation_command_id=input.command_id
                    AND linked_command.tenant_id=input.tenant_id
                    AND linked_command.session_id=input.session_id
                )
              )
              AND (input.target_run_id IS NULL OR NEW.run_id IS NULL OR input.target_run_id=NEW.run_id)
          ) THEN
            RAISE EXCEPTION 'session_event_input_authority_mismatch' USING ERRCODE='23514';
          END IF;
          IF NEW.result_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.session_model_results AS result
            WHERE result.id=NEW.result_id
              AND result.tenant_id=NEW.tenant_id
              AND result.session_id=NEW.session_id
              AND result.run_id=NEW.run_id
              AND result.turn_id=NEW.scope_json->>'turn_id'
              AND result.round_id=NEW.scope_json->>'round_id'
          ) THEN
            RAISE EXCEPTION 'session_event_result_authority_mismatch' USING ERRCODE='23514';
          END IF;
          IF NEW.invocation_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.session_tool_invocations AS invocation
            WHERE invocation.id=NEW.invocation_id
              AND invocation.tenant_id=NEW.tenant_id
              AND invocation.session_id=NEW.session_id
              AND invocation.run_id=NEW.run_id
              AND invocation.round_id=NEW.scope_json->>'round_id'
          ) THEN
            RAISE EXCEPTION 'session_event_invocation_authority_mismatch' USING ERRCODE='23514';
          END IF;
          IF jsonb_typeof(COALESCE(NEW.metadata_json,'{{}}'::jsonb)->'v2_payload') IS DISTINCT FROM 'object'
             OR jsonb_typeof(COALESCE(NEW.metadata_json,'{{}}'::jsonb)->'actor') IS DISTINCT FROM 'object'
             OR jsonb_typeof(COALESCE(NEW.metadata_json,'{{}}'::jsonb)->'visibility') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'incomplete SessionEventV2 payload authority' USING ERRCODE='23514';
          END IF;
          IF NEW.item_kind='assistant_text'
             AND COALESCE(NEW.metadata_json->'v2_payload'->>'phase','unknown') <> 'unknown' THEN
            RAISE EXCEPTION 'assistant_text phase must be unknown' USING ERRCODE='23514';
          ELSIF NEW.item_kind='assistant_commentary'
             AND COALESCE(NEW.metadata_json->'v2_payload'->>'phase','commentary') <> 'commentary' THEN
            RAISE EXCEPTION 'assistant_commentary phase must be commentary' USING ERRCODE='23514';
          ELSIF NEW.item_kind='assistant_reasoning_summary'
             AND COALESCE(NEW.metadata_json->'v2_payload'->>'phase','reasoning_summary') <> 'reasoning_summary' THEN
            RAISE EXCEPTION 'assistant_reasoning_summary phase must be reasoning_summary' USING ERRCODE='23514';
          ELSIF NEW.item_kind='assistant_reasoning_private'
             AND COALESCE(NEW.metadata_json->'v2_payload'->>'phase','reasoning_private') <> 'reasoning_private' THEN
            RAISE EXCEPTION 'assistant_reasoning_private phase must be reasoning_private' USING ERRCODE='23514';
          ELSIF NEW.item_kind='assistant_final'
             AND COALESCE(NEW.metadata_json->'v2_payload'->>'phase','final') <> 'final' THEN
            RAISE EXCEPTION 'assistant_final phase must be final' USING ERRCODE='23514';
          ELSIF NEW.item_kind NOT IN (
            'assistant_text','assistant_commentary','assistant_reasoning_summary',
            'assistant_reasoning_private','assistant_final'
          )
             AND NEW.metadata_json->'v2_payload' ? 'phase' THEN
            RAISE EXCEPTION 'assistant phase is illegal for this item kind' USING ERRCODE='23514';
          END IF;
          IF NEW.item_kind='hook' THEN
            boundary := NEW.metadata_json->'v2_payload'->>'boundary';
            source_value := COALESCE(NEW.metadata_json->'v2_payload'->>'source','');
            hook_rule := hook_matrix->boundary;
            IF hook_rule IS NULL OR NOT (hook_rule->'lifecycles' ? NEW.lifecycle)
               OR NOT (hook_rule->'scopes' ? level)
               OR (jsonb_array_length(hook_rule->'sources') > 0
                   AND NOT (hook_rule->'sources' ? source_value))
               OR (jsonb_array_length(hook_rule->'sources') = 0
                   AND NEW.metadata_json->'v2_payload' ? 'source') THEN
              RAISE EXCEPTION 'illegal SessionEventV2 hook boundary tuple' USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$
    """


def build_session_writer_epoch_function_sql() -> str:
    """Reject new/late writers from every RuntimeTask mutation at the DB fence."""

    return """
        CREATE OR REPLACE FUNCTION public.enforce_session_writer_epoch()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE epoch public.session_writer_epochs%ROWTYPE;
        BEGIN
          SELECT * INTO epoch FROM public.session_writer_epochs WHERE id='global';
          IF NOT FOUND OR epoch.enforcement_mode='observe' THEN RETURN NEW; END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.writer_generation <> epoch.new_run_generation
               OR NOT epoch.allowed_existing_generations_json @> to_jsonb(ARRAY[NEW.writer_generation]) THEN
              RAISE EXCEPTION 'writer_epoch_rejected new run generation' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END IF;
          IF NEW.writer_generation <> OLD.writer_generation THEN
            RAISE EXCEPTION 'writer_generation is immutable' USING ERRCODE='23514';
          END IF;
          IF NOT epoch.allowed_existing_generations_json @> to_jsonb(ARRAY[OLD.writer_generation]) THEN
            RAISE EXCEPTION 'writer_epoch_rejected late runtime mutation' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
        $$
    """


_SESSION_V2_AUTHORITY_RULES: tuple[tuple[str, str], ...] = (
    ("session_event_cursors", "TRUE"),
    (
        "session_event_outbox",
        """EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS event
          WHERE event.id=NEW.event_id
            AND event.tenant_id=NEW.tenant_id
            AND event.session_id=NEW.session_id
            AND event.sequence=NEW.sequence
        )""",
    ),
    (
        "session_commands",
        """NEW.causation_command_id IS NULL OR EXISTS (
          SELECT 1 FROM public.session_commands AS cause
          WHERE cause.id=NEW.causation_command_id
            AND cause.tenant_id=NEW.tenant_id
            AND cause.session_id=NEW.session_id
        )""",
    ),
    (
        "session_turn_inputs",
        """EXISTS (
          SELECT 1 FROM public.session_commands AS command
          WHERE command.id=NEW.command_id
            AND command.tenant_id=NEW.tenant_id
            AND command.session_id=NEW.session_id
        )
        AND (NEW.target_run_id IS NULL OR EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.target_run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
        ))
        AND (NEW.request_item_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS request_event
          WHERE request_event.item_id=NEW.request_item_id
            AND request_event.tenant_id=NEW.tenant_id
            AND request_event.session_id=NEW.session_id
        ))
        AND (NEW.fork_after_sequence IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS fork_event
          WHERE fork_event.sequence=NEW.fork_after_sequence
            AND fork_event.tenant_id=NEW.tenant_id
            AND fork_event.session_id=NEW.session_id
        ))""",
    ),
    (
        "session_input_admissions",
        """EXISTS (
          SELECT 1
          FROM public.session_commands AS command
          JOIN public.session_turn_inputs AS turn_input
            ON turn_input.id=NEW.input_id
           AND turn_input.command_id=command.id
           AND turn_input.tenant_id=command.tenant_id
           AND turn_input.session_id=command.session_id
          WHERE command.id=NEW.command_id
            AND command.tenant_id=NEW.tenant_id
            AND command.session_id=NEW.session_id
            AND NEW.input_revision > 0
            AND NEW.input_revision <= turn_input.revision
        )
        AND (NEW.hook_item_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS hook_event
          WHERE hook_event.item_id=NEW.hook_item_id
            AND hook_event.tenant_id=NEW.tenant_id
            AND hook_event.session_id=NEW.session_id
            AND hook_event.item_kind='hook'
        ))""",
    ),
    (
        "session_carry_forwards",
        """EXISTS (
          SELECT 1
          FROM public.session_input_admissions AS admission
          JOIN public.session_turn_inputs AS turn_input
            ON turn_input.id=admission.input_id
           AND turn_input.tenant_id=admission.tenant_id
           AND turn_input.session_id=admission.session_id
          WHERE admission.id=NEW.source_admission_id
            AND admission.input_id=NEW.source_input_id
            AND admission.hook_run_id=NEW.source_hook_run_id
            AND admission.tenant_id=NEW.tenant_id
            AND admission.session_id=NEW.session_id
        )
        AND (NEW.target_turn_id IS NULL OR EXISTS (
          SELECT 1 FROM public.session_model_results AS target_result
          WHERE target_result.tenant_id=NEW.tenant_id
            AND target_result.session_id=NEW.session_id
            AND target_result.turn_id=NEW.target_turn_id
        ) OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS target_event
          WHERE target_event.tenant_id=NEW.tenant_id
            AND target_event.session_id=NEW.session_id
            AND target_event.scope_json->>'turn_id'=NEW.target_turn_id
        ))
        AND (NEW.target_round_id IS NULL OR EXISTS (
          SELECT 1 FROM public.session_model_results AS target_result
          WHERE target_result.tenant_id=NEW.tenant_id
            AND target_result.session_id=NEW.session_id
            AND target_result.turn_id=NEW.target_turn_id
            AND target_result.round_id=NEW.target_round_id
        ))
        AND (NEW.consumed_event_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS consumed_event
          WHERE consumed_event.id=NEW.consumed_event_id
            AND consumed_event.item_id=NEW.context_source_item_id
            AND consumed_event.tenant_id=NEW.tenant_id
            AND consumed_event.session_id=NEW.session_id
            AND consumed_event.scope_json->>'turn_id'=NEW.target_turn_id
            AND consumed_event.scope_json->>'round_id'=NEW.target_round_id
        ))""",
    ),
    (
        "session_control_inputs",
        """EXISTS (
          SELECT 1 FROM public.session_commands AS command
          WHERE command.id=NEW.command_id
            AND command.tenant_id=NEW.tenant_id
            AND command.session_id=NEW.session_id
        )
        AND EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.expected_run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
        )
        AND (NEW.request_item_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS request_event
          WHERE request_event.item_id=NEW.request_item_id
            AND request_event.tenant_id=NEW.tenant_id
            AND request_event.session_id=NEW.session_id
        ))""",
    ),
    (
        "session_turn_replacements",
        """EXISTS (
          SELECT 1
          FROM public.session_commands AS saga_command
          JOIN public.session_commands AS parent_command
            ON parent_command.id=saga_command.causation_command_id
           AND parent_command.tenant_id=saga_command.tenant_id
           AND parent_command.session_id=saga_command.session_id
          JOIN public.session_turn_inputs AS replacement_input
            ON replacement_input.id=NEW.replacement_input_id
           AND replacement_input.command_id=parent_command.id
           AND replacement_input.tenant_id=saga_command.tenant_id
           AND replacement_input.session_id=saga_command.session_id
          JOIN public.session_input_admissions AS admission
            ON admission.input_id=replacement_input.id
           AND admission.command_id=parent_command.id
           AND admission.tenant_id=saga_command.tenant_id
           AND admission.session_id=saga_command.session_id
           AND admission.input_revision=replacement_input.revision
          WHERE saga_command.id=NEW.command_id
            AND saga_command.namespace='turn_replacement'
            AND parent_command.namespace='human_input'
            AND parent_command.command_kind='interrupt_and_replace'
            AND replacement_input.intent='interrupt_and_replace'
            AND (
              (NEW.state IN ('requested','cancel_accepted','old_run_fenced')
               AND replacement_input.target_turn_id=NEW.old_turn_id
               AND replacement_input.target_run_id=NEW.old_run_id)
              OR
              (NEW.state IN ('replacement_queued','replacement_admitted','completed','failed','needs_reconciliation')
               AND replacement_input.target_turn_id IN (NEW.old_turn_id,NEW.replacement_turn_id))
            )
            AND admission.state='admitted'
            AND saga_command.tenant_id=NEW.tenant_id
            AND saga_command.session_id=NEW.session_id
        )
        AND (
          (NEW.state IN ('requested','needs_reconciliation')
           AND NEW.cancel_control_id IS NULL
           AND NEW.cancel_command_id IS NULL)
          OR
          (NEW.state<>'requested'
           AND NEW.cancel_control_id IS NOT NULL
           AND NEW.cancel_command_id IS NOT NULL
           AND EXISTS (
             SELECT 1
             FROM public.session_commands AS saga_command
             JOIN public.session_commands AS cancel_command
               ON cancel_command.id=NEW.cancel_command_id
              AND cancel_command.causation_command_id=saga_command.id
              AND cancel_command.tenant_id=saga_command.tenant_id
              AND cancel_command.session_id=saga_command.session_id
              AND cancel_command.namespace='control_input'
             JOIN public.session_control_inputs AS cancel_control
               ON cancel_control.id=NEW.cancel_control_id
              AND cancel_control.command_id=cancel_command.id
              AND cancel_control.expected_run_id=NEW.old_run_id
              AND cancel_control.tenant_id=saga_command.tenant_id
              AND cancel_control.session_id=saga_command.session_id
             WHERE saga_command.id=NEW.command_id
               AND saga_command.tenant_id=NEW.tenant_id
               AND saga_command.session_id=NEW.session_id
           ))
        )
        AND EXISTS (
          SELECT 1 FROM public.runtime_tasks AS old_run
          WHERE old_run.id=NEW.old_run_id
            AND old_run.tenant_id=NEW.tenant_id
            AND old_run.parent_session_id=NEW.session_id::text
            AND old_run.parent_agent_id=bound_agent_id
            AND COALESCE(
                  NULLIF(old_run.metadata_json->>'turn_id',''),
                  'turn-' || replace(old_run.id::text,'-','')
                )=NEW.old_turn_id
        )
        AND (NEW.last_event_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS last_event
          WHERE last_event.id=NEW.last_event_id
            AND last_event.tenant_id=NEW.tenant_id
            AND last_event.session_id=NEW.session_id
        ))""",
    ),
    (
        "session_tool_invocations",
        """EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
        )
        AND EXISTS (
          SELECT 1 FROM public.session_model_results AS result
          WHERE result.tenant_id=NEW.tenant_id
            AND result.session_id=NEW.session_id
            AND result.run_id=NEW.run_id
            AND result.round_id=NEW.round_id
        )
        AND (NEW.result_event_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS result_event
          WHERE result_event.id=NEW.result_event_id
            AND result_event.item_kind='tool_result'
            AND result_event.lifecycle='completed'
            AND result_event.invocation_id=NEW.id
            AND result_event.provider_tool_use_id=NEW.provider_tool_use_id
            AND result_event.tenant_id=NEW.tenant_id
            AND result_event.session_id=NEW.session_id
            AND result_event.run_id=NEW.run_id
            AND result_event.scope_json->>'round_id'=NEW.round_id
        ))""",
    ),
    (
        "session_model_results",
        """EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
            AND COALESCE(
                  NULLIF(run.metadata_json->>'turn_id',''),
                  'turn-' || replace(run.id::text,'-','')
                )=NEW.turn_id
        )
        AND jsonb_typeof(NEW.bound_input_ids_json)='array'
        AND NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(NEW.bound_input_ids_json) AS input_ref(value)
          LEFT JOIN public.session_turn_inputs AS bound_input
            ON bound_input.id=CASE
                 WHEN input_ref.value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                 THEN input_ref.value::uuid
               END
           AND bound_input.tenant_id=NEW.tenant_id
           AND bound_input.session_id=NEW.session_id
          WHERE bound_input.id IS NULL
        )
        AND (NEW.round_committed_event_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS commit_event
          WHERE commit_event.id=NEW.round_committed_event_id
            AND commit_event.result_id=NEW.id
            AND commit_event.tenant_id=NEW.tenant_id
            AND commit_event.session_id=NEW.session_id
            AND commit_event.run_id=NEW.run_id
            AND commit_event.scope_json->>'turn_id'=NEW.turn_id
            AND commit_event.scope_json->>'round_id'=NEW.round_id
        ))""",
    ),
    (
        "session_round_obligations",
        """EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
        )
        AND EXISTS (
          SELECT 1 FROM public.session_model_results AS source_result
          WHERE source_result.id=NEW.source_result_id
            AND source_result.tenant_id=NEW.tenant_id
            AND source_result.session_id=NEW.session_id
            AND source_result.run_id=NEW.run_id
            AND source_result.turn_id=NEW.turn_id
        )""",
    ),
    (
        "session_next_round_plans",
        """EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
        )
        AND EXISTS (
          SELECT 1 FROM public.session_model_results AS source_result
          WHERE source_result.id=NEW.source_result_id
            AND source_result.tenant_id=NEW.tenant_id
            AND source_result.session_id=NEW.session_id
            AND source_result.run_id=NEW.run_id
        )
        AND jsonb_typeof(NEW.obligation_ids_json)='array'
        AND NOT EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(NEW.obligation_ids_json) AS obligation_ref(value)
          LEFT JOIN public.session_round_obligations AS obligation
            ON obligation.id=CASE
                 WHEN obligation_ref.value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                 THEN obligation_ref.value::uuid
               END
           AND obligation.tenant_id=NEW.tenant_id
           AND obligation.session_id=NEW.session_id
           AND obligation.run_id=NEW.run_id
           AND obligation.source_result_id=NEW.source_result_id
          WHERE obligation.id IS NULL
        )""",
    ),
    (
        "session_run_outcomes",
        """EXISTS (
          SELECT 1 FROM public.runtime_tasks AS run
          WHERE run.id=NEW.run_id
            AND run.tenant_id=NEW.tenant_id
            AND run.parent_session_id=NEW.session_id::text
            AND run.parent_agent_id=bound_agent_id
        )
        AND EXISTS (
          SELECT 1 FROM public.session_model_results AS terminal_result
          WHERE terminal_result.id=NEW.terminal_result_id
            AND terminal_result.tenant_id=NEW.tenant_id
            AND terminal_result.session_id=NEW.session_id
            AND terminal_result.run_id=NEW.run_id
            AND terminal_result.turn_id=NEW.turn_id
        )
        AND (NEW.terminal_event_id IS NULL OR EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS terminal_event
          WHERE terminal_event.id=NEW.terminal_event_id
            AND terminal_event.result_id=NEW.terminal_result_id
            AND terminal_event.tenant_id=NEW.tenant_id
            AND terminal_event.session_id=NEW.session_id
            AND terminal_event.run_id=NEW.run_id
            AND terminal_event.scope_json->>'turn_id'=NEW.turn_id
        ))""",
    ),
    (
        "session_feedback_aggregates",
        """EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS target_event
          WHERE target_event.item_id=NEW.target_item_id
            AND target_event.tenant_id=NEW.tenant_id
            AND target_event.session_id=NEW.session_id
            AND (NEW.target_result_id IS NULL OR target_event.result_id=NEW.target_result_id)
        )
        AND (NEW.target_result_id IS NULL OR EXISTS (
          SELECT 1 FROM public.session_model_results AS target_result
          WHERE target_result.id=NEW.target_result_id
            AND target_result.tenant_id=NEW.tenant_id
            AND target_result.session_id=NEW.session_id
        ))
        AND EXISTS (
          SELECT 1 FROM public.chat_transcript_events AS mutation_event
          WHERE mutation_event.item_id=NEW.last_mutation_item_id
            AND mutation_event.item_kind='evaluation_feedback_mutation'
            AND mutation_event.tenant_id=NEW.tenant_id
            AND mutation_event.session_id=NEW.session_id
        )""",
    ),
)

SESSION_V2_AUTHORITY_TABLES = tuple(table_name for table_name, _rule in _SESSION_V2_AUTHORITY_RULES)


def build_session_tenant_binding_function_sql() -> str:
    """Bind each Session V2 row and every typed reference to one authority frame."""

    branches = []
    for index, (table_name, condition) in enumerate(_SESSION_V2_AUTHORITY_RULES):
        keyword = "IF" if index == 0 else "ELSIF"
        branches.append(
            f"""          {keyword} TG_TABLE_NAME='{table_name}' THEN
            SELECT ({condition}) INTO authority_ok;"""
        )
    authority_branches = "\n".join(branches)
    return f"""
        CREATE OR REPLACE FUNCTION public.enforce_session_v2_tenant_binding()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
          bound_tenant_id uuid;
          bound_agent_id uuid;
          authority_ok boolean := FALSE;
        BEGIN
          IF TG_OP='UPDATE' AND (
            OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
            OR OLD.session_id IS DISTINCT FROM NEW.session_id
          ) THEN
            RAISE EXCEPTION 'session_v2_authority_frame_immutable' USING ERRCODE='23514';
          END IF;
          SELECT tenant_id,agent_id INTO bound_tenant_id,bound_agent_id
          FROM public.chat_sessions WHERE id=NEW.session_id;
          IF NOT FOUND OR NEW.tenant_id IS DISTINCT FROM bound_tenant_id THEN
            RAISE EXCEPTION 'session_v2_tenant_binding_mismatch' USING ERRCODE='23514';
          END IF;
{authority_branches}
          ELSE
            authority_ok := FALSE;
          END IF;
          IF NOT COALESCE(authority_ok,FALSE) THEN
            RAISE EXCEPTION 'session_v2_authority_binding_mismatch' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
        $$
    """


_SCOPE_FIELDS = {level: fields - {"level"} for level, fields in SCOPE_FIELDS.items()}
_ACTOR_TYPES = ACTOR_TYPES
_AUDIENCES = AUDIENCES


def _value(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    return clean or None


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return _text(value) or datetime.now(timezone.utc).isoformat()


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SessionEventContractError(f"{name} must be an object")
    return value


def _decode_json_pointer_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        if char != "~":
            decoded.append(char)
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise SessionEventContractError("redaction path contains an invalid JSON Pointer escape")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _redaction_pointer_tokens(pointer: Any) -> tuple[str, ...]:
    if not isinstance(pointer, str) or not pointer.startswith("/payload/"):
        raise SessionEventContractError("redaction path must be an absolute /payload JSON Pointer")
    raw_tokens = pointer.split("/")[1:]
    tokens = tuple(_decode_json_pointer_token(token) for token in raw_tokens)
    if not tokens or tokens[0] != "payload" or any(token == "" for token in tokens):
        raise SessionEventContractError("redaction path contains an empty or invalid segment")
    return tokens


def _validated_redaction_paths(
    visibility: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[tuple[str, tuple[str, ...]]]:
    raw_paths = visibility.get("redaction_paths", [])
    if not isinstance(raw_paths, (list, tuple)):
        raise SessionEventContractError("visibility.redaction_paths must be an array")
    validated: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for raw_pointer in raw_paths:
        pointer = str(raw_pointer) if isinstance(raw_pointer, str) else raw_pointer
        tokens = _redaction_pointer_tokens(pointer)
        if pointer in seen:
            raise SessionEventContractError("redaction paths must be unique")
        seen.add(pointer)
        node: Any = payload
        relative_tokens = tokens[1:]
        for token in relative_tokens[:-1]:
            if isinstance(node, Mapping):
                if token not in node:
                    raise SessionEventContractError(f"redaction path is out of bounds: {pointer}")
                node = node[token]
            elif isinstance(node, (list, tuple)):
                if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                    raise SessionEventContractError(f"redaction path has an invalid array index: {pointer}")
                item_index = int(token)
                if item_index >= len(node):
                    raise SessionEventContractError(f"redaction path is out of bounds: {pointer}")
                node = node[item_index]
            else:
                raise SessionEventContractError(f"redaction path crosses a scalar: {pointer}")
        leaf = relative_tokens[-1]
        if not isinstance(node, Mapping) or leaf not in node:
            raise SessionEventContractError(f"redaction path must resolve to an existing object field: {pointer}")
        validated.append((pointer, relative_tokens))

    token_paths = [tokens for _pointer, tokens in validated]
    for index, tokens in enumerate(token_paths):
        for other_index, other in enumerate(token_paths):
            if index != other_index and len(tokens) < len(other) and other[: len(tokens)] == tokens:
                raise SessionEventContractError("redaction paths may not overlap")
    return validated


def validate_session_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate an exact V2 envelope and return the same object."""

    if event.get("schema") != SESSION_EVENT_SCHEMA or event.get("schema_version") != SESSION_EVENT_SCHEMA_VERSION:
        raise SessionEventContractError("unsupported session event schema")
    item_kind = str(event.get("item_kind") or "")
    lifecycle = str(event.get("lifecycle") or "")
    rule = EVENT_KIND_MATRIX.get(item_kind)
    if rule is None or lifecycle not in rule.lifecycles:
        raise SessionEventContractError(f"illegal event kind/lifecycle: {item_kind}.{lifecycle}")
    expected_kind = f"{item_kind}.{lifecycle}"
    if event.get("kind") != expected_kind:
        raise SessionEventContractError(f"kind must equal {expected_kind}")
    expected_payload_schema = f"hive.session.payload.{item_kind}.{lifecycle}.v2"
    if event.get("payload_schema") != expected_payload_schema:
        raise SessionEventContractError(f"payload_schema must equal {expected_payload_schema}")

    scope = _require_mapping(event.get("scope"), "scope")
    level = str(scope.get("level") or "")
    if level not in rule.scopes:
        raise SessionEventContractError(f"{expected_kind} cannot use {level!r} scope")
    required_scope_fields = _SCOPE_FIELDS[level]
    for field_name in required_scope_fields:
        if not _text(scope.get(field_name)):
            raise SessionEventContractError(f"scope.{field_name} is required for {level}")
    forbidden = set().union(*_SCOPE_FIELDS.values()) - required_scope_fields
    if any(_text(scope.get(field_name)) for field_name in forbidden):
        raise SessionEventContractError(f"scope contains fields outside {level} scope")
    if _text(scope.get("thread_id")) != _text(scope.get("session_id")):
        raise SessionEventContractError("scope.thread_id must equal scope.session_id")
    event_run_id = _text(event.get("run_id"))
    scope_run_id = _text(scope.get("run_id"))
    if level in {"session", "turn"} and event_run_id is not None:
        raise SessionEventContractError("run_id is forbidden outside run or round scope")
    if level in {"run", "round"} and event_run_id != scope_run_id:
        raise SessionEventContractError("run_id must equal scope.run_id")

    if not _text(event.get("event_id")) or int(event.get("sequence") or 0) <= 0:
        raise SessionEventContractError("event_id and positive sequence are required")
    if not _text(event.get("tenant_id")) or not _text(event.get("item_id")):
        raise SessionEventContractError("tenant_id and item_id are required")
    actor = _require_mapping(event.get("actor"), "actor")
    if actor.get("type") not in _ACTOR_TYPES:
        raise SessionEventContractError("unsupported actor.type")
    visibility = _require_mapping(event.get("visibility"), "visibility")
    if visibility.get("audience") not in _AUDIENCES:
        raise SessionEventContractError("unsupported visibility.audience")
    payload = _require_mapping(event.get("payload"), "payload")
    _validated_redaction_paths(visibility, payload)

    if item_kind == "hook":
        boundary = str(payload.get("boundary") or "")
        boundary_rule = HOOK_BOUNDARY_MATRIX.get(boundary)
        if boundary_rule is None:
            raise SessionEventContractError(f"unsupported hook boundary: {boundary!r}")
        if level not in boundary_rule.scopes or lifecycle not in boundary_rule.lifecycles:
            raise SessionEventContractError(f"illegal hook boundary tuple: {boundary}/{level}/{lifecycle}")
        if boundary_rule.sources is not None:
            if payload.get("source") not in boundary_rule.sources:
                raise SessionEventContractError(f"unsupported hook source for {boundary}")
        elif "source" in payload:
            raise SessionEventContractError(f"hook source is not legal for {boundary}")

    phase = payload.get("phase")
    expected_phase = ASSISTANT_PHASES.get(item_kind)
    if expected_phase is not None and phase not in {None, expected_phase}:
        raise SessionEventContractError(f"{item_kind} phase must be {expected_phase}")
    if expected_phase is None and phase is not None:
        raise SessionEventContractError("assistant phase is not legal for this item kind")
    return event


def _legacy_kind(row: Any, metadata: Mapping[str, Any]) -> str | None:
    event_type = str(_value(row, "event_type", "") or "").strip().lower()
    item_type = str(_value(row, "item_type", "") or "").strip().lower()
    explicit_phase = str(metadata.get("phase") or "").strip().lower()
    if event_type in {"assistant_delta", "assistant_message", "response_repair"} or item_type == "agent_message":
        if explicit_phase == "final":
            return "assistant_final"
        if explicit_phase == "commentary":
            return "assistant_commentary"
        return "assistant_text"
    if event_type in {"reasoning", "thinking"} or item_type == "reasoning":
        return "assistant_reasoning_summary"
    explicit = {
        "tool_call": "tool_call",
        "tool_result": "tool_result",
        "context_compaction": "context_compaction",
        "workflow": "workflow_run",
        "workflow_step": "workflow_step",
        "subagent": "subagent",
        "artifact": "artifact",
        "file_change": "file_change",
        "hook": "hook",
        "user_message": "human_input",
        "run_started": "run",
        "run_completed": "run",
        "run_failed": "run",
        "run_cancelled": "run",
    }
    return explicit.get(event_type) or explicit.get(item_type)


def _legacy_lifecycle(row: Any, item_kind: str, metadata: Mapping[str, Any]) -> str | None:
    explicit = str(metadata.get("lifecycle") or "").strip().lower()
    if explicit in EVENT_KIND_MATRIX[item_kind].lifecycles:
        return explicit
    event_type = str(_value(row, "event_type", "") or "").strip().lower()
    item_status = str(_value(row, "item_status", "") or "").strip().lower()
    if item_kind == "tool_result":
        return "completed"
    if item_kind == "human_input":
        return "accepted"
    if item_kind == "run":
        return {
            "run_started": "running",
            "run_completed": "completed",
            "run_failed": "failed",
            "run_cancelled": "cancelled",
        }.get(event_type, "running")
    candidate = {
        "running": "delta" if item_kind.startswith("assistant_") else "started",
        "succeeded": "completed",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "waiting_user": "waiting",
    }.get(item_status)
    if candidate in EVENT_KIND_MATRIX[item_kind].lifecycles:
        return candidate
    if event_type in {"assistant_delta", "thinking"} and "delta" in EVENT_KIND_MATRIX[item_kind].lifecycles:
        return "delta"
    return None


def _legacy_scope(row: Any, item_kind: str, metadata: Mapping[str, Any]) -> dict[str, str] | None:
    session_id = _text(_value(row, "session_id")) or ""
    turn_id = _text(_value(row, "turn_id")) or _text(metadata.get("turn_id"))
    run_id = _text(_value(row, "run_id")) or _text(metadata.get("run_id"))
    round_id = _text(_value(row, "round_id")) or _text(metadata.get("round_id"))
    if not session_id:
        return None
    available = {
        "session": bool(session_id),
        "turn": bool(session_id and turn_id),
        "run": bool(session_id and turn_id and run_id),
        "round": bool(session_id and turn_id and run_id and round_id),
    }
    level = next(
        (
            candidate
            for candidate in ("round", "run", "turn", "session")
            if candidate in EVENT_KIND_MATRIX[item_kind].scopes and available[candidate]
        ),
        None,
    )
    if level is None:
        return None
    scope = {"level": level, "session_id": session_id, "thread_id": session_id}
    if level in {"turn", "run", "round"}:
        scope["turn_id"] = turn_id
    if level in {"run", "round"}:
        scope["run_id"] = run_id
    if level == "round":
        scope["round_id"] = round_id
    return scope


def _compatibility_event(
    row: Any,
    *,
    metadata: Mapping[str, Any],
    projected_metadata: Mapping[str, Any],
    redacted_fields: list[str],
    legacy_kind: str | None,
    legacy_lifecycle: str | None,
    reason: str,
) -> dict[str, Any]:
    """Preserve unprovable V1 evidence without presenting it as canonical V2."""

    event: dict[str, Any] = {
        "schema": SESSION_EVENT_COMPATIBILITY_SCHEMA,
        "schema_version": 1,
        "compatibility_status": "needs_reconciliation",
        "reason": reason,
        "legacy_kind": legacy_kind or "legacy_unknown",
        "legacy_lifecycle": legacy_lifecycle or "legacy_unknown",
        "legacy_event_type": _text(_value(row, "event_type")) or "legacy_unknown",
        "legacy_item_type": _text(_value(row, "item_type")) or "legacy_unknown",
        "legacy_item_status": _text(_value(row, "item_status")) or "legacy_unknown",
        "payload": {
            "content": str(_value(row, "content", "") or ""),
            "parts": list(_value(row, "parts_json", []) or []),
            "metadata": dict(projected_metadata),
        },
        "occurred_at": _iso(_value(row, "created_at")),
    }
    for target, source in (
        ("event_id", "id"),
        ("sequence", "sequence"),
        ("tenant_id", "tenant_id"),
        ("session_id", "session_id"),
        ("run_id", "run_id"),
        ("turn_id", "turn_id"),
        ("item_id", "item_id"),
    ):
        value = _value(row, source)
        if value is not None and str(value):
            event[target] = int(value) if target == "sequence" else str(value)
    if redacted_fields:
        event["redacted_fields"] = list(redacted_fields)
    return event


def _serialize_v2_row(row: Any, *, audience: str) -> dict[str, Any]:
    metadata = dict(_value(row, "metadata_json", {}) or {})
    payload = dict(_require_mapping(metadata.get("v2_payload") or {}, "payload"))
    actor = dict(metadata.get("actor") or {"type": _text(_value(row, "actor_type")) or "system"})
    visibility = dict(
        metadata.get("visibility") or {"audience": _text(_value(row, "visibility_scope")) or "direct_user"}
    )
    if audience == "operator":
        visibility["audience"] = "operator"
    created_at = _iso(_value(row, "created_at"))
    event: dict[str, Any] = {
        "schema": SESSION_EVENT_SCHEMA,
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "event_id": _text(_value(row, "id")) or "",
        "sequence": int(_value(row, "sequence", 0) or 0),
        "tenant_id": _text(_value(row, "tenant_id")) or "",
        "scope": dict(_value(row, "scope_json", {}) or {}),
        "item_id": _text(_value(row, "item_id")) or "",
        "item_kind": _text(_value(row, "item_kind")) or "",
        "kind": _text(_value(row, "event_type")) or "",
        "lifecycle": _text(_value(row, "lifecycle")) or "",
        "payload_schema": _text(_value(row, "payload_schema")) or "",
        "actor": actor,
        "visibility": visibility,
        "payload": payload,
        "occurred_at": created_at,
        "persisted_at": _text(metadata.get("v2_persisted_at")) or created_at,
    }
    if _value(row, "run_id") is not None:
        event["run_id"] = _text(_value(row, "run_id"))
    for target, source in (
        ("ordinal", "ordinal"),
        ("command_id", "command_id"),
        ("input_id", "input_id"),
        ("result_id", "result_id"),
        ("invocation_id", "invocation_id"),
        ("provider_tool_use_id", "provider_tool_use_id"),
        ("content_hash", "content_hash"),
        ("parent_item_id", "parent_item_id"),
        ("causation_event_id", "parent_event_id"),
        ("correlation_id", "correlation_id"),
    ):
        value = _value(row, source)
        if value is not None and str(value):
            event[target] = int(value) if target == "ordinal" else str(value)
    for key in ("display", "evidence_refs"):
        value = metadata.get(key)
        if value:
            event[key] = value
    validate_session_event(event)
    event["payload"], redacted_fields = _redacted_payload(
        payload,
        visibility=visibility,
        audience=audience,
    )
    if redacted_fields:
        event_visibility = dict(event["visibility"])
        event_visibility["redacted_fields"] = sorted(
            set([*event_visibility.get("redacted_fields", []), *redacted_fields])
        )
        event["visibility"] = event_visibility
    return event


def _redacted_payload(
    payload: Mapping[str, Any],
    *,
    visibility: Mapping[str, Any],
    audience: str,
) -> tuple[dict[str, Any], list[str]]:
    validated = _validated_redaction_paths(visibility, payload)
    projected = copy.deepcopy(dict(payload))
    if audience == "operator":
        return projected, []
    for pointer, tokens in validated:
        node: Any = projected
        for token in tokens[:-1]:
            node = node[int(token)] if isinstance(node, list) else node[token]
        del node[tokens[-1]]
    return projected, sorted(pointer for pointer, _tokens in validated)


def _redacted_metadata(
    metadata: Mapping[str, Any],
    *,
    visibility: Mapping[str, Any],
    audience: str,
) -> tuple[dict[str, Any], list[str]]:
    wrapper, redacted = _redacted_payload(
        {"metadata": dict(metadata)},
        visibility=visibility,
        audience=audience,
    )
    return dict(wrapper["metadata"]), redacted


def serialize_session_event(row: Any, *, audience: str = "operator") -> dict[str, Any]:
    """Serialize a V2 row, or explicitly adapt a legacy row without semantic guessing."""

    if isinstance(row, dict) and row.get("schema") == SESSION_EVENT_SCHEMA:
        event = copy.deepcopy(validate_session_event(row))
        event["payload"], redacted_fields = _redacted_payload(
            _require_mapping(event.get("payload"), "payload"),
            visibility=_require_mapping(event.get("visibility"), "visibility"),
            audience=audience,
        )
        if audience == "operator":
            return event
        visibility = dict(event["visibility"])
        if redacted_fields:
            visibility["redacted_fields"] = sorted(set([*visibility.get("redacted_fields", []), *redacted_fields]))
        event["visibility"] = visibility
        return event
    if int(_value(row, "schema_version", 1) or 1) == SESSION_EVENT_SCHEMA_VERSION:
        return _serialize_v2_row(row, audience=audience)
    metadata = dict(_value(row, "metadata_json", {}) or {})
    visibility_authority = (
        dict(metadata.get("visibility") or {}) if isinstance(metadata.get("visibility"), Mapping) else {}
    )
    projected_metadata, redacted_fields = _redacted_metadata(
        metadata,
        visibility=visibility_authority,
        audience=audience,
    )
    event_id = _text(_value(row, "event_id")) or _text(_value(row, "id")) or ""
    item_kind = _legacy_kind(row, metadata)
    lifecycle = _legacy_lifecycle(row, item_kind, metadata) if item_kind else None
    scope = _legacy_scope(row, item_kind, metadata) if item_kind and lifecycle else None
    if item_kind == "hook" and not _text(metadata.get("boundary")):
        scope = None
    if item_kind is None or lifecycle is None or scope is None:
        return _compatibility_event(
            row,
            metadata=metadata,
            projected_metadata=projected_metadata,
            redacted_fields=redacted_fields,
            legacy_kind=item_kind,
            legacy_lifecycle=lifecycle,
            reason=(
                "unmapped_legacy_kind"
                if item_kind is None
                else "unmapped_legacy_lifecycle"
                if lifecycle is None
                else "insufficient_legacy_scope"
            ),
        )
    item_id = _text(_value(row, "item_id")) or event_id
    payload: dict[str, Any] = {
        "content": str(_value(row, "content", "") or ""),
        "parts": list(_value(row, "parts_json", []) or []),
        "metadata": projected_metadata,
        "legacy": True,
    }
    if item_kind == "assistant_text":
        payload["phase"] = "unknown"
    elif item_kind == "assistant_commentary":
        payload["phase"] = "commentary"
    elif item_kind == "assistant_final":
        payload["phase"] = "final"
    if item_kind == "hook":
        payload["boundary"] = metadata["boundary"]

    actor_type = _text(_value(row, "actor_type")) or "system"
    if actor_type not in _ACTOR_TYPES:
        actor_type = "system"
    visibility_audience = (
        "operator" if audience == "operator" else (_text(_value(row, "visibility_scope")) or "direct_user")
    )
    if visibility_audience not in _AUDIENCES:
        visibility_audience = "direct_user"
    created_at = _iso(_value(row, "created_at"))
    event: dict[str, Any] = {
        "schema": SESSION_EVENT_SCHEMA,
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "sequence": int(_value(row, "sequence", 0) or 0),
        "tenant_id": _text(_value(row, "tenant_id")) or "",
        "scope": scope,
        "item_id": item_id,
        "item_kind": item_kind,
        "kind": f"{item_kind}.{lifecycle}",
        "lifecycle": lifecycle,
        "payload_schema": f"hive.session.payload.{item_kind}.{lifecycle}.v2",
        "actor": {"type": actor_type},
        "visibility": {"audience": visibility_audience},
        "payload": payload,
        "occurred_at": created_at,
        "persisted_at": created_at,
    }
    if _value(row, "run_id") is not None:
        event["run_id"] = _text(_value(row, "run_id"))
    for target, source in (
        ("ordinal", "ordinal"),
        ("command_id", "command_id"),
        ("input_id", "input_id"),
        ("result_id", "result_id"),
        ("invocation_id", "invocation_id"),
        ("provider_tool_use_id", "provider_tool_use_id"),
        ("content_hash", "content_hash"),
        ("parent_item_id", "parent_item_id"),
        ("causation_event_id", "parent_event_id"),
        ("correlation_id", "correlation_id"),
    ):
        value = _value(row, source)
        if value is not None and str(value) != "":
            event[target] = int(value) if target == "ordinal" else str(value)
    actor_id = _text(_value(row, "actor_id")) or _text(metadata.get("actor_user_id"))
    if actor_id:
        event["actor"]["id"] = actor_id
    if redacted_fields:
        event["visibility"]["redacted_fields"] = redacted_fields
    return validate_session_event(event)


@dataclass(frozen=True, slots=True)
class SessionItemV2:
    id: str
    kind: str
    scope: dict[str, Any]
    lifecycle: str
    terminal: bool
    revision: int
    visibility: dict[str, Any]
    first_sequence: int
    last_sequence: int
    parent_id: str | None = None
    assistant_phase: str | None = None
    invocation_id: str | None = None
    title: str | None = None
    summary: str | None = None
    content: str = ""
    detail_ref: str | None = None
    artifact_refs: tuple[str, ...] = ()
    child_session_refs: tuple[str, ...] = ()
    source_blocks: tuple[dict[str, Any], ...] = ()
    started_at: str | None = None
    completed_at: str | None = None
    last_ordinal: int | None = None

    @property
    def status(self) -> str:  # compatibility selector; lifecycle remains authoritative.
        return self.lifecycle


@dataclass(frozen=True, slots=True)
class SessionReducerState:
    items: dict[str, SessionItemV2] = field(default_factory=dict)
    seen_event_ids: frozenset[str] = frozenset()
    ignored_event_ids: tuple[str, ...] = ()


def _tuple_strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if item is not None and str(item))


def _reduce_one(state: SessionReducerState, raw_event: Any) -> SessionReducerState:
    event = (
        serialize_session_event(raw_event)
        if not (isinstance(raw_event, dict) and raw_event.get("schema") == SESSION_EVENT_SCHEMA)
        else validate_session_event(raw_event)
    )
    event_id = str(event["event_id"])
    if event_id in state.seen_event_ids:
        return state
    seen = state.seen_event_ids | {event_id}
    item_id = str(event["item_id"])
    lifecycle = str(event["lifecycle"])
    item_kind = str(event["item_kind"])
    sequence = int(event["sequence"])
    ordinal = int(event["ordinal"]) if event.get("ordinal") is not None else None
    prior = state.items.get(item_id)
    if prior is not None and prior.terminal:
        return SessionReducerState(state.items, frozenset(seen), (*state.ignored_event_ids, event_id))
    if prior is not None and ordinal is not None and prior.last_ordinal is not None and ordinal <= prior.last_ordinal:
        return SessionReducerState(state.items, frozenset(seen), (*state.ignored_event_ids, event_id))

    payload = dict(event.get("payload") or {})
    display = dict(event.get("display") or {})
    content_delta = str(payload.get("content") or "")
    content = (
        content_delta
        if prior is None
        else (content_delta if lifecycle == "snapshot" else f"{prior.content}{content_delta}")
    )
    metadata = dict(payload.get("metadata") or {})
    phase = {
        "assistant_text": "unknown",
        "assistant_commentary": "commentary",
        "assistant_final": "final",
    }.get(item_kind)
    rule = EVENT_KIND_MATRIX[item_kind]
    terminal = lifecycle in rule.terminal
    item = SessionItemV2(
        id=item_id,
        kind=item_kind,
        scope=dict(event["scope"]),
        lifecycle=lifecycle,
        terminal=terminal,
        revision=(prior.revision + 1) if prior else 1,
        visibility=dict(event.get("visibility") or {}),
        first_sequence=prior.first_sequence if prior else sequence,
        last_sequence=sequence,
        parent_id=_text(event.get("parent_item_id")),
        assistant_phase=phase,
        invocation_id=_text(event.get("invocation_id")),
        title=_text(display.get("title")) or (prior.title if prior else None),
        summary=_text(display.get("summary")) or (prior.summary if prior else None),
        content=content,
        detail_ref=_text(display.get("detail_ref")) or (prior.detail_ref if prior else None),
        artifact_refs=_tuple_strings(payload.get("artifact_refs") or metadata.get("artifact_refs"))
        or (prior.artifact_refs if prior else ()),
        child_session_refs=_tuple_strings(payload.get("child_session_refs") or metadata.get("child_session_refs"))
        or (prior.child_session_refs if prior else ()),
        source_blocks=tuple(dict(block) for block in payload.get("source_blocks", []) if isinstance(block, Mapping))
        or (prior.source_blocks if prior else ()),
        started_at=(prior.started_at if prior else event.get("occurred_at"))
        if lifecycle not in rule.terminal
        else (prior.started_at if prior else None),
        completed_at=event.get("occurred_at") if terminal else None,
        last_ordinal=ordinal if ordinal is not None else (prior.last_ordinal if prior else None),
    )
    items = dict(state.items)
    items[item_id] = item
    return SessionReducerState(items, frozenset(seen), state.ignored_event_ids)


def reduce_session_events(events: Iterable[Any]) -> SessionReducerState:
    state = SessionReducerState()
    for event in events:
        state = _reduce_one(state, event)
    return state
