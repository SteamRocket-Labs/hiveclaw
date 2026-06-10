from __future__ import annotations


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
