from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "add_sso_scan_sessions_updated_at_0419.py"
    )
    spec = importlib.util.spec_from_file_location("add_sso_scan_sessions_updated_at_0419", migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


def test_upgrade_adds_missing_sso_scan_session_updated_at_column() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    assert fake_op.statements == [
        (
            "ALTER TABLE sso_scan_sessions "
            "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        )
    ]


def test_downgrade_drops_sso_scan_session_updated_at_column() -> None:
    module = _load_migration()
    fake_op = _FakeOp()
    module.op = fake_op

    module.downgrade()

    assert fake_op.dropped_columns == [("sso_scan_sessions", "updated_at")]
