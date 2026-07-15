from __future__ import annotations


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
            stdout=f"{target}:1:--files\n",
            stderr="",
        )

    monkeypatch.setattr(workspace.shutil, "which", lambda name: "/usr/bin/rg" if name == "rg" else None)
    monkeypatch.setattr(workspace.subprocess, "run", fake_run)

    result = workspace._grep_search(tmp_path, "--files", root="workspace")

    assert captured["command"] == [
        "rg",
        "--line-number",
        "--color",
        "never",
        "--",
        "--files",
        str(search_root.resolve()),
    ]
    assert captured["kwargs"] == {"capture_output": True, "text": True, "check": False}
    assert "workspace/record.txt:1:--files" in result


def test_grep_search_fallback_treats_pattern_as_regex(monkeypatch, tmp_path):
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

    monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)

    result = workspace._grep_search(
        tmp_path,
        r"净利润:\s*-",
        root="workspace/tool_results",
    )

    assert "records.txt:6" in result
    assert "净利润: -20000000" in result
    assert "No matches" not in result


def test_glob_search_preserves_more_than_one_hundred_matches(tmp_path):
    from app.services.agent_tool_domains import workspace

    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(125):
        (root / f"record-{index:03d}.md").write_text(str(index), encoding="utf-8")

    result = workspace._glob_search(tmp_path, "workspace/*.md")

    assert "125 match(es)" in result
    assert "record-124.md" in result


def test_grep_search_defaults_to_complete_results_and_honors_explicit_limit(monkeypatch, tmp_path):
    from app.services.agent_tool_domains import workspace

    root = tmp_path / "workspace"
    root.mkdir()
    for index in range(75):
        (root / f"record-{index:03d}.txt").write_text(f"needle {index}\n", encoding="utf-8")
    monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)

    complete = workspace._grep_search(tmp_path, "needle", root="workspace")
    explicit = workspace._grep_search(tmp_path, "needle", root="workspace", max_results=7)

    assert "75 match(es)" in complete
    assert "record-074.txt" in complete
    assert "7 match(es)" in explicit
    assert "record-007.txt" not in explicit
