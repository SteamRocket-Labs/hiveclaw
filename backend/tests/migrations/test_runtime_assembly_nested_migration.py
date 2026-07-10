"""Regression contract for the RuntimeAssembly single-source migration."""

import importlib.util
from pathlib import Path


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "runtime_assembly_nested_0710.py"
_SPEC = importlib.util.spec_from_file_location("runtime_assembly_nested_0710", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_promote_runtime_assembly = _MODULE._promote_runtime_assembly


def test_promote_runtime_assembly_moves_legacy_mirrors_and_preserves_nested_authority():
    metadata = {
        "source": "web",
        "tool_result_ledger": [{"legacy": True}],
        "activation_events": [{"event_type": "legacy"}],
        "runtime_assembly_state": {
            "tool_result_ledger": [{"canonical": True}],
            "skill_catalog_ranking": [{"skill": "existing"}],
        },
    }

    promoted, changed = _promote_runtime_assembly(metadata)

    assert changed is True
    assert promoted["source"] == "web"
    assert "tool_result_ledger" not in promoted
    assert "activation_events" not in promoted
    assert promoted["runtime_assembly_state"] == {
        "schema": "hive.ccplus.runtime_assembly_state.v1",
        "tool_result_ledger": [{"canonical": True}],
        "activation_events": [{"event_type": "legacy"}],
        "skill_catalog_ranking": [{"skill": "existing"}],
    }


def test_promote_runtime_assembly_is_idempotent_and_does_not_create_empty_state():
    canonical = {
        "source": "channel",
        "runtime_assembly_state": {
            "schema": "hive.ccplus.runtime_assembly_state.v1",
            "activation_candidates": [{"candidate_id": "one"}],
        },
    }

    promoted, changed = _promote_runtime_assembly(canonical)
    untouched, untouched_changed = _promote_runtime_assembly({"source": "channel"})

    assert changed is False
    assert promoted == canonical
    assert untouched_changed is False
    assert untouched == {"source": "channel"}
