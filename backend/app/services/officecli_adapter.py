from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.config import get_settings


ALLOWED_OFFICECLI_COMMANDS: frozenset[str] = frozenset(
    {
        "view",
        "get",
        "query",
        "set",
        "add",
        "remove",
        "batch",
        "validate",
        "render",
        "watch",
    }
)

ALLOWED_OFFICECLI_VIEW_MODES: frozenset[str] = frozenset(
    {
        "text",
        "annotated",
        "outline",
        "stats",
        "issues",
        "html",
        "svg",
        "screenshot",
        "forms",
    }
)


class OfficeCLIError(RuntimeError):
    """Base class for OfficeCLI adapter failures."""


class OfficeCLICommandError(OfficeCLIError):
    """Raised when a caller asks for a non-whitelisted OfficeCLI command."""


class OfficeCLIOutputError(OfficeCLIError):
    """Raised when OfficeCLI does not return parseable JSON."""


class OfficeCLIExecutionError(OfficeCLIError):
    """Raised when OfficeCLI exits non-zero."""

    def __init__(
        self,
        *,
        command: str,
        returncode: int,
        stderr: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        self.payload = payload
        message = f"OfficeCLI command {command!r} failed with exit code {returncode}"
        if stderr:
            message = f"{message}: {stderr}"
        super().__init__(message)


class OfficeCLITimeoutError(OfficeCLIError):
    """Raised when OfficeCLI exceeds the configured execution deadline."""

    def __init__(self, *, command: str, timeout_seconds: int) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        super().__init__(f"OfficeCLI command {command!r} timed out after {timeout_seconds} seconds")


Runner = Callable[..., subprocess.CompletedProcess[str]]


class OfficeCLIAdapter:
    """Narrow subprocess adapter for OfficeCLI.

    The adapter never exposes raw shell execution. Callers select a whitelisted
    OfficeCLI subcommand, pass a document path, and receive parsed JSON only.
    """

    def __init__(
        self,
        *,
        binary: str | None = None,
        binary_sha256: str | None = None,
        runner: Runner | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        settings = get_settings()
        self.binary = binary or getattr(settings, "OFFICECLI_BIN", "officecli")
        self.binary_sha256 = binary_sha256 if binary_sha256 is not None else getattr(settings, "OFFICECLI_SHA256", "")
        self.runner = runner or subprocess.run
        self.timeout_seconds = timeout_seconds or int(getattr(settings, "OFFICECLI_TIMEOUT_SECONDS", 45))
        self._version: str | None = None

    def run(
        self,
        command: str,
        path: str | Path,
        *,
        options: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        command = (command or "").strip()
        if command not in ALLOWED_OFFICECLI_COMMANDS:
            raise OfficeCLICommandError(f"OfficeCLI command {command!r} is not allowed")

        normalized_options = dict(options or {})
        if command == "view":
            mode = str(normalized_options.pop("mode", "outline") or "outline")
            page = normalized_options.pop("page", None)
            return self.run_view(
                path,
                mode=mode,
                page=page,
                options=normalized_options,
                cwd=cwd,
            )

        self._verify_binary_sha256()

        args = [self.binary, command, str(path), "--json"]
        args.extend(self._option_args(normalized_options))
        return self._run_json(command, args, cwd=cwd)

    def run_view(
        self,
        path: str | Path,
        *,
        mode: str,
        page: int | None = None,
        options: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run the real OfficeCLI positional view contract.

        OfficeCLI 1.0.88 accepts ``view <file> <mode> [options]``. Keeping this
        shape explicit prevents a generic flag serializer from regressing it to
        the invalid ``--mode`` form.
        """

        normalized_mode = (mode or "").strip().lower()
        if normalized_mode not in ALLOWED_OFFICECLI_VIEW_MODES:
            raise OfficeCLICommandError(f"OfficeCLI view mode {normalized_mode!r} is not allowed")

        normalized_options = dict(options or {})
        reserved_options = {"command", "path", "file", "mode", "page", "json"}
        conflicting_options = reserved_options.intersection(normalized_options)
        if conflicting_options:
            names = ", ".join(sorted(conflicting_options))
            raise OfficeCLICommandError(f"OfficeCLI view options cannot override reserved fields: {names}")

        self._verify_binary_sha256()
        if page is not None:
            normalized_options["page"] = page
        args = [self.binary, "view", str(path), normalized_mode, "--json"]
        args.extend(self._option_args(normalized_options))
        return self._run_json("view", args, cwd=cwd)

    def version(self) -> str:
        """Return a stable renderer version for preview cache evidence."""

        if self._version is not None:
            return self._version
        self._verify_binary_sha256()
        completed = self._run_process([self.binary, "--version"], command="version", cwd=None)
        if completed.returncode != 0:
            raise OfficeCLIExecutionError(
                command="version",
                returncode=completed.returncode,
                stderr=completed.stderr or "",
            )
        version = (completed.stdout or "").strip().splitlines()
        if not version:
            raise OfficeCLIOutputError("OfficeCLI version returned empty output")
        self._version = version[0][:256]
        return self._version

    def _run_json(
        self,
        command: str,
        args: list[str],
        *,
        cwd: str | Path | None,
    ) -> dict[str, Any]:
        completed = self._run_process(args, command=command, cwd=cwd)
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        payload = self._parse_json(stdout, allow_empty=completed.returncode != 0)

        if completed.returncode != 0:
            raise OfficeCLIExecutionError(
                command=command,
                returncode=completed.returncode,
                stderr=stderr,
                payload=payload,
            )
        if payload is None:
            raise OfficeCLIOutputError(f"OfficeCLI command {command!r} returned no JSON output")
        return payload

    def _run_process(
        self,
        args: list[str],
        *,
        command: str,
        cwd: str | Path | None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["OFFICECLI_SKIP_UPDATE"] = "1"
        try:
            return self.runner(
                args,
                cwd=Path(cwd) if cwd is not None else None,
                env=env,
                timeout=self.timeout_seconds,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OfficeCLITimeoutError(command=command, timeout_seconds=self.timeout_seconds) from exc
        except FileNotFoundError as exc:
            raise OfficeCLIExecutionError(
                command=command,
                returncode=127,
                stderr="OfficeCLI binary is unavailable",
            ) from exc
        except PermissionError as exc:
            raise OfficeCLIExecutionError(
                command=command,
                returncode=126,
                stderr="OfficeCLI binary is not executable",
            ) from exc
        except OSError as exc:
            raise OfficeCLIExecutionError(
                command=command,
                returncode=126,
                stderr=f"OfficeCLI process could not start ({type(exc).__name__})",
            ) from exc

    def _verify_binary_sha256(self) -> None:
        expected = (self.binary_sha256 or "").strip().lower()
        if not expected:
            return
        binary_path = Path(self.binary)
        if not binary_path.is_file():
            raise OfficeCLIExecutionError(
                command="verify",
                returncode=127,
                stderr=f"OfficeCLI binary not found for sha256 verification: {self.binary}",
            )
        actual = hashlib.sha256(binary_path.read_bytes()).hexdigest()
        if actual.lower() != expected:
            raise OfficeCLIExecutionError(
                command="verify",
                returncode=126,
                stderr="OfficeCLI binary sha256 mismatch",
                payload={"expected": expected, "actual": actual},
            )

    @staticmethod
    def _option_args(options: dict[str, Any]) -> list[str]:
        args: list[str] = []
        for key, value in options.items():
            if value is None or value is False:
                continue
            flag = f"--{str(key).replace('_', '-')}"
            if value is True:
                args.append(flag)
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    args.extend([flag, str(item)])
                continue
            args.extend([flag, str(value)])
        return args

    @staticmethod
    def _parse_json(stdout: str, *, allow_empty: bool) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            if allow_empty:
                return None
            raise OfficeCLIOutputError("OfficeCLI returned empty output")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OfficeCLIOutputError(f"OfficeCLI returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise OfficeCLIOutputError("OfficeCLI JSON output must be an object")
        return payload
