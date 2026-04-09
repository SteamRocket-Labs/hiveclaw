"""Tests for model-aware, task-aware context budgets."""

from __future__ import annotations


def test_infer_task_profile_coding():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile(
        "请修复 auth.py 里的 bug，补测试，并检查 API 响应是否回归",
    )

    assert profile.name == "coding"


def test_infer_task_profile_research():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile(
        "请研究最新的竞品动态、行业新闻和公开资料，给我带来源链接的分析",
    )

    assert profile.name == "research"


def test_infer_task_profile_does_not_treat_status_as_typescript_file():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile("latest status")

    assert profile.name == "research"


def test_infer_task_profile_treats_review_regression_request_as_coding():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile("please review this implementation and verify regressions")

    assert profile.name == "coding"


def test_infer_task_profile_recognizes_memory_recall_requests():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile("请回忆我们上次关于 md-first memory system 的决定，并搜索会话历史")

    assert profile.name == "memory_recall"


def test_infer_task_profile_recognizes_self_evolution_requests():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile("把这个重复成功的 workflow 沉淀成 reusable skill 并保存")

    assert profile.name == "self_evolution"


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
    assert budget.active_packs_budget_chars >= 4000
    assert budget.memory_budget_chars >= 24000
    assert budget.restore_budget_chars >= 60000
    assert budget.skill_catalog_budget_chars >= 6000
    assert budget.semantic_limit >= 12
    assert budget.rerank_max_select >= 8


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


def test_infer_task_profile_does_not_suggest_mcp_pack_for_generic_operations():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile(
        "请排查 deployment server 的 incident，查看监控、trigger 和 worker 日志",
    )

    assert profile.name == "operations"
    assert "mcp_admin_pack" not in profile.suggested_pack_names


def test_infer_task_profile_suggests_mcp_pack_for_explicit_platform_extension():
    from app.runtime.context_budget import infer_task_profile

    profile = infer_task_profile(
        "请帮我导入一个 MCP server 扩展能力，并读取它暴露的 resource",
    )

    assert "mcp_admin_pack" in profile.suggested_pack_names
