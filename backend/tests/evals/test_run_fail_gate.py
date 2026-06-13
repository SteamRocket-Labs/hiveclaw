"""E8: app.evals.run --fail-under exit semantics (G3 regression fail-gate)."""

from __future__ import annotations

from pathlib import Path

from app.evals.run import main


def test_fail_under_exits_nonzero_below_threshold(tmp_path: Path) -> None:
    code = main(
        ["--suite", "core_v1", "--target", "clawith", "--mode", "internal", "--fail-under", "101"],
        output_root=str(tmp_path),
    )
    assert code == 1


def test_fail_under_passes_when_threshold_met(tmp_path: Path) -> None:
    code = main(
        ["--suite", "core_v1", "--target", "clawith", "--mode", "internal", "--fail-under", "0"],
        output_root=str(tmp_path),
    )
    assert code == 0


def test_no_fail_under_returns_zero(tmp_path: Path) -> None:
    code = main(
        ["--suite", "core_v1", "--target", "clawith", "--mode", "internal"],
        output_root=str(tmp_path),
    )
    assert code == 0
