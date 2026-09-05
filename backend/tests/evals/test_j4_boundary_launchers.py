from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def _hermes_attempt_environment(state_root: Path) -> dict[str, str]:
    directories = {
        "HERMES_HOME": state_root / "hermes-home",
        "HOME": state_root / "os-home",
        "CODEX_HOME": state_root / "codex-home",
        "HERMES_MANAGED_DIR": state_root / "managed",
        "TMPDIR": state_root / "tmp",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    temporary = str(directories["TMPDIR"].resolve())
    return {
        **{name: str(path.resolve()) for name, path in directories.items()},
        "TMP": temporary,
        "TEMP": temporary,
        "HERMES_SAFE_MODE": "1",
        "HERMES_IGNORE_USER_CONFIG": "1",
        "HERMES_IGNORE_RULES": "1",
        "HERMES_BUNDLED_SKILLS": str((state_root / "nonexistent-bundled-skills").resolve()),
        "PYTHONNOUSERSITE": "1",
    }


def test_hermes_file_guards_confine_reads_and_searches_and_return_errors(tmp_path: Path) -> None:
    import app.evals.hermes_j4_launcher as launcher

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    calls: list[tuple[str, str]] = []

    def read_file(path: str, **_kwargs) -> str:
        calls.append(("read", path))
        return "read-ok"

    def search_files(_pattern: str, *, path: str, **_kwargs) -> str:
        calls.append(("search", path))
        return "search-ok"

    file_tools = SimpleNamespace(read_file_tool=read_file, search_tool=search_files)
    launcher._install_workspace_file_guards(file_tools, workspace.resolve())

    assert file_tools.read_file_tool("inside.txt") == "read-ok"
    assert file_tools.search_tool("inside", path=".") == "search-ok"
    assert calls == [("read", str(workspace / "inside.txt")), ("search", str(workspace))]

    for path in (outside / "secret.txt", workspace / "escape" / "secret.txt", "../outside/secret.txt"):
        error = json.loads(file_tools.read_file_tool(str(path)))
        assert error == {"success": False, "error": "j4_workspace_read_denied"}
    search_error = json.loads(file_tools.search_tool("secret", path=str(workspace / "escape")))
    assert search_error == {"success": False, "error": "j4_workspace_search_denied"}
    assert len(calls) == 2

    def exploding_read(_path: str, **_kwargs) -> str:
        raise RuntimeError("must not escape the tool boundary")

    exploding_tools = SimpleNamespace(read_file_tool=exploding_read, search_tool=search_files)
    launcher._install_workspace_file_guards(exploding_tools, workspace.resolve())
    assert json.loads(exploding_tools.read_file_tool("inside.txt")) == {
        "success": False,
        "error": "j4_workspace_read_denied",
    }


def test_hermes_launcher_supports_isolated_site_packages_and_installs_guard(tmp_path: Path) -> None:
    import app.evals.hermes_j4_launcher as launcher

    source_root = tmp_path / "source"
    site_packages = tmp_path / "site-packages"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    outside = tmp_path / "outside.txt"
    for directory in (source_root / "hermes_cli", source_root / "tools", site_packages, workspace, state_root):
        directory.mkdir(parents=True, exist_ok=True)
    outside.write_text("private", encoding="utf-8")
    (site_packages / "j4_site_dependency.py").write_text('VALUE = "from-site-packages"\n', encoding="utf-8")
    (source_root / "hermes_state.py").write_text(
        "from pathlib import Path\n"
        "from j4_site_dependency import VALUE\n"
        "DEFAULT_DB_PATH = Path('/unredirected/state.db')\n",
        encoding="utf-8",
    )
    (source_root / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (source_root / "tools" / "file_tools.py").write_text(
        "def read_file_tool(path, **kwargs):\n"
        "    return path\n"
        "def search_tool(pattern, target='content', path='.', **kwargs):\n"
        "    return path\n",
        encoding="utf-8",
    )
    (source_root / "hermes_cli" / "main.py").write_text(
        "import json\n"
        "import os\n"
        "import hermes_state\n"
        "from tools import file_tools\n"
        "def main():\n"
        "    print(json.dumps({\n"
        "        'site_value': hermes_state.VALUE,\n"
        "        'state_db': str(hermes_state.DEFAULT_DB_PATH),\n"
        "        'write_root': os.environ.get('HERMES_WRITE_SAFE_ROOT'),\n"
        "        'terminal_cwd': os.environ.get('TERMINAL_CWD'),\n"
        "        'outside_read': json.loads(file_tools.read_file_tool(os.environ['J4_TEST_OUTSIDE'])),\n"
        "    }))\n",
        encoding="utf-8",
    )
    state_db = state_root / "state.db"
    attempt_env = _hermes_attempt_environment(state_root)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-S", str(Path(launcher.__file__).resolve()), "--version"],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "PATH": os.environ.get("PATH", ""),
            launcher.SOURCE_ROOT_ENV: str(source_root),
            launcher.SITE_PACKAGES_ENV: str(site_packages),
            launcher.STATE_DB_ENV: str(state_db),
            launcher.WORKSPACE_ROOT_ENV: str(workspace),
            "J4_TEST_OUTSIDE": str(outside),
            **attempt_env,
        },
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "site_value": "from-site-packages",
        "state_db": str(state_db),
        "write_root": str(workspace),
        "terminal_cwd": str(workspace),
        "outside_read": {"success": False, "error": "j4_workspace_read_denied"},
    }


def test_hermes_attest_session_uses_read_only_db_and_emits_no_prompt(monkeypatch, tmp_path: Path, capsys) -> None:
    import app.evals.hermes_j4_launcher as launcher

    prompt = "private benchmark prompt bytes"
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    source_root = tmp_path / "source"
    site_packages = tmp_path / "site-packages"
    workspace = tmp_path / "workspace"
    state_root = tmp_path / "state"
    for directory in (source_root, site_packages, workspace, state_root):
        directory.mkdir()
    state_db = state_root / "state.db"
    opened: list[tuple[Path, bool]] = []
    closed: list[bool] = []

    class FakeSessionDB:
        def __init__(self, db_path: Path, read_only: bool = False):
            opened.append((db_path, read_only))

        def session_count(self, *, include_archived: bool) -> int:
            assert include_archived is True
            return 2

        def get_compression_lineage(self, session_id: str) -> list[str]:
            assert session_id == "tip-id"
            return ["root-id", "tip-id"]

        def get_compression_tip(self, session_id: str) -> str:
            assert session_id == "root-id"
            return "tip-id"

        def get_session(self, session_id: str) -> dict[str, object]:
            if session_id == "root-id":
                return {
                    "id": "root-id",
                    "source": "p08-j4",
                    "parent_session_id": None,
                    "end_reason": "compression",
                }
            return {
                "id": "tip-id",
                "source": "p08-j4",
                "parent_session_id": "root-id",
                "end_reason": None,
            }

        def get_messages(self, session_id: str, *, include_inactive: bool) -> list[dict[str, str]]:
            assert include_inactive is True
            return [{"role": "user", "content": prompt}] if session_id == "root-id" else []

        def close(self) -> None:
            closed.append(True)

    hermes_state = ModuleType("hermes_state")
    hermes_state.DEFAULT_DB_PATH = Path("/unused")
    hermes_state.SessionDB = FakeSessionDB
    monkeypatch.setitem(sys.modules, "hermes_state", hermes_state)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv(launcher.SOURCE_ROOT_ENV, str(source_root))
    monkeypatch.setenv(launcher.SITE_PACKAGES_ENV, str(site_packages))
    monkeypatch.setenv(launcher.STATE_DB_ENV, str(state_db))
    monkeypatch.setenv(launcher.WORKSPACE_ROOT_ENV, str(workspace))
    for name, value in _hermes_attempt_environment(state_root).items():
        monkeypatch.setenv(name, value)

    launcher.main(
        [
            "attest-session",
            "--session-id",
            "tip-id",
            "--expected-prompt-sha256",
            prompt_sha256,
            "--expected-source",
            "p08-j4",
        ]
    )

    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert prompt not in captured.out
    payload = json.loads(captured.out)
    assert payload == {
        "ok": True,
        "session_id": "tip-id",
        "tip_session_id": "tip-id",
        "lineage_session_ids": ["root-id", "tip-id"],
        "session_count": 2,
        "lineage_count": 2,
        "source": "p08-j4",
        "all_sources_match": True,
        "first_user_content_sha256": prompt_sha256,
        "prompt_sha256_match": True,
        "tip_id_match": True,
        "unique_lineage": True,
        "errors": [],
    }
    assert opened == [(state_db.resolve(), True)]
    assert closed == [True]


def test_hermes_attest_session_rejects_extra_session_and_prompt_mismatch(tmp_path: Path) -> None:
    import app.evals.hermes_j4_launcher as launcher

    class FakeSessionDB:
        def __init__(self, db_path: Path, read_only: bool = False):
            assert db_path == tmp_path / "state.db"
            assert read_only is True

        def session_count(self, *, include_archived: bool) -> int:
            return 2

        def get_compression_lineage(self, _session_id: str) -> list[str]:
            return ["tip-id"]

        def get_compression_tip(self, _session_id: str) -> str:
            return "tip-id"

        def get_session(self, _session_id: str) -> dict[str, object]:
            return {"source": "p08-j4", "parent_session_id": None, "end_reason": None}

        def get_messages(self, _session_id: str, *, include_inactive: bool) -> list[dict[str, str]]:
            return [{"role": "user", "content": "different"}]

    payload = launcher._attest_session(
        FakeSessionDB,
        db_path=tmp_path / "state.db",
        session_id="tip-id",
        expected_prompt_sha256="0" * 64,
        expected_source="p08-j4",
    )

    assert payload["ok"] is False
    assert payload["unique_lineage"] is False
    assert payload["prompt_sha256_match"] is False
    assert payload["errors"] == ["compression_lineage", "prompt_sha256"]
    assert "different" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("tool_name", "tool_input", "expected_relative"),
    [
        ("Read", {"file_path": "inside.txt"}, "inside.txt"),
        ("Write", {"file_path": "created.txt", "content": "secret"}, "created.txt"),
        ("Edit", {"file_path": "inside.txt", "old_string": "a", "new_string": "b"}, "inside.txt"),
        ("Glob", {"path": ".", "pattern": "**/*.py"}, "."),
        ("Grep", {"pattern": "needle"}, "."),
    ],
)
def test_freecode_hook_allows_only_five_file_tools_in_workspace(
    tmp_path: Path,
    tool_name: str,
    tool_input: dict[str, object],
    expected_relative: str,
) -> None:
    import app.evals.freecode_j4_hook as hook

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    allowed, relative, _reason, logged_tool, tool_use_id_hash = hook._evaluate(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": "tool-use-1",
        },
        workspace.resolve(),
    )

    assert allowed is True
    assert relative == expected_relative
    assert logged_tool == tool_name
    assert tool_use_id_hash == hashlib.sha256(b"tool-use-1").hexdigest()


def test_freecode_hook_denies_outside_symlink_and_absolute_glob(tmp_path: Path) -> None:
    import app.evals.freecode_j4_hook as hook

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    for tool_name, tool_input in (
        ("Read", {"file_path": str(outside / "secret.txt")}),
        ("Write", {"file_path": str(workspace / "escape" / "new.txt"), "content": "secret"}),
        ("Glob", {"path": ".", "pattern": str(outside / "*.txt")}),
        ("Glob", {"path": ".", "pattern": "{../outside,**}/*.txt"}),
    ):
        allowed, relative, _reason, _tool, _hash = hook._evaluate(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": "tool-use-2",
            },
            workspace.resolve(),
        )
        assert allowed is False
        assert relative == "<outside>"


def test_freecode_hook_script_emits_permission_json_and_minimal_log(tmp_path: Path) -> None:
    import app.evals.freecode_j4_hook as hook

    script = Path(hook.__file__).resolve()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    log_path = tmp_path / "hook.jsonl"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    assert script.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")
    assert script.stat().st_mode & stat.S_IXUSR
    env = os.environ.copy()
    env[hook.WORKSPACE_ROOT_ENV] = str(workspace)
    env[hook.LOG_PATH_ENV] = str(log_path)
    secret = "must-not-appear-in-log"
    allowed_input = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(workspace / "answer.md"), "content": secret},
        "tool_use_id": "write-call",
    }
    allowed = subprocess.run(  # noqa: S603
        [str(script)],
        input=json.dumps(allowed_input),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert allowed.returncode == 0
    assert allowed.stderr == ""
    assert len(allowed.stdout.splitlines()) == 1
    assert json.loads(allowed.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            "allowed": True,
            "resolved_relative_path": "answer.md",
            "tool_name": "Write",
            "tool_use_id_hash": hashlib.sha256(b"write-call").hexdigest(),
        }
    ]
    assert secret not in log_path.read_text(encoding="utf-8")

    denied_input = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(outside / "secret.txt")},
        "tool_use_id": "read-call",
    }
    denied = subprocess.run(  # noqa: S603
        [str(script)],
        input=json.dumps(denied_input),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )

    assert denied.returncode == 0
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    log_text = log_path.read_text(encoding="utf-8")
    assert str(outside) not in log_text
    assert json.loads(log_text.splitlines()[-1]) == {
        "allowed": False,
        "resolved_relative_path": "<outside>",
        "tool_name": "Read",
        "tool_use_id_hash": hashlib.sha256(b"read-call").hexdigest(),
    }
