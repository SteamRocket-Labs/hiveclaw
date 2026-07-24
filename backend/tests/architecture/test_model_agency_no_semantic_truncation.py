from __future__ import annotations

import re
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[2] / "app"
REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_ROOT = REPO_ROOT / "frontend/src"


def _handbook_section(source: str, heading: str, next_heading: str) -> str:
    return source.split(heading, 1)[1].split(next_heading, 1)[0]


def test_agent_handbooks_share_one_exact_model_agency_contract() -> None:
    heading = "## Model Agency Boundary — 模型语义主权与平台治理边界"
    next_heading = "## Delivery Discipline — One Complete Pass, No MVP"
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    agents_contract = _handbook_section(agents, heading, next_heading)
    claude_contract = _handbook_section(claude, heading, next_heading)

    assert agents_contract == claude_contract
    for required in (
        "the LLM is the sole owner of semantic judgment",
        "Hard-constraint allowlist",
        "Forbidden implementation patterns",
        "Mechanical fallback contract",
        "explicit anchored command grammar",
        "treating a client-echoed server hash as semantic authority",
    ):
        assert required in agents_contract


def test_agent_handbooks_share_one_exact_north_star_decision_order() -> None:
    heading = "## North Star Decision Order — 北极星裁决顺序"
    next_heading = "## Reference Baselines — 对照物顺序"
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    agents_contract = _handbook_section(agents, heading, next_heading)
    claude_contract = _handbook_section(claude, heading, next_heading)

    assert agents_contract == claude_contract
    for required in (
        "Goal 1 is built and judged first",
        "AI-native and Model Agency Boundary",
        "complete authorized evidence availability",
        "Personal Knowledge Base is tool-only",
        "Codex Desktop",
        "seven-atom standard",
        "not by making model behavior easier for the platform to predict",
    ):
        assert required in agents_contract


def test_reusable_atomic_review_prompt_is_timeless_and_north_star_complete() -> None:
    prompt_path = REPO_ROOT / "docs/reusable-agent-native-atomic-review-prompt.md"
    source = prompt_path.read_text(encoding="utf-8")

    for required in (
        "可复用正文",
        "最强数字员工",
        "公司级控制中台",
        "hermes-agent",
        "先释放模型，再约束行动",
        "Personal KB 必须保持 tool-only",
        "Codex Desktop",
        "输入（Input）",
        "权威（Authority）",
        "执行（Execution）",
        "证据（Evidence）",
        "恢复（Recovery）",
        "消费（Consumption）",
        "验收（Acceptance）",
    ):
        assert required in source

    assert re.search(r"20\d{2}-\d{2}-\d{2}", source) is None
    for historical_marker in ("第一轮", "第二轮", "第三轮", "28个断点", "28 个断点"):
        assert historical_marker not in source

    docs_index = (REPO_ROOT / "docs/README.md").read_text(encoding="utf-8")
    assert prompt_path.name in docs_index


def test_canonical_north_star_docs_preserve_agency_and_scope_boundaries() -> None:
    master = (REPO_ROOT / "docs/hive-sota-master-goal.md").read_text(encoding="utf-8")
    ccplus = (REPO_ROOT / "docs/ccplus-north-star-contract-2026-06-24.md").read_text(encoding="utf-8")
    evolution = (REPO_ROOT / "docs/self-evolution-sota-plan.md").read_text(encoding="utf-8")

    for required in (
        "对齐的目标是保住并增强 Agent 能力，不是让模型行为更容易被平台预测",
        "Personal KB 必须保持 tool-only",
        "| G5 | Workflow durable orchestration |",
        "| G16 | 用户消费与 UI/UX 闭环 |",
    ):
        assert required in master

    for required in (
        "semantic floor, not a line-by-line implementation template",
        "capability-preserving determinism",
        "Personal Knowledge Base: governed tool-only knowledge source",
        "Codex Desktop is the UI/UX benchmark",
    ):
        assert required in ccplus

    for required in (
        "Current authority guard",
        '"direct" memory means an immediately session-visible overlay or candidate',
        "deterministic checks govern exact tests, schemas, states, receipts, permissions, and side effects",
        "Personal KB remains a governed tool-only knowledge source",
    ):
        assert required in evolution


def test_workflow_promotion_requires_model_or_human_review_not_mechanical_eligibility() -> None:
    roots = (APP_ROOT, FRONTEND_ROOT)
    forbidden = ("promotion_eligible", "promotion_eligibility", "promotionEligible")
    violations: list[str] = []

    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in source:
                    violations.append(f"{path.relative_to(REPO_ROOT)}: {marker}")

    assert violations == []


def test_channel_permission_effects_use_explicit_command_grammar_not_prose_regexes() -> None:
    source = (APP_ROOT / "services/channel_agent_runtime.py").read_text(encoding="utf-8")
    action_parser = source.split("def _parse_channel_permission_action", 1)[1].split(
        "_CHANNEL_PERMISSION_MODE_LABELS", 1
    )[0]
    mode_parser = source.split("def _parse_channel_permission_mode_command", 1)[1].split(
        "async def _load_channel_session", 1
    )[0]

    assert "re.search" not in action_parser
    assert "re.search" not in mode_parser
    assert "exact_commands" in action_parser
    assert 'lower.startswith(("/permissions", "/permission"))' in mode_parser


def test_durable_runtime_truth_has_no_fixed_character_slices() -> None:
    forbidden_by_file = {
        "services/web_chat_runtime.py": (
            "raw_str = raw_str[:50000]",
            'result_summary=str(getattr(terminal_event, "content", None) or "")[:500]',
        ),
        "services/trigger_daemon.py": (
            'completion_summary = (final_reply or "Trigger completed.")[:2000]',
            'failure_summary = f"Trigger invocation failed: {str(e)[:500]}"',
        ),
        "services/workflow_runtime_service.py": (
            "error=error[:4000]",
            "error=reason[:4000]",
            "output_text = output_text[:3997]",
        ),
        "services/business_task_runtime.py": (
            "outcome.summary[:20_000]",
            ")[:20_000]",
        ),
        "services/dream_runtime.py": (
            ")[:20_000]",
            "error[:20_000]",
            "error[:2_000]",
        ),
        "services/approval_execution_runtime.py": (
            ")[:20_000]",
            "error[:20_000]",
            "error[:2_000]",
        ),
        "services/hr_provisioning_runtime.py": (
            ")[:20_000]",
            "reason[:2_000]",
        ),
        "services/runtime_notification_outbox.py": ("str(task.result_summary or '')[:20_000]",),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_model_visible_errors_hooks_and_signals_have_no_fixed_character_slices() -> None:
    forbidden_by_file = {
        "kernel/engine.py": (
            'normalized.lstrip("❌ ")[:300]',
            "error_message[:300]",
        ),
        "kernel/loop_guard.py": ("str(result)[:200]",),
        "kernel/turn_orchestrator.py": (
            '"error": str(exc)[:500]',
            "str(exc)[:200]",
            '(raw_args or "")[:200]',
            "(raw_args or '')[:200]",
            "str(_r)[:200]",
        ),
        "agents/subagent.py": ('(result.content or result.error or "")[:500]',),
        "agents/orchestrator.py": ("failed: {type(exc).__name__}: {str(exc)[:300]}",),
        "tools/service.py": (
            "tool_result=result_text[:500]",
            "error=error_text[:500]",
            '"message": str(exc)[:500]',
        ),
        "runtime/failure_policy.py": ('str(message or "").strip()[:500]',),
        "services/officecli_adapter.py": ("stderr[:500]",),
        "services/agent_tool_domains/feishu_docs.py": ('(resp.text or "")[:200]',),
        "services/web_chat_run_orchestrator.py": (
            '"error": str(exc)[:500]',
            '"error": str(terminal)[:500]',
            '"original_error": str(original)[:500]',
        ),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_model_visible_tool_discovery_and_search_results_are_not_mechanically_pruned() -> None:
    forbidden_by_file = {
        "services/agent_tool_domains/triggers.py": (
            "str(t.config)[:50]",
            "t.reason[:40]",
            "findings[:20]",
            'replace("|", "/")[:160]',
        ),
        "services/agent_tool_domains/workspace.py": ("matching_skills[:20]",),
        "tools/handlers/search.py": (
            "results[:8]",
            'r.get("summary", "")[:100]',
            "str(e)[:200]",
        ),
        "services/resource_discovery.py": (
            'srv.get("description", "")[:200]',
            "existing_server_tools[:5]",
        ),
        "services/agent_tool_domains/feishu_users.py": (
            "scored_cache[:5]",
            "scored_members[:5]",
            "scored_users[:5]",
        ),
        "services/agent_tool_domains/web_mcp.py": (
            "snippet[:300]",
            "str(snippet)[:300]",
            "str(text)[:500]",
            "summary[:200]",
            "answer[:1000]",
            "r.get('content', '')[:400]",
        ),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_web_fetch_defaults_to_full_content_and_only_honors_explicit_model_limits() -> None:
    source = (APP_ROOT / "services/agent_tool_domains/web_mcp.py").read_text(encoding="utf-8")
    definitions = (APP_ROOT / "tools/handlers/search.py").read_text(encoding="utf-8")

    assert 'arguments.get("max_chars", 8000)' not in source
    assert 'arguments.get("max_chars", 12000)' not in source
    assert 'min(_safe_int(arguments.get("max_chars")' not in source
    assert '"maximum": 30000' not in definitions
    assert "default 12000, max 30000" not in definitions


def test_feishu_readers_default_to_full_content() -> None:
    drive = (APP_ROOT / "services/agent_tool_domains/feishu_drive.py").read_text(encoding="utf-8")
    docs = (APP_ROOT / "services/agent_tool_domains/feishu_docs.py").read_text(encoding="utf-8")
    definitions = (APP_ROOT / "tools/handlers/feishu.py").read_text(encoding="utf-8")

    assert 'arguments.get("max_chars", 6000)' not in drive
    assert 'arguments.get("max_chars", 6000)' not in docs
    assert '"max_chars": 6000' not in docs
    assert "default 6000, max 20000" not in definitions


def test_memory_curation_inputs_and_loads_are_not_fixed_count_or_character_pruned() -> None:
    forbidden_by_file = {
        "memory/assembler.py": ("compact[:4]", "total_chars + line_len > budget_chars"),
        "memory/t0/ledger.py": ("decision.sanitized_text[:4000]",),
        "memory/t2/segment_package.py": (
            "not in set(selected_ids)][:4]",
            "][:4]",
        ),
        "memory/t3_consolidation.py": ("_relation_lines(content)[:6]",),
        "memory/t2/job_sweep.py": ('(manifest.get("issues") or [])][:5]',),
        "memory/t3_platform_gate.py": ("dropped[:3]",),
        "tools/handlers/memory.py": ("ids = ids[:20]",),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_learning_recall_and_runtime_evidence_are_not_semantically_pruned() -> None:
    forbidden_by_file = {
        "services/fast_reflection_learning_brain.py": (
            "value[:limit]",
            "text[:500]",
        ),
        "runtime/runtime_reminder_candidate.py": (),
        "services/skill_flywheel.py": ("lesson[:240]",),
        "services/heartbeat_t3_core.py": ("issues[:3]",),
        "services/heartbeat.py": (
            "return summary[:100]",
            "result.summary[:120]",
        ),
        "services/subagent_run_service.py": ("str(exc)[:500]",),
        "services/trigger_failure_policy.py": ("str(error)[:1000]",),
        "services/session_recall.py": (
            "max_tokens=180",
            "max_tokens=1024",
            "snippets[0][:120]",
            "line[:120]",
        ),
        "services/t0_logger.py": (
            "warnings[:20]",
            '(soul_candidate.get("source_refs") or [])[:5]',
            '"error" in head[:20]',
        ),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []

    reminder_source = (APP_ROOT / "runtime/runtime_reminder_candidate.py").read_text(encoding="utf-8")
    assert '"content": reminder_text' in reminder_source


def test_prompt_context_budget_arguments_are_advisory_not_semantic_pruners() -> None:
    forbidden_by_file = {
        "services/agent_context.py": (
            "preview = content[:offset]",
            "description = val[:200]",
            "description = line[:200]",
            "content = content[:max_chars]",
            "max_chars: int | None = 3000",
        ),
        "runtime/prompt_builder.py": (
            "head = stripped[:line_budget]",
            "return (head + _TRIM_MARKER)",
        ),
        "runtime/prompt_sections/knowledge.py": ("if len(text) > available_budget",),
        "runtime/prompt_sections/memory.py": ("snapshot = snapshot[:budget_chars]",),
        "runtime/prompt_sections/subagent_listing.py": ("manifests[: max(1, int(limit or 20))]",),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_model_visible_results_and_durable_evidence_preserve_complete_content() -> None:
    forbidden_by_file = {
        "services/llm_client.py": (
            "response.text[:500]",
            "error_body[:500]",
            "e.response.text[:200]",
        ),
        "services/email_service.py": (
            'f"Failed to send email: {str(e)[:200]}"',
            'body = body[:500] + "..."',
            'f"Failed to read emails: {str(e)[:200]}"',
        ),
        "services/approval_ticket.py": ("str(result)[:20_000]",),
        "services/runtime_task_worker.py": ('f"{type(exc).__name__}: {str(exc)[:500]}"',),
        "services/task_executor.py": (
            "error_msg[:150]",
            "error_msg[:500]",
            '"reply": reply[:500]',
        ),
        "services/chat_transcript.py": ("str(exc)[:300]",),
        "services/ai_assets.py": (
            'f"{type(error).__name__}: {error}"[:2000]',
            'f"{type(exc).__name__}: {exc}"[:2000]',
        ),
        "services/invocation_trace.py": ("str(error)[:4000]",),
        "services/runtime_notification_outbox.py": (
            "error[:1_000]",
            "str(error)[:1000]",
        ),
        "services/hr_creation_service.py": (
            "error_message[:4000]",
            "message[:4000]",
        ),
        "services/hr_provisioning_runner.py": (
            "result[:300]",
            'str(result["error"])[:300]',
            "str(mcp_err)[:300]",
            "str(_json_err)[:300]",
            "str(ch_err)[:300]",
            "str(ext_err)[:300]",
        ),
        "services/channel_ingress_inbox.py": ("str(error)[:1000]",),
        "services/channel_agent_runtime.py": ("error_msg[:150]",),
        "services/resource_discovery.py": (
            "conn_resp.text[:200]",
            "str(e)[:200]",
        ),
        "services/mcp_client.py": ("str(e)[:200]",),
        "services/code_execution/probe.py": ("str(exc)[:500]",),
        "services/code_execution/vercel_provider.py": ("str(e)[:300]",),
        "services/agent_tool_domains/image_upload.py": (
            "resp.text[:300]",
            "str(e)[:300]",
        ),
        "services/agent_tool_domains/feishu_helpers.py": ("str(exc)[:160]",),
        "services/agent_tool_domains/plaza.py": (
            ".order_by(PlazaComment.created_at).limit(5)",
            "content = content[:500]",
            "content = content[:300]",
            "str(e)[:200]",
        ),
        "services/agent_tool_domains/workspace.py": ("proc.stderr.strip()[:200]",),
        "services/agent_tool_domains/web_mcp.py": (
            "domains[:5]",
            "str(e)[:200]",
        ),
        "services/trigger_daemon.py": (
            "current_str[:500]",
            "str(last_value)[:200]",
            "current_str[:200]",
            '(msg.content or "")[:2000]',
            'cfg["_matched_message"][:500]',
            "payload_str = payload_str[:2000]",
            "str(exc)[:500]",
            "str(exc)[:1000]",
        ),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_ingress_delivery_and_governed_projections_do_not_silently_slice_semantics() -> None:
    forbidden_by_file = {
        "api/plaza.py": (
            "content=body.content[:500]",
            "content=body.content[:300]",
        ),
        "api/webhooks.py": (
            "repr(body[:2000])",
            '"_webhook_payload": payload_str[:8000]',
        ),
        "services/business_task_runtime.py": ("text=text[:20_000]",),
        "services/budget_transition_outbox.py": (
            "result.message[:1000]",
            "str(error)[:1000]",
        ),
        "services/channel_delivery_outbox.py": (
            "result.message[:1000]",
            "str(error)[:1000]",
        ),
        "services/runtime_control_bus.py": ("str(exc)[:1000]",),
        "services/personal_knowledge_proposals.py": (
            "str(value).strip()[:500]",
            'str(reason or "").strip()[:2000]',
            'str(purpose or "").strip()[:2000]',
        ),
        "services/personal_knowledge_service.py": ('f"personal_kb_import_worker_error:{error}"[:500]',),
        "services/workflow_confirmation_service.py": ("str(message)[:4000]",),
        "services/skill_distillation_runner.py": ("save_result[:500]",),
        "services/daemon_liveness.py": (
            "str(exc)[:500]",
            "reason[:500]",
        ),
        "services/external_principal_service.py": ('str(reason or "explicit binding")[:1000]',),
        "runtime/invocation_orchestrator.py": ("note=assistant_text[:500]",),
        "skills/parser.py": (
            'self._string_value(frontmatter.get("description"))[:1024]',
            "description = line[:1024]",
        ),
        "tools/handlers/hr.py": ("RuntimeError(message[:300]",),
        "services/agent_tool_domains/email.py": ("str(e)[:200]",),
        "services/llm_error_policy.py": ("msg[:80]",),
        "services/mcp_metadata_trust.py": ("str(validation_error)[:160]",),
        "services/org_sync_service.py": (
            "str(e)[:100]",
            "str(e)[:200]",
        ),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_operational_failures_keep_complete_typed_diagnostics() -> None:
    forbidden_by_file = {
        "api/enterprise.py": (
            '(response.content or "")[:100]',
            "str(e)[:500]",
        ),
        "api/feishu.py": ("session.error_msg = str(exc)[:500]",),
        "api/teams.py": ("error_body[:500]",),
        "api/telegram.py": ("resp.text[:200]",),
        "api/upload.py": (
            '"error": str(e)[:300]',
            '"error": str(exc)[:300]',
        ),
        "main.py": ("str(exc)[:500]",),
        "services/mcp_oauth.py": (
            "str(exc)[:200]",
            "resp.text[:500]",
            "str(data)[:300]",
        ),
        "services/feishu_ws.py": ("error[:500]",),
        "services/web_chat_runtime.py": ("str(exc)[:300]",),
        "services/wecom_stream.py": ("str(e)[:100]",),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_observability_read_models_retain_full_canonical_detail() -> None:
    forbidden_by_file = {
        "tools/execution_pipeline.py": (
            '"result": result_text[:300]',
            "default=str)[:100]",
            "else str(value)[:100]",
        ),
        "services/agent_tool_domains/messaging.py": (
            '"message": message_text[:200]',
            '"reply": target_reply[:200]',
        ),
        "services/channel_delivery_service.py": ("result.message[:200]",),
        "services/runtime_control_bus.py": ("str(exc)[:300]",),
        "services/runtime_task_worker.py": (
            "str(exc)[:300]",
            "str(exc)[:500]",
        ),
        "services/web_chat_stream_bus.py": ("str(exc)[:300]",),
        "scripts/migrate_schedules_to_triggers.py": ("instruction[:500]",),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_false_verifier_repair_is_filtered_and_observable_on_failure() -> None:
    source = (APP_ROOT / "services/false_tool_evidence_repair.py").read_text(encoding="utf-8")

    assert "ChatMessage.content.like(" in source
    assert 'logger.exception("Failed to repair false tool-evidence notice' in source


def test_subagent_results_and_model_declared_attention_are_never_mechanically_capped() -> None:
    subagent_source = (APP_ROOT / "agents/subagent.py").read_text(encoding="utf-8")
    working_set_source = (APP_ROOT / "memory/session_working_set.py").read_text(encoding="utf-8")

    assert "content = content[: budget.max_source_chars]" not in subagent_source
    assert "len(captured_sources) >= budget.max_sources" not in subagent_source
    assert "content = content[: budget.max_output_chars]" not in subagent_source
    assert 'item.get("pinned")' in working_set_source
    assert "working_set.items[: max(1, top_n)]" not in working_set_source


def test_no_dead_mechanical_semantic_classifier_can_be_reactivated() -> None:
    auto_dream = (APP_ROOT / "services/auto_dream.py").read_text(encoding="utf-8")
    skill_curator = (APP_ROOT / "services/skill_curator.py").read_text(encoding="utf-8")

    assert "_programmatic_dedup" not in auto_dream
    assert "_simple_dedup" not in auto_dream
    assert "_build_dream_consolidation_prompt" not in auto_dream
    assert "apply_skill_auto_transitions" not in skill_curator
    assert "_STALE_AFTER_DAYS" not in skill_curator
    assert not (APP_ROOT / "memory/t2/engineering_labels.py").exists()

    distiller = (APP_ROOT / "services/skill_distiller.py").read_text(encoding="utf-8")
    distillation_runner = (APP_ROOT / "services/skill_distillation_runner.py").read_text(encoding="utf-8")
    assert "_select_direct_skill_candidate" not in distiller
    assert "_select_direct_skill_candidate" not in distillation_runner
    assert "reviewable[0]" not in distillation_runner


def test_semantic_gate_scores_are_explanations_not_platform_cutoffs() -> None:
    segment = (APP_ROOT / "memory/t2/segment_package.py").read_text(encoding="utf-8")
    t3_gate = (APP_ROOT / "memory/t3_platform_gate.py").read_text(encoding="utf-8")
    dream = (APP_ROOT / "services/auto_dream.py").read_text(encoding="utf-8")

    for forbidden in (
        "_T3_INTAKE_REVIEW_THRESHOLDS",
        "_EPISODE_T3_INTAKE_THRESHOLDS",
        "_REVIEW_RUBRIC_SCORE_WEIGHTS",
        "_EPISODE_REVIEW_RUBRIC_SCORE_WEIGHTS",
    ):
        assert forbidden not in segment
    assert "below accepted threshold" not in t3_gate
    assert "Soul Memory Gate score below threshold" not in dream


def test_memory_retrieval_never_uses_top_k_or_similarity_as_semantic_authority() -> None:
    retriever = (APP_ROOT / "memory/retriever.py").read_text(encoding="utf-8")
    wiki = (APP_ROOT / "memory/wiki_retrieval.py").read_text(encoding="utf-8")
    assembler = (APP_ROOT / "memory/assembler.py").read_text(encoding="utf-8")

    assert ".limit(previous_limit)" not in retriever
    assert "_content_similar" not in retriever
    assert "episodic_limit" not in retriever
    assert "semantic_limit" not in retriever
    assert "external_limit" not in retriever
    assert "_SCORE_FLOOR" not in wiki
    assert "ranked[: max(1, limit)]" not in wiki
    assert "_content_hash" not in assembler


def test_fast_reflection_and_skill_flywheel_do_not_invent_semantics_from_markers() -> None:
    import inspect

    from app.services.fast_reflection_service import _classify_signal
    from app.services.skill_flywheel import propose_skill_candidate_from_fast_reflection

    classifier = inspect.getsource(_classify_signal)
    flywheel = inspect.getsource(propose_skill_candidate_from_fast_reflection)

    assert "fast_reflection_signal" not in classifier
    assert "user_correction" not in classifier
    assert "repeated_workflow_signature" not in classifier
    assert "learning_brain_decision" in flywheel
    assert "model_did_not_nominate_skill" in flywheel


def test_heartbeat_prompt_does_not_map_numeric_scores_to_memory_actions() -> None:
    heartbeat = (APP_ROOT / "templates/HEARTBEAT.md").read_text(encoding="utf-8")

    assert ">= 0.85" not in heartbeat
    assert "0.50-0.85" not in heartbeat
    assert "< 0.50" not in heartbeat
    assert "Scores are evidence explanations, never platform cutoffs" in heartbeat


def test_semantic_auxiliary_calls_use_the_selected_models_output_budget() -> None:
    forbidden_by_file = {
        "services/skill_distiller.py": (
            'max_tokens=min(getattr(model, "max_output_tokens", None) or 8192, 8192)',
            'max_tokens=getattr(model, "max_output_tokens", None) or 8192',
            'max_tokens=min(getattr(model, "max_output_tokens", None) or 4096, 4096)',
        ),
        "services/heartbeat_t3_core.py": ("min(int(configured), default)",),
        "services/auto_dream.py": (
            "max_tokens=8_192",
            "max_tokens=20_000",
            "max_tokens=400",
            "max_tokens=3000",
        ),
        "memory/t2/segment_package.py": ("max_tokens=8192",),
        "services/session_memory.py": ("max_tokens=4096",),
        "services/personal_knowledge_extractor.py": ("max_tokens=4096",),
        "services/fast_reflection_learning_brain.py": ("max_tokens=_LEARNING_BRAIN_MAX_TOKENS",),
        "services/skill_curator.py": ('getattr(model, "max_output_tokens", None) or 8192',),
        "services/provisional_trial.py": ('getattr(model, "max_output_tokens", None) or 8192',),
        "agents/subagent_memory.py": ("max_tokens=8192",),
        "services/subagent_generator.py": ("max_tokens=8192",),
        "tools/handlers/hr.py": ("max_tokens=8192",),
        "memory/write_gate.py": ("max_tokens=1000",),
    }

    violations: list[str] = []
    for relative_path, forbidden_snippets in forbidden_by_file.items():
        source = (APP_ROOT / relative_path).read_text(encoding="utf-8")
        for snippet in forbidden_snippets:
            if snippet in source:
                violations.append(f"{relative_path}: {snippet}")

    assert violations == []


def test_skill_distiller_does_not_hide_workflow_steps_in_model_visible_name_hint() -> None:
    distiller = (APP_ROOT / "services/skill_distiller.py").read_text(encoding="utf-8")

    assert "for part in parts[:3]" not in distiller
    assert "return title[:120]" not in distiller


def test_t3_consolidation_and_dream_do_not_batch_away_pending_evidence() -> None:
    consolidation = (APP_ROOT / "memory/t3_consolidation.py").read_text(encoding="utf-8")
    dream = (APP_ROOT / "services/memory_dream.py").read_text(encoding="utf-8")

    assert "max_packages: int = 8" not in consolidation
    assert "max_explicit_entries: int = 8" not in consolidation
    assert "][:max_explicit_entries]" not in consolidation
    assert "if len(package_dirs) >= limit" not in consolidation
    assert "max_packages: int = 8" not in dream
    assert "if len(package_dirs) >= max_packages" not in dream
    assert "package_dirs[:max_packages]" not in dream

    auto_dream = (APP_ROOT / "services/auto_dream.py").read_text(encoding="utf-8")
    assert "limit_sessions=20" not in auto_dream


def test_t3_source_bundle_contains_model_readable_t2_and_t0_evidence() -> None:
    consolidation = (APP_ROOT / "memory/t3_consolidation.py").read_text(encoding="utf-8")

    for required in (
        '"summary_md"',
        '"labels_md"',
        '"synthesis_md"',
        '"review_md"',
        '"t0_evidence"',
        '"source_md"',
        '"events_jsonl"',
        '"available"',
    ):
        assert required in consolidation


def test_personal_knowledge_graph_never_silently_truncates_model_semantics() -> None:
    extractor = (APP_ROOT / "services/personal_knowledge_extractor.py").read_text(encoding="utf-8")
    index_search = (APP_ROOT / "services/personal_knowledge_index_search.py").read_text(encoding="utf-8")

    assert "return clean[:max_len]" not in extractor
    assert 'str(value or "").strip())[:max_len]' not in index_search


def test_explicit_recall_and_document_tools_have_no_hidden_default_caps() -> None:
    memory_handler = (APP_ROOT / "tools/handlers/memory.py").read_text(encoding="utf-8")
    knowledge_handler = (APP_ROOT / "tools/handlers/knowledge.py").read_text(encoding="utf-8")
    filesystem_handler = (APP_ROOT / "tools/handlers/filesystem.py").read_text(encoding="utf-8")
    session_recall = (APP_ROOT / "services/session_recall.py").read_text(encoding="utf-8")
    taxonomy = (APP_ROOT / "services/governance_capability_taxonomy.py").read_text(encoding="utf-8")

    assert 'arguments.get("limit", 10)' not in memory_handler
    assert 'min(int(arguments.get("limit"' not in memory_handler
    assert 'request.arguments.get("limit") or 5' not in knowledge_handler
    assert 'request.arguments.get("max_chars") or 8_000' not in knowledge_handler
    assert "min(20_000" not in knowledge_handler
    assert 'arguments.get("max_chars", 8000)' not in filesystem_handler
    assert 'return_format=str(arguments.get("return_format") or "preview")' not in filesystem_handler
    assert "\n    limit: int = 3," not in session_recall
    assert "_ADMIN_PACK_QUERY_KEYWORDS" not in taxonomy


def test_model_callable_domains_do_not_hide_evidence_or_actions_behind_implicit_caps() -> None:
    plaza = (APP_ROOT / "services/agent_tool_domains/plaza.py").read_text(encoding="utf-8")
    email_domain = (APP_ROOT / "services/agent_tool_domains/email.py").read_text(encoding="utf-8")
    email_service = (APP_ROOT / "services/email_service.py").read_text(encoding="utf-8")
    calendar = (APP_ROOT / "services/agent_tool_domains/feishu_calendar.py").read_text(encoding="utf-8")
    wiki = (APP_ROOT / "services/agent_tool_domains/feishu_wiki.py").read_text(encoding="utf-8")
    base = (APP_ROOT / "services/agent_tool_domains/feishu_base.py").read_text(encoding="utf-8")
    memory_handler = (APP_ROOT / "tools/handlers/memory.py").read_text(encoding="utf-8")
    skill_description_path = APP_ROOT / "services/skill_creator_files/scripts__improve_description.py"
    plugin_adapter = (APP_ROOT / "services/external_capabilities/cc_plugin_adapter.py").read_text(encoding="utf-8")

    assert 'arguments.get("limit", 10)' not in plaza
    assert 'arguments.get("limit", 10)' not in email_domain
    assert "limit: int = 10" not in email_service
    assert "limit = min(limit, 30)" not in email_service
    assert 'arguments.get("max_results", 20)' not in calendar
    assert "attendee_emails[:20]" not in calendar
    assert "depth < 2" not in wiki
    assert "range(20)" not in wiki
    assert 'arguments.get("max_records", 1000)' not in base
    assert "range(100)" not in base
    assert "content[:80]" not in memory_handler
    assert not skill_description_path.exists()
    assert "stripped[:1024]" not in plugin_adapter


def test_resume_recall_ledger_and_skill_authoring_keep_complete_model_evidence() -> None:
    ledger = (APP_ROOT / "services/agent_work_ledger.py").read_text(encoding="utf-8")
    memory_handler = (APP_ROOT / "tools/handlers/memory.py").read_text(encoding="utf-8")
    subagent_resume = (APP_ROOT / "services/subagent_run_service.py").read_text(encoding="utf-8")
    memory_backend = (APP_ROOT / "memory/backend.py").read_text(encoding="utf-8")
    skill_creator = (APP_ROOT / "services/skill_creator_content.py").read_text(encoding="utf-8")

    for forbidden in (
        'ledger.get("progress", [])[-20:]',
        'ledger.get("failures", [])[-10:]',
        'ledger.get("replans", [])[-10:]',
        'ledger.get("findings", [])[-10:]',
    ):
        assert forbidden not in ledger
    assert 'results.append("  Complete transcript:")' in memory_handler
    assert "if selected and used + cost > budget_chars" not in subagent_resume
    assert "limit: int = 10" not in memory_backend
    assert "claude-with-access-to-the-skill" not in skill_creator
    assert '"scripts__improve_description.py": "scripts/improve_description.py"' not in skill_creator


def test_forked_agents_receive_complete_context_and_nested_delegation_contract_is_truthful() -> None:
    handler = (APP_ROOT / "tools/handlers/subagent.py").read_text(encoding="utf-8")
    service = (APP_ROOT / "services/subagent_run_service.py").read_text(encoding="utf-8")
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    for source in (handler, service):
        assert "limit: int = 120" not in source
        assert ".limit(max(1, limit))" not in source
    assert "Subagents cannot delegate or spawn further" not in handler
    assert "core_tools_only=True` to prevent nested delegation loops" not in claude_md


def test_semantic_decisions_are_not_overridden_by_numeric_or_fuzzy_platform_cutoffs() -> None:
    distiller = (APP_ROOT / "services/skill_distiller.py").read_text(encoding="utf-8")
    explicit_overlay = (APP_ROOT / "memory/explicit_overlay.py").read_text(encoding="utf-8")

    assert "Approval requires all scores >= 3" not in distiller
    assert "return all(int(review.scores.get(key) or 0) >= 3" not in distiller
    assert "MEMORY_DEDUP_THRESHOLD" not in explicit_overlay
    assert "jaccard_similarity(content, entry.content)" not in explicit_overlay
    assert "looks_episodic_observation" not in explicit_overlay

    memory_handler = (APP_ROOT / "tools/handlers/memory.py").read_text(encoding="utf-8")
    assert "looks_episodic_observation" not in memory_handler
    assert "Similar explicit memory already exists" not in memory_handler


def test_feishu_base_discovery_defaults_to_complete_recoverable_enumeration() -> None:
    base = (APP_ROOT / "services/agent_tool_domains/feishu_base.py").read_text(encoding="utf-8")
    definitions = (APP_ROOT / "tools/handlers/feishu.py").read_text(encoding="utf-8")

    assert 'arguments.get("limit", 50)' not in base
    assert (
        'arguments.get("limit", 100)'
        not in base.split("async def _feishu_base_field_list", 1)[1].split("async def _feishu_base_field_create", 1)[0]
    )
    assert "Omit to return every table" in definitions
    assert "Omit to return every field" in definitions

    drive = (APP_ROOT / "services/agent_tool_domains/feishu_drive.py").read_text(encoding="utf-8")
    assert '"limit": arguments.get("limit", 100)' not in drive
    assert 'record_args["fetch_all"] = True' in drive


def test_client_and_model_action_surfaces_do_not_echo_server_owned_plan_hashes() -> None:
    task_adapter = (FRONTEND_ROOT / "api/domains/tasks.ts").read_text(encoding="utf-8")
    workflow_adapter = (FRONTEND_ROOT / "api/domains/workflows.ts").read_text(encoding="utf-8")
    business_tasks = (FRONTEND_ROOT / "pages/agent-detail/AgentBusinessTasksSection.tsx").read_text(encoding="utf-8")
    command_registry = (APP_ROOT / "services/command_registry.py").read_text(encoding="utf-8")
    plan_gate_helper = (APP_ROOT / "api/_plan_gate.py").read_text(encoding="utf-8")
    task_tool = (APP_ROOT / "services/agent_tool_domains/tasks.py").read_text(encoding="utf-8")

    assert "confirmed_plan_hash?:" not in task_adapter
    assert "planHash?:" not in workflow_adapter
    assert "confirmed_plan_hash:" not in business_tasks
    assert "plan_hash: plan.plan_hash" not in business_tasks
    assert '"confirmed_plan_hash": {"type": "string"}' not in command_registry
    assert 'canonical_plan_hash", None) or confirmed_plan_hash' not in plan_gate_helper
    assert 'plan_hash=args.get("confirmed_plan_hash")' not in task_tool
