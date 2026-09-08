from __future__ import annotations

import hashlib
import json
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


def test_officecli_adapter_resolves_and_executes_the_path_verified_by_sha256(tmp_path, monkeypatch):
    from app.services.officecli_adapter import OfficeCLIAdapter

    binary = tmp_path / "officecli"
    binary.write_bytes(b"release-officecli-binary")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    captured = {}

    def fake_runner(args, **_kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    adapter = OfficeCLIAdapter(
        binary="officecli",
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        runner=fake_runner,
    )

    assert adapter.run_view(tmp_path / "demo.docx", mode="html") == {"success": True}
    assert captured["args"][0] == str(binary)


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


def test_officecli_adapter_run_batch_uses_real_input_contract(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter

    commands = tmp_path / "commands.json"
    commands.write_text('[{"command": "validate"}]', encoding="utf-8")
    captured = {}

    def fake_runner(args, **_kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)
    doc = tmp_path / "demo.docx"

    assert adapter.run_batch(doc, input_file=commands, cwd=tmp_path) == {"success": True}
    assert captured["args"] == [
        "officecli",
        "batch",
        str(doc),
        "--json",
        "--input",
        str(commands),
        "--stop-on-error",
    ]


def test_officecli_adapter_run_batch_can_continue_past_failures_when_requested(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter

    commands = tmp_path / "commands.json"
    commands.write_text("[]", encoding="utf-8")
    captured = {}

    def fake_runner(args, **_kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    adapter.run_batch(tmp_path / "demo.docx", input_file=commands, stop_on_error=False)

    assert "--stop-on-error" not in captured["args"]
    assert "--force" not in captured["args"]
    assert "--operations" not in captured["args"]
    assert "--output" not in captured["args"]


def test_officecli_adapter_run_batch_rejects_missing_input_file_before_execution(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLICommandError

    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"success": true}', stderr="")

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLICommandError):
        adapter.run_batch(tmp_path / "demo.docx", input_file=tmp_path / "missing.json")

    assert calls == []


def test_officecli_adapter_batch_is_not_reachable_through_generic_flag_serializer(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLICommandError

    adapter = OfficeCLIAdapter(
        binary="officecli", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="{}", stderr="")
    )

    with pytest.raises(OfficeCLICommandError):
        adapter.run(
            "batch",
            tmp_path / "demo.docx",
            options={"operations": str(tmp_path / "ops.json")},
        )


def test_officecli_adapter_nonzero_exit_with_non_json_stdout_reports_execution_error(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLIExecutionError

    def fake_runner(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=2,
            stdout="unrecognized command or argument 'operations'",
            stderr="batch failed",
        )

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLIExecutionError) as exc:
        adapter.run_batch(
            tmp_path / "demo.docx",
            input_file=_write_commands(tmp_path),
        )

    assert exc.value.returncode == 2
    assert exc.value.payload is None
    assert "batch failed" in str(exc.value)


def test_officecli_adapter_nonzero_exit_preserves_structured_payload_with_warning_stderr(tmp_path):
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLIExecutionError

    stdout = json.dumps(
        {
            "success": False,
            "data": {
                "results": [
                    {
                        "index": 0,
                        "success": False,
                        "error": (
                            "Unknown element type 'heading' for /body. Valid types: paragraph (p), "
                            "run (r), table (tbl), row, cell, picture, chart, ole (object, embed), "
                            "equation, comment, section, footnote, endnote, toc, style, watermark, "
                            "bookmark, hyperlink, field, break, sdt, header, footer. "
                            "Use 'officecli docx add' for details."
                        ),
                        "item": {
                            "command": "add",
                            "parent": "/body",
                            "type": "heading",
                            "props": {"text": "synthetic", "level": "1"},
                        },
                    }
                ],
                "summary": {"total": 2, "executed": 1, "succeeded": 0, "failed": 1, "skipped": 1},
            },
        }
    )

    def fake_runner(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=stdout,
            stderr=(
                "Warning: batch is reading from --commands/--input but stdin is also "
                "redirected; stdin will be ignored..."
            ),
        )

    adapter = OfficeCLIAdapter(binary="officecli", runner=fake_runner)

    with pytest.raises(OfficeCLIExecutionError) as exc:
        adapter.run_batch(
            tmp_path / "demo.docx",
            input_file=_write_commands(tmp_path),
        )

    assert exc.value.returncode == 1
    assert "stdin will be ignored" in exc.value.stderr
    assert exc.value.payload == json.loads(stdout)
    failed_result = exc.value.payload["data"]["results"][0]
    assert "Unknown element type 'heading'" in failed_result["error"]
    assert exc.value.payload["data"]["summary"] == {
        "total": 2,
        "executed": 1,
        "succeeded": 0,
        "failed": 1,
        "skipped": 1,
    }


def _write_commands(root):
    commands = root / "commands.json"
    commands.write_text('[{"command": "validate"}]', encoding="utf-8")
    return commands
