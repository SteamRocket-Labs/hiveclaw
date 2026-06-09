"""Plan Mode functional core — pure logic, zero IO / zero DB.

This module is the *functional core* of Plan Mode (see ``docs/plan-mode-design.md``).
Everything here is a deterministic, side-effect-free function so it can be unit
tested input -> output with no mocks. The imperative shell
(``app.services.plan_mode_service.PlanModeService``) wires these helpers to the
database, the LLM, and the filesystem.

Security-critical responsibilities concentrated here:

* §6.3 — deterministic ``plan_json`` skeleton + schema validation
* §7   — state-machine transition legality
* §8.1 — self-confirm prohibition (the agent may not confirm its own plan)
* §8.2 — confirmation must bind to the exact ``plan_version`` + ``plan_hash``
* canonical sha256 hashing (key-order independent, stable across processes)
* §6.2 — markdown frontmatter that mirrors the DB record
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from uuid import UUID

# ---------------------------------------------------------------------------
# Vocabulary (kept in lockstep with the design doc + the DB model)
# ---------------------------------------------------------------------------

PLAN_SCHEMA = "hive_plan.v1"

#: §6 / §5.1 — the intents that mandate a plan.
INTENT_TYPES: tuple[str, ...] = (
    "autonomous_wake",
    "in_session_execution",
    "delegation",
)

#: §9.0 / §9.2 — the "open autonomous behavior" actions the PlanModeGate
#: arbitrates. Each one maps onto exactly one :data:`INTENT_TYPES` member so
#: the gate can label a freshly-needed plan with the right intent.
ACTION_KINDS: tuple[str, ...] = (
    "create_enabled_trigger",
    "enable_autonomous_wake",
    "start_long_task",
    "start_delegation",
    "start_workflow",
)

#: action_kind -> intent_type. Trigger/wake creation maps to a recurring
#: autonomous wake; in-session execution and delegations carry their own intents
#: (§5.1 / §6). (``activate_objective_wake`` was removed with the objective subsystem.)
_ACTION_INTENT: dict[str, str] = {
    "create_enabled_trigger": "autonomous_wake",
    "enable_autonomous_wake": "autonomous_wake",
    "start_long_task": "in_session_execution",
    "start_delegation": "delegation",
    # §9 P4: high-risk ephemeral workflow launches confirm through Plan Mode
    # (§10 decision 3 — graded by risk, not by ephemeral/registered).
    "start_workflow": "in_session_execution",
}


@dataclass(frozen=True)
class PlanModeEntryDecision:
    """UX-layer decision for whether a chat turn should enter or recommend Plan Mode.

    ``mode`` is one of:
    - ``none``: normal chat execution.
    - ``recommend``: stop before execution and ask whether to enter Plan Mode
      (a suggestion — it never enters on its own).
    - ``explicit``: the user/frontend explicitly selected Plan Mode.
    - ``declined``: the user explicitly declined a Plan Mode recommendation.

    There is deliberately no ``auto`` mode: the agent's judgment must never trigger
    Plan Mode entry (A — user correction). Entry is always user-explicit.

    This classifier is intentionally **not** the security boundary. Tool/runtime
    backstops still protect execution. Its job is to keep the normal UX from
    reaching a hard ``needs_plan`` block for cases where a recommendation is the
    right first move.
    """

    mode: str
    intent_type: str | None = None
    action_kind: str | None = None
    tool_name: str | None = None
    title: str = ""
    reason: str = ""


_EXPLICIT_PLAN_MODE_RE = re.compile(
    r"(计划模式|进入计划|用计划模式|先做计划|plan\s*mode|planning\s*mode)",
    re.IGNORECASE,
)
_DECLINE_PLAN_MODE_RE = re.compile(
    r"((不用|不需要|不要|跳过|先不|无需).{0,8}(计划模式|计划|plan\s*mode)|"
    r"(skip|decline|no)\s+(the\s+)?(plan\s*mode|planning\s*mode))",
    re.IGNORECASE,
)
_ACCEPT_PLAN_MODE_RE = re.compile(
    r"^\s*((同意|好|可以|行|开始|确认).{0,8})?(进入计划模式|进入计划|用计划模式|plan\s*mode|planning\s*mode)"
    r"([。.!！])?\s*$",
    re.IGNORECASE,
)
_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_PLAN_ID_CONFIRM_RE = re.compile(
    rf"^\s*(确认|同意|批准|开始|执行|confirm|approve|start).{{0,16}}(?:plan_id|计划)\s*[=:：]?\s*({_UUID_RE})\s*[。.!！]?\s*$",
    re.IGNORECASE,
)
_LATEST_PLAN_CONFIRM_RE = re.compile(
    r"^\s*(确认|同意|批准|开始|执行|confirm|approve|start).{0,10}(上一个|这个|该|当前|latest|current).{0,8}(计划|plan)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)
_SCHEDULE_RE = re.compile(
    r"(每天|每周|每月|每小时|定时|定期|到时候|提醒我|监控|盯着|盯一下|有变化|"
    r"schedule|cron|daily|weekly|monthly|monitor|watch)",
    re.IGNORECASE,
)
PLAN_MODE_RECOMMENDATION_MARKER = "建议先进入计划模式，确认执行频率、范围、成本、停止条件和通知方式"
PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY = "plan_mode_trusted_user_decline"


@dataclass(frozen=True)
class PlanConfirmationRequest:
    """A trusted channel text request to confirm a Plan Mode plan."""

    plan_id: str | None = None
    latest: bool = False


def is_plan_mode_acceptance_reply(content: str) -> bool:
    """Return true only for a bare acceptance of the latest Plan Mode recommendation."""
    return bool(_ACCEPT_PLAN_MODE_RE.search(str(content or "")))


def extract_plan_confirmation_request(content: str) -> PlanConfirmationRequest | None:
    """Parse channel text that explicitly confirms an awaiting plan.

    This intentionally does not treat a bare "确认" as plan confirmation: channel
    users may be confirming recipients, facts, or other workflow details. The
    message must either carry a plan_id or explicitly refer to the latest/current
    plan in the conversation.
    """
    text = str(content or "").strip()
    if not text:
        return None
    match = _PLAN_ID_CONFIRM_RE.search(text)
    if match:
        return PlanConfirmationRequest(plan_id=match.group(2).lower(), latest=False)
    if _LATEST_PLAN_CONFIRM_RE.search(text):
        return PlanConfirmationRequest(latest=True)
    return None


def classify_plan_mode_entry(content: str, *, explicit: bool = False) -> PlanModeEntryDecision:
    """Classify a user turn into Plan Mode UX behavior.

    This is the "suggest, never trigger" layer (A — user correction: the agent's
    judgment must NEVER auto-enter Plan Mode; entry is always user-explicit):
    - explicit frontend/user Plan Mode selection enters Plan Mode;
    - explicit textual decline lets the normal agent continue with an auditable opt-out;
    - schedule/monitor wording RECOMMENDS Plan Mode first (a suggestion — it does
      not enter); the agent's own "should we plan?" judgment is taught in the prompt
      (plan_mode_guidance), surfaced as a suggestion in its reply, never an auto-entry;
    - everything else stays normal.
    """
    text = str(content or "").strip()
    if not text:
        return PlanModeEntryDecision(mode="none")

    if not explicit and _DECLINE_PLAN_MODE_RE.search(text):
        return PlanModeEntryDecision(mode="declined", title=text[:120], reason="user_declined_recommended_plan_mode")

    has_explicit = explicit or bool(_EXPLICIT_PLAN_MODE_RE.search(text))
    has_schedule = bool(_SCHEDULE_RE.search(text))

    if has_explicit:
        if has_schedule:
            return PlanModeEntryDecision(
                mode="explicit",
                intent_type="autonomous_wake",
                action_kind="create_enabled_trigger",
                tool_name="set_trigger",
                title=text[:120],
                reason="explicit_plan_mode_schedule",
            )
        return PlanModeEntryDecision(
            mode="explicit",
            intent_type="in_session_execution",
            action_kind="start_long_task",
            tool_name="continue_current_session",
            title=text[:120],
            reason="explicit_plan_mode",
        )

    if has_schedule:
        return PlanModeEntryDecision(
            mode="recommend",
            intent_type="autonomous_wake",
            action_kind="create_enabled_trigger",
            tool_name="set_trigger",
            title=text[:120],
            reason="schedule_or_monitor_intent",
        )

    return PlanModeEntryDecision(mode="none")


def recent_plan_mode_recommendation_was_shown(messages: list[dict] | tuple[dict, ...] | None) -> bool:
    """Return true when the recent assistant history contains our recommendation.

    This closes the opt-out loop: a user may decline a recommendation that was
    actually shown, but a first-turn "skip plan mode" string is not a trusted
    authorization for autonomous scheduling.
    """
    if not messages:
        return False
    for message in reversed(list(messages)[-8:]):
        content = getattr(message, "content", None)
        role = getattr(message, "role", None)
        if isinstance(message, dict):
            content = message.get("content", content)
            role = message.get("role", role)
        if role == "assistant" and isinstance(content, str) and PLAN_MODE_RECOMMENDATION_MARKER in content:
            return True
    return False


def trusted_decline_metadata(
    *,
    content: str,
    messages: list[dict] | tuple[dict, ...] | None,
    explicit: bool = False,
) -> dict[str, str] | None:
    """Build runtime metadata when a real prior recommendation was declined."""
    decision = classify_plan_mode_entry(content, explicit=explicit)
    if decision.mode != "declined":
        return None
    if not recent_plan_mode_recommendation_was_shown(messages):
        return None
    return {
        "reason": "user_declined_recommended_plan_mode",
        "title": (decision.title or content)[:120],
    }


#: §9.0 — the only cutover exemption marker honoured by the backstop layer. A
#: pre-existing enabled trigger may be grandfathered in compatibility mode by
#: tagging its artifact with this reason; anything else still needs a plan.
PLAN_EXEMPT_PREEXISTING: str = "preexisting_before_cutover"
PLAN_EXEMPT_CONFIRMED_HR_BLUEPRINT: str = "confirmed_hr_blueprint"
PLAN_EXEMPT_USER_DECLINED: str = "user_declined_plan_mode"
PLAN_MODE_DECLINE_VALUES: tuple[str, ...] = ("declined", "skip", "skipped", "not_needed", "no_plan")


def plan_mode_user_declined(decision: str | None) -> bool:
    """Return true when a caller carries an explicit Plan Mode opt-out."""
    return str(decision or "").strip().lower() in PLAN_MODE_DECLINE_VALUES


def stamp_user_declined_plan_exemption(config: dict | None) -> dict:
    """Stamp an autonomous artifact with the audited user opt-out exemption."""
    stamped = dict(config or {})
    metadata = dict(stamped.get("metadata") or {})
    metadata["plan_exempt_reason"] = PLAN_EXEMPT_USER_DECLINED
    stamped["metadata"] = metadata
    return stamped


#: §7 — every status a PlanRequest can hold.
PLAN_STATUSES: tuple[str, ...] = (
    "draft",
    "planning",
    "planning_failed",
    "awaiting_confirmation",
    "confirmed",
    "rejected",
    "superseded",
    "expired",
)

#: §7 — terminal states have no outgoing transitions. ``confirmed`` is also
#: terminal *with respect to status*: handoff success/failure is recorded on
#: ``handoff_status`` rather than mutating ``status`` (§13).
TERMINAL_STATUSES: frozenset[str] = frozenset({"confirmed", "rejected", "superseded", "expired"})

#: §7 — the legal directed edges of the state machine. The single path into
#: the execution layer is ``awaiting_confirmation -> confirmed`` and it is
#: gated separately by :func:`validate_confirmation`.
_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"planning"}),
    "planning": frozenset({"awaiting_confirmation", "planning_failed"}),
    "planning_failed": frozenset({"planning", "rejected"}),
    "awaiting_confirmation": frozenset({"confirmed", "planning", "rejected", "expired", "superseded"}),
    # Terminal states intentionally map to the empty set.
    "confirmed": frozenset(),
    "rejected": frozenset(),
    "superseded": frozenset(),
    "expired": frozenset(),
}

#: §13 — handoff outcome lives on its own field so a failed handoff never
#: corrupts the ``confirmed`` audit semantics.
HANDOFF_STATUSES: tuple[str, ...] = ("not_started", "completed", "failed", "skipped")

#: §6.3 — required top-level keys of ``plan_json``.
REQUIRED_PLAN_FIELDS: tuple[str, ...] = (
    "schema",
    "title",
    "intent_type",
    "objective",
    "motivation",
    "steps",
    "success_criteria",
    "wake_policy",
    "required_capabilities",
    "external_side_effects",
    "risk_assessment",
    "estimated_cost",
    "stop_conditions",
    "handoff",
)

_RISK_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})

_GENERIC_EXTERNAL_EFFECT_LABELS: frozenset[str] = frozenset(
    {
        "external action",
        "external_action",
        "external side effect",
        "外部动作",
        "外部副作用",
    }
)

_CAPABILITY_LABELS: dict[str, str] = {
    "web_search": "Web 来源核验",
    "web_fetch": "Web 来源核验",
    "firecrawl_fetch": "Web 来源核验",
    "xcrawl_scrape": "Web 来源核验",
    "list_files": "只读工作区检查",
    "read_file": "只读工作区检查",
    "fs_list": "只读工作区检查",
    "fs_read": "只读工作区检查",
    "glob_search": "只读工作区检查",
    "grep_search": "只读工作区检查",
    "write_file": "工作区文件生成",
    "edit_file": "工作区文件生成",
    "file_write": "工作区文件生成",
    "fs_write": "工作区文件生成",
    "send_channel_message": "用户通知",
    "channel_message": "用户通知",
    "send_feishu_message": "用户通知",
    "send_channel_file": "文件交付",
    "deep_research_start": "Deep Research",
    "deep_research_check": "Deep Research",
    "deep_research_export": "Deep Research",
    "office_document_create": "Office 交付物生成",
    "continue_current_session": "当前会话继续执行",
    "manage_tasks": "任务跟踪",
    "set_trigger": "定时自动化",
    "update_trigger": "定时自动化",
    "delegate_to_agent": "Agent 协作",
}

#: Per-intent handoff target (§13) used to seed the skeleton.
#:
#: G/H.2/G/H.3 convergence: every target here resolves to a *registered* handler
#: (see ``plan_mode_registry.register_plan_mode_handoffs``) — no dead target that
#: silently degrades to ``no_handler_registered`` -> ``skipped``.
#: - ``in_session_execution`` (intent, formerly ``long_task``) seeds ``continue_current_session``
#:   rather than the ambiguous legacy ``"long_task"`` *target* word (a confirmed run continues
#:   in the live session, or fails closed with a visible reason when unattended).
#:   The legacy ``"long_task"`` *target* stays registered (compat for already-persisted
#:   plans) but is no longer seeded here.
#:
#: Exec/automation CC-alignment (2026-06-08): ``external_action`` / ``state_change``
#: were orphan intents (no ``ACTION_KIND`` mapped onto them, no producer) — removed.
_INTENT_HANDOFF_TARGET: dict[str, str] = {
    "autonomous_wake": "scheduled_trigger",
    "in_session_execution": "continue_current_session",
    "delegation": "delegation",
}


# ---------------------------------------------------------------------------
# Canonical JSON + hashing
# ---------------------------------------------------------------------------


def canonical_plan_json(plan_json: dict) -> str:
    """Serialise ``plan_json`` deterministically.

    Keys are sorted recursively and separators are tight so the same logical
    plan always produces the same byte string regardless of dict insertion
    order. This is the substrate for :func:`compute_plan_hash`.
    """
    return json.dumps(
        plan_json,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def compute_plan_hash(plan_json: dict) -> str:
    """Return ``"sha256:<hexdigest>"`` over the canonical JSON of the plan."""
    digest = hashlib.sha256(canonical_plan_json(plan_json).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Skeleton generation (§6.3 / §10.2)
# ---------------------------------------------------------------------------


def build_plan_skeleton(*, intent_type: str, title: str, original_request: str) -> dict:
    """Build a deterministic ``plan_json`` skeleton for ``intent_type``.

    The skeleton carries every field required by §6.3 with empty / placeholder
    values that a restricted planning invocation later fills in. Determinism
    matters: identical inputs must yield an identical skeleton so the hash is
    reproducible.

    Raises:
        ValueError: if ``intent_type`` is not one of :data:`INTENT_TYPES`.
    """
    if intent_type not in INTENT_TYPES:
        raise ValueError(f"unknown intent_type {intent_type!r}; expected one of {INTENT_TYPES}")

    is_recurring_wake = intent_type == "autonomous_wake"
    wake_policy: dict = (
        {"type": "cron", "timezone": "Asia/Shanghai", "expr": ""} if is_recurring_wake else {"type": "none"}
    )

    return {
        "schema": PLAN_SCHEMA,
        "title": title,
        "intent_type": intent_type,
        "objective": "",
        "motivation": original_request,
        # The agent-authored plan body (CC parity: the plan IS a markdown article,
        # not a field form). Empty for machine-intercepted plans, which fall back
        # to the structured render. Carried in plan_json so it is hash-covered.
        "plan_markdown": "",
        "steps": [],
        "success_criteria": [],
        "wake_policy": wake_policy,
        "required_capabilities": [],
        "external_side_effects": [],
        "risk_assessment": {"level": "medium", "reasons": []},
        "estimated_cost": {"tokens_per_run": "unknown", "expected_duration": "unknown"},
        "stop_conditions": [],
        "handoff": {
            "target": _INTENT_HANDOFF_TARGET[intent_type],
            "create_trigger": is_recurring_wake,
        },
    }


def build_plan_execution_instruction(
    *,
    plan_id: UUID | str,
    plan_version: int | str,
    plan_markdown: str = "",
    objective: str = "",
    original_request: str = "",
    source: str = "live",
) -> str:
    """Render the confirmed-plan marching orders (E).

    The single source both the live-session continuation handoff and the
    scheduled-trigger wake render from, so the "execute this confirmed plan"
    instruction cannot drift between paths. ``plan_markdown`` (the agent-authored
    article) is the substance; ``objective`` then ``original_request`` are the
    fallbacks for machine-intercepted plans with no authored body.

    ``source`` only varies the closing delivery clause — ``"live"`` continues in
    the current chat; ``"trigger"`` executes a scheduled autonomous wake.
    """
    body = (plan_markdown or "").strip()
    if not body:
        body = (objective or "").strip() or (original_request or "").strip() or "（计划正文为空，按目标执行）"
    if source == "trigger":
        delivery = (
            "这是一次定时唤醒：请直接按上面已确认的计划执行本次任务，并用 work ledger 工具"
            "（track_todo / record_finding）带证据记录进展。不要重新进入计划模式，"
            "也不要再次请求确认——用户已经确认过了。如果遇到必须由用户决定的阻断点，按计划的"
            "停止条件停下并留下说明。"
        )
    else:
        delivery = (
            "请直接按上面的计划执行，并把成果交付到当前这个对话里。不要重新进入计划模式，"
            "也不要再次请求确认——用户已经确认过了。如果执行中遇到必须由用户决定的阻断点，"
            "用 ask_user_question 询问；其余按计划自主完成。"
        )
    return (
        f"你的计划已获用户确认（plan_id={plan_id}，version={plan_version}），现在开始执行。\n\n"
        f"# 已确认的计划\n\n{body}\n\n"
        f"{delivery}"
    )


# ---------------------------------------------------------------------------
# Intercept-then-create mapping (§9.2) — tool args -> plan_json ``fill``.
#
# When the tool gate (or an auto-sync path) blocks an autonomous action, it
# seeds an awaiting PlanRequest from the very arguments the agent supplied, so
# the user is shown a concrete plan to confirm rather than an empty shell. The
# arg -> fill mapping and the idempotency signature are pure, so they live here.
# ---------------------------------------------------------------------------


def _first_nonblank(*values: object) -> str:
    """Return the first stringifiable, non-blank value, else ``""``."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _wake_policy_from_trigger_args(arguments: dict) -> dict:
    """Build a ``plan_json.wake_policy`` from ``set_trigger`` style arguments.

    Mirrors the trigger ``type`` + ``config`` (§6.3 / §9.2). Unknown shapes fall
    back to a bare ``{"type": <type or "cron">}`` so the plan still validates.
    """
    trigger_type = _first_nonblank(arguments.get("type")) or "cron"
    config = arguments.get("config")
    config = config if isinstance(config, dict) else {}

    wake_policy: dict = {"type": trigger_type}
    if trigger_type == "cron":
        wake_policy["timezone"] = _first_nonblank(config.get("timezone")) or "Asia/Shanghai"
        wake_policy["expr"] = _first_nonblank(config.get("expr"))
    elif trigger_type == "interval":
        minutes = config.get("minutes", config.get("interval"))
        if minutes is not None:
            wake_policy["minutes"] = minutes
    elif trigger_type == "once":
        wake_policy["at"] = _first_nonblank(config.get("at"))
    elif trigger_type == "poll":
        wake_policy["url"] = _first_nonblank(config.get("url"))
    return wake_policy


def tool_args_to_plan_fill(*, tool_name: str, action_kind: str, arguments: dict) -> dict:
    """Map a gated tool's arguments onto a ``plan_json`` ``fill`` (§9.2).

    The returned dict is the ``fill`` consumed by
    ``PlanModeService.generate_plan`` — it carries ``title`` / ``objective`` /
    ``motivation`` and (for wake intents) a ``wake_policy``, plus a
    ``required_capabilities`` entry recording the intercepted tool. Every field
    is non-blank so the generated plan always validates rather than failing into
    ``planning_failed`` (a blocked autonomous action must yield a confirmable
    plan, not a dead one).

    The canonical worked example (§9.2) is ``set_trigger`` whose
    ``name`` / ``reason`` / ``config`` / ``type`` map onto
    ``title`` / ``objective`` / ``wake_policy``.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    intent_type = intent_type_for_action(action_kind)

    # Title: prefer an explicit name/title; else a stable tool-derived label.
    title = (
        _first_nonblank(
            arguments.get("name"),
            arguments.get("title"),
            arguments.get("agent_name") and f"Delegate to {arguments.get('agent_name')}",
        )
        or f"Autonomous action via {tool_name}"
    )

    # Objective: the agent's stated reason / message / description.
    objective = (
        _first_nonblank(
            arguments.get("reason"),
            arguments.get("message"),
            arguments.get("description"),
            arguments.get("instruction"),
        )
        or f"Confirm before {tool_name} runs this autonomous action."
    )

    fill: dict = {
        "title": title,
        "objective": objective,
        "motivation": objective,
        "required_capabilities": [tool_name],
    }

    if intent_type == "autonomous_wake":
        fill["wake_policy"] = _wake_policy_from_trigger_args(arguments)
    else:
        fill["wake_policy"] = {"type": "none"}

    if intent_type == "delegation":
        payload = {
            key: arguments[key]
            for key in (
                "agent_name",
                "target_agent_id",
                "message",
                "tool_profile",
                "max_tool_rounds",
                "parent_session_id",
            )
            if arguments.get(key) is not None
        }
        fill["handoff"] = {
            "target": "delegation",
            "create_objective": False,
            "create_trigger": False,
            "payload": payload,
        }
        fill["external_side_effects"] = [
            {
                "kind": "agent_delegation",
                "channel": "internal",
                "audience": _first_nonblank(arguments.get("agent_name"), arguments.get("target_agent_id")),
                "requires_confirmation": True,
            }
        ]

    return fill


def action_kind_to_intent_signature(*, action_kind: str, tool_name: str, arguments: dict) -> tuple[str, str]:
    """Return ``(intent_type, signature)`` identifying a logical gated action (§9.2).

    The ``signature`` is a short, stable sha256 over the action_kind, tool name
    and canonical arguments — re-ordering dict keys does not change it. The tool
    gate keys idempotency on ``(agent_id, intent_type, signature)`` so the *same*
    blocked call within a short window reuses one awaiting plan rather than
    spawning duplicates.
    """
    arguments = arguments if isinstance(arguments, dict) else {}
    intent_type = intent_type_for_action(action_kind)
    payload = canonical_plan_json({"action_kind": action_kind, "tool_name": tool_name, "arguments": arguments})
    signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return intent_type, signature


# ---------------------------------------------------------------------------
# Planner output normalization (§10.2 pre-validation)
# ---------------------------------------------------------------------------


def normalize_plan_json_for_validation(plan_json: dict) -> dict:
    """Repair common agent-planner shape drift before strict schema validation.

    The agent owns the substantive plan content, but the runtime owns the stable
    ``hive_plan.v1`` envelope. Minor LLM shape differences such as missing step
    order or a non-standard risk label should not erase an otherwise useful plan.
    """
    if not isinstance(plan_json, dict):
        return plan_json

    normalized = dict(plan_json)
    if "steps" in normalized:
        normalized["steps"] = _normalize_plan_steps(normalized.get("steps"))
    for field in ("success_criteria", "stop_conditions"):
        if field in normalized:
            normalized[field] = _normalize_string_list(normalized.get(field))
    if "required_capabilities" in normalized:
        normalized["required_capabilities"] = _normalize_capability_list(normalized.get("required_capabilities"))
    if "external_side_effects" in normalized:
        normalized["external_side_effects"] = _normalize_external_side_effects(normalized.get("external_side_effects"))
    if "risk_assessment" in normalized:
        normalized["risk_assessment"] = _normalize_risk_assessment(normalized.get("risk_assessment"))
    return normalized


def _normalize_plan_steps(value: object) -> object:
    if not isinstance(value, list):
        return value
    steps: list[dict] = []
    for index, raw_step in enumerate(value, start=1):
        if isinstance(raw_step, dict):
            step = dict(raw_step)
            order = _positive_int(step.get("order"))
            step["order"] = order if order is not None else index
            description = _first_nonblank(
                step.get("description"),
                step.get("title"),
                step.get("name"),
                step.get("task"),
                step.get("action"),
            )
            if description:
                step["description"] = description
            steps.append(step)
            continue
        description = str(raw_step or "").strip()
        steps.append({"order": index, "description": description})
    return steps


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_string_list(value: object) -> object:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return value
    items: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = _first_nonblank(
                item.get("description"),
                item.get("criterion"),
                item.get("condition"),
                item.get("capability"),
                item.get("title"),
                item.get("name"),
            )
        else:
            text = str(item or "").strip()
        if text:
            items.append(text)
    return items


def _normalize_capability_list(value: object) -> object:
    raw_items = _normalize_string_list(value)
    if not isinstance(raw_items, list):
        return raw_items
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        label = _CAPABILITY_LABELS.get(str(item).strip(), str(item).strip())
        if not label or label in seen:
            continue
        seen.add(label)
        normalized.append(label)
    return normalized


def _normalize_external_side_effects(value: object) -> object:
    if value is None:
        return []
    if not isinstance(value, list):
        return value

    effects: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, str):
            label = item.strip()
            if label and label.lower() not in _GENERIC_EXTERNAL_EFFECT_LABELS:
                effects.append({"description": label, "requires_confirmation": True})
            continue
        if not isinstance(item, dict):
            continue

        kind = str(item.get("kind") or "").strip()
        channel = str(item.get("channel") or "").strip()
        audience = str(item.get("audience") or "").strip()
        description = _first_nonblank(item.get("description"), item.get("summary"), item.get("action")) or ""
        visible_label = " ".join(part for part in (kind, channel, audience, description) if part).strip()
        if not visible_label or visible_label.lower() in _GENERIC_EXTERNAL_EFFECT_LABELS:
            continue

        effect: dict[str, object] = {}
        if kind:
            effect["kind"] = kind
        if channel:
            effect["channel"] = channel
        if audience:
            effect["audience"] = audience
        if description:
            effect["description"] = description
        effect["requires_confirmation"] = bool(item.get("requires_confirmation", True))
        effects.append(effect)
    return effects


def _normalize_risk_assessment(value: object) -> object:
    if value is None:
        return value
    risk = dict(value) if isinstance(value, dict) else {"level": value, "reasons": []}
    original_level = risk.get("level")
    normalized_level = _normalize_risk_level(original_level)
    risk["level"] = normalized_level

    raw_reasons = risk.get("reasons")
    if isinstance(raw_reasons, list):
        reasons = [str(item).strip() for item in raw_reasons if str(item).strip()]
    elif raw_reasons is None:
        reasons = []
    else:
        reason = str(raw_reasons).strip()
        reasons = [reason] if reason else []

    original_text = str(original_level or "").strip()
    if original_text and original_text.lower() not in _RISK_LEVELS:
        reasons.insert(
            0, f"Planner supplied non-standard risk level {original_text!r}; normalized to {normalized_level}."
        )
    risk["reasons"] = reasons
    return risk


def _normalize_risk_level(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in _RISK_LEVELS:
        return text
    if text in {"moderate", "mid", "normal", "medium risk"} or "medium" in text or "中" in text:
        return "medium"
    if text in {"critical", "severe", "elevated", "high risk"} or "high" in text or "高" in text:
        return "high"
    if text in {"minimal", "minor", "low risk"} or "low" in text or "低" in text:
        return "low"
    return "medium"


# ---------------------------------------------------------------------------
# Schema validation (§10.2 step 3)
# ---------------------------------------------------------------------------


def validate_plan_json(plan_json: dict) -> list[str]:
    """Validate ``plan_json`` against the ``hive_plan.v1`` contract.

    Returns a list of human-readable error strings; an empty list means the
    plan is well formed. The shell promotes a non-empty result to
    ``planning_failed`` (§10.2).
    """
    errors: list[str] = []

    if not isinstance(plan_json, dict):
        return ["plan_json must be an object"]

    for field in REQUIRED_PLAN_FIELDS:
        if field not in plan_json:
            errors.append(f"missing required field: {field}")

    if plan_json.get("schema") != PLAN_SCHEMA:
        errors.append(f"schema must be {PLAN_SCHEMA!r}")

    intent_type = plan_json.get("intent_type")
    if intent_type is not None and intent_type not in INTENT_TYPES:
        errors.append(f"intent_type must be one of {INTENT_TYPES}")

    if "title" in plan_json and not str(plan_json.get("title") or "").strip():
        errors.append("title must be a non-empty string")

    if "objective" in plan_json and not str(plan_json.get("objective") or "").strip():
        errors.append("objective must be a non-empty string")

    _validate_steps(plan_json.get("steps"), errors)
    _validate_str_list(plan_json.get("success_criteria"), "success_criteria", errors)
    _validate_str_list(plan_json.get("stop_conditions"), "stop_conditions", errors)
    _validate_risk(plan_json.get("risk_assessment"), errors)
    _validate_wake_policy(plan_json.get("wake_policy"), errors)

    return errors


# Plan Mode meta-steps: steps describing the *planning ritual* (submitting the
# plan card, calling exit_plan_mode, waiting for confirmation) or explicit
# non-execution placeholders. ``steps`` must list only the real execution work
# the user is confirming — meta-steps are the canonical 偏离 the CC alignment
# targets (e.g. a step "本步不实施 / 提交计划卡片"). Markers are specific phrases
# to avoid false positives on legitimate steps like "等待数据源返回".
_META_STEP_MARKERS: tuple[str, ...] = (
    "exit_plan_mode",
    "exit plan mode",
    "提交计划",
    "提交本计划",
    "提交该计划",
    "提交计划卡片",
    "等待用户确认",
    "等待确认",
    "wait for confirmation",
    "wait for user confirmation",
    "await confirmation",
    "await user confirmation",
    "submit the plan",
    "submit this plan",
    "present the plan",
    "本步不实施",
    "本步骤不实施",
    "this step does not implement",
    "no-op step",
)


def _step_is_meta(description: str) -> bool:
    text = description.strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _META_STEP_MARKERS)


def _validate_steps(steps: object, errors: list[str]) -> None:
    if steps is None:
        return
    if not isinstance(steps, list):
        errors.append("steps must be a list")
        return
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{index}] must be an object")
            continue
        if "order" not in step:
            errors.append(f"steps[{index}] missing 'order'")
        elif not isinstance(step.get("order"), int):
            errors.append(f"steps[{index}].order must be an integer")
        description = str(step.get("description") or "").strip()
        if not description:
            errors.append(f"steps[{index}].description must be a non-empty string")
        elif _step_is_meta(description):
            errors.append(
                f"steps[{index}] is a Plan Mode meta-step ({description!r}); "
                "list only the real execution steps the user is confirming, "
                "not the planning ritual (submitting/awaiting the plan card)"
            )


def _validate_str_list(value: object, field: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field}[{index}] must be a non-empty string")


def _validate_risk(risk: object, errors: list[str]) -> None:
    if risk is None:
        return
    if not isinstance(risk, dict):
        errors.append("risk_assessment must be an object")
        return
    level = risk.get("level")
    if level not in _RISK_LEVELS:
        errors.append(f"risk_assessment.level must be one of {sorted(_RISK_LEVELS)}")


def _validate_wake_policy(wake_policy: object, errors: list[str]) -> None:
    if wake_policy is None:
        return
    if not isinstance(wake_policy, dict):
        errors.append("wake_policy must be an object")
        return
    if "type" not in wake_policy:
        errors.append("wake_policy missing 'type'")


# ---------------------------------------------------------------------------
# State machine (§7)
# ---------------------------------------------------------------------------


def can_transition(src: str, dst: str) -> bool:
    """Return whether the ``src -> dst`` status transition is legal (§7)."""
    return dst in _TRANSITIONS.get(src, frozenset())


# ---------------------------------------------------------------------------
# Confirmation validation (§8.1 + §8.2 + §8.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfirmationCheck:
    """Outcome of validating a confirmation attempt.

    ``error_code`` maps cleanly onto an API response:

    * ``not_confirmable``        -> 409 (wrong status)
    * ``version_mismatch``       -> 409
    * ``hash_mismatch``          -> 409
    * ``missing_confirming_user``-> 401/403
    """

    ok: bool
    error_code: str | None = None
    message: str | None = None


def validate_confirmation(
    *,
    status: str,
    stored_version: int,
    stored_hash: str,
    requested_by_user_id: UUID | str | None,
    submitted_version: int,
    submitted_hash: str,
    confirming_user_id: UUID | str | None,
) -> ConfirmationCheck:
    """Decide whether a confirmation attempt is valid.

    Enforces, in order:

    1. The plan is in ``awaiting_confirmation`` (only confirmable status).
    2. A real confirming user is present (§8.5 — no system/agent confirm).
    3. The submitted ``plan_version`` matches the stored one (§8.2).
    4. The submitted ``plan_hash`` matches the stored one (§8.2).
    """
    if status != "awaiting_confirmation":
        return ConfirmationCheck(
            ok=False,
            error_code="not_confirmable",
            message=f"plan in status {status!r} cannot be confirmed",
        )

    if confirming_user_id is None:
        return ConfirmationCheck(
            ok=False,
            error_code="missing_confirming_user",
            message="confirmation requires an authenticated user action",
        )

    if submitted_version != stored_version:
        return ConfirmationCheck(
            ok=False,
            error_code="version_mismatch",
            message=(f"submitted plan_version {submitted_version} does not match current version {stored_version}"),
        )

    if submitted_hash != stored_hash:
        return ConfirmationCheck(
            ok=False,
            error_code="hash_mismatch",
            message="submitted plan_hash does not match the current plan",
        )

    return ConfirmationCheck(ok=True)


# ---------------------------------------------------------------------------
# Plan gate decision core (§9.0 / §9.2) — pure helpers shared by the early
# intercept layer (tool/REST) and the fail-closed backstop (reconciler/daemon).
# ---------------------------------------------------------------------------


def intent_type_for_action(action_kind: str) -> str:
    """Map a gate ``action_kind`` to the :data:`INTENT_TYPES` it needs a plan for.

    Raises:
        ValueError: if ``action_kind`` is not one of :data:`ACTION_KINDS`.
    """
    try:
        return _ACTION_INTENT[action_kind]
    except KeyError:
        raise ValueError(f"unknown action_kind {action_kind!r}; expected one of {ACTION_KINDS}") from None


def extract_plan_exempt_reason(artifact: dict | None) -> str | None:
    """Return the cutover ``plan_exempt_reason`` carried by an artifact, if any (§9.0).

    Different autonomous artifacts stash governance metadata in different
    places: triggers under ``config.metadata``, ORM rows expose ``metadata_json``,
    and ad-hoc dicts use a plain ``metadata``. We probe all three and return the
    first non-blank reason. Returns ``None`` when no exemption is present.
    """
    if not isinstance(artifact, dict):
        return None

    candidates: list[object] = [
        _nested(artifact, "metadata", "plan_exempt_reason"),
        _nested(artifact, "metadata_json", "plan_exempt_reason"),
        _nested(artifact, "config", "metadata", "plan_exempt_reason"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _nested(obj: object, *keys: str) -> object:
    """Walk ``obj[k0][k1]...`` defensively, returning ``None`` on any miss."""
    current: object = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# ---------------------------------------------------------------------------
# Backstop-layer classification (§9.0) — pure predicates shared by the wake
# reconciler and the trigger-daemon preflight.
# ---------------------------------------------------------------------------

#: §9.0 — the objective ``autonomy_class`` values that are *platform-internal*
#: and therefore never need a user-confirmed plan to auto-wake. The recovery /
#: self-evolution loop (``intake_internal_self_improvement``) tags its objectives
#: ``internal_self_improvement``; keeping that path exempt is what stops the
#: backstop from breaking Hive's own self-improvement triggers.
PLATFORM_INTERNAL_AUTONOMY_CLASSES: frozenset[str] = frozenset({"internal_self_improvement"})
PLAN_EXEMPT_AUTONOMY_CLASSES: frozenset[str] = frozenset({PLAN_EXEMPT_CONFIRMED_HR_BLUEPRINT})

#: §9.0 — autonomous trigger *types* that self-initiate work on a schedule/loop
#: (cron/interval/once/poll). These are the ones that, when enabled, run agent
#: work without a human in the loop and therefore need a confirmed plan or a
#: cutover exemption. ``on_message`` / ``webhook`` are reactive (they wait for an
#: external event) — the design (§9.0) treats them as cutover legacy, not as the
#: "open autonomous behavior" the gate must arbitrate, so they are excluded.
AUTONOMOUS_TRIGGER_TYPES: frozenset[str] = frozenset({"cron", "interval", "once", "poll"})

#: §9.0 — trigger *classes* that are platform-internal plumbing and must keep
#: running through cutover without a plan. ``event_wait`` is reactive and already
#: carries its own lifecycle gate; ``system_maintenance`` is internal upkeep.
PLATFORM_INTERNAL_TRIGGER_CLASSES: frozenset[str] = frozenset({"event_wait", "system_maintenance"})


def objective_wake_exempt_reason(
    *,
    objective_metadata: dict | None,
    autonomy_class: str | None,
) -> str | None:
    """Why an objective may auto-wake *without* a fresh confirmed plan, if at all.

    The wake reconciler auto-creates an enabled trigger for active objectives.
    Post-cutover that is only legitimate when the objective traces to a confirmed
    plan or is platform-internal. Returns a stable reason string when exempt, or
    ``None`` when the objective must NOT be auto-woken (the conversation-intent
    bypass the backstop closes — §9.0).

    Exemptions, in precedence order:

    1. ``plan_id`` present in objective metadata — a confirmed-plan handoff drove
       this objective (``plan_mode_handoff`` stamps it). -> ``"confirmed_plan"``.
    2. ``plan_exempt_reason`` present — an explicit cutover/operator exemption.
       -> that reason verbatim.
    3. ``autonomy_class`` is a confirmed HR blueprint.
       -> ``"confirmed_hr_blueprint"``.
    4. ``autonomy_class`` is platform-internal (self-evolution / recovery).
       -> ``"platform_internal"``.
    """
    metadata = objective_metadata if isinstance(objective_metadata, dict) else {}

    plan_id = metadata.get("plan_id")
    if isinstance(plan_id, str) and plan_id.strip():
        return "confirmed_plan"
    if plan_id is not None and not isinstance(plan_id, str) and str(plan_id).strip():
        # tolerate a UUID/other scalar stamped without str() coercion
        return "confirmed_plan"

    # The objective metadata dict *is* the metadata bag, so the exemption marker
    # sits at the top level here (unlike a trigger artifact, which nests it under
    # config.metadata). Probe the top level first, then fall back to the nested
    # shapes ``extract_plan_exempt_reason`` understands.
    direct = metadata.get("plan_exempt_reason")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    exempt = extract_plan_exempt_reason(metadata)
    if exempt is not None:
        return exempt

    autonomy = str(autonomy_class or "").strip()
    if autonomy in PLAN_EXEMPT_AUTONOMY_CLASSES:
        return autonomy

    if autonomy in PLATFORM_INTERNAL_AUTONOMY_CLASSES:
        return "platform_internal"

    return None


def trigger_is_autonomous(*, trigger_type: str | None, trigger_class: str | None) -> bool:
    """Whether an enabled trigger self-initiates autonomous work (§9.0).

    True for schedule/loop triggers (cron/interval/once/poll) that are not
    platform-internal plumbing. Reactive (``on_message`` / ``webhook``) and
    internal (``event_wait`` / ``system_maintenance``) triggers return False:
    the daemon backstop leaves them to their existing gates / cutover handling
    rather than demanding a plan.

    Note the ``objective_task`` class is *not* listed here: an objective_task
    trigger's autonomy is governed by its bound objective (it already has a
    dedicated preflight gate), and a plan-born objective_task carries
    ``config.plan_id`` which the gate validates directly. Classifying it here
    too would double-gate it.
    """
    cls = str(trigger_class or "").strip()
    if cls in PLATFORM_INTERNAL_TRIGGER_CLASSES:
        return False
    if cls == "objective_task":
        return False
    return str(trigger_type or "").strip() in AUTONOMOUS_TRIGGER_TYPES


@dataclass(frozen=True)
class HandoffCheck:
    """Outcome of validating a confirmed-plan handoff at the gate (§9.0 step 1).

    Distinct from :class:`ConfirmationCheck` (which gates the *user confirming*
    a plan); this gates an *already-confirmed* plan being handed to execution.

    ``error_code`` is one of ``plan_not_confirmed`` /
    ``missing_plan_version_hash`` / ``version_mismatch`` / ``hash_mismatch`` —
    all of which collapse to ``needs_plan`` at the gate.
    """

    ok: bool
    error_code: str | None = None
    message: str | None = None


def validate_plan_handoff(
    *,
    status: str,
    stored_version: int,
    stored_hash: str,
    submitted_version: int | None,
    submitted_hash: str | None,
) -> HandoffCheck:
    """Decide whether a referenced plan may hand off to the execution layer (§9.0).

    The plan must be ``confirmed`` and the caller must submit both
    ``submitted_version`` and ``submitted_hash``. They must match the stored
    values exactly (§8.2 / §8.3 — a confirmed plan that was edited has a new
    hash and cannot execute the old confirmation).
    """
    if status != "confirmed":
        return HandoffCheck(
            ok=False,
            error_code="plan_not_confirmed",
            message=f"referenced plan is in status {status!r}, not 'confirmed'",
        )
    if submitted_version is None or submitted_hash is None:
        return HandoffCheck(
            ok=False,
            error_code="missing_plan_version_hash",
            message="confirmed plan handoff requires plan_version and plan_hash",
        )
    if submitted_version is not None and submitted_version != stored_version:
        return HandoffCheck(
            ok=False,
            error_code="version_mismatch",
            message=(f"submitted plan_version {submitted_version} does not match confirmed version {stored_version}"),
        )
    if submitted_hash is not None and submitted_hash != stored_hash:
        return HandoffCheck(
            ok=False,
            error_code="hash_mismatch",
            message="submitted plan_hash does not match the confirmed plan",
        )
    return HandoffCheck(ok=True)


#: §9.2 — default copy for the ``needs_plan`` envelope. Kept terse and
#: agent-actionable; the next_action mirrors deep_research's RC14 wording so the
#: agent STOPs and waits for a real user confirmation rather than self-confirming.
_NEEDS_PLAN_SUMMARY = (
    "Confirm a plan before starting this autonomous action. Present the plan to the user and collect their "
    "explicit approval first; preference memory or prior authorization may prefill the plan but is NOT approval."
)
_NEEDS_PLAN_NEXT_ACTION = (
    "STOP and create/show a plan, then WAIT for the user to confirm it in a new message. Do not enable triggers, "
    "start long tasks, or delegate until a confirmed plan exists. Never self-confirm on the user's behalf."
)


def default_needs_plan_summary() -> str:
    """Return the default ``needs_plan`` summary copy (§9.2).

    Exposed so the shell can reuse it as a fallback without reaching into a
    module-private constant.
    """
    return _NEEDS_PLAN_SUMMARY


def build_needs_plan_payload(
    *,
    plan_id: str | UUID | None = None,
    plan_version: int | None = None,
    summary: str | None = None,
    plan_preview: dict | None = None,
    next_action: str | None = None,
) -> dict:
    """Assemble the §9.2 ``needs_plan`` response envelope.

    Isomorphic with the deep_research ``needs_plan`` contract so every gate
    surface (tool result, REST body) speaks the same shape. Optional fields are
    omitted when absent rather than emitted as ``None``.
    """
    payload: dict = {
        "ok": False,
        "status": "needs_plan",
        "summary": summary or _NEEDS_PLAN_SUMMARY,
        "next_action": next_action or _NEEDS_PLAN_NEXT_ACTION,
    }
    if plan_id is not None:
        payload["plan_id"] = str(plan_id)
    if plan_version is not None:
        payload["plan_version"] = plan_version
    if plan_preview is not None:
        payload["plan_preview"] = plan_preview
    return payload


def stamp_confirmed_plan_provenance(
    artifact: dict | None,
    *,
    plan_id: object | None,
    plan_version: object | None,
    plan_hash: object | None,
) -> dict:
    """Return ``artifact`` with confirmed-plan provenance stamped when complete.

    Execution backstops validate durable artifacts, not the transient REST/tool
    request body. Any endpoint/tool that creates or re-enables autonomous work
    after a successful gate check must persist the exact confirmed plan id,
    version, and hash onto that artifact.
    """
    stamped = dict(artifact or {})
    if plan_id is None or plan_version is None or plan_hash is None:
        return stamped
    stamped["plan_id"] = str(plan_id)
    stamped["plan_version"] = int(plan_version)
    stamped["plan_hash"] = str(plan_hash)
    return stamped


# ---------------------------------------------------------------------------
# Markdown rendering (§6.2)
# ---------------------------------------------------------------------------


def _yaml_scalar(value: object) -> str:
    """Render a Python value as a minimal, safe YAML scalar for frontmatter."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    # Quote when the value could otherwise be misparsed by YAML. A bare colon
    # is only significant when followed by whitespace ("key: value"), so a
    # token like "sha256:abc" stays a plain scalar.
    needs_quote = (
        text == ""
        or ": " in text
        or text.endswith(":")
        or "#" in text
        or "\n" in text
        or text != text.strip()
        or text[0] in "[]{}>|*&!%@`\"'"
    )
    if needs_quote:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def render_plan_markdown(
    *,
    plan_id: UUID | str,
    agent_id: UUID | str,
    tenant_id: UUID | str | None,
    status: str,
    plan_version: int,
    plan_hash: str,
    intent_type: str,
    created_at: str,
    plan_json: dict,
    confirmed_by: UUID | str | None = None,
    confirmed_at: str | None = None,
) -> str:
    """Render the user-facing ``plans/{plan_id}.md`` artifact (§6.2).

    The leading frontmatter block mirrors the canonical DB fields (it never
    *replaces* the DB). The body is the agent-authored ``plan_markdown`` article
    when present (CC parity: the plan IS a markdown document); machine-intercepted
    plans with no article fall back to a structured summary of ``plan_json``.
    """
    frontmatter_lines = [
        "---",
        f"schema: {PLAN_SCHEMA}",
        f"plan_id: {_yaml_scalar(plan_id)}",
        f"agent_id: {_yaml_scalar(agent_id)}",
        f"tenant_id: {_yaml_scalar(tenant_id)}",
        f"status: {_yaml_scalar(status)}",
        f"plan_version: {plan_version}",
        f"plan_hash: {_yaml_scalar(plan_hash)}",
        f"intent_type: {_yaml_scalar(intent_type)}",
        f"created_at: {_yaml_scalar(created_at)}",
        f"confirmed_by: {_yaml_scalar(confirmed_by)}",
        f"confirmed_at: {_yaml_scalar(confirmed_at)}",
        "---",
    ]

    # Agent-authored body wins (the plan is an article, not a field dump). The
    # structured fields remain in plan_json (hash-covered, folded in the UI), so
    # we don't re-stitch them here when the agent already wrote the plan.
    authored = str(plan_json.get("plan_markdown") or "").strip()
    if authored:
        return "\n".join([*frontmatter_lines, "", authored, ""])

    body_lines = [
        "",
        f"# {plan_json.get('title') or 'Plan'}",
        "",
        "## Objective",
        str(plan_json.get("objective") or ""),
        "",
        "## Motivation",
        str(plan_json.get("motivation") or ""),
        "",
        "## Steps",
    ]
    steps = plan_json.get("steps") or []
    if steps:
        for step in steps:
            order = step.get("order") if isinstance(step, dict) else None
            description = step.get("description") if isinstance(step, dict) else str(step)
            expected = step.get("expected_output") if isinstance(step, dict) else None
            prefix = f"{order}. " if order is not None else "- "
            line = f"{prefix}{description}"
            if expected:
                line += f" (expected: {expected})"
            body_lines.append(line)
    else:
        body_lines.append("- (none yet)")

    body_lines += ["", "## Success criteria"]
    body_lines += _bullet_block(plan_json.get("success_criteria"))

    body_lines += [
        "",
        "## Wake policy",
        "```json",
        json.dumps(plan_json.get("wake_policy") or {}, ensure_ascii=False, indent=2),
        "```",
    ]

    risk = plan_json.get("risk_assessment") or {}
    body_lines += ["", "## Risk", f"Level: {risk.get('level', 'unknown')}"]
    body_lines += _bullet_block(risk.get("reasons"))

    body_lines += [
        "",
        "## Estimated cost",
        "```json",
        json.dumps(plan_json.get("estimated_cost") or {}, ensure_ascii=False, indent=2),
        "```",
    ]

    body_lines += ["", "## Stop conditions"]
    body_lines += _bullet_block(plan_json.get("stop_conditions"))

    body_lines += [""]

    return "\n".join(frontmatter_lines + body_lines)


def _bullet_block(items: object) -> list[str]:
    if not isinstance(items, list) or not items:
        return ["- (none yet)"]
    return [f"- {item}" for item in items]
