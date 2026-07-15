from __future__ import annotations

import ast
import inspect
from pathlib import Path


def test_unsigned_recovery_module_exposes_no_live_storage_api() -> None:
    import app.runtime.recovery_manifest as manifest_module

    assert not hasattr(manifest_module, "load_recovery_manifest")
    assert not hasattr(manifest_module, "persist_recovery_manifest")
    assert not hasattr(manifest_module, "recovery_manifest_path")


def test_kernel_recovery_consumers_never_open_manifest_storage() -> None:
    from app.kernel import engine

    for consumer in (
        engine._build_runtime_attachment_sections,
        engine._build_restoration_context,
        engine._execute_recovered_pending_tool_frames,
    ):
        source = inspect.getsource(consumer)
        tree = ast.parse(source)
        called_names = {
            node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "load_recovery_manifest" not in called_names


def test_legacy_singleton_paths_are_confined_to_authority_store_migration() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    owners: list[str] = []
    for path in (backend_root / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if (
            "LEGACY_RUNTIME_RECOVERY_MANIFEST_REL_PATH" in source
            or "LEGACY_WORKSPACE_RECOVERY_MANIFEST_REL_PATH" in source
        ):
            owners.append(path.relative_to(backend_root).as_posix())

    assert sorted(owners) == [
        "app/runtime/recovery_manifest.py",
        "app/runtime/recovery_manifest_store.py",
    ]

    workspace_source = (backend_root / "app/tools/workspace.py").read_text(encoding="utf-8")
    assert '"workspace/recovery_manifest.json": "runtime_artifacts/recovery_manifest.json"' not in workspace_source
