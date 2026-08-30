from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = ROOT / "backend" / "scripts" / "weekend_rc_worker_gate.py"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("weekend_rc_worker_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()


def _targets(timeout: int) -> dict[str, object]:
    return {
        "targets": [
            {
                "name": "zcode",
                "argv": ["/opt/zcode-acp", "--prompt-timeout-secs", str(timeout)],
            }
        ]
    }


def test_preflight_reports_the_effective_timeout_without_blocking_the_worker() -> None:
    result = GATE.validate_preflight(
        _targets(900),
        target_name="zcode",
        outer_timeout_seconds=1800,
    )

    assert result["ready"] is True
    assert result["target_internal_timeout_seconds"] == 900
    assert result["effective_timeout_seconds"] == 900
    assert "effective deadline" in result["warnings"][0]
    assert result["semantic_verdict"] == "not_computed_by_tool"


def test_preflight_accepts_a_bounded_outer_timeout_with_buffer() -> None:
    result = GATE.validate_preflight(
        _targets(3600),
        target_name="zcode",
        outer_timeout_seconds=3000,
    )

    assert result["ready"] is True
    assert result["errors"] == []
    assert result["warnings"] == []
    assert result["effective_timeout_seconds"] == 3000


def test_receipt_classifies_cancelled_as_reviewable_interruption() -> None:
    result = GATE.validate_receipt(
        {"target": "zcode", "cwd": "/tmp/worktree"},
        {
            "status": "success",
            "exit_code": 0,
            "stop_reason": "cancelled",
            "protocol_errors": [],
        },
        expected_target="zcode",
        expected_cwd="/tmp/worktree",
        worktree_changed=False,
    )

    assert result["receipt_valid"] is True
    assert result["ready_for_independent_review"] is True
    assert result["dispatch_state"] == "interrupted"
    assert result["errors"] == []
    assert result["transport_issues"] == []
    assert any("no changes" in warning for warning in result["warnings"])


def test_receipt_terminal_gate_still_defers_semantic_acceptance() -> None:
    result = GATE.validate_receipt(
        {"target": "zcode", "cwd": "/tmp/worktree"},
        {
            "status": "success",
            "exit_code": 0,
            "stop_reason": "end_turn",
            "protocol_errors": [],
        },
        expected_target="zcode",
        expected_cwd="/tmp/worktree",
        worktree_changed=True,
    )

    assert result["receipt_valid"] is True
    assert result["dispatch_state"] == "returned"
    assert result["errors"] == []
    assert result["semantic_verdict"] == "not_computed_by_tool"


def test_receipt_accepts_equivalent_macos_tmp_paths() -> None:
    result = GATE.validate_receipt(
        {"target": "zcode", "cwd": "/private/tmp/worktree"},
        {
            "status": "success",
            "exit_code": 0,
            "stop_reason": "end_turn",
            "protocol_errors": [],
        },
        expected_target="zcode",
        expected_cwd="/tmp/worktree",
    )

    assert result["receipt_valid"] is True
    assert result["errors"] == []


def test_transport_failure_remains_reviewable_for_partial_artifacts() -> None:
    result = GATE.validate_receipt(
        {"target": "zcode", "cwd": "/tmp/worktree"},
        {
            "status": "failed",
            "exit_code": 1,
            "stop_reason": "error",
            "protocol_errors": [{"message": "transport closed"}],
        },
        expected_target="zcode",
        expected_cwd="/tmp/worktree",
        worktree_changed=True,
    )

    assert result["receipt_valid"] is True
    assert result["ready_for_independent_review"] is True
    assert result["dispatch_state"] == "transport_failed"
    assert len(result["transport_issues"]) == 3
