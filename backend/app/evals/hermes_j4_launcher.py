"""Process-local boundary for the P08-J4 Hermes comparison runtime."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import ssl
import sqlite3
import sys
import tomllib
from contextlib import redirect_stdout
from functools import wraps
from pathlib import Path
from typing import Any


SOURCE_ROOT_ENV = "HIVE_J4_HERMES_SOURCE_ROOT"
SITE_PACKAGES_ENV = "HIVE_J4_HERMES_SITE_PACKAGES"
STATE_DB_ENV = "HIVE_J4_HERMES_STATE_DB"
WORKSPACE_ROOT_ENV = "HIVE_J4_WORKSPACE_ROOT"
EXPECTED_SOURCE = "p08-j4"


def _required_directory(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if not raw or not path.is_absolute():
        raise RuntimeError(f"{name} must be an absolute directory")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise RuntimeError(f"{name} must be an absolute directory")
    return resolved


def _required_state_db() -> Path:
    raw = os.environ.get(STATE_DB_ENV, "").strip()
    path = Path(raw).expanduser() if raw else Path()
    if not raw or not path.is_absolute():
        raise RuntimeError(f"{STATE_DB_ENV} must be an absolute path")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise RuntimeError(f"{STATE_DB_ENV} parent must be a directory")
    return path.resolve(strict=False)


def _require_attempt_environment(state_root: Path) -> None:
    for name, relative in (
        ("HERMES_HOME", "hermes-home"),
        ("HOME", "os-home"),
        ("CODEX_HOME", "codex-home"),
        ("HERMES_MANAGED_DIR", "managed"),
        ("TMPDIR", "tmp"),
    ):
        raw = os.environ.get(name, "").strip()
        if not raw:
            raise RuntimeError(f"{name} must be attempt-local")
        try:
            observed = Path(raw).expanduser().resolve(strict=True)
            expected = (state_root / relative).resolve(strict=True)
        except OSError as exc:
            raise RuntimeError(f"{name} must be attempt-local") from exc
        if observed != expected or not observed.is_dir():
            raise RuntimeError(f"{name} must be attempt-local")
    temporary = (state_root / "tmp").resolve(strict=True)
    for name in ("TMP", "TEMP"):
        if Path(os.environ.get(name, "")).resolve(strict=False) != temporary:
            raise RuntimeError(f"{name} must be attempt-local")
    for name in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES", "PYTHONNOUSERSITE"):
        if os.environ.get(name) != "1":
            raise RuntimeError(f"{name} must be enabled")
    bundled_skills = (state_root / "nonexistent-bundled-skills").resolve(strict=False)
    if (
        Path(os.environ.get("HERMES_BUNDLED_SKILLS", "")).resolve(strict=False) != bundled_skills
        or bundled_skills.exists()
    ):
        raise RuntimeError("HERMES_BUNDLED_SKILLS must stay unavailable")
    if os.environ.get("HERMES_CODEX_BASE_URL"):
        raise RuntimeError("HERMES_CODEX_BASE_URL must be absent")


def _configure_import_paths(source_root: Path, site_packages: Path) -> None:
    configured = (str(source_root), str(site_packages))
    sys.path[:] = [entry for entry in sys.path if entry not in configured]
    sys.path[:0] = configured


def _resolve_workspace_path(workspace_root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid workspace path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("path is outside the J4 workspace") from exc
    return resolved


def _tool_error(code: str) -> str:
    return json.dumps({"success": False, "error": code}, sort_keys=True, separators=(",", ":"))


def _install_workspace_file_guards(file_tools: Any, workspace_root: Path) -> None:
    original_read = file_tools.read_file_tool
    original_search = file_tools.search_tool

    @wraps(original_read)
    def guarded_read(path: str, offset: int = 1, limit: int = 2000, task_id: str = "default") -> str:
        try:
            resolved = _resolve_workspace_path(workspace_root, path)
            return original_read(str(resolved), offset=offset, limit=limit, task_id=task_id)
        except Exception:  # Tool boundaries return typed errors to the model.
            return _tool_error("j4_workspace_read_denied")

    @wraps(original_search)
    def guarded_search(
        pattern: str,
        target: str = "content",
        path: str = ".",
        file_glob: str | None = None,
        limit: int = 50,
        offset: int = 0,
        output_mode: str = "content",
        context: int = 0,
        task_id: str = "default",
    ) -> str:
        try:
            resolved = _resolve_workspace_path(workspace_root, path)
            return original_search(
                pattern,
                target=target,
                path=str(resolved),
                file_glob=file_glob,
                limit=limit,
                offset=offset,
                output_mode=output_mode,
                context=context,
                task_id=task_id,
            )
        except Exception:  # Tool boundaries return typed errors to the model.
            return _tool_error("j4_workspace_search_denied")

    file_tools.read_file_tool = guarded_read
    file_tools.search_tool = guarded_search


def _failed_attestation(
    *,
    session_id: str = "",
    expected_source: str = "",
    errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "session_id": session_id,
        "tip_session_id": None,
        "lineage_session_ids": [],
        "session_count": 0,
        "lineage_count": 0,
        "source": expected_source,
        "all_sources_match": False,
        "first_user_content_sha256": None,
        "prompt_sha256_match": False,
        "tip_id_match": False,
        "unique_lineage": False,
        "errors": errors or ["attestation_failed"],
    }


def _first_user_content_sha256(session_db: Any, lineage: list[str]) -> str | None:
    for lineage_id in lineage:
        messages = session_db.get_messages(lineage_id, include_inactive=True)
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if not isinstance(content, str):
                    return None
                return hashlib.sha256(content.encode("utf-8")).hexdigest()
    return None


def _attest_session(
    session_db_type: Any,
    *,
    db_path: Path,
    session_id: str,
    expected_prompt_sha256: str,
    expected_source: str,
) -> dict[str, Any]:
    payload = _failed_attestation(session_id=session_id, expected_source=expected_source)
    session_db = None
    try:
        session_db = session_db_type(db_path=db_path, read_only=True)
        session_count = session_db.session_count(include_archived=True)
        lineage = session_db.get_compression_lineage(session_id)
        if not isinstance(lineage, list) or any(not isinstance(value, str) or not value for value in lineage):
            lineage = []
        lineage_count = len(lineage)
        sessions = [session_db.get_session(value) for value in lineage]
        all_sources_match = bool(sessions) and all(
            isinstance(session, dict) and session.get("source") == expected_source for session in sessions
        )
        links_match = bool(sessions) and isinstance(sessions[0], dict) and not sessions[0].get("parent_session_id")
        for index in range(1, len(sessions)):
            parent = sessions[index - 1]
            child = sessions[index]
            links_match = links_match and bool(
                isinstance(parent, dict)
                and isinstance(child, dict)
                and parent.get("end_reason") == "compression"
                and child.get("parent_session_id") == lineage[index - 1]
            )
        tip_session_id = session_db.get_compression_tip(lineage[0]) if lineage else None
        tip_id_match = bool(lineage) and tip_session_id == lineage[-1] == session_id
        unique_lineage = bool(lineage) and len(set(lineage)) == lineage_count == session_count and links_match
        prompt_sha256 = _first_user_content_sha256(session_db, lineage)
        prompt_sha256_match = prompt_sha256 == expected_prompt_sha256
        errors: list[str] = []
        if not unique_lineage:
            errors.append("compression_lineage")
        if not all_sources_match:
            errors.append("source")
        if not tip_id_match:
            errors.append("tip_session_id")
        if not prompt_sha256_match:
            errors.append("prompt_sha256")
        payload = {
            "ok": not errors,
            "session_id": session_id,
            "tip_session_id": tip_session_id,
            "lineage_session_ids": lineage,
            "session_count": session_count,
            "lineage_count": lineage_count,
            "source": expected_source,
            "all_sources_match": all_sources_match,
            "first_user_content_sha256": prompt_sha256,
            "prompt_sha256_match": prompt_sha256_match,
            "tip_id_match": tip_id_match,
            "unique_lineage": unique_lineage,
            "errors": errors,
        }
    except Exception:
        payload["errors"] = ["session_db"]
    finally:
        close = getattr(session_db, "close", None)
        if callable(close):
            close()
    return payload


def _parse_attest_arguments(argv: list[str]) -> tuple[str, str, str] | None:
    if len(argv) != 7 or argv[0] != "attest-session":
        return None
    arguments: dict[str, str] = {}
    for index in range(1, len(argv), 2):
        name, value = argv[index : index + 2]
        if name in arguments:
            return None
        arguments[name] = value
    if set(arguments) != {"--session-id", "--expected-prompt-sha256", "--expected-source"}:
        return None
    session_id = arguments["--session-id"].strip()
    prompt_sha256 = arguments["--expected-prompt-sha256"].strip().lower()
    expected_source = arguments["--expected-source"].strip()
    if (
        not session_id
        or len(prompt_sha256) != 64
        or any(character not in "0123456789abcdef" for character in prompt_sha256)
        or expected_source != EXPECTED_SOURCE
    ):
        return None
    return session_id, prompt_sha256, expected_source


def _module_path(module: Any) -> Path | None:
    value = getattr(module, "__file__", None)
    if not isinstance(value, str) or not value or value.startswith("<"):
        return None
    try:
        return Path(value).resolve(strict=True)
    except OSError:
        return None


def _inside(root: Path, path: Path | None) -> bool:
    if path is None:
        return False
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _attest_runtime(source_root: Path, site_packages: Path, expected_version: str) -> dict[str, Any]:
    base_root = Path(sys.executable).resolve().parent.parent
    errors: list[str] = []
    try:
        source_version = str(
            tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        )
    except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError):
        source_version = ""
    stdlib_modules = {
        "json": importlib.import_module("json"),
        "sqlite3": sqlite3,
        "ssl": ssl,
        "_ssl": importlib.import_module("_ssl"),
    }
    hermes_modules = {
        name: importlib.import_module(name) for name in ("hermes_state", "tools.file_tools", "hermes_cli.main")
    }
    executable_match = Path(sys.executable).resolve() == base_root / "bin" / Path(sys.executable).name
    prefix_match = Path(sys.prefix).resolve() == base_root and Path(sys.base_prefix).resolve() == base_root
    stdlib_origins_match = all(_inside(base_root, _module_path(module)) for module in stdlib_modules.values())
    hermes_origins_match = all(_inside(source_root, _module_path(module)) for module in hermes_modules.values())
    allowed_roots = (base_root, source_root, site_packages)
    launcher_path = Path(__file__).resolve()
    unexpected_loaded_origins = 0
    inspected_loaded_origins = 0
    for module in tuple(sys.modules.values()):
        origin = _module_path(module)
        if origin is None:
            continue
        inspected_loaded_origins += 1
        if origin != launcher_path and not any(_inside(root, origin) for root in allowed_roots):
            unexpected_loaded_origins += 1
    if not executable_match:
        errors.append("executable")
    if not prefix_match:
        errors.append("prefix")
    if not stdlib_origins_match:
        errors.append("stdlib_origins")
    if not hermes_origins_match:
        errors.append("hermes_origins")
    if unexpected_loaded_origins:
        errors.append("loaded_origins")
    if source_version != expected_version:
        errors.append("version")
    return {
        "ok": not errors,
        "executable_match": executable_match,
        "prefix_match": prefix_match,
        "stdlib_origins_match": stdlib_origins_match,
        "hermes_origins_match": hermes_origins_match,
        "unexpected_loaded_origin_count": unexpected_loaded_origins,
        "inspected_loaded_origin_count": inspected_loaded_origins,
        "source_version": source_version,
        "errors": errors,
    }


def _load_runtime_environment() -> tuple[Path, Path, Path, Path]:
    source_root = _required_directory(SOURCE_ROOT_ENV)
    site_packages = _required_directory(SITE_PACKAGES_ENV)
    state_db = _required_state_db()
    _require_attempt_environment(state_db.parent)
    workspace_root = _required_directory(WORKSPACE_ROOT_ENV)
    _configure_import_paths(source_root, site_packages)
    os.environ["TERMINAL_CWD"] = str(workspace_root)
    os.environ["HERMES_WRITE_SAFE_ROOT"] = str(workspace_root)
    return source_root, site_packages, state_db, workspace_root


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["attest-runtime"]:
        expected_version = arguments[2] if len(arguments) == 3 and arguments[1] == "--expected-version" else ""
        try:
            with redirect_stdout(sys.stderr):
                source_root, site_packages, state_db, _workspace_root = _load_runtime_environment()
                hermes_state = importlib.import_module("hermes_state")
                hermes_state.DEFAULT_DB_PATH = state_db
                payload = _attest_runtime(source_root, site_packages, expected_version)
        except Exception:
            payload = {
                "ok": False,
                "executable_match": False,
                "prefix_match": False,
                "stdlib_origins_match": False,
                "hermes_origins_match": False,
                "unexpected_loaded_origin_count": -1,
                "inspected_loaded_origin_count": 0,
                "source_version": "",
                "errors": ["launcher"],
            }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    if arguments[:1] == ["attest-session"]:
        parsed = _parse_attest_arguments(arguments)
        if parsed is None:
            payload = _failed_attestation(errors=["invalid_arguments"])
        else:
            session_id, prompt_sha256, expected_source = parsed
            try:
                with redirect_stdout(sys.stderr):
                    _source_root, _site_packages, state_db, _workspace_root = _load_runtime_environment()
                    hermes_state = importlib.import_module("hermes_state")
                    hermes_state.DEFAULT_DB_PATH = state_db
                    payload = _attest_session(
                        hermes_state.SessionDB,
                        db_path=state_db,
                        session_id=session_id,
                        expected_prompt_sha256=prompt_sha256,
                        expected_source=expected_source,
                    )
            except Exception:
                payload = _failed_attestation(
                    session_id=session_id,
                    expected_source=expected_source,
                    errors=["launcher"],
                )
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return

    _source_root, _site_packages, state_db, workspace_root = _load_runtime_environment()
    hermes_state = importlib.import_module("hermes_state")
    hermes_state.DEFAULT_DB_PATH = state_db
    file_tools = importlib.import_module("tools.file_tools")
    _install_workspace_file_guards(file_tools, workspace_root)
    hermes_main = importlib.import_module("hermes_cli.main").main
    hermes_main()


if __name__ == "__main__":
    main()
