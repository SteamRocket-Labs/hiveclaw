"""Immutable projection-sidecar delta for the Session V2 event trigger."""

from __future__ import annotations

from migration_snapshots.session_v2_admission_revision_contract_0716 import (
    build_session_event_contract_function_sql as _build_parent_event_contract,
)


_EPOCH_ANCHOR = "          SELECT * INTO epoch FROM public.session_writer_epochs WHERE id='global';\n"

_PROJECTION_ONLY_GUARD = """          IF TG_OP='UPDATE'
             AND (to_jsonb(NEW) - ARRAY[
                    'metadata_json','projection_status','projection_attempts',
                    'projection_error','projected_at'
                 ]::text[])
                 = (to_jsonb(OLD) - ARRAY[
                    'metadata_json','projection_status','projection_attempts',
                    'projection_error','projected_at'
                 ]::text[])
             AND (COALESCE(NEW.metadata_json,'{}'::jsonb) - ARRAY[
                    't0_bridge_pending','t0_bridge_last_error','t0_bridge_attempts',
                    't0_bridge_relayed_at','t0_bridge_relay_source',
                    't0_bridge_segment_id','t0_bridge_event_id','t0_bridge_sequence'
                 ]::text[])
                 = (COALESCE(OLD.metadata_json,'{}'::jsonb) - ARRAY[
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
"""


def build_session_event_contract_function_sql() -> str:
    """Apply the frozen 0716 projection delta to the frozen base revision."""

    base = _build_parent_event_contract()
    if base.count(_EPOCH_ANCHOR) != 1:
        raise RuntimeError("session_v2_projection_epoch_anchor_drift")
    return base.replace(_EPOCH_ANCHOR, _PROJECTION_ONLY_GUARD + _EPOCH_ANCHOR, 1)
