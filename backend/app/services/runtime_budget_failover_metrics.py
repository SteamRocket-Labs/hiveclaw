"""Low-cardinality process metrics for runtime-budget root failover."""

from __future__ import annotations

from collections import Counter
from threading import Lock


_ROOT_FAILURES: Counter[tuple[str, str]] = Counter()
_LOCK = Lock()

_INTERACTIVE_SOURCES = frozenset(
    {
        "web",
        "web_chat",
        "web_chat_turn",
        "chat",
        "channel",
        "feishu",
        "wecom",
        "wechat_personal",
        "slack",
        "microsoft_teams",
        "discord",
        "dingtalk",
        "telegram",
        "local_bridge",
        "local_agent",
    }
)
_AUTONOMOUS_SOURCES = frozenset(
    {
        "goal_continuation",
        "scheduled",
        "trigger",
        "workflow",
        "agent_team",
        "team_member",
        "agent",
        "subagent",
        "subagent_wake",
        "task",
        "task_notification",
        "heartbeat",
        "loop_same_session",
        "loop_self_pace_kickoff",
    }
)
_DECISIONS = frozenset({"interactive_degraded", "fail_closed"})


def _source_class(source: str) -> str:
    normalized = str(source or "").strip().lower()
    if normalized in _INTERACTIVE_SOURCES:
        return "interactive"
    if normalized in _AUTONOMOUS_SOURCES:
        return "autonomous"
    return "other"


def record_runtime_budget_root_failure(*, source: str, decision: str) -> None:
    decision_label = decision if decision in _DECISIONS else "fail_closed"
    with _LOCK:
        _ROOT_FAILURES[(_source_class(source), decision_label)] += 1


def reset_runtime_budget_failover_metrics() -> None:
    with _LOCK:
        _ROOT_FAILURES.clear()


def render_runtime_budget_failover_prometheus() -> str:
    lines = [
        "# HELP runtime_budget_root_failures_total Runtime budget root admission failures by bounded source and decision.",
        "# TYPE runtime_budget_root_failures_total counter",
    ]
    with _LOCK:
        snapshot = sorted(_ROOT_FAILURES.items())
    for (source, decision), count in snapshot:
        lines.append(
            f'runtime_budget_root_failures_total{{decision="{decision}",source="{source}"}} {count}'
        )
    return "\n".join(lines) + "\n"
