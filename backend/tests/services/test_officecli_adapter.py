from __future__ import annotations

import subprocess
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
        "outline",
        "--json",
        "--page",
        "2",
    ]
    assert captured["cwd"] == tmp_path
    assert captured["env"]["OFFICECLI_SKIP_UPDATE"] == "1"
    assert captured["timeout"] == 12
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["check"] is False


def test_officecli_adapter_rejects_unknown_view_mode_before_execution(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLICommandError

    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLICommandError):
        adapter.run_view(tmp_path / "demo.docx", mode="shell")

    assert calls == []


def test_officecli_adapter_rejects_reserved_view_options_before_execution(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLICommandError

    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLICommandError):
        adapter.run_view(tmp_path / "demo.docx", mode="html", options={"mode": "text"})

    assert calls == []


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


def test_officecli_adapter_maps_missing_binary_to_typed_execution_error(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLIExecutionError

    def missing_runner(*_args, **_kwargs):
        raise FileNotFoundError("officecli")

    adapter = OfficeCLIAdapter(binary="missing-officecli", runner=missing_runner)

    with pytest.raises(OfficeCLIExecutionError) as exc:
        adapter.run_view(tmp_path / "demo.docx", mode="html")

    assert exc.value.command == "view"
    assert exc.value.returncode == 127
    assert "binary is unavailable" in str(exc.value)


def test_officecli_adapter_maps_timeout_to_typed_timeout_error(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLITimeoutError

    def timeout_runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="officecli", timeout=3)

    adapter = OfficeCLIAdapter(binary="officecli", runner=timeout_runner, timeout_seconds=3)

    with pytest.raises(OfficeCLITimeoutError) as exc:
        adapter.run_view(tmp_path / "demo.docx", mode="text")

    assert exc.value.command == "view"
    assert exc.value.timeout_seconds == 3
