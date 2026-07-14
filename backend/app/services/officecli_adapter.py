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

        self._verify_binary_sha256()

        args = [self.binary, command, str(path), "--json"]
        args.extend(self._option_args(options or {}))
        env = os.environ.copy()
        env["OFFICECLI_SKIP_UPDATE"] = "1"

        completed = self.runner(
            args,
            cwd=Path(cwd) if cwd is not None else None,
            env=env,
            timeout=self.timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
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
