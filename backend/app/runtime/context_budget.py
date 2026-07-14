"""Model-window-aware context planning with model-owned task semantics."""

from __future__ import annotations

from dataclasses import dataclass


_DEFAULT_SYSTEM_PROMPT_CHAR_BUDGET = 60000
_SYSTEM_PROMPT_CONTEXT_RATIO = 0.20
_CHARS_PER_TOKEN = 3.5
_MIN_SYSTEM_PROMPT_BUDGET = 15000
_MAX_SYSTEM_PROMPT_BUDGET = 350000  # ~100K tokens — lets 1M-window models use materially more context


def _normalize_deferred_tool_group_name(name: str) -> str:
    normalized = str(name or "").strip()
    if normalized.endswith("_pack"):
        normalized = normalized[: -len("_pack")]
    return normalized


def _legacy_pack_name(group_name: str) -> str:
    normalized = str(group_name or "").strip()
    if not normalized:
        return ""
    return normalized if normalized.endswith("_pack") else f"{normalized}_pack"


def compute_system_prompt_budget(context_window_tokens: int | None) -> int:
    """Derive the overall system-prompt budget from the model context window."""
    if not context_window_tokens or context_window_tokens <= 0:
        return _DEFAULT_SYSTEM_PROMPT_CHAR_BUDGET
    budget_chars = int(context_window_tokens * _SYSTEM_PROMPT_CONTEXT_RATIO * _CHARS_PER_TOKEN)
    return max(_MIN_SYSTEM_PROMPT_BUDGET, min(budget_chars, _MAX_SYSTEM_PROMPT_BUDGET))


@dataclass(frozen=True, slots=True, init=False)
class TaskProfile:
    name: str
    complexity: str
    execution_shape: str = "direct"
    suggested_deferred_tool_group_names: tuple[str, ...] = ()

    def __init__(
        self,
        name: str,
        complexity: str,
        suggested_deferred_tool_group_names: tuple[str, ...] = (),
        execution_shape: str = "direct",
        *,
        suggested_pack_names: tuple[str, ...] | None = None,
    ) -> None:
        raw_groups = suggested_deferred_tool_group_names or tuple(suggested_pack_names or ())
        groups = tuple(group for group in (_normalize_deferred_tool_group_name(item) for item in raw_groups) if group)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "complexity", complexity)
        object.__setattr__(self, "execution_shape", normalize_execution_shape(execution_shape))
        object.__setattr__(self, "suggested_deferred_tool_group_names", groups)

    @property
    def suggested_pack_names(self) -> tuple[str, ...]:
        """Legacy compatibility alias for stored/plugin policy surfaces."""
        return tuple(
            legacy_name
            for legacy_name in (_legacy_pack_name(item) for item in self.suggested_deferred_tool_group_names)
            if legacy_name
        )


@dataclass(frozen=True, slots=True)
class TurnModelRoute:
    model: object | None
    fallback_model: object | None
    supports_vision: bool
    reason: str
    task_profile: TaskProfile
    config_source: str


@dataclass(frozen=True, slots=True)
class ContextBudget:
    task_profile: TaskProfile
    system_prompt_budget_chars: int
    active_tool_groups_budget_chars: int
    retrieval_budget_chars: int
    knowledge_budget_chars: int
    memory_budget_chars: int
    skill_catalog_budget_chars: int
    soul_budget_chars: int
    relationships_budget_chars: int
    company_info_budget_chars: int
    org_structure_budget_chars: int
    focus_budget_chars: int
    runtime_triggers_budget_chars: int
    restore_budget_chars: int
    restore_per_file_cap_chars: int
    semantic_limit: int
    episodic_limit: int
    external_limit: int
    rerank_max_select: int


_EXECUTION_SHAPES = {
    "direct",
    "one_off_parallel",
    "fixed_sequence",
    "approval_gate",
    "long_running",
    "recurrent",
}


def normalize_execution_shape(value: object) -> str:
    shape = str(value or "").strip().lower()
    return shape if shape in _EXECUTION_SHAPES else "direct"


def infer_execution_shape(query: str, messages: list[dict] | None = None) -> str:
    """Return the neutral shape; the model declares orchestration through its tool choices.

    ``query`` and ``messages`` remain in the compatibility signature so existing
    callers do not break. Natural-language keyword matching must never select a
    workflow, approval, recurrent, or delegation strategy for the model.
    """
    del query, messages
    return "direct"


def execution_shape_from_round_state(round_state: dict | None) -> str:
    if not isinstance(round_state, dict):
        return "direct"
    return normalize_execution_shape(round_state.get("execution_shape"))


def build_tool_execution_shape_decision(tool_name: str, execution_shape: str) -> dict[str, object]:
    shape = normalize_execution_shape(execution_shape)
    recommendation = "continue"
    severity = "info"
    warning = ""
    if tool_name in {"propose_dynamic_workflow", "preview_workflow", "start_workflow"}:
        if shape == "one_off_parallel":
            recommendation = "use_spawn_subagent"
            severity = "warning"
            warning = "One-off independent fan-out usually belongs to spawn_subagent, not Dynamic Workflow."
        elif shape in {"fixed_sequence", "approval_gate", "long_running", "recurrent"}:
            recommendation = "use_dynamic_workflow"
            severity = "ok"
    elif tool_name == "spawn_subagent":
        if shape in {"fixed_sequence", "approval_gate", "recurrent"}:
            recommendation = "use_dynamic_workflow"
            severity = "warning"
            warning = "Fixed sequence, approval gate, or recurrent work should usually be a workflow."
        elif shape == "long_running":
            recommendation = "use_dynamic_workflow_or_background_subagent"
            severity = "warning"
            warning = "Long-running work needs an explicit durable continuation contract."
        elif shape == "one_off_parallel":
            recommendation = "use_spawn_subagent"
            severity = "ok"
    return {
        "schema": "hive.ccplus.execution_shape_admission.v1",
        "tool_name": tool_name,
        "execution_shape": shape,
        "allowed": True,
        "severity": severity,
        "recommendation": recommendation,
        "warning": warning,
    }


def infer_task_profile(query: str, messages: list[dict] | None = None) -> TaskProfile:
    """Return a neutral profile without interpreting user prose.

    The legacy name is retained for API compatibility. Task type, complexity,
    execution shape, and deferred tools are model decisions, not platform
    keyword-classification outputs.
    """
    del query, messages
    return TaskProfile(
        name="model_owned",
        complexity="model_owned",
        suggested_deferred_tool_group_names=(),
        execution_shape="direct",
    )


def resolve_turn_model_route(
    *,
    primary_model: object | None,
    fallback_model: object | None,
    query: str,
    messages: list[dict] | None = None,
    invocation_scope: str | None = None,
    session_source: str | None = None,
    supports_vision: bool = False,
    routing_config: dict[str, object] | None = None,
    model_routing_locked: bool = False,
) -> TurnModelRoute:
    """Keep the configured primary model; fallback is provider-failure recovery.

    Routing configuration may govern availability and failover, but natural
    language, prompt length, or keyword-derived task labels must not silently
    downgrade the model that reasons about the turn.
    """

    profile = infer_task_profile(query, messages=messages)
    del invocation_scope, session_source
    primary_supports_vision = bool(supports_vision or getattr(primary_model, "supports_vision", False))
    config_source = "agent_config" if routing_config is not None else "runtime_default"
    if primary_model is None:
        return TurnModelRoute(
            model=None,
            fallback_model=fallback_model,
            supports_vision=primary_supports_vision,
            reason="primary_model",
            task_profile=profile,
            config_source=config_source,
        )

    if model_routing_locked:
        return TurnModelRoute(
            model=primary_model,
            fallback_model=fallback_model,
            supports_vision=primary_supports_vision,
            reason="user_model_lock",
            task_profile=profile,
            config_source=config_source,
        )

    return TurnModelRoute(
        model=primary_model,
        fallback_model=fallback_model,
        supports_vision=primary_supports_vision,
        reason="primary_model",
        task_profile=profile,
        config_source=config_source,
    )


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def compute_context_budget(
    *,
    context_window_tokens: int | None,
    query: str = "",
    messages: list[dict] | None = None,
    active_pack_count: int = 0,
) -> ContextBudget:
    """Compute hardware-window budgets without classifying request semantics."""
    system_budget = compute_system_prompt_budget(context_window_tokens)
    profile = infer_task_profile(query, messages=messages)

    # Use one capability-preserving envelope for every request. These values are
    # observability/planning hints at prompt assembly and capacity limits at
    # retrieval boundaries; no user wording changes what the model can see.
    retrieval_ratio = 0.15
    knowledge_ratio = 0.10
    memory_ratio = 0.30
    focus_ratio = 0.08
    restore_ratio = 0.65
    triggers_ratio = 0.07
    semantic_base = 20
    episodic_base = 7
    external_base = 8
    if system_budget >= 300000:
        large_context_bonus = 8
    elif system_budget >= 160000:
        large_context_bonus = 4
    elif system_budget >= 80000:
        large_context_bonus = 2
    else:
        large_context_bonus = 0

    active_tool_groups_budget = _clamp(
        int(system_budget * 0.04) + active_pack_count * 500,
        2000,
        24000,
    )
    retrieval_budget = _clamp(int(system_budget * retrieval_ratio), 3000, 48000)
    knowledge_budget = _clamp(int(system_budget * knowledge_ratio), 1500, 40000)
    memory_budget = _clamp(int(system_budget * memory_ratio), 12000, 96000)
    skill_catalog_budget = _clamp(int(system_budget * 0.11), 6000, 48000)
    soul_budget = _clamp(int(system_budget * 0.22), 16000, 80000)
    relationships_budget = _clamp(int(system_budget * 0.035), 2000, 16000)
    company_info_budget = _clamp(int(system_budget * 0.07), 5000, 32000)
    org_structure_budget = _clamp(int(system_budget * 0.035), 2000, 16000)
    focus_budget = _clamp(int(system_budget * focus_ratio), 3000, 36000)
    runtime_triggers_budget = _clamp(int(system_budget * triggers_ratio), 2000, 24000)
    restore_budget = _clamp(int(system_budget * restore_ratio), 12000, 240000)
    restore_per_file_cap = _clamp(int(restore_budget * 0.2), 2500, 40000)

    semantic_limit = _clamp(semantic_base + large_context_bonus * 2, 8, 64)
    episodic_limit = _clamp(episodic_base + large_context_bonus, 3, 16)
    external_limit = _clamp(external_base + large_context_bonus, 2, 24)
    rerank_max_select = _clamp(max(semantic_limit // 2, 8), 5, 24)

    return ContextBudget(
        task_profile=profile,
        system_prompt_budget_chars=system_budget,
        active_tool_groups_budget_chars=active_tool_groups_budget,
        retrieval_budget_chars=retrieval_budget,
        knowledge_budget_chars=knowledge_budget,
        memory_budget_chars=memory_budget,
        skill_catalog_budget_chars=skill_catalog_budget,
        soul_budget_chars=soul_budget,
        relationships_budget_chars=relationships_budget,
        company_info_budget_chars=company_info_budget,
        org_structure_budget_chars=org_structure_budget,
        focus_budget_chars=focus_budget,
        runtime_triggers_budget_chars=runtime_triggers_budget,
        restore_budget_chars=restore_budget,
        restore_per_file_cap_chars=restore_per_file_cap,
        semantic_limit=semantic_limit,
        episodic_limit=episodic_limit,
        external_limit=external_limit,
        rerank_max_select=rerank_max_select,
    )
