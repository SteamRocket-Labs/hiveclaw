from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_officecli_adapter_allows_only_whitelisted_commands(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLICommandError

    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLICommandError):
        adapter.run("shell", tmp_path / "demo.docx")

    assert calls == []


def test_officecli_adapter_forces_json_and_disables_auto_update(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter

    captured = {}

    def fake_runner(args, *, cwd, env, timeout, capture_output, text, check):
        captured["args"] = args
        captured["cwd"] = cwd
        captured["env"] = env
        captured["timeout"] = timeout
        captured["capture_output"] = capture_output
        captured["text"] = text
        captured["check"] = check
        return SimpleNamespace(returncode=0, stdout='{"outline": ["Title"]}', stderr="")

    path = tmp_path / "demo.docx"
    path.write_bytes(b"demo")
    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner, timeout_seconds=12)

    result = adapter.run("view", path, options={"mode": "outline", "page": 2}, cwd=tmp_path)

    assert result == {"outline": ["Title"]}
    assert captured["args"] == [
        "officecli",
        "view",
        str(path),
        "--json",
        "--mode",
        "outline",
        "--page",
        "2",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["env"]["OFFICECLI_SKIP_UPDATE"] == "1"
    assert captured["timeout"] == 12
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False


def test_officecli_adapter_rejects_invalid_json(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLIOutputError

    def fake_runner(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLIOutputError):
        adapter.run("validate", tmp_path / "demo.docx")


def test_officecli_adapter_surfaces_nonzero_exit(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLIExecutionError

    def fake_runner(*args, **kwargs):
        return SimpleNamespace(returncode=2, stdout='{"error": "bad document"}', stderr="failed")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLIExecutionError) as exc:
        adapter.run("validate", tmp_path / "demo.docx")

    assert exc.value.returncode == 2
    assert exc.value.payload == {"error": "bad document"}
    assert "failed" in str(exc.value)
