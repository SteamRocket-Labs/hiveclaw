"""Five display labels; execution states and replay rules are unchanged."""


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
