"""Five display labels; execution states and replay rules are unchanged."""


RUNTIME_LIMIT_MESSAGES = {
    "turn_token_budget_exhausted": "这次用完了用量预算，没有完成。",
    "tool_round_limit": "这次已达到运行轮次上限，没有完成。可以重试继续。",
}
TERMINAL_TASK_READ_PREFIX = "任务结果未变化，已停止本次处理："
DELEGATED_TASK_RUNNING_MESSAGE = "同事正在处理，结果回来后会在这里继续。"

_FAILURE_MESSAGES = {
    **RUNTIME_LIMIT_MESSAGES,
    "tool_budget": RUNTIME_LIMIT_MESSAGES["turn_token_budget_exhausted"],
    "provider_error": "这次调用模型失败，没有完成。",
    "task_failed": "这次执行失败，没有完成。",
    "task_killed": "这次任务已停止。",
    "task_skipped": "这次任务已跳过。",
    "task_needs_reconciliation": "这次任务的结果待确认。",
}
_TERMINAL_READ_MESSAGES = frozenset(TERMINAL_TASK_READ_PREFIX + reason for reason in _FAILURE_MESSAGES.values())
_FAILURE_MESSAGES["identical_terminal_task_read"] = "任务结果未变化，已停止本次处理。"


def runtime_failure_observation(status: str, metadata: dict, *, result_summary: str | None = None) -> dict | None:
    """Expose terminal execution facts without releasing a failed model's partial body."""
    if not isinstance(status, str):
        return None
    if status in {"killed", "skipped", "needs_reconciliation"}:
        reason = f"task_{status}"
    elif status == "failed":
        reason = metadata.get("error_code")
        if not isinstance(reason, str) or reason not in _FAILURE_MESSAGES:
            reason = metadata.get("terminal_reason")
        if not isinstance(reason, str) or reason not in _FAILURE_MESSAGES:
            reason = "task_failed"
    else:
        return None
    message = _FAILURE_MESSAGES[reason]
    if reason == "identical_terminal_task_read" and isinstance(result_summary, str) and result_summary in _TERMINAL_READ_MESSAGES:
        message = result_summary  # Only fixed runtime copy, never a failed model's partial body.
    return {"ok": True, "status": status, "reason_code": reason, "reason": message,
        "retryable": reason == "tool_round_limit"}


def is_runtime_failure_observation(payload: object) -> bool:
    """Recognize only the native reader's fixed terminal-status projection."""
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    expected = runtime_failure_observation(payload.get("status"), {"error_code": payload.get("reason_code")},
        result_summary=payload.get("reason"))
    return expected is not None and all(payload.get(key) == value for key, value in expected.items())


def runtime_display_status(status: str) -> str:
    if status in {"pending", "queued", "running", "active", "resumable", "resuming", "summary_only", "waiting_for_child", "waiting_for_workflow"}:
        return "运行中"
    if status in {"suspended", "waiting_budget_approval", "awaiting_approval", "waiting_user", "blocked", "waiting_for_permission"}:
        return "等你决定"
    if status in {"completed", "succeeded", "done", "complete"}:
        return "完成"
    if status in {"killed", "cancelled", "hard_stopped", "expired", "stopped", "exhausted", "paused", "budget_limited", "usage_limited"}:
        return "已停止"
    return "失败"
