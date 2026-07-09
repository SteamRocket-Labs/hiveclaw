"""Security + behavior tests for external-capability activation cleanup.

The cleanup path deletes files (``shutil.rmtree`` / ``unlink``) by component
name, so a path-traversal or absolute-path name must never reach outside the
agent workspace. These tests exercise the real filesystem (no mocks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.external_capabilities.activation_cleanup import deactivate_activation_components


def _write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_removes_legitimate_skill_and_subagent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skill_dir = workspace / "skills" / "research-helper"
    _write(skill_dir / "SKILL.md")
    subagent = _write(workspace / "subagents" / "critic.md")

    result = deactivate_activation_components(
        [
            {"component_type": "skill", "name": "research-helper"},
            {"component_type": "subagent", "name": "critic"},
        ],
        workspace=workspace,
    )

    assert [entry["status"] for entry in result] == ["removed", "removed"]
    assert not skill_dir.exists()
    assert not subagent.exists()


def test_rejects_relative_traversal_without_deleting_outside_target(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "subagents").mkdir(parents=True)
    secret = _write(tmp_path / "secret.txt", "keep me")

    result = deactivate_activation_components(
        [
            {"component_type": "skill", "name": "../../secret.txt"},
            {"component_type": "subagent", "name": "../../secret"},
        ],
        workspace=workspace,
    )

    assert result[0]["status"] == "invalid_component_name"
    assert result[1]["status"] == "invalid_component_name"
    assert secret.exists()
    assert secret.read_text(encoding="utf-8") == "keep me"


def test_rejects_absolute_path_name(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    outside = _write(tmp_path / "abs_secret.txt", "keep")

    result = deactivate_activation_components(
        [{"component_type": "skill", "name": str(outside)}],
        workspace=workspace,
    )

    assert result[0]["status"] == "invalid_component_name"
    assert outside.exists()


def test_rejects_dotdot_segment(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    sibling = _write(workspace / "soul.md", "identity")

    result = deactivate_activation_components(
        [{"component_type": "skill", "name": ".."}],
        workspace=workspace,
    )

    assert result[0]["status"] == "invalid_component_name"
    assert sibling.exists()


def test_symlink_target_is_not_followed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    outside_dir = tmp_path / "outside_pkg"
    _write(outside_dir / "keep.txt", "keep")
    link = workspace / "skills" / "evil-link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")

    result = deactivate_activation_components(
        [{"component_type": "skill", "name": "evil-link"}],
        workspace=workspace,
    )

    assert result[0]["status"] == "invalid_component_path"
    assert outside_dir.exists()
    assert (outside_dir / "keep.txt").exists()


def test_absent_component_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)

    result = deactivate_activation_components(
        [{"component_type": "skill", "name": "never-installed"}],
        workspace=workspace,
    )

    assert result[0]["status"] == "already_absent"


def test_mcp_and_unknown_types_are_passthrough(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"

    result = deactivate_activation_components(
        [
            {"component_type": "mcp_server", "name": "notion"},
            {"component_type": "widget", "name": "x"},
        ],
        workspace=workspace,
    )

    assert result[0]["status"] == "manual_revoke_required"
    assert result[1]["status"] == "unsupported_deactivation_component"
