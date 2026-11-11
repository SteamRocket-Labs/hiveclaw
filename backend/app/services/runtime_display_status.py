"""Five display labels; execution states and replay rules are unchanged."""


_FAILURE_MESSAGES = {
    "tool_budget": "这次用完了用量预算，没有完成。",
    "turn_token_budget_exhausted": "这次用完了用量预算，没有完成。",
    "tool_round_limit": "这次已达到运行轮次上限，没有完成。可以重试继续。",
    "provider_error": "这次调用模型失败，没有完成。",
    "task_failed": "这次执行失败，没有完成。",
}


def runtime_failure_observation(status: str, metadata: dict) -> dict | None:
    """Expose terminal execution facts without releasing a failed model's partial body."""
    if status != "failed":
        return None
    reason = metadata.get("error_code")
    if reason not in _FAILURE_MESSAGES:
        reason = metadata.get("terminal_reason")
    if reason not in _FAILURE_MESSAGES:
        reason = "task_failed"
    return {"ok": True, "status": "failed", "reason_code": reason, "reason": _FAILURE_MESSAGES[reason], "retryable": False}


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
