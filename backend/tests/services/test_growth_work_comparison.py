"""Mechanical comparisons of bindings whose authority is checked by native readers."""

from types import SimpleNamespace

from app.services.growth_work_comparison import _envelope
from app.services.session_model_inputs import _hash


def _two_round_work(identity):
    results = []
    for index in (1, 2):
        snapshot = {"provider": "controlled", "model": "controlled-model", "wire_request": {
            "tools": [], "reasoning": {}, "max_tokens": None},
            "bound_input_ids": [identity] if index == 1 else [],
            "input_bindings": {"schema": "hive.model_input_bindings.v1",
                "dependency_coverage": {"complete": True},
                "messages": [{"source_ref": f"session-input:{identity}",
                    "command_ref": f"session-command:{identity}", "message_hash": "same-business-input-hash"}],
                "dependencies": [{"source_kind": "session_command", "source_id": identity,
                    "source_version": f"different-mechanical-command-hash-{identity}"}]}}
        results.append(SimpleNamespace(state="round_committed", model_request_snapshot_json=snapshot,
            model_request_hash=_hash(snapshot)))
    return results


def test_carried_own_inputs_do_not_become_different_evidence_because_of_run_command_ids():
    baseline = _envelope(_two_round_work("baseline"), None)
    candidate = _envelope(_two_round_work("candidate"), None)
    assert baseline["business_input"] == candidate["business_input"] == ["same-business-input-hash"]
    assert baseline["authorized_evidence"] == candidate["authorized_evidence"] == []
