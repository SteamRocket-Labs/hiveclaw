"""Add the Session V2 canonical command, event and recovery plane.

Revision ID: session_v2_0716
Revises: hr_runtime_authority_0715
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

from migration_snapshots.session_v2_contract_0716 import (
    SESSION_V2_AUTHORITY_TABLES,
    SESSION_V2_TRIGGER_FUNCTION_SIGNATURES,
    build_session_event_contract_function_sql,
    build_session_tenant_binding_function_sql,
    build_session_writer_epoch_function_sql,
)


revision = "session_v2_0716"
down_revision = "hr_runtime_authority_0715"
branch_labels = None
depends_on = None


SESSION_V2_TENANT_TABLES = SESSION_V2_AUTHORITY_TABLES

SESSION_V2_GLOBAL_TABLES = (
    "session_writer_heartbeats",
    "session_writer_epochs",
)

SESSION_V2_EVENT_COLUMNS = (
    "item_id",
    "item_kind",
    "lifecycle",
    "payload_schema",
    "scope_json",
    "ordinal",
    "command_id",
    "input_id",
    "result_id",
    "invocation_id",
    "provider_tool_use_id",
    "content_hash",
    "parent_item_id",
)

SESSION_V2_RUNTIME_TASK_COLUMNS = ("writer_generation",)

SESSION_V2_FUNCTIONS = SESSION_V2_TRIGGER_FUNCTION_SIGNATURES

SESSION_V2_QUERY_INDEXES = (
    ("ix_session_event_cursors_tenant_id", "session_event_cursors", "tenant_id"),
    ("ix_session_event_outbox_available_at", "session_event_outbox", "available_at"),
    ("ix_session_event_outbox_session_id", "session_event_outbox", "session_id"),
    ("ix_session_event_outbox_tenant_id", "session_event_outbox", "tenant_id"),
    ("ix_session_commands_principal_id", "session_commands", "principal_id"),
    ("ix_session_commands_session_id", "session_commands", "session_id"),
    ("ix_session_commands_tenant_id", "session_commands", "tenant_id"),
    ("ix_session_turn_inputs_session_id", "session_turn_inputs", "session_id"),
    ("ix_session_turn_inputs_tenant_id", "session_turn_inputs", "tenant_id"),
    ("ix_session_input_admissions_session_id", "session_input_admissions", "session_id"),
    ("ix_session_input_admissions_tenant_id", "session_input_admissions", "tenant_id"),
    ("ix_session_carry_forwards_session_id", "session_carry_forwards", "session_id"),
    ("ix_session_carry_forwards_tenant_id", "session_carry_forwards", "tenant_id"),
    ("ix_session_control_inputs_session_id", "session_control_inputs", "session_id"),
    ("ix_session_control_inputs_tenant_id", "session_control_inputs", "tenant_id"),
    ("ix_session_turn_replacements_session_id", "session_turn_replacements", "session_id"),
    ("ix_session_turn_replacements_tenant_id", "session_turn_replacements", "tenant_id"),
    ("ix_session_tool_invocations_run_id", "session_tool_invocations", "run_id"),
    ("ix_session_tool_invocations_session_id", "session_tool_invocations", "session_id"),
    ("ix_session_tool_invocations_tenant_id", "session_tool_invocations", "tenant_id"),
    ("ix_session_model_results_run_id", "session_model_results", "run_id"),
    ("ix_session_model_results_session_id", "session_model_results", "session_id"),
    ("ix_session_model_results_tenant_id", "session_model_results", "tenant_id"),
    ("ix_session_model_results_turn_id", "session_model_results", "turn_id"),
    ("ix_session_round_obligations_session_id", "session_round_obligations", "session_id"),
    ("ix_session_round_obligations_tenant_id", "session_round_obligations", "tenant_id"),
    ("ix_session_next_round_plans_session_id", "session_next_round_plans", "session_id"),
    ("ix_session_next_round_plans_tenant_id", "session_next_round_plans", "tenant_id"),
    ("ix_session_run_outcomes_session_id", "session_run_outcomes", "session_id"),
    ("ix_session_run_outcomes_tenant_id", "session_run_outcomes", "tenant_id"),
    ("ix_session_run_outcomes_turn_id", "session_run_outcomes", "turn_id"),
    ("ix_session_feedback_aggregates_session_id", "session_feedback_aggregates", "session_id"),
    ("ix_session_feedback_aggregates_tenant_id", "session_feedback_aggregates", "tenant_id"),
    ("ix_session_writer_heartbeats_last_seen_at", "session_writer_heartbeats", "last_seen_at"),
)

SESSION_V2_EXISTING_TRANSCRIPT_INDEXES = (
    (
        "ix_chat_transcript_events_item_id",
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_chat_transcript_events_item_id" '
        'ON public."chat_transcript_events" (item_id)',
    ),
    (
        "ix_chat_transcript_events_command_id",
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_chat_transcript_events_command_id" '
        'ON public."chat_transcript_events" (command_id)',
    ),
    (
        "ix_chat_transcript_events_input_id",
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_chat_transcript_events_input_id" '
        'ON public."chat_transcript_events" (input_id)',
    ),
    (
        "ix_chat_transcript_events_result_id",
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_chat_transcript_events_result_id" '
        'ON public."chat_transcript_events" (result_id)',
    ),
    (
        "ix_chat_transcript_events_invocation_id",
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_chat_transcript_events_invocation_id" '
        'ON public."chat_transcript_events" (invocation_id)',
    ),
    (
        "uq_chat_transcript_tool_result_invocation",
        'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "uq_chat_transcript_tool_result_invocation" '
        'ON public."chat_transcript_events" (session_id, invocation_id) '
        "WHERE schema_version = 2 AND item_kind = 'tool_result' AND lifecycle = 'completed'",
    ),
)

_TRANSCRIPT_INDEX_STATE_SQL = text(
    """
    SELECT index_row.indisvalid,index_row.indisready
    FROM pg_catalog.pg_index AS index_row
    JOIN pg_catalog.pg_class AS index_class
      ON index_class.oid=index_row.indexrelid
    JOIN pg_catalog.pg_namespace AS index_namespace
      ON index_namespace.oid=index_class.relnamespace
    JOIN pg_catalog.pg_class AS table_class
      ON table_class.oid=index_row.indrelid
    JOIN pg_catalog.pg_namespace AS table_namespace
      ON table_namespace.oid=table_class.relnamespace
    WHERE index_namespace.nspname='public'
      AND table_namespace.nspname='public'
      AND table_class.relname='chat_transcript_events'
      AND index_class.relname=:index_name
    """
)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_public_index(index_name: str) -> str:
    frozen_names = {name for name, _statement in SESSION_V2_EXISTING_TRANSCRIPT_INDEXES}
    if index_name not in frozen_names:
        raise ValueError(f"unknown Session V2 transcript index: {index_name}")
    return f"{_quote_identifier('public')}.{_quote_identifier(index_name)}"


def _transcript_index_state(index_name: str):
    return op.get_bind().execute(_TRANSCRIPT_INDEX_STATE_SQL, {"index_name": index_name}).mappings().one_or_none()


def _ensure_existing_transcript_indexes() -> None:
    """Recover invalid concurrent-build residue without rebuilding healthy siblings."""

    with op.get_context().autocommit_block():
        for index_name, create_sql in SESSION_V2_EXISTING_TRANSCRIPT_INDEXES:
            state = _transcript_index_state(index_name)
            if state is not None and (not state["indisvalid"] or not state["indisready"]):
                op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_qualified_public_index(index_name)}")
            op.execute(create_sql)
            rebuilt_state = _transcript_index_state(index_name)
            if rebuilt_state is None or not rebuilt_state["indisvalid"] or not rebuilt_state["indisready"]:
                raise RuntimeError(f"session_v2_transcript_index_rebuild_failed: {_qualified_public_index(index_name)}")


_DDL = (
    """
    CREATE TABLE IF NOT EXISTS session_commands (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        principal_id uuid NOT NULL,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        namespace varchar(40) NOT NULL,
        causation_command_id uuid NULL REFERENCES session_commands(id),
        idempotency_key varchar(200) NOT NULL,
        command_kind varchar(80) NOT NULL,
        request_hash varchar(64) NOT NULL,
        target_hash varchar(64) NOT NULL,
        request_json jsonb NOT NULL,
        target_json jsonb NOT NULL,
        status varchar(32) NOT NULL DEFAULT 'accepted',
        receipt_ref varchar(300) NOT NULL,
        rejection_json jsonb NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_commands_idempotency UNIQUE
            (tenant_id, principal_id, session_id, namespace, idempotency_key),
        CONSTRAINT ck_session_commands_namespace CHECK
            (namespace IN ('human_input','control_input','evaluation_feedback','turn_replacement')),
        CONSTRAINT ck_session_commands_status CHECK
            (status IN ('accepted','applied','rejected','failed','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_event_cursors (
        session_id uuid PRIMARY KEY REFERENCES chat_sessions(id) ON DELETE CASCADE,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        next_sequence bigint NOT NULL DEFAULT 1,
        version integer NOT NULL DEFAULT 1,
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_session_event_cursor_positive CHECK (next_sequence > 0)
    )
    """,
    "ALTER TABLE runtime_tasks ADD COLUMN IF NOT EXISTS writer_generation integer NOT NULL DEFAULT 1",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS item_id uuid",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS item_kind varchar(64)",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS lifecycle varchar(40)",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS payload_schema varchar(200)",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS scope_json jsonb",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS ordinal integer",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS command_id uuid REFERENCES session_commands(id)",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS input_id uuid",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS result_id uuid",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS invocation_id uuid",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS provider_tool_use_id varchar(300)",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS content_hash varchar(64)",
    "ALTER TABLE chat_transcript_events ADD COLUMN IF NOT EXISTS parent_item_id uuid",
    """
    CREATE TABLE IF NOT EXISTS session_event_outbox (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        event_id uuid NOT NULL REFERENCES chat_transcript_events(id) ON DELETE CASCADE,
        sequence bigint NOT NULL,
        envelope_json jsonb NOT NULL,
        envelope_sha256 varchar(64) NOT NULL,
        status varchar(24) NOT NULL DEFAULT 'pending',
        attempts integer NOT NULL DEFAULT 0,
        available_at timestamptz NOT NULL DEFAULT now(),
        claimed_by varchar(200),
        claim_expires_at timestamptz,
        last_error text,
        published_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_event_outbox_event UNIQUE (event_id),
        CONSTRAINT uq_session_event_outbox_session_sequence UNIQUE (session_id, sequence),
        CONSTRAINT ck_session_event_outbox_status CHECK (status IN ('pending','publishing','published','failed')),
        CONSTRAINT ck_session_event_outbox_sha CHECK (char_length(envelope_sha256) = 64)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_turn_inputs (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        command_id uuid NOT NULL REFERENCES session_commands(id) ON DELETE CASCADE,
        intent varchar(48) NOT NULL,
        content_parts_json jsonb NOT NULL,
        content_hash varchar(64) NOT NULL,
        target_turn_id varchar(200),
        target_run_id uuid REFERENCES runtime_tasks(id),
        request_item_id uuid,
        fork_after_sequence bigint,
        terminal_fallback varchar(32),
        queue_priority varchar(16) NOT NULL,
        queue_ordinal bigint NOT NULL,
        revision integer NOT NULL DEFAULT 1,
        status varchar(32) NOT NULL DEFAULT 'accepted',
        bound_round_id varchar(200),
        model_request_snapshot_ref varchar(300),
        rolled_over_to_turn_id varchar(200),
        settlement_ref varchar(300),
        recovery_owner varchar(200),
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_turn_inputs_command UNIQUE (command_id),
        CONSTRAINT uq_session_turn_inputs_fifo UNIQUE (session_id, queue_priority, queue_ordinal),
        CONSTRAINT ck_session_turn_inputs_intent CHECK (intent IN
          ('start_turn','steer_current_turn','queue_next_turn','interrupt_and_replace','answer_request','fork_side_thread')),
        CONSTRAINT ck_session_turn_inputs_priority CHECK (queue_priority IN ('now','next','later')),
        CONSTRAINT ck_session_turn_inputs_status CHECK (status IN
          ('accepted','queued','bound','applied','rolled_over','rejected','cancelled','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_input_admissions (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        command_id uuid NOT NULL REFERENCES session_commands(id) ON DELETE CASCADE,
        input_id uuid NOT NULL REFERENCES session_turn_inputs(id) ON DELETE CASCADE,
        state varchar(40) NOT NULL DEFAULT 'admission_pending',
        hook_run_id uuid NOT NULL,
        hook_idempotency_key varchar(200) NOT NULL,
        hook_result_hash varchar(64),
        hook_item_id uuid,
        additional_context_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
        carry_forward varchar(40) NOT NULL DEFAULT 'none',
        lease_owner varchar(200),
        lease_expires_at timestamptz,
        recovery_owner varchar(200),
        version integer NOT NULL DEFAULT 1,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_input_admissions_input UNIQUE (input_id),
        CONSTRAINT uq_session_input_admissions_hook_run UNIQUE (hook_run_id),
        CONSTRAINT ck_session_input_admissions_state CHECK (state IN
          ('admission_pending','hook_running','hook_result_committed','admitted','rejected','cancelled','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_carry_forwards (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        purpose varchar(64) NOT NULL DEFAULT 'prevented_prompt_context',
        source_admission_id uuid NOT NULL REFERENCES session_input_admissions(id) ON DELETE CASCADE,
        source_input_id uuid NOT NULL REFERENCES session_turn_inputs(id) ON DELETE CASCADE,
        source_hook_run_id uuid NOT NULL,
        source_evidence_refs_json jsonb NOT NULL DEFAULT '[]'::jsonb,
        context_source_item_id uuid NOT NULL,
        state varchar(32) NOT NULL DEFAULT 'pending',
        target_turn_id varchar(200),
        target_round_id varchar(200),
        claim_generation integer NOT NULL DEFAULT 0,
        claim_owner varchar(200),
        claim_lease_expires_at timestamptz,
        model_request_snapshot_ref varchar(300),
        consumed_event_id uuid REFERENCES chat_transcript_events(id),
        recovery_owner varchar(200),
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_carry_forward_source UNIQUE (tenant_id, source_admission_id, purpose),
        CONSTRAINT uq_session_carry_forward_context_item UNIQUE (tenant_id, context_source_item_id),
        CONSTRAINT ck_session_carry_forward_state CHECK (state IN
          ('pending','turn_claimed','round_bound','consumed','needs_reconciliation')),
        CONSTRAINT ck_session_carry_forward_consumed CHECK
          (state <> 'consumed' OR (target_turn_id IS NOT NULL AND target_round_id IS NOT NULL
           AND model_request_snapshot_ref IS NOT NULL AND consumed_event_id IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_control_inputs (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        command_id uuid NOT NULL REFERENCES session_commands(id) ON DELETE CASCADE,
        kind varchar(48) NOT NULL,
        expected_run_id uuid NOT NULL REFERENCES runtime_tasks(id),
        request_item_id uuid,
        request_version integer,
        authority_snapshot_hash varchar(64) NOT NULL,
        response_schema varchar(300),
        response_payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
        response_payload_hash varchar(64) NOT NULL,
        status varchar(32) NOT NULL DEFAULT 'accepted',
        settlement_ref varchar(300),
        recovery_owner varchar(200),
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_control_inputs_command UNIQUE (command_id),
        CONSTRAINT ck_session_control_inputs_kind CHECK (kind IN
          ('cancel_run','approval_response','permission_response','workflow_gate_response')),
        CONSTRAINT ck_session_control_inputs_status CHECK (status IN
          ('accepted','applying','applied','rejected','failed','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_turn_replacements (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        command_id uuid NOT NULL REFERENCES session_commands(id) ON DELETE CASCADE,
        old_turn_id varchar(200) NOT NULL,
        old_run_id uuid NOT NULL REFERENCES runtime_tasks(id),
        cancel_control_id uuid NOT NULL,
        cancel_command_id uuid NOT NULL REFERENCES session_commands(id),
        replacement_turn_id varchar(200) NOT NULL,
        replacement_input_id uuid NOT NULL REFERENCES session_turn_inputs(id),
        state varchar(40) NOT NULL DEFAULT 'requested',
        lease_owner varchar(200),
        lease_expires_at timestamptz,
        generation integer NOT NULL DEFAULT 1,
        last_event_id uuid REFERENCES chat_transcript_events(id),
        CONSTRAINT uq_session_turn_replacements_command UNIQUE (command_id),
        CONSTRAINT uq_session_turn_replacements_turn UNIQUE (replacement_turn_id),
        CONSTRAINT ck_session_turn_replacements_state CHECK (state IN
          ('requested','cancel_accepted','old_run_fenced','replacement_queued','replacement_admitted','completed','failed','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_tool_invocations (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        run_id uuid NOT NULL REFERENCES runtime_tasks(id) ON DELETE CASCADE,
        round_id varchar(200) NOT NULL,
        provider_request_id varchar(300) NOT NULL,
        provider_tool_use_id varchar(300) NOT NULL,
        invocation_item_id uuid NOT NULL,
        args_hash varchar(64) NOT NULL,
        authority_snapshot_hash varchar(64) NOT NULL,
        effect_idempotency_key varchar(300) NOT NULL,
        effect_state varchar(32) NOT NULL DEFAULT 'prepared_not_started',
        execution_fence_ref varchar(300),
        receipt_ref varchar(300),
        result_event_id uuid UNIQUE REFERENCES chat_transcript_events(id),
        recovery_owner varchar(200),
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_tool_provider_mapping UNIQUE (provider_request_id, provider_tool_use_id),
        CONSTRAINT uq_session_tool_effect_key UNIQUE (effect_idempotency_key),
        CONSTRAINT ck_session_tool_effect_state CHECK (effect_state IN
          ('prepared_not_started','effect_started','effect_committed','failed','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_model_results (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        turn_id varchar(200) NOT NULL,
        run_id uuid NOT NULL REFERENCES runtime_tasks(id) ON DELETE CASCADE,
        round_id varchar(200) NOT NULL,
        provider_request_id varchar(300) NOT NULL,
        state varchar(32) NOT NULL DEFAULT 'prepared',
        model_request_hash varchar(64) NOT NULL,
        model_request_snapshot_json jsonb NOT NULL,
        bound_input_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
        last_content_sequence bigint,
        seal_json jsonb,
        round_committed_event_id uuid REFERENCES chat_transcript_events(id),
        reconciliation_owner varchar(200),
        reconciliation_lease_expires_at timestamptz,
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_model_results_provider_request UNIQUE (provider_request_id),
        CONSTRAINT uq_session_model_results_round UNIQUE (run_id, round_id),
        CONSTRAINT ck_session_model_results_state CHECK (state IN
          ('prepared','streaming','sealed','round_committed','failed','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_round_obligations (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        turn_id varchar(200) NOT NULL,
        run_id uuid NOT NULL REFERENCES runtime_tasks(id) ON DELETE CASCADE,
        source_result_id uuid NOT NULL REFERENCES session_model_results(id) ON DELETE CASCADE,
        kind varchar(32) NOT NULL,
        source_generation integer NOT NULL,
        source_ref varchar(300) NOT NULL,
        payload_json jsonb NOT NULL,
        state varchar(32) NOT NULL DEFAULT 'pending',
        claim_owner varchar(200),
        claim_lease_expires_at timestamptz,
        settlement_ref varchar(300),
        recovery_owner varchar(200),
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_round_obligation_source UNIQUE
          (source_result_id, kind, source_generation, source_ref),
        CONSTRAINT ck_session_round_obligation_kind CHECK
          (kind IN ('tool_followup','pending_input','hook_retry','compact_continue')),
        CONSTRAINT ck_session_round_obligation_state CHECK
          (state IN ('pending','claimed','settled','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_next_round_plans (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        run_id uuid NOT NULL REFERENCES runtime_tasks(id) ON DELETE CASCADE,
        source_result_id uuid NOT NULL REFERENCES session_model_results(id) ON DELETE CASCADE,
        next_round_id varchar(200) NOT NULL,
        obligation_ids_json jsonb NOT NULL,
        ordered_sources_json jsonb NOT NULL,
        fences_json jsonb NOT NULL,
        plan_hash varchar(64) NOT NULL,
        state varchar(32) NOT NULL DEFAULT 'prepared',
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_next_round_plan_round UNIQUE (run_id, next_round_id),
        CONSTRAINT ck_session_next_round_plan_state CHECK
          (state IN ('prepared','committed','dispatched','abandoned','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_run_outcomes (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        turn_id varchar(200) NOT NULL,
        run_id uuid NOT NULL REFERENCES runtime_tasks(id) ON DELETE CASCADE,
        terminal_result_id uuid NOT NULL REFERENCES session_model_results(id),
        state varchar(32) NOT NULL DEFAULT 'prepared',
        eligibility_snapshot_hash varchar(64) NOT NULL,
        seal_json jsonb,
        terminal_event_id uuid REFERENCES chat_transcript_events(id),
        reconciliation_owner varchar(200),
        reconciliation_lease_expires_at timestamptz,
        version integer NOT NULL DEFAULT 1,
        CONSTRAINT uq_session_run_outcomes_run UNIQUE (run_id),
        CONSTRAINT ck_session_run_outcomes_state CHECK
          (state IN ('prepared','sealed','terminal_committed','failed','needs_reconciliation'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_feedback_aggregates (
        id uuid PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        session_id uuid NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
        target_item_id uuid NOT NULL,
        target_result_id uuid,
        revision integer NOT NULL DEFAULT 1,
        current_value_json jsonb,
        status varchar(20) NOT NULL DEFAULT 'active',
        last_mutation_item_id uuid NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_feedback_aggregate_scope UNIQUE (tenant_id, session_id, id),
        CONSTRAINT ck_session_feedback_aggregate_status CHECK (status IN ('active','withdrawn'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_writer_epochs (
        id varchar(40) PRIMARY KEY,
        state varchar(24) NOT NULL DEFAULT 'legacy_open',
        new_run_generation integer NOT NULL DEFAULT 1,
        allowed_existing_generations_json jsonb NOT NULL DEFAULT '[1]'::jsonb,
        enforcement_mode varchar(16) NOT NULL DEFAULT 'observe',
        release_id varchar(200),
        version integer NOT NULL DEFAULT 1,
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT ck_session_writer_epoch_state CHECK (state IN ('legacy_open','v1_draining','v2_only')),
        CONSTRAINT ck_session_writer_epoch_enforcement CHECK (enforcement_mode IN ('observe','enforce'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_writer_heartbeats (
        id uuid PRIMARY KEY,
        service varchar(40) NOT NULL,
        instance_id varchar(200) NOT NULL,
        artifact_digest varchar(128) NOT NULL,
        supported_generations_json jsonb NOT NULL,
        last_seen_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_session_writer_heartbeat_instance UNIQUE (service, instance_id)
    )
    """,
)


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_{table} ON {table}
        USING (
          current_setting('app.current_tenant_id', true) = 'BYPASS'
          OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        WITH CHECK (
          current_setting('app.current_tenant_id', true) = 'BYPASS'
          OR tenant_id::text = current_setting('app.current_tenant_id', true)
        )
        """
    )


def _install_writer_epoch_trigger() -> None:
    op.execute(build_session_writer_epoch_function_sql())
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_writer_epoch() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_session_writer_epoch ON runtime_tasks")
    op.execute(
        """
        CREATE TRIGGER trg_session_writer_epoch
        BEFORE INSERT OR UPDATE ON runtime_tasks
        FOR EACH ROW EXECUTE FUNCTION public.enforce_session_writer_epoch()
        """
    )


def _install_generated_event_contract_trigger() -> None:
    """Install the immutable 0716 SQL snapshot used at this revision boundary."""

    op.execute(build_session_event_contract_function_sql())
    op.execute(build_session_tenant_binding_function_sql())
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_event_v2_contract() FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION public.enforce_session_v2_tenant_binding() FROM PUBLIC")
    op.execute("DROP TRIGGER IF EXISTS trg_session_event_v2_contract ON chat_transcript_events")
    op.execute(
        """
        CREATE TRIGGER trg_session_event_v2_contract
        BEFORE INSERT OR UPDATE ON chat_transcript_events
        FOR EACH ROW EXECUTE FUNCTION public.enforce_session_event_v2_contract()
        """
    )
    for table in SESSION_V2_TENANT_TABLES:
        trigger = f"trg_{table}_tenant_binding"
        op.execute(f'DROP TRIGGER IF EXISTS "{trigger}" ON "{table}"')
        op.execute(
            f'CREATE TRIGGER "{trigger}" BEFORE INSERT OR UPDATE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION public.enforce_session_v2_tenant_binding()"
        )


def upgrade() -> None:
    for statement in _DDL:
        op.execute(statement)
    for index_name, table_name, columns in SESSION_V2_QUERY_INDEXES:
        op.execute(f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table_name}" ({columns})')
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_session_event_outbox_claim ON session_event_outbox(status,available_at,claim_expires_at)"
    )
    # These six indexes target the pre-existing, production-hot transcript
    # table. The helper owns one Alembic autocommit boundary because PostgreSQL
    # rejects concurrent CREATE/DROP inside a transaction block. New Session V2
    # tables above remain transactionally indexed with their table creation.
    _ensure_existing_transcript_indexes()
    op.execute(
        """
        INSERT INTO session_event_cursors(session_id,tenant_id,next_sequence,version)
        SELECT s.id,s.tenant_id,COALESCE(MAX(e.sequence),0)+1,1
        FROM chat_sessions s LEFT JOIN chat_transcript_events e ON e.session_id=s.id
        GROUP BY s.id,s.tenant_id
        ON CONFLICT (session_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO session_writer_epochs(id,state,new_run_generation,allowed_existing_generations_json,enforcement_mode,version)
        VALUES ('global','legacy_open',1,'[1]'::jsonb,'observe',1)
        ON CONFLICT (id) DO NOTHING
        """
    )
    for table in SESSION_V2_TENANT_TABLES:
        _enable_rls(table)
    _install_generated_event_contract_trigger()
    _install_writer_epoch_trigger()


def _assert_safe_schema_only_downgrade() -> None:
    """Fence writers and reject rollback after any generation-2 cutover fact."""

    # SHARE conflicts with the RowExclusive lock taken by INSERT/UPDATE/DELETE.
    # Holding all three tables until the migration transaction ends closes the
    # preflight-to-trigger-drop race without changing epoch or evidence state.
    op.execute("LOCK TABLE session_writer_epochs, runtime_tasks, chat_transcript_events IN SHARE MODE")
    op.execute(
        """
        DO $session_v2_downgrade_guard$
        DECLARE
          epoch session_writer_epochs%ROWTYPE;
        BEGIN
          SELECT * INTO epoch FROM session_writer_epochs WHERE id='global';
          IF NOT FOUND THEN
            RAISE EXCEPTION 'session_v2_downgrade_blocked: writer epoch is missing'
              USING ERRCODE='23514';
          END IF;
          IF epoch.state <> 'legacy_open'
             OR epoch.enforcement_mode <> 'observe'
             OR epoch.new_run_generation <> 1
             OR epoch.allowed_existing_generations_json <> '[1]'::jsonb THEN
            RAISE EXCEPTION
              'session_v2_downgrade_blocked: writer epoch has crossed the generation-2 cutover'
              USING ERRCODE='23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM runtime_tasks WHERE writer_generation >= 2
          ) THEN
            RAISE EXCEPTION
              'session_v2_downgrade_blocked: generation-2 RuntimeTask evidence exists'
              USING ERRCODE='23514';
          END IF;
          IF EXISTS (
            SELECT 1 FROM chat_transcript_events WHERE schema_version=2
          ) THEN
            RAISE EXCEPTION
              'session_v2_downgrade_blocked: SessionEventV2 evidence exists'
              USING ERRCODE='23514';
          END IF;
        END;
        $session_v2_downgrade_guard$
        """
    )


def downgrade() -> None:
    # Secure/schema-preserving rollback: keep canonical V2 evidence and additive
    # columns so a rollback artifact can replay/reconcile without reviving a V1
    # writer.  Only relax enforcement while retaining all facts.
    _assert_safe_schema_only_downgrade()
    op.execute("UPDATE session_writer_epochs SET enforcement_mode='observe', updated_at=now() WHERE id='global'")
    op.execute("DROP TRIGGER IF EXISTS trg_session_writer_epoch ON runtime_tasks")
    op.execute("DROP TRIGGER IF EXISTS trg_session_event_v2_contract ON chat_transcript_events")
