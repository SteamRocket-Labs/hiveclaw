"""Immutable authority-contract delta for Session V2 input/control revision 0716.

The original Session V2 snapshot remains the frozen revision-cut contract.  This
snapshot composes that immutable source with the one reviewed replacement-saga
authority delta owned by ``session_v2_input_control_0716``.  It never imports
live application code.
"""

from __future__ import annotations

from migration_snapshots.session_v2_contract_0716 import (
    build_session_event_contract_function_sql as _build_parent_event_sql,
    build_session_tenant_binding_function_sql as _build_parent_authority_sql,
)


_PARENT_REPLACEMENT_AUTHORITY = """          JOIN public.session_commands AS cancel_command
            ON cancel_command.id=NEW.cancel_command_id
           AND cancel_command.causation_command_id=saga_command.id
           AND cancel_command.tenant_id=saga_command.tenant_id
           AND cancel_command.session_id=saga_command.session_id
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
          JOIN public.session_control_inputs AS cancel_control
            ON cancel_control.id=NEW.cancel_control_id
           AND cancel_control.command_id=cancel_command.id
           AND cancel_control.expected_run_id=NEW.old_run_id
           AND cancel_control.tenant_id=saga_command.tenant_id
           AND cancel_control.session_id=saga_command.session_id
          WHERE saga_command.id=NEW.command_id
            AND saga_command.namespace='turn_replacement'
            AND cancel_command.namespace='control_input'
            AND parent_command.namespace='human_input'"""

_INPUT_FIRST_REPLACEMENT_AUTHORITY = """          JOIN public.session_turn_inputs AS replacement_input
            ON replacement_input.id=NEW.replacement_input_id
           AND replacement_input.command_id=parent_command.id
           AND replacement_input.tenant_id=saga_command.tenant_id
           AND replacement_input.session_id=saga_command.session_id
          JOIN public.session_input_admissions AS admission
            ON admission.input_id=replacement_input.id
           AND admission.command_id=parent_command.id
           AND admission.tenant_id=saga_command.tenant_id
           AND admission.session_id=saga_command.session_id
          WHERE saga_command.id=NEW.command_id
            AND saga_command.namespace='turn_replacement'
            AND parent_command.namespace='human_input'"""

_PARENT_ADMITTED_TAIL = """            AND admission.state='admitted'
            AND saga_command.tenant_id=NEW.tenant_id
            AND saga_command.session_id=NEW.session_id
        )
        AND EXISTS (
          SELECT 1 FROM public.runtime_tasks AS old_run"""

_INPUT_FIRST_ADMITTED_TAIL = """            AND admission.state='admitted'
            AND saga_command.tenant_id=NEW.tenant_id
            AND saga_command.session_id=NEW.session_id
        )
        AND (
          (NEW.state='requested'
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
          SELECT 1 FROM public.runtime_tasks AS old_run"""

_PARENT_REPLACEMENT_TARGET = """            AND replacement_input.intent='interrupt_and_replace'
            AND replacement_input.target_turn_id=NEW.old_turn_id
            AND replacement_input.target_run_id=NEW.old_run_id
            AND admission.state='admitted'"""

_STATE_AWARE_REPLACEMENT_TARGET = """            AND replacement_input.intent='interrupt_and_replace'
            AND (
              (NEW.state IN ('requested','cancel_accepted','old_run_fenced')
               AND replacement_input.target_turn_id=NEW.old_turn_id
               AND replacement_input.target_run_id=NEW.old_run_id)
              OR
              (NEW.state IN ('replacement_queued','replacement_admitted','completed','failed','needs_reconciliation')
               AND replacement_input.target_turn_id IN (NEW.old_turn_id,NEW.replacement_turn_id))
            )
            AND admission.state='admitted'"""

_PARENT_INPUT_EVENT_AUTHORITY = """              AND input.tenant_id=NEW.tenant_id
              AND input.session_id=NEW.session_id
              AND (NEW.command_id IS NULL OR input.command_id=NEW.command_id)
              AND (input.target_run_id IS NULL OR NEW.run_id IS NULL OR input.target_run_id=NEW.run_id)"""

_INPUT_FIRST_EVENT_AUTHORITY = """              AND input.tenant_id=NEW.tenant_id
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
              AND (input.target_run_id IS NULL OR NEW.run_id IS NULL OR input.target_run_id=NEW.run_id)"""


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("frozen Session V2 input/control authority anchor drifted")
    return source.replace(old, new, 1)


def build_session_tenant_binding_function_sql() -> str:
    """Return the migration-owned input-first replacement authority function."""

    sql = _build_parent_authority_sql()
    sql = _replace_once(sql, _PARENT_REPLACEMENT_AUTHORITY, _INPUT_FIRST_REPLACEMENT_AUTHORITY)
    sql = _replace_once(sql, _PARENT_REPLACEMENT_TARGET, _STATE_AWARE_REPLACEMENT_TARGET)
    return _replace_once(sql, _PARENT_ADMITTED_TAIL, _INPUT_FIRST_ADMITTED_TAIL)


def build_session_event_contract_function_sql() -> str:
    """Allow saga events to reference their causally owned HumanInput."""

    return _replace_once(
        _build_parent_event_sql(),
        _PARENT_INPUT_EVENT_AUTHORITY,
        _INPUT_FIRST_EVENT_AUTHORITY,
    )
