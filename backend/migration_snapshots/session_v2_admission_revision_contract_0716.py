"""Frozen authority delta for revisioned Session V2 input admission attempts."""

from __future__ import annotations

from migration_snapshots.session_v2_input_control_contract_0716 import (
    build_session_event_contract_function_sql as _build_parent_event_sql,
    build_session_tenant_binding_function_sql as _build_parent_authority_sql,
)


_PARENT_ADMISSION_AUTHORITY = """          WHERE command.id=NEW.command_id
            AND command.tenant_id=NEW.tenant_id
            AND command.session_id=NEW.session_id
        )
        AND (NEW.hook_item_id IS NULL OR EXISTS ("""

_REVISIONED_ADMISSION_AUTHORITY = """          WHERE command.id=NEW.command_id
            AND command.tenant_id=NEW.tenant_id
            AND command.session_id=NEW.session_id
            AND NEW.input_revision > 0
            AND NEW.input_revision <= turn_input.revision
        )
        AND (NEW.hook_item_id IS NULL OR EXISTS ("""

_PARENT_REPLACEMENT_ADMISSION = """           AND admission.tenant_id=saga_command.tenant_id
           AND admission.session_id=saga_command.session_id
          WHERE saga_command.id=NEW.command_id"""

_REVISIONED_REPLACEMENT_ADMISSION = """           AND admission.tenant_id=saga_command.tenant_id
           AND admission.session_id=saga_command.session_id
           AND admission.input_revision=replacement_input.revision
          WHERE saga_command.id=NEW.command_id"""

_PARENT_NULL_CANCEL_STATE = """          (NEW.state='requested'
           AND NEW.cancel_control_id IS NULL"""

_RECONCILABLE_NULL_CANCEL_STATE = """          (NEW.state IN ('requested','needs_reconciliation')
           AND NEW.cancel_control_id IS NULL"""

_PARENT_CONTROL_INPUT_RULE = (
    '"control_input":{"lifecycles":["accepted","applied","failed","needs_reconciliation",'
    '"reconciled","rejected","started"],"scopes":["run"]}'
)
_REVISIONED_CONTROL_INPUT_RULE = (
    '"control_input":{"lifecycles":["accepted","applied","failed","needs_reconciliation",'
    '"reconciled","rejected","started"],"scopes":["run","session"]}'
)
_PARENT_CARRY_FORWARD_ANCHOR = '"code_execution":{"lifecycles"'
_REVISIONED_CARRY_FORWARD_ANCHOR = (
    '"carry_forward":{"lifecycles":["bound","claimed","consumed","needs_reconciliation","reconciled"],'
    '"scopes":["round"]},"code_execution":{"lifecycles"'
)
_PARENT_NON_ASSISTANT_PHASE_RULE = (
    "          ELSIF NEW.item_kind NOT IN "
    "('assistant_text','assistant_commentary','assistant_reasoning_summary','assistant_reasoning_private','assistant_final')\n"
    "             AND NEW.metadata_json->'v2_payload' ? 'phase' THEN"
)
_REVISIONED_NON_ASSISTANT_PHASE_RULE = """          ELSIF NEW.item_kind NOT IN (
            'assistant_text','assistant_commentary','assistant_reasoning_summary',
            'assistant_reasoning_private','assistant_final'
          )
             AND NEW.metadata_json->'v2_payload' ? 'phase' THEN"""


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("frozen Session V2 admission revision authority anchor drifted")
    return source.replace(old, new, 1)


def build_session_tenant_binding_function_sql() -> str:
    sql = _build_parent_authority_sql()
    sql = _replace_once(sql, _PARENT_ADMISSION_AUTHORITY, _REVISIONED_ADMISSION_AUTHORITY)
    sql = _replace_once(sql, _PARENT_REPLACEMENT_ADMISSION, _REVISIONED_REPLACEMENT_ADMISSION)
    return _replace_once(sql, _PARENT_NULL_CANCEL_STATE, _RECONCILABLE_NULL_CANCEL_STATE)


def build_session_event_contract_function_sql() -> str:
    sql = _build_parent_event_sql()
    sql = _replace_once(sql, _PARENT_CONTROL_INPUT_RULE, _REVISIONED_CONTROL_INPUT_RULE)
    sql = _replace_once(sql, _PARENT_CARRY_FORWARD_ANCHOR, _REVISIONED_CARRY_FORWARD_ANCHOR)
    return _replace_once(sql, _PARENT_NON_ASSISTANT_PHASE_RULE, _REVISIONED_NON_ASSISTANT_PHASE_RULE)
