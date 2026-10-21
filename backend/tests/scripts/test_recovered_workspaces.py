"""Sealed physical-volume relocation preserves exact Agent/worktree scope."""

from copy import deepcopy
from pathlib import Path
import uuid

import pytest

from app.scripts import self_hosted_recovery as recovery


@pytest.fixture
def relocation(tmp_path):
    old, new = tmp_path / "original", tmp_path / "restored"
    old.mkdir()
    new.mkdir()
    agent = uuid.uuid4()
    key = "synthetic-volume-map-key"
    mapping = {"schema": "hive.workspace_relocation.v1", "manifest_sha256": "a" * 64,
        "source_roots": [str(old)], "target_roots": [str(new)]}
    mapping["mac"] = recovery._relocation_mac(mapping, key)
    journal = {"schema": "hive.self_hosted_restore.v1", "status": "restored",
        "manifest_sha256": "a" * 64, "workspace_relocation": mapping}
    recovery.write_json(new / recovery.CONTROL, journal)
    return old, new, agent, key, journal


@pytest.mark.parametrize("suffix", ["", "runtime_artifacts/subagent_worktrees/child-depth-2"])
def test_recovery_relocates_exact_agent_and_preserves_worktree_suffix(relocation, suffix):
    old, new, agent, key, _journal = relocation
    workspace = old / str(agent) / suffix
    resolved = recovery.resolve_recovered_workspace(workspace, agent_id=agent, agents=new, key=key)
    assert resolved == new / str(agent) / suffix
    assert recovery.resolve_recovered_workspace(resolved, agent_id=agent, agents=new, key=key) == resolved


@pytest.mark.parametrize("mutation", ["mac", "source", "target", "manifest", "status", "key"])
def test_changed_or_misbound_relocation_is_unavailable(relocation, mutation):
    old, new, agent, key, original = relocation
    journal = deepcopy(original)
    mapping = journal["workspace_relocation"]
    if mutation == "mac":
        mapping["mac"] = "0" * 64
    elif mutation == "source":
        mapping["source_roots"] = ["/different-volume"]
    elif mutation == "target":
        mapping["target_roots"] = ["/different-volume"]
        mapping["mac"] = recovery._relocation_mac(mapping, key)
    elif mutation == "manifest":
        journal["manifest_sha256"] = "b" * 64
    elif mutation == "status":
        journal["status"] = "started"
    else:
        key = "a-different-installation-key"
    recovery.write_json(new / recovery.CONTROL, journal, replace=True)
    with pytest.raises(recovery.RecoveryError):
        recovery.resolve_recovered_workspace(old / str(agent), agent_id=agent, agents=new, key=key)


def test_recovery_never_uses_another_agent_or_a_parent_escape(relocation):
    old, new, agent, key, _journal = relocation
    for workspace in (old / str(uuid.uuid4()), old / str(agent) / ".." / str(uuid.uuid4()), Path("relative")):
        with pytest.raises(recovery.RecoveryError):
            recovery.resolve_recovered_workspace(workspace, agent_id=agent, agents=new, key=key)


@pytest.mark.parametrize("alias", ["control", "agent", "worktree"])
def test_recovery_refuses_aliases_at_control_and_workspace_boundaries(relocation, tmp_path, alias):
    old, new, agent, key, _journal = relocation
    outside = tmp_path / "retained"
    outside.mkdir()
    workspace = old / str(agent)
    if alias == "control":
        control = new / recovery.CONTROL
        control.rename(outside / "control.json")
        control.symlink_to(outside / "control.json")
    elif alias == "agent":
        (new / str(agent)).symlink_to(outside, target_is_directory=True)
    else:
        (new / str(agent)).mkdir()
        (new / str(agent) / "worktree").symlink_to(outside, target_is_directory=True)
        workspace /= "worktree"
    with pytest.raises((recovery.RecoveryError, OSError)):
        recovery.resolve_recovered_workspace(workspace, agent_id=agent, agents=new, key=key)
    assert list(outside.iterdir()) == ([outside / "control.json"] if alias == "control" else [])


def test_unrelocated_custom_workspaces_and_legacy_same_mount_remain_compatible(tmp_path):
    root = tmp_path / "agents"
    root.mkdir()
    agent = uuid.uuid4()
    custom = tmp_path / "isolated-subagent"
    assert recovery.resolve_recovered_workspace(custom, agent_id=agent, agents=root, key="") == custom
    recovery.write_json(root / recovery.CONTROL, {"schema": "hive.self_hosted_restore.v1", "status": "restored"})
    assert recovery.resolve_recovered_workspace(root / str(agent), agent_id=agent, agents=root, key="") == root / str(agent)
    with pytest.raises(recovery.RecoveryError):
        recovery.resolve_recovered_workspace(custom, agent_id=agent, agents=root, key="")


def test_authenticated_volume_lineage_survives_a_later_runtime_mount(relocation, tmp_path):
    old, current, agent, key, journal = relocation
    prior = recovery._read_workspace_relocation(current, key)
    third = tmp_path / "third"
    third.mkdir()
    mapping = {"schema": "hive.workspace_relocation.v1", "manifest_sha256": "b" * 64,
        "source_roots": recovery._volume_locators([str(current), *prior["source_roots"], *prior["target_roots"]]),
        "target_roots": [str(third), "/runtime/agents"]}
    mapping["mac"] = recovery._relocation_mac(mapping, key)
    recovery.write_json(third / recovery.CONTROL, {**journal, "manifest_sha256": "b" * 64, "workspace_relocation": mapping})
    assert recovery.resolve_recovered_workspace(old / str(agent), agent_id=agent, agents=third, key=key) == third / str(agent)


def test_current_volume_work_does_not_depend_on_legacy_relocation_metadata(relocation):
    _old, current, agent, key, _journal = relocation
    (current / recovery.CONTROL).unlink()
    workspace = current / str(agent)
    assert recovery.resolve_recovered_workspace(workspace, agent_id=agent, agents=current,
        key=key, required_manifest="a" * 64) == workspace
