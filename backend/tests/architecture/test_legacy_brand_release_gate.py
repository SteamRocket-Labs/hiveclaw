from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[3]
GATE_PATH = ROOT / "backend" / "scripts" / "legacy_brand_release_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("legacy_brand_release_gate", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_tree_contains_only_allowlisted_legacy_brand_identifiers() -> None:
    gate = _load_gate()

    assert gate.release_errors(gate.working_tree_snapshot(ROOT)) == []


def test_git_archive_reader_uses_the_exact_committed_bytes() -> None:
    gate = _load_gate()
    snapshot = gate.git_archive_snapshot("HEAD", ROOT)
    committed_license = subprocess.run(
        ["git", "show", "HEAD:LICENSE"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout

    assert snapshot["LICENSE"] == committed_license


def test_same_count_cannot_move_approved_identifier_into_product_copy() -> None:
    gate = _load_gate()
    snapshot = gate.working_tree_snapshot(ROOT)
    dockerfile = snapshot["Dockerfile"]
    approved = gate.APPROVED_COMPATIBILITY_LINES["Dockerfile"][0]
    replacement = b"LABEL legacy_product_name=" + gate.LEGACY_BRAND
    snapshot["Dockerfile"] = dockerfile.replace(approved, replacement, 1)

    assert gate.release_errors(snapshot)


def test_release_paths_are_scanned_as_well_as_file_bytes() -> None:
    gate = _load_gate()
    snapshot = gate.working_tree_snapshot(ROOT)
    snapshot[f"frontend/{gate.LEGACY_BRAND.decode('ascii')}-logo.svg"] = b""

    assert any("release path" in error for error in gate.release_errors(snapshot))


def test_committed_archive_rejects_known_personal_identifiers() -> None:
    gate = _load_gate()
    marker = gate.PERSONAL_RELEASE_MARKERS[0]

    assert gate.personal_identifier_errors({"evidence.json": b'{"path":"' + marker + b'/repo"}'})


def test_ordinary_names_do_not_trigger_personal_identifier_gate() -> None:
    gate = _load_gate()

    assert gate.personal_identifier_errors({"example.txt": "示例用户 A".encode()}) == []
