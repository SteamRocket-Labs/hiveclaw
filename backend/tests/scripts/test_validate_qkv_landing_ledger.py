from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LANDING_DOC = PROJECT_ROOT / "docs" / "ccplus-transformer-style-memory-runtime-upgrade-plan-2026-07-05.md"


def test_validate_qkv_landing_ledger_accepts_current_plan() -> None:
    from app.scripts.validate_qkv_landing_ledger import validate_landing_ledger

    report = validate_landing_ledger(LANDING_DOC)

    assert report["status_landing"] is True
    assert report["plan_count"] == 4
    assert report["atomic_count"] == 48
    assert report["unique_atomic_count"] == 48
    assert report["first_id"] == "A-01"
    assert report["last_id"] == "J-03"
    assert report["evidence_cells"] == 48
    assert report["ok"] is True


def test_validate_qkv_landing_ledger_rejects_duplicate_atomic_ids(tmp_path: Path) -> None:
    from app.scripts.validate_qkv_landing_ledger import validate_landing_ledger

    broken = tmp_path / "broken.md"
    broken.write_text(
        "\n".join(
            [
                "状态：落地施工账本",
                "| 计划 | 作用 | 交付物 | 完成标准 |",
                "| --- | --- | --- | --- |",
                "| P1 主架构计划 | a | b | c |",
                "| P2 原子落地账本 | a | b | c |",
                "| P3 Backfill / Rebuild / Compatibility 计划 | a | b | c |",
                "| P4 验收 / 大召回 / 回滚计划 | a | b | c |",
                "| ID | 原子项 | 主要触点 | 测试门 | 提交证据 |",
                "| --- | --- | --- | --- | --- |",
                "| A-01 | one | x | y | 待执行 |",
                "| A-01 | duplicate | x | y | 待执行 |",
            ]
        ),
        encoding="utf-8",
    )

    report = validate_landing_ledger(broken, expected_atomic_count=2)

    assert report["ok"] is False
    assert "duplicate atomic ids" in report["errors"]
