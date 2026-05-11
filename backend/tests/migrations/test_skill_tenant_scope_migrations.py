from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration(filename: str):
    migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(filename.replace(".py", ""), migration_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def test_scope_skill_uniqueness_limits_global_indexes_to_builtin_skills() -> None:
    module = _load_migration("scope_skill_registry_uniqueness_0511.py")
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    joined = "\n".join(fake_op.statements)
    assert "uq_skills_global_name" in joined
    assert "WHERE tenant_id IS NULL AND is_builtin IS TRUE" in joined
    assert "uq_skills_tenant_folder_name" in joined


def test_enforce_skill_custom_tenant_scope_adds_future_write_guard() -> None:
    module = _load_migration("enforce_skill_custom_tenant_scope_0511.py")
    fake_op = _FakeOp()
    module.op = fake_op

    module.upgrade()

    joined = "\n".join(fake_op.statements)
    assert "DROP INDEX IF EXISTS uq_skills_global_name" in joined
    assert "WHERE tenant_id IS NULL AND is_builtin IS TRUE" in joined
    assert "ck_skills_custom_requires_tenant" in joined
    assert "CHECK (tenant_id IS NOT NULL OR is_builtin IS TRUE)" in joined
    assert "NOT VALID" in joined
