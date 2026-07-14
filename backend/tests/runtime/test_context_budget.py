"""Tests for model-aware context budgets without platform semantic routing."""

from __future__ import annotations


def test_user_prose_does_not_select_a_platform_task_profile():
    from app.runtime.context_budget import infer_task_profile

    prompts = (
        "请修复 auth.py 里的 bug，补测试，并检查 API 响应是否回归",
        "请研究最新的竞品动态、行业新闻和公开资料，给我带来源链接的分析",
        "please review this implementation and verify regressions",
        "请回忆我们上次关于 md-first memory system 的决定，并搜索会话历史",
        "把这个 workflow 沉淀成 reusable skill 并保存",
    )

    profiles = [infer_task_profile(prompt) for prompt in prompts]

    assert {profile.name for profile in profiles} == {"model_owned"}
    assert {profile.complexity for profile in profiles} == {"model_owned"}
    assert all(profile.suggested_deferred_tool_group_names == () for profile in profiles)


def test_user_prose_does_not_select_an_execution_shape():
    from app.runtime.context_budget import infer_task_profile

    parallel = infer_task_profile("请并行派三个独立 worker 分别研究 API、Runtime、前端，然后汇总。")
    approval = infer_task_profile("这个流程必须固定顺序执行，中间需要人工审批 gate，通过后继续。")
    recurrent = infer_task_profile("每天早上 9 点重复运行这个检查，并在有变化时继续处理。")

    assert parallel.execution_shape == "direct"
    assert approval.execution_shape == "direct"
    assert recurrent.execution_shape == "direct"


def test_compute_context_budget_256k_research_is_more_aggressive():
    from app.runtime.context_budget import compute_context_budget

    budget = compute_context_budget(
        context_window_tokens=256000,
        query="请研究最新行业动态并给出带来源的深度分析",
        active_pack_count=2,
    )

    # 256K * 0.20 * 3.5 = 179200 (within 180K ceiling)
    assert budget.system_prompt_budget_chars == 179200
    assert budget.retrieval_budget_chars >= 12000
    assert budget.knowledge_budget_chars >= 4000
    assert budget.active_tool_groups_budget_chars >= 4000
    assert budget.memory_budget_chars >= 24000
    assert budget.restore_budget_chars >= 60000
    assert budget.skill_catalog_budget_chars >= 6000
    assert budget.semantic_limit >= 12
    assert budget.rerank_max_select >= 8


def test_compute_context_budget_1m_model_uses_long_context_capacity():
    from app.runtime.context_budget import compute_context_budget

    budget = compute_context_budget(
        context_window_tokens=1_000_000,
        query="请做非常全面的 deep research，比较多个公开资料、历史记录和工作区证据",
        active_pack_count=4,
    )

    assert budget.system_prompt_budget_chars >= 300_000
    assert budget.retrieval_budget_chars >= 40_000
    assert budget.knowledge_budget_chars >= 30_000
    assert budget.memory_budget_chars >= 80_000
    assert budget.restore_budget_chars >= 160_000
    assert budget.restore_per_file_cap_chars >= 30_000
    assert budget.semantic_limit >= 32
    assert budget.external_limit >= 12


def test_compute_context_budget_small_model_stays_bounded():
    from app.runtime.context_budget import compute_context_budget

    budget = compute_context_budget(
        context_window_tokens=8000,
        query="请修复单个小 bug",
        active_pack_count=0,
    )

    assert budget.system_prompt_budget_chars == 15000
    assert budget.retrieval_budget_chars >= 3000
    assert budget.retrieval_budget_chars <= 6000
    assert budget.restore_budget_chars <= 30000


def test_task_profile_accepts_only_explicit_typed_tool_group_declarations():
    from app.runtime.context_budget import TaskProfile, infer_task_profile

    profile = infer_task_profile(
        "请帮我导入一个 MCP server 扩展能力，并读取它暴露的 resource",
    )

    assert profile.suggested_deferred_tool_group_names == ()
    assert profile.suggested_pack_names == ()

    legacy_profile = TaskProfile(
        name="research",
        complexity="medium",
        suggested_pack_names=("web_pack",),
    )
    assert legacy_profile.suggested_deferred_tool_group_names == ("web",)


def test_resolve_turn_model_route_keeps_primary_for_simple_general_turn_without_explicit_routing():
    from types import SimpleNamespace

    from app.runtime.context_budget import resolve_turn_model_route

    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        max_input_tokens=32000,
        supports_vision=False,
    )

    route = resolve_turn_model_route(
        primary_model=primary_model,
        fallback_model=fallback_model,
        query="帮我润色这句话，让语气更礼貌。",
        invocation_scope="conversation",
        session_source="websocket",
    )

    assert route.model is primary_model
    assert route.fallback_model is fallback_model
    assert route.supports_vision is True
    assert route.reason == "primary_model"
    assert route.task_profile.name == "model_owned"
    assert route.task_profile.complexity == "model_owned"


def test_smart_routing_never_downgrades_model_from_user_prose():
    from types import SimpleNamespace

    from app.runtime.context_budget import resolve_turn_model_route

    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        max_input_tokens=32000,
        supports_vision=False,
    )

    route = resolve_turn_model_route(
        primary_model=primary_model,
        fallback_model=fallback_model,
        query="帮我润色这句话，让语气更礼貌。",
        invocation_scope="conversation",
        session_source="websocket",
        routing_config={"enabled": True},
    )

    assert route.model is primary_model
    assert route.fallback_model is fallback_model
    assert route.supports_vision is True
    assert route.reason == "primary_model"
    assert route.task_profile.name == "model_owned"
    assert route.task_profile.complexity == "model_owned"
    assert route.config_source == "agent_config"


def test_resolve_turn_model_route_keeps_primary_for_coding_turn():
    from types import SimpleNamespace

    from app.runtime.context_budget import resolve_turn_model_route

    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        max_input_tokens=32000,
        supports_vision=False,
    )

    route = resolve_turn_model_route(
        primary_model=primary_model,
        fallback_model=fallback_model,
        query="请修复 auth.py 的 bug，补测试并检查回归。",
        invocation_scope="conversation",
        session_source="websocket",
    )

    assert route.model is primary_model
    assert route.fallback_model is fallback_model
    assert route.supports_vision is True
    assert route.reason == "primary_model"
    assert route.task_profile.name == "model_owned"


def test_resolve_turn_model_route_respects_explicit_disabled_routing_config():
    from types import SimpleNamespace

    from app.runtime.context_budget import resolve_turn_model_route

    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        max_input_tokens=128000,
        supports_vision=True,
    )
    fallback_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1-mini",
        max_input_tokens=32000,
        supports_vision=False,
    )

    route = resolve_turn_model_route(
        primary_model=primary_model,
        fallback_model=fallback_model,
        query="帮我润色这句话，让语气更礼貌。",
        invocation_scope="conversation",
        session_source="websocket",
        routing_config={"enabled": False},
    )

    assert route.model is primary_model
    assert route.fallback_model is fallback_model
    assert route.reason == "primary_model"
    assert route.config_source == "agent_config"


def test_resolve_turn_model_route_respects_user_model_lock_even_when_smart_routing_is_enabled():
    from types import SimpleNamespace

    from app.runtime.context_budget import resolve_turn_model_route

    primary_model = SimpleNamespace(model="gpt-4.1", provider="openai")
    fallback_model = SimpleNamespace(model="gpt-4.1-mini", provider="openai")

    route = resolve_turn_model_route(
        primary_model=primary_model,
        fallback_model=fallback_model,
        query="hello",
        invocation_scope="standard",
        session_source="web",
        routing_config={"enabled": True, "max_simple_chars": 160, "max_simple_words": 28},
        model_routing_locked=True,
    )

    assert route.model is primary_model
    assert route.fallback_model is fallback_model
    assert route.reason == "user_model_lock"


def test_context_budget_is_invariant_to_semantic_keywords():
    from app.runtime.context_budget import compute_context_budget

    coding = compute_context_budget(
        context_window_tokens=128000,
        query="fix auth.py and run tests",
        active_pack_count=2,
    )
    research = compute_context_budget(
        context_window_tokens=128000,
        query="research current primary sources",
        active_pack_count=2,
    )
    approval = compute_context_budget(
        context_window_tokens=128000,
        query="run this workflow after human approval",
        active_pack_count=2,
    )

    assert coding == research == approval
    assert coding.task_profile.name == "model_owned"
