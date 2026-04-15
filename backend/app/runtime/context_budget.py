"""Model-aware, task-aware context budget planning."""

from __future__ import annotations

from dataclasses import dataclass
import re


_DEFAULT_SYSTEM_PROMPT_CHAR_BUDGET = 60000
_SYSTEM_PROMPT_CONTEXT_RATIO = 0.20
_CHARS_PER_TOKEN = 3.5
_MIN_SYSTEM_PROMPT_BUDGET = 15000
_MAX_SYSTEM_PROMPT_BUDGET = 180000  # ~51K tokens — allows 256K models to use full 20% ratio


def compute_system_prompt_budget(context_window_tokens: int | None) -> int:
    """Derive the overall system-prompt budget from the model context window."""
    if not context_window_tokens or context_window_tokens <= 0:
        return _DEFAULT_SYSTEM_PROMPT_CHAR_BUDGET
    budget_chars = int(context_window_tokens * _SYSTEM_PROMPT_CONTEXT_RATIO * _CHARS_PER_TOKEN)
    return max(_MIN_SYSTEM_PROMPT_BUDGET, min(budget_chars, _MAX_SYSTEM_PROMPT_BUDGET))


@dataclass(frozen=True, slots=True)
class TaskProfile:
    name: str
    complexity: str
    suggested_pack_names: tuple[str, ...] = ()


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
    active_packs_budget_chars: int
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


_CODING_HINTS = (
    "bug", "fix", "code", "refactor", "test", "stack trace", "traceback",
    "compile", "api", "endpoint", "migration", "function", "class", ".py",
    ".ts", ".tsx", ".js", "read_file", "write_file", "repo",
    "修复", "代码", "测试", "接口", "函数", "文件", "编译", "回归",
)
_CODING_REVIEW_HINTS = (
    "review",
    "audit",
    "verify",
    "verification",
    "inspect",
    "regression",
    "regressions",
    "implementation",
    "代码审查",
    "评审",
    "审查",
    "复核",
    "回归",
    "实现",
)
_RESEARCH_HINTS = (
    "research", "analyze", "analysis", "compare", "market", "competitor",
    "latest", "news", "source", "sources", "report", "web", "browse",
    "investigate", "trend", "公开资料", "来源", "研究", "竞品", "行业",
    "新闻", "链接", "分析", "调研",
)
_OPERATIONS_HINTS = (
    "deploy", "monitor", "incident", "alert", "cron", "trigger", "heartbeat",
    "automation", "ops", "runbook", "queue", "worker", "dashboard",
    "告警", "触发器", "心跳", "自动化", "运维", "部署", "监控",
)
_MEMORY_RECALL_HINTS = (
    "recall",
    "remember",
    "search_memory",
    "session history",
    "chat history",
    "transcript",
    "memory system",
    "回忆",
    "想起",
    "会话历史",
    "聊天记录",
    "上次",
    "之前",
    "决定",
)
_SELF_EVOLUTION_HINTS = (
    "save_skill",
    "skill",
    "workflow",
    "runbook",
    "reusable",
    "repeatable",
    "playbook",
    "沉淀",
    "复用",
    "可复用",
    "重复成功",
    "工作流",
)
_MCP_EXTENSION_HINTS = (
    "mcp",
    "import_mcp_server",
    "discover_resources",
    "smithery",
    "modelscope",
    "install tool",
    "install tools",
    "install capability",
    "platform extension",
    "扩展能力",
    "导入 mcp",
    "导入一个 mcp",
    "安装 mcp",
    "安装工具",
    "安装能力",
)

_FILE_EXTENSION_HINTS = (".py", ".ts", ".tsx", ".js")
_FILE_EXTENSION_PATTERN = re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js)\b")
_URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
_CHEAP_MODEL_HINTS = (
    "mini",
    "haiku",
    "flash",
    "nano",
    "lite",
    "small",
    "fast",
    "turbo",
    "8b",
    "7b",
    "3b",
)
_NO_CHEAP_ROUTE_SESSION_SOURCES = {"task", "trigger", "heartbeat", "agent"}
_NO_CHEAP_ROUTE_EXECUTION_MODES = {"task", "heartbeat", "coordinator"}


def _contains_keyword(haystack: str, keyword: str) -> bool:
    if keyword in _FILE_EXTENSION_HINTS:
        suffix = keyword[1:]
        return any(match.endswith(f".{suffix}") for match in _FILE_EXTENSION_PATTERN.findall(haystack))

    lowered = keyword.lower()
    if any("\u4e00" <= char <= "\u9fff" for char in lowered):
        return lowered in haystack

    if " " in lowered:
        return lowered in haystack

    pattern = re.compile(rf"(?<![a-z0-9_]){re.escape(lowered)}(?![a-z0-9_])")
    return pattern.search(haystack) is not None


def _keyword_score(haystack: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if _contains_keyword(haystack, keyword))


def _model_descriptor(model: object | None) -> str:
    if model is None:
        return ""
    parts = (
        str(getattr(model, "provider", "") or ""),
        str(getattr(model, "label", "") or ""),
        str(getattr(model, "model", "") or ""),
    )
    return " ".join(part.strip().lower() for part in parts if part and part.strip())


def _cheap_model_signal_score(model: object | None) -> int:
    descriptor = _model_descriptor(model)
    if not descriptor:
        return 0

    score = sum(1 for hint in _CHEAP_MODEL_HINTS if hint in descriptor)
    max_input_tokens = getattr(model, "max_input_tokens", None)
    if isinstance(max_input_tokens, int) and 0 < max_input_tokens <= 64000:
        score += 1
    return score


def _is_clearly_cheaper_model(candidate: object | None, primary: object | None) -> bool:
    candidate_score = _cheap_model_signal_score(candidate)
    primary_score = _cheap_model_signal_score(primary)
    if candidate_score <= primary_score:
        return False
    return candidate_score >= 1


def _allows_cheap_route(*, execution_mode: str | None, session_source: str | None) -> bool:
    if execution_mode and execution_mode.lower() in _NO_CHEAP_ROUTE_EXECUTION_MODES:
        return False
    if session_source and session_source.lower() in _NO_CHEAP_ROUTE_SESSION_SOURCES:
        return False
    return True


def _is_simple_turn_candidate(query: str, task_profile: TaskProfile) -> bool:
    return _is_simple_turn_candidate_with_limits(query, task_profile, max_chars=160, max_words=28)


def _is_simple_turn_candidate_with_limits(
    query: str,
    task_profile: TaskProfile,
    *,
    max_chars: int,
    max_words: int,
) -> bool:
    text = query.strip()
    if not text:
        return False
    if task_profile.name != "general" or task_profile.complexity != "low":
        return False
    if len(text) > max_chars:
        return False
    if len(text.split()) > max_words:
        return False
    if text.count("\n") > 1:
        return False
    if "```" in text or "`" in text:
        return False
    if _URL_PATTERN.search(text):
        return False
    if _FILE_EXTENSION_PATTERN.search(text.lower()):
        return False
    return True


def _coerce_routing_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _routing_enabled(routing_config: dict[str, object] | None) -> bool | None:
    if routing_config is None:
        return None
    return bool(routing_config.get("enabled", False))


def infer_task_profile(query: str, messages: list[dict] | None = None) -> TaskProfile:
    """Infer the dominant task shape from the latest request."""
    haystack = " ".join(
        part for part in [
            query.strip(),
            " ".join(
                str(msg.get("content", ""))
                for msg in messages or []
                if msg.get("role") == "user" and isinstance(msg.get("content"), str)
            ),
        ]
        if part
    ).lower()

    scores = {
        "coding": _keyword_score(haystack, _CODING_HINTS),
        "research": _keyword_score(haystack, _RESEARCH_HINTS),
        "operations": _keyword_score(haystack, _OPERATIONS_HINTS),
        "memory_recall": _keyword_score(haystack, _MEMORY_RECALL_HINTS),
        "self_evolution": _keyword_score(haystack, _SELF_EVOLUTION_HINTS),
    }
    coding_review_score = _keyword_score(haystack, _CODING_REVIEW_HINTS)
    if coding_review_score >= 2:
        scores["coding"] += 3

    # Specialized agent behaviors should route to their own playbooks when there
    # is strong evidence, rather than being flattened into "general".
    if scores["memory_recall"] >= 2:
        scores["memory_recall"] += 2
    if scores["self_evolution"] >= 2:
        scores["self_evolution"] += 2

    name = max(scores, key=scores.get) if any(scores.values()) else "general"

    query_len = len(haystack)
    if query_len > 400 or sum(scores.values()) >= 6:
        complexity = "high"
    elif query_len > 120 or sum(scores.values()) >= 3:
        complexity = "medium"
    else:
        complexity = "low"

    explicit_mcp_extension = any(hint in haystack for hint in _MCP_EXTENSION_HINTS)

    suggested_packs: tuple[str, ...] = ()
    if explicit_mcp_extension:
        suggested_packs = ("mcp_admin_pack",)
    elif name == "research":
        suggested_packs = ("web_pack",)

    return TaskProfile(name=name, complexity=complexity, suggested_pack_names=suggested_packs)


def resolve_turn_model_route(
    *,
    primary_model: object | None,
    fallback_model: object | None,
    query: str,
    messages: list[dict] | None = None,
    execution_mode: str | None = None,
    session_source: str | None = None,
    supports_vision: bool = False,
    routing_config: dict[str, object] | None = None,
) -> TurnModelRoute:
    """Resolve the effective model for the current turn.

    Conservative by design:
    - Only route to a cheaper fallback when the turn looks like a simple
      low-stakes conversation request.
    - Task/heartbeat/trigger/delegation paths always stay on the primary model.
    """

    profile = infer_task_profile(query, messages=messages)
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

    if not _allows_cheap_route(execution_mode=execution_mode, session_source=session_source):
        return TurnModelRoute(
            model=primary_model,
            fallback_model=fallback_model,
            supports_vision=primary_supports_vision,
            reason="primary_model",
            task_profile=profile,
            config_source=config_source,
        )

    routing_enabled = _routing_enabled(routing_config)
    if routing_enabled is False:
        return TurnModelRoute(
            model=primary_model,
            fallback_model=fallback_model,
            supports_vision=primary_supports_vision,
            reason="primary_model",
            task_profile=profile,
            config_source=config_source,
        )

    if not _is_clearly_cheaper_model(fallback_model, primary_model):
        return TurnModelRoute(
            model=primary_model,
            fallback_model=fallback_model,
            supports_vision=primary_supports_vision,
            reason="primary_model",
            task_profile=profile,
            config_source=config_source,
        )

    max_simple_chars = _coerce_routing_int(
        routing_config.get("max_simple_chars") if routing_config else None,
        160,
    )
    max_simple_words = _coerce_routing_int(
        routing_config.get("max_simple_words") if routing_config else None,
        28,
    )
    if not _is_simple_turn_candidate_with_limits(
        query,
        profile,
        max_chars=max_simple_chars,
        max_words=max_simple_words,
    ):
        return TurnModelRoute(
            model=primary_model,
            fallback_model=fallback_model,
            supports_vision=primary_supports_vision,
            reason="primary_model",
            task_profile=profile,
            config_source=config_source,
        )

    return TurnModelRoute(
        model=fallback_model,
        fallback_model=primary_model,
        supports_vision=bool(supports_vision or getattr(fallback_model, "supports_vision", False)),
        reason="simple_turn_cheap_model",
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
    """Compute section budgets for prompts, memory recall, and restoration."""
    system_budget = compute_system_prompt_budget(context_window_tokens)
    profile = infer_task_profile(query, messages=messages)

    # P1.3: Task-aware ratio profiles — each task type prioritizes different context
    #
    # Coding: boost restore (recent files, pending, write artifacts), moderate memory
    # Research: boost external/knowledge, high memory for accumulated findings
    # Operations: boost focus/triggers/failure patterns, moderate restore
    # General: balanced defaults
    if profile.name == "coding":
        retrieval_ratio = 0.12
        knowledge_ratio = 0.04
        memory_ratio = 0.24
        focus_ratio = 0.07       # higher — current task state matters
        restore_ratio = 0.65     # higher — recent files/writes/pending critical
        triggers_ratio = 0.03
        semantic_base = 16
        episodic_base = 4
        external_base = 4
    elif profile.name == "research":
        retrieval_ratio = 0.15
        knowledge_ratio = 0.10   # higher — external evidence is king
        memory_ratio = 0.28      # higher — accumulated findings
        focus_ratio = 0.05
        restore_ratio = 0.50
        triggers_ratio = 0.03
        semantic_base = 20
        episodic_base = 5
        external_base = 8        # higher — more external sources
    elif profile.name == "operations":
        retrieval_ratio = 0.10
        knowledge_ratio = 0.05
        memory_ratio = 0.22
        focus_ratio = 0.08       # higher — operational state
        restore_ratio = 0.55
        triggers_ratio = 0.07    # higher — trigger/cron state matters
        semantic_base = 14
        episodic_base = 4
        external_base = 4
    elif profile.name == "memory_recall":
        retrieval_ratio = 0.14
        knowledge_ratio = 0.03
        memory_ratio = 0.30
        focus_ratio = 0.07
        restore_ratio = 0.60
        triggers_ratio = 0.03
        semantic_base = 18
        episodic_base = 7
        external_base = 2
    elif profile.name == "self_evolution":
        retrieval_ratio = 0.10
        knowledge_ratio = 0.03
        memory_ratio = 0.24
        focus_ratio = 0.07
        restore_ratio = 0.58
        triggers_ratio = 0.03
        semantic_base = 16
        episodic_base = 5
        external_base = 2
    else:
        retrieval_ratio = 0.09
        knowledge_ratio = 0.04
        memory_ratio = 0.20
        focus_ratio = 0.06
        restore_ratio = 0.55
        triggers_ratio = 0.05
        semantic_base = 12
        episodic_base = 4
        external_base = 3

    complexity_bonus = {"low": 0, "medium": 1, "high": 2}[profile.complexity]
    large_context_bonus = 2 if system_budget >= 80000 else 0

    active_packs_budget = _clamp(
        int(system_budget * 0.04) + active_pack_count * 500,
        2000,
        12000,
    )
    retrieval_budget = _clamp(int(system_budget * retrieval_ratio), 3000, 24000)
    knowledge_budget = _clamp(int(system_budget * knowledge_ratio), 1500, 16000)
    memory_budget = _clamp(int(system_budget * memory_ratio), 12000, 36000)
    skill_catalog_budget = _clamp(int(system_budget * 0.08), 4000, 12000)
    if profile.name == "self_evolution":
        skill_catalog_budget = _clamp(int(system_budget * 0.11), 6000, 16000)
    soul_budget = _clamp(int(system_budget * 0.22), 16000, 32000)
    relationships_budget = _clamp(int(system_budget * 0.035), 2000, 6000)
    company_info_budget = _clamp(int(system_budget * 0.07), 5000, 12000)
    org_structure_budget = _clamp(int(system_budget * 0.035), 2000, 6000)
    focus_budget = _clamp(int(system_budget * focus_ratio), 3000, 12000)
    runtime_triggers_budget = _clamp(int(system_budget * triggers_ratio), 2000, 10000)
    restore_budget = _clamp(int(system_budget * restore_ratio), 12000, 100000)
    restore_per_file_cap = _clamp(int(restore_budget * 0.2), 2500, 12000)

    semantic_limit = _clamp(semantic_base + complexity_bonus * 2 + large_context_bonus * 2, 8, 32)
    episodic_limit = _clamp(episodic_base + complexity_bonus + large_context_bonus, 3, 8)
    external_limit = _clamp(external_base + complexity_bonus + large_context_bonus, 2, 10)
    rerank_max_select = _clamp(max(semantic_limit // 2, 8), 5, 12)

    return ContextBudget(
        task_profile=profile,
        system_prompt_budget_chars=system_budget,
        active_packs_budget_chars=active_packs_budget,
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
