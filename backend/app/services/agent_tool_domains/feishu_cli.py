"""Optional Lark CLI adapter for cloud office operations."""

from __future__ import annotations

import asyncio
import json
import shutil

from app.config import get_settings


class FeishuCliError(RuntimeError):
    """Structured error for optional lark-cli execution."""

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "provider_error",
        retryable: bool = False,
        actionable_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.retryable = retryable
        self.actionable_hint = actionable_hint


async def _run_feishu_cli_command(args: list[str]) -> tuple[int, str, str]:
    settings = get_settings()
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.FEISHU_CLI_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise FeishuCliError(
            "lark-cli command timed out.",
            error_class="timeout",
            retryable=True,
            actionable_hint="Retry later or narrow the request scope.",
        ) from exc

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    return process.returncode, stdout, stderr


async def _feishu_cli_available() -> bool:
    settings = get_settings()
    if not settings.FEISHU_CLI_ENABLED:
        return False
    if shutil.which(settings.FEISHU_CLI_BIN) is None:
        return False

    return_code, _stdout, _stderr = await _run_feishu_cli_command(
        [settings.FEISHU_CLI_BIN, "auth", "status"]
    )
    return return_code == 0


async def _feishu_cli_api_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    identity: str | None = None,
) -> dict:
    settings = get_settings()
    if not await _feishu_cli_available():
        raise FeishuCliError(
            "lark-cli is not enabled or not authenticated.",
            error_class="not_configured",
            retryable=False,
            actionable_hint=(
                "Enable FEISHU_CLI_ENABLED, install lark-cli, run "
                "`lark-cli auth login`, then retry the office operation."
            ),
        )

    command = [
        settings.FEISHU_CLI_BIN,
        "api",
        method.upper(),
        path,
        "--format",
        "json",
        "--as",
        identity or settings.FEISHU_CLI_IDENTITY,
    ]
    if params:
        command.extend(["--params", json.dumps(params, ensure_ascii=False)])
    if body:
        command.extend(["--body", json.dumps(body, ensure_ascii=False)])

    return_code, stdout, stderr = await _run_feishu_cli_command(command)
    if return_code != 0:
        raise FeishuCliError(
            stderr or stdout or "lark-cli command failed.",
            error_class="provider_unavailable",
            retryable=True,
            actionable_hint="Verify lark-cli auth status, scopes, and command arguments.",
        )

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FeishuCliError(
            "lark-cli returned non-JSON output.",
            error_class="provider_error",
            retryable=False,
            actionable_hint="Run the same lark-cli command manually and inspect the output format.",
        ) from exc
