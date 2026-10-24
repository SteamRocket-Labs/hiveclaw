from __future__ import annotations

import pytest


def test_grep_search_terminates_rg_options_before_untrusted_pattern(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import workspace

    search_root = tmp_path / "workspace"
    search_root.mkdir()
    target = search_root / "record.txt"
    target.write_text("--files\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return workspace.subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{command[-1]}:1:--files\n",
            stderr="",
        )

    monkeypatch.setattr(workspace.shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    result = workspace._grep_search(tmp_path, "--files", root="workspace")

    assert captured["command"] == [
        "rg",
        "--with-filename",
        "--line-number",
        "--color",
        "never",
        "--",
        "--files",
        captured["command"][-1],
    ]
    descriptor = captured["kwargs"]["pass_fds"]
    assert len(descriptor) == 1 and captured["command"][-1] == f"/dev/fd/{descriptor[0]}"
    assert captured["kwargs"] == {"pass_fds": descriptor, "capture_output": True, "text": True, "check": False}
    assert "workspace/record.txt:1:--files" in result


@pytest.mark.parametrize("native", [False, True])
def test_grep_search_treats_pattern_as_regex(monkeypatch, tmp_path, native):
    from app.services.agent_tool_domains import workspace

    data_dir = tmp_path / "workspace" / "tool_results"
    data_dir.mkdir(parents=True)
    (data_dir / "records.txt").write_text(
        "\n".join(
            [
                "- `rec1`",
                "  - 项目名称: 正利润企业",
                "  - 净利润: 300000000",
                "- `rec2`",
                "  - 项目名称: 共模半导体-B轮",
                "  - 净利润: -20000000",
            ]
        ),
        encoding="utf-8",
    )

    if native:
        assert workspace.shutil.which("rg")
    else:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)

    result = workspace._grep_search(
        tmp_path,
        r"净利润:\s*-",
        root="workspace/tool_results",
    )

    assert "records.txt:6" in result
    assert "净利润: -20000000" in result
    assert "No matches" not in result
    if native:
        assert "records.txt:2" in workspace._grep_search(tmp_path, r"\p{Han}+", root="workspace/tool_results/records.txt")


def test_glob_search_preserves_more_than_one_hundred_matches(tmp_path):
    from app.services.agent_tool_domains import workspace

    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(125):
        (root / f"record-{index:03d}.md").write_text(str(index), encoding="utf-8")

    result = workspace._glob_search(tmp_path, "workspace/*.md")

    assert "125 match(es)" in result
    assert "record-124.md" in result


@pytest.mark.parametrize("native", [False, True])
def test_grep_search_defaults_to_complete_results_and_honors_explicit_limit(monkeypatch, tmp_path, native):
    from app.services.agent_tool_domains import workspace

    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(75):
        (root / f"record-{index:03d}.txt").write_text(f"needle {index}\n", encoding="utf-8")
    if native:
        assert workspace.shutil.which("rg")
    else:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)

    complete = workspace._grep_search(tmp_path, "needle", root="workspace")
    explicit = workspace._grep_search(tmp_path, "needle", root="workspace", max_results=7)

    assert "75 match(es)" in complete
    assert "record-074.txt" in complete
    assert "7 match(es)" in explicit
    assert "record-007.txt" not in explicit
    assert "/dev/fd/" not in complete
    single = workspace._grep_search(tmp_path, "needle", root="workspace/record-074.txt")
    assert "1 match(es)" in single and "record-074.txt:1:needle 74" in single


@pytest.mark.parametrize("native", [False, True])
def test_grep_search_binds_and_searches_the_same_snapshot_bytes(monkeypatch, tmp_path, native):
    import hashlib
    import os
    from uuid import uuid4
    from app.services.agent_tool_domains import workspace
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope, WorkspaceExecutionInputs, WorkspaceInputRead

    relative = "workspace/report.txt"
    target = tmp_path / relative
    target.parent.mkdir()
    original = b"verified line\n"
    target.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    receipt = uuid4()
    scope = WorkspaceAuthorityScope(agent_id=uuid4(), user_id=uuid4(), root_session_id=uuid4(),
        allowed_paths=frozenset({relative}), operator_view=False, authority_source="resource_owner",
        input_reads=(WorkspaceInputRead(relative, receipt, 1, digest, "synthetic-dependencies"),),
        execution_inputs=WorkspaceExecutionInputs())
    descriptors = []
    if native:
        assert workspace.shutil.which("rg")
        real_run = workspace.subprocess.run
        def change_original_before_search(command, **kwargs):
            descriptors.extend(kwargs["pass_fds"])
            target.write_text("changed private line\n")
            return real_run(command, **kwargs)
        monkeypatch.setattr(workspace.subprocess, "run", change_original_before_search)
    else:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
        native_read = workspace._read_receipted_workspace_bytes
        def change_original_after_read(*args, **kwargs):
            raw = native_read(*args, **kwargs)
            target.write_text("changed private line\n")
            return raw
        monkeypatch.setattr(workspace, "_read_receipted_workspace_bytes", change_original_after_read)
    result = workspace._grep_search(tmp_path, "line", root="workspace", authority_scope=scope)
    assert "workspace/report.txt:1:verified line" in result and "changed private" not in result
    assert target.read_text() == "changed private line\n"
    assert scope.execution_inputs.snapshot() == {"schema": "hive.workspace_execution_inputs.v1", "observed": True,
        "sources": [{"source_kind": "workspace_artifact", "source_id": str(receipt), "source_version": digest}], "unbound": []}
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
