# Hive Agent 框架 — CC 基线 + SOTA 原子级审计（2026-06-15）

> 方法论：17 个原子维度（3 个 CC 基线 + 13 个 SOTA + 1 个铁律闸门），每个维度先由调查者做 LIVE 入口点追踪取证，再由 2–3 个对抗 verifier（Wiring/Liveness · Substance/Depth · 专项 Adversary）独立复核并尝试证伪；最后由完整性 critic 盘点遗漏维度。本报告的每条 ≥95% 结论都满足两个硬条件：(a) 存在生产 LIVE 入口点追踪；(b) 无 verifier 以 material/critical 严重度证伪。否则一律降置信并写明取证缺口。原始审计阶段独立复核了承重证伪点（RLS owner-bypass、load_skill 截断、desktop 硬删、behavior_report 永久 hold、workflow_completed 零消费、Anthropic 单块 unwrap、eval baseline provisional）；其中已修复/过期项在下方 follow-up 中改写。

> **2026-06-15 Codex follow-up（当前工作树 delta）**：本审计原稿之后，P0/P1 blocker 已按代码路径补齐：① `load_skill` 显式路径不再经普通 `read_file` 的 16K cap 截断；② Anthropic 单 text block 带 `cache_control` 时不再 unwrap 成裸字符串；③ nightly behavior eval 缺 secrets 时写 `behavior_eval_evidence.v1` 并非零退出；④ prompt-facing `team_context` / `teammate_mailbox` 与 Work Ledger 派生 `Progress Ledger` 已落地；⑤ LLM-facing `save_skill` 不再创建 active skill，而是写 `evolution/skill_candidates/<candidate_id>/` inactive Skill Candidate Package；旧 `skill_activation_candidates.md` 与 `_save_skill()` direct activation path 已退役；⑥ `RuntimeConfig.skill_distiller_behavior_report` 接最新可信 live behavior report，distiller 缺报告即 hold；⑦ fresh bootstrap + migration 对所有 `RLS_TENANT_TABLES` FORCE RLS；⑧ subagent/delegation 只对 replay-safe profile/type 开 restart resume，unsafe resume fail-closed；⑨ desktop sub-agent 删除改 `soft_delete_agent()`；⑩ memory/skill counter 已进入 read-side heat/retention；⑪ `decision/<id>` linkback 已贯通 chat message/session feedback；⑫ `Progress Ledger` 的 `needs_replan` 进入 runtime reminder；⑬ connector ACL mirror 同时覆盖 knowledge injection 与 retriever external layer；⑭ code-execution providers 现在有 env allowlist + sandbox evidence。**这些修复不改变铁律结论**：live behavior eval 仍未跑出真实分数，不能 claim 超越 hermes/SOTA。

---

## 1. 总判（≥95% 置信）

Hive 的 agent 框架在 **CC 晴天基线**上是**真实对齐且局部超越**的：单一 `invoke_agent()` → `AgentKernel.handle()` 内核循环 + `ToolRuntimeService.execute()` 治理咽喉是真的、不可绕过的（8 个生产 caller + 3 个自定义 executor 全部 wrap-then-call，无 live 旁路），并叠加了 CC 没有的 `LoopGuard` 三通道语义循环检测；压缩在主路径上把**完整历史**喂给主模型（20K 输出预算，非旧的 `[-40:]`+2500-cap 机械违例）；skill/subagent/plan-mode 渐进式披露与 replace 语义结构性镜像了 CC。P0/P1 follow-up 已把原审计的几个安全尖点补到代码路径：`save_skill` 从自授权 active write 改为候选 lane，behavior eval report 接入 `RuntimeConfig` 与 distiller promotion gate，RLS FORCE 覆盖全部 tenant-scoped runtime 表，desktop 删除改软删，decision linkback 与 Progress Ledger runtime reminder 接通，code execution provider 级 env allowlist/evidence 已落地。但在 **SOTA 自进化/持久执行/隔离层**，Hive 仍不能 claim 已超越：behavior eval 仍没有一份真实 live `invoke_agent` 分数覆盖 provisional baseline；restart resume 只对 replay-safe subagent/delegation 开启，mutating delegation 仍 fail-closed；Vercel microVM 仍缺生产 round-trip 证据；connector ACL 还不是 Glean 全 source ACL ingest。**决定性地：外部行为 eval CI 从未跑过 live `invoke_agent`**（baseline `commit_sha="pending-e2-live-run"` / `provisional:true` / 六场景 `score_p50=0.0`，`git log` 仅一个 seed commit），因此按 §9 铁律，任何「超越 hermes」claim 当前都是**未验证的 Speculation，被禁止**。净判：**CC 基线对齐、P0/P1 安全 blocker 已关、SOTA 机制骨架更诚实；但 self-evolution / durable / isolation 的“生产成功运行”仍必须等 live 行为分数、真实重启/生产 sandbox trace 与 full connector ACL 证据。**

---

## 2. CC 基线对齐（3 个 CC-foundation cell）

### 2.1 内核循环 + 工具注册 + 治理咽喉 — **aligned（≥95%）**

单一入口 `invoke_agent()`（`runtime/invoker.py:1019,1118`）→ `AgentKernel.handle()`（`kernel/engine.py:2015`）→ `for round_i in range(max_rounds)`（`engine.py:2533`，`max_rounds = request.max_tool_rounds or runtime_config.max_tool_rounds`，常规 agent loop 默认 200；heartbeat 当前已退为 direct T3 core，不进入完整 tool loop）→ 每次工具调用经 `deps.execute_tool` → `agent_tools.py:846 execute_tool` → `tools/service.py:277 ToolRuntimeService.execute()`：plan-mode gate（:294）→ governance（:317，security zone + capability gate）→ preflight（:327）**先于** `execute_with_context`（:334）。三个 verifier 独立穷举 LIVE kernel/tool-loop 入口 + 所有 `tool_executor=` 传入方，全部 wrap-then-call 治理路径；唯一旁路 `hive_live_runner.py` 仅被 eval CI 引用、零生产入口。内核零 DB：`engine.py` 及 `kernel/*.py` 无 `app.models`/`app.core.database`/`sqlalchemy` import（grep clean，仅 direct-import 级，未穷尽传递闭包）。`LoopGuard` 三通道 live（`engine.py:2247`，observe wired 3098/3235/3354/3493），round-pressure 在 `int(max_rounds*0.8)` 和 `max_rounds-2`（`reminder_scheduler.py:291-292`）。**诚实差距（不证伪本维度，只把「CC-equivalent」上界钉死）**：循环内无 provider 重试矩阵（HTTP 层 overload 可一击杀回合）、无工具级执行中可中断（cancel 仅在回合间 `engine.py:2534`）、30s 默认工具超时（`service.py:331`）、`governance_resolver` 在 hot path 跑在 `enter_rls_bypass` 内。

### 2.2 上下文压缩 + token 预算（AI-Native L1）— **aligned（≥90%，无 material 证伪）**

主路径是真 LLM 压缩、喂完整历史、主模型、20K 输出：`memory_service.py:443-481`（LLM 为 primary，regex `_extract_summary` 仅 OBSERVABLE fallback，带 `compaction_llm_fallback` metric + 熔断），summary 模型 = 租户 summary → 主对话模型 → default（`memory_service.py:749-779`，CC `mainLoopModel` 哲学）；`conversation_summarizer.py:483-484` 输出 `_SUMMARY_MAX_OUTPUT_TOKENS=20000`（逐字移植 CC `COMPACT_MAX_OUTPUT_TOKENS=20_000`），输入 = 完整历史 + 超预算才 oldest-first head-drop（带 `summary_input_head_drop` 日志），`[-10:]` 切片只在机械 fallback `_extract_summary` 内、不在 LLM 路径。CJK 近-never-fire 缺口已关：真 `response.usage` 写入 `usage_anchor_tokens`（`engine.py:3083-3096`），`current_tokens=max(estimate, anchor)`（`memory_service.py:422`）。Proactive（75%/每 3 轮，`engine.py:3744`）+ reactive PTL（LLM-first，`engine.py:2724`）均 LIVE-wired，无 default-off flag。**诚实差距（minor，Substance verifier 标 minor）**：summary 输入对**单条消息**设了 8K user/assistant、12K tool-result 上限（`conversation_summarizer.py:505,511,513`），而工具结果驱逐阈值是 50K（`engine.py:92`）——12K–50K 的工具结果对 live 模型内联可见，但被压缩时丢内容，是一个**常规（非纯防御）**的 summary-input 保真缺口；CC 不设此 cap。

### 2.3 Skill 渐进式披露 + subagent + plan mode + tool exposure — **aligned（≥90%，load_skill 截断已修复）**

结构对齐真实且 live：catalog 进 per-round dynamic suffix（Step-9，`prompt_builder.py:629-633`），`_split_system_prompt_for_api` 在 `PROMPT_CACHE_BOUNDARY` 切分、dynamic suffix 变成 per-round 临时 `[System Notice]` user 消息（`engine.py:262-281,2615-2617`），不污染 cached system prefix；subagent replace 语义忠实匹配 CC `runAgent.ts:906`（`invoker.py:338-340` standalone 即整个 frozen prefix，:398-399 host memory 强制空），builtin worker/critic/explorer prompt 是真实质料文本（`subagent.py:160-300`，反驳旧空-prompt L1 违例）；`request_plan_mode`/`exit_plan_mode` 是注册 @tool（`plan_mode.py:470`），内核暂停等用户（`engine.py:1464-1480`，CC EnterPlanMode parity）。原审计发现的 `load_skill` 显式路径 16K 截断已修复：`_read_skill_file()` 现在直接读取完整 skill instruction body，并继续做 managed credential guidance sanitization；普通 `read_file` 的 16K 输出保护不变。回归测试钉住 `skills/long-skill/SKILL.md` 末尾 sentinel 可见且无 `[truncated]`。

---

## 3. SOTA 13 维逐维表

| 维度 | SOTA 那条线 | Hive 核实状态 | 置信 | 关键证据(file:line) | 诚实差距 |
|---|---|---|---|---|---|
| **D1** 学习脑（M2 快反思）| 「学什么」永远全模型判断，无轻量分类器 | **verified_live（机制）/ overstated（fork-complete-agent）** | **88%** | `fast_reflection_learning_brain.py:88-132,181-235`（全 message 上下文，单 `client.complete` max_tokens=1600）；`memory_service.py:749-779`（summary 模型层，非轻量分类器）；旧 last-8 分类器已删（grep 空）；`main.py:412` register_memory_hooks live | 「fork 完整 agent」是夸大——实为单发 1600-token 判断，非 `invoke_agent` fork 带工具/记忆/多轮；1600 < 自己声明的 8192 辅助底线（`extract_agent.py:617`）；live `client.complete()` 从未被测试实际执行（全 mock）|
| **D2** Skill 习得 + patch-first（M3）| patch 已加载 skill 先于新 skill 晋升 + Devin 成败对比 | **wired_unproven** | **62%** | patch-first 真实：`skill_distiller.py:992-1012`；Devin `<patch_first_policy>`：:685-693；`loaded_skill_names` 来自真 `load_skill` args：:469-488；全 patch 链 + skill_guard gate：:1097-1267；flag 默认 True（`invoker.py:91`）；daemon live（`heartbeat.py:1833`）| **`record_skill_execution` 从不在 tools/kernel/runtime 调用（独立 grep 确认空）**——成败是事后从自报 `[OUTCOME:failure]` 重导出；证据源仅 {heartbeat,trigger,task}，排除主 web-chat；`workaround` 状态从 `_normalize_session_status` 不可达；零生产 ledger 显示 `patched`；输入机械截断 `evidence[:3]`/`successful[:4]` |
| **D3** 验证无回归硬门（M1 skill_guard）**[最高风险]** | skill/policy 晋升经外部、硬、架构隔离的能力级验证（含 LIVE 行为 eval），非 LLM 自评 | **partial+（硬门 wiring 真 / LIVE 行为 report 未运营）** | **55%** | 静态臂真且外部：`evolution_verification.py` 隔离 load smoke + tool/pack dry-run + resource check；`RuntimeConfig.skill_distiller_behavior_report` 已接入最新可信 behavior report（`runtime/invoker.py`），distiller 读取 `_runtime_behavior_report()` 并把 `behavior_report` 交给 `decide_behavior_gated_promotion()`；LLM-facing `save_skill` 现在只写 `evolution/skill_candidates/<candidate_id>/` inactive package，`active_skill_created: false`，不再自授权 active write | live behavior eval 仍未产生一份可信报告，因此生产 distiller 仍会 hold；`record_skill_execution` 仍未覆盖 web-chat 主路径，skill success/failure 的有机 telemetry 还不完整。结论从“洞”升级为“门已接线但缺 live 运营证据” |
| **D4** 长期记忆（M4 ACE delta + 计数器）| delta 合并 + grow-and-refine 计数器 + 确定性去重 | **partial+（写路径达标 / read-side heat 已接 / retention 未全量）** | **45%** | 写真：`t3_store.py` counter seed + 重复 reinforce 不加 prose；去重确定性 Jaccard；`memory/activation.py` 用 `access_count/last_accessed` 计算 `usage_heat`，`memory/retriever.py` join sidecar telemetry；`skill_curator.py` 用 `use_count/view_count` 给 stale/archive grace | dream cap/eviction 仍未完整使用 `reinforcement_count/harmful_count` 做保留与驱逐，M4 只能从“读侧为假”升到“关键 read-side heat 已接、retention 仍 partial” |
| **D5** 记忆企业治理（O2 会话反馈）| 文档级 ACL + 查询前预过滤 + 生成后复检；反馈 link 回 decision/<id> | **verified_live（治理）/ partial+（decision linkback wired）** | **75%** | feedback API → `session_feedback.py` → write gate → T3 lifecycle 仍 live；`DecisionTrace` now carries tenant/agent/user/session/message/tool/checkpoint join keys；`chat_messages.decision_trace_id` 与 `session_feedback_events.decision_trace_id` 已有 migration；`web_chat_runtime` 从 tool result 抽 `decision/<id>`；feedback 校验 decision 属于同 tenant/agent/session 并把 `decision/<id>` 写进 source refs | linkback 机制已不再结构性不可能，但 DecisionTrace 仍是 JSONL store 而非 PG RLS trace table；缺一条真实 UI feedback → decision trace → T3 source ref 的 production trace |
| **D6** 持久执行（K1 重试 + W1 去重 + S1 重启恢复）| 去重边界跨 workflow ∧ subagent ∧ delegation，resume 读不重执 | **verified_live（workflow + K1）/ partial-governed（safe subagent/delegation resume）** | **60%** | K1 与 W1 workflow 仍 verified；`orchestrator.py` 仅对 `review_readonly` / `research_readonly` replay-safe tool profile 写 `resumable_delegation + resume_after_restart`；`subagent_run_service.py` 仅对 `explorer` / `critic` 写 `resumable_subagent + resume_after_restart`；orphan reconcile 保留这些 safe records，unsafe 仍 fail-closed | mutating delegation/subagent 仍不是 Temporal 式 completion-journal replay；这是有意治理边界，不是全量 S1。要到 95% 还需要 per-step side-effect journal 或把 claim 收窄为 read-only/replay-safe lane |
| **D7** 多 agent 编排（重规划 + signal）| 并行收集 + 串行决策 + 进度账本重规划 + signal 被消费 | **partial++（prompt state + runtime replan reminder wired）** | **62%** | delegate wrap invoke_agent + core_tools_only + cycle guard；Lease+Signal live；`agent_team_context.py` 渲染 team context/mailbox；`agent_work_ledger.py` 派生 `agent_progress_ledger.v1`；`kernel/reminder_scheduler.py` 在 `needs_replan=true` 时立即注入 Progress Ledger runtime policy，要求 `record_finding(type='replan')` 与更新 todo/owner | 仍不是 Magentic-One 完整外循环：replan 由模型响应 reminder 执行，不是 orchestration hard transition；无 live multi-agent behavior eval 证明复杂任务收益 |
| **D8** 上下文/cache 经济（C2 CJK + cache anchor）| 稳定 prefix 字节 + 真用量 cache anchor + CJK token 核算 | **verified_live（CJK + frozen prefix + assistant anchor formatter fixed）** | **88%** | CJK 估算 live（`token_tracker.py:14,22-34`，21 调用点）；frozen prefix 字节稳定（`engine.py:262-267`，cache key 排除 user-name/window/memory `engine.py:1088`）；apply_cache_hints 在 LLM 调用前（`engine.py:2623-2628`）；`llm_client.py` 仅在纯 `{"type":"text","text":...}` block 时 unwrap，带 `cache_control` 的单 text block 保留 list 结构 | 原审计的 Anthropic 单块 unwrap 丢 `cache_control` 已修复并有回归覆盖。剩余诚实边界：CJK 仍是启发式估算而非真 tokenizer；尚未抓取真实 Anthropic provider payload trace 作为 live artifact |
| **D9** 执行隔离 & 安全（G1 + Vercel sandbox）| OS sandbox/microVM + 凭据 egress 注入 | **partial+（provider env/evidence 真 / microVM 生产未证）** | **70%** | `code_execution/env_policy.py` 是 provider 级 allowlist，local/vercel provider 即使收到 unsafe caller env 也会二次净化；`CodeExecutionResult.evidence` 返回 provider/isolation/network_policy/env_policy；Vercel provider 不把 backend provider credentials 暴露给 sandbox；artifact gate 返回 `sandbox_evidence`；Vercel missing credentials fail-closed，默认 network `deny-all` | microVM 执行仍仅对 `_FakeVercelSandbox` 证明，无 in-repo production uname/Firecracker/network-denied trace；`.env.example` 默认仍是 `local_os_sandbox`，Railway 启用 vercel_sandbox 依赖 operator config |
| **D10** Agent 身份/控制面（ID1 sponsor 生命周期）| 一等非人身份 + sponsor + 软删级联 → Entra 三层 | **verified_live（身份 + desktop soft delete）** | **82%** | 身份真 live：Participant unique type/ref_id、sponsor NOT NULL FK + before_flush 兜底、运行时 lifecycle 门；desktop sub-agent delete 现在调用 `soft_delete_agent(db, agent, actor_id, reason='desktop_delete_sub_agent')`，测试钉死不再 `db.delete(agent)`，历史/身份保留 | 仍缺跨所有 agent deletion entrypoint 的统一 soft-delete conformance sweep；但原 `desktop_agents.py` 硬删 blocker 已关闭 |
| **D11** 权限感知数据（P1 principal 预过滤）| memory ∧ knowledge 按 principal 预过滤（Glean 线）+ 生成后复检 | **partial+（memory 真 / connector ACL mirror 双入口）** | **62%** | memory 预过滤真 live；`knowledge_inject.py` 与 `memory/retriever.py` external layer 都调用 `filter_connector_results_for_prompt()`，OpenViking result 携 `acl/access/permissions/visibility` 时 tenant/principal 不匹配即 fail-closed；`viking_client.add_resource()` 可写 ACL/metadata/identity headers | 仍非 Glean 全量：无 ACL metadata 的 legacy result 兼容通过；Feishu/Drive/Office 等 read model 未完成 source ACL ingest；无生成后复检 |
| **D12** 可观测/审计（O1 invocation_spans）| 默认开全链 trace-tree + 租户 RLS + Prometheus；DecisionTrace 持久 | **verified_live（spans）/ partial+（DecisionTrace linkback wired）** | **92%** | spans 真 live：唯一生产 wiring + 三 span fire + 10 join keys + FORCE RLS + reader + metrics；DecisionTrace 现在有 tenant/agent/user/session/message/tool/checkpoint join keys，feedback 与 chat message 可挂 `decision_trace_id`，session_feedback 会写 `decision/<id>` source ref | DecisionTrace 仍是 JSONL store，非 PG RLS append-only trace table；span 写 fail-soft 仅 WARNING，无聚合告警 |
| **D13** 互操作（MCP authz + A2A + K2 签名）| token-passthrough 禁 + 不支持面诚实 not_exposed + 签名 round-trip 无伪造 | **verified_live（≥90%）** | **90%** | MCP authz 双咽喉 fail-closed：`mcp_authz.py:38-71`（剥 userinfo + token keys），`mcp_client.py:28`（无条件 sanitize），`web_mcp.py:1067-1081`（init 前 assert）；A2A card + profile 诚实 not_exposed（`interoperability.py:53-63,116-133`，RFC9728/8707/PKCE 自声明缺）；thinking block 仅 reasoning_content ∧ signature 才发（`llm_client.py:142-151`，无 synthetic 伪造），beta header live（:1848-1854），签名 round-trip（`engine.py:1150,3270`）| 无 RFC 8707 audience / RFC 9728 discovery / PKCE（profile 自声明）；「K2 thinking signature」是误名（K2 无 Anthropic beta header，走通用 `reasoning_signature`）；无 live runtime round-trip 取证 |

---

## 4. 声明核实 — 18 个 §12 milestone 在对抗审视下是否成立

| Milestone | claim | 裁定 | 证伪/限定（若有）|
|---|---|---|---|
| **M1** 验证无回归硬门 | skill_guard 复合硬门 + LIVE 行为 eval 硬门 | **partial+（硬门接线完成 / live report 未运营）** | 静态臂真；`RuntimeConfig.skill_distiller_behavior_report` + distiller behavior gate 已接；LLM-facing `save_skill` 改为候选 lane，不再自授权 active skill。剩余：没有真实 live behavior report 覆盖 provisional baseline，故 promotion 仍运营性 hold |
| **M2** 学习脑快反思 | 全模型「学什么」，无轻量分类器；fork 完整 agent | **verified_live（机制）/ overstated（fork）** | 全模型判断 + 无轻量分类器**成立**且 live-wired；「fork 完整 agent」夸大——单发 1600-token，非 invoke_agent fork；scorecard 措辞 looser than 实现（Substance verifier refuted minor）|
| **M3** Skill 习得 patch-first | patch 先于新 skill + Devin 对比 | **wired_unproven** | 机制全实装 + live daemon；但 `record_skill_execution` 从不在 live 路径调用、证据源排除 web-chat、零生产 `patched` ledger（从未声称 demonstrated outcome，故 survive 为 wired_unproven）|
| **M4** 记忆 ACE delta + 计数器 | delta + 计数器 + 确定性去重；counter-driven 保留 | **partial+（写真 / heat 真 / retention partial）** | 写路径 + 去重成立且 live；`access_count/last_accessed` 已进入 activation `usage_heat`，skill `use_count/view_count` 已进入 stale/archive grace。剩余：dream cap/eviction 未完整消费 reinforcement/harmful counters |
| **O2** 会话反馈治理 | append-only PG + 治理写 + link decision/<id> | **partial+（治理真 / linkback wired）** | 治理写 + PL4 + lifecycle + append-only RLS 成立；`decision_trace_id` 已贯通 chat_messages/session_feedback，feedback 校验同 tenant/agent/session 并写 `decision/<id>` source ref。剩余：DecisionTrace store 仍非 PG RLS |
| **K1** LLM 重试 + 529 fallback | 10 次重试 + 529→fallback / 429→user | **verified_live** | CC-aligned，全 provider complete()/stream() wired（`llm_client.py:449-486`，`engine.py:2972-3013`），唯一未证 = 无 live 529 实跑 trace |
| **K2** thinking 签名保真 | beta header + 签名 round-trip 无伪造 | **verified_live** | 无 synthetic 伪造（grep 确认唯一出现是注释），双 POST header，round-trip 写/读全闭合；「K2」是误名（走通用字段，非 K2-specific）|
| **W1** workflow 完成去重 | run-level 幂等边界，replay 不重放副作用 | **verified_live（workflow）** | FOR-UPDATE + idempotency_key（`workflow_runtime_service.py:1006-1038`）真；仅 workflow lane 成立（delegation/subagent 见 S1）|
| **S1** 重启可恢复不被 orphan-swept | workflow/web_chat/delegation 可恢复，resume 读不重执 | **partial-governed** | workflow/web_chat 真；replay-safe delegation/subagent 会被 orphan reconcile 保留并由 startup resume，unsafe mutating lane fail-closed。不是全量 S1，claim 必须收窄到 replay-safe profile/type |
| **G1** 隔离闭环 + Vercel sandbox | env 不透传 userinfo + microVM 接 live code-exec | **partial+（凭据 egress + evidence 真 / microVM 生产未证）** | provider 级 env allowlist + URL userinfo strip + `CodeExecutionResult.evidence` + artifact `sandbox_evidence` 已落地；Vercel fail-close 与 deny-all 默认仍真。剩余：真实生产 microVM trace 未入库 |
| **ID1** sponsor 生命周期 | per-agent sponsor + Participant + 软删级联 | **verified_live（desktop soft-delete blocker closed）** | 身份 + 运行时门 + 遗留 backfill 成立；desktop sub-agent 删除已改 `soft_delete_agent()` 并有测试覆盖。剩余：仍需跨所有 deletion entrypoint 做统一 conformance sweep |
| **P1** principal 预过滤 | memory ∧ knowledge 按 principal 预过滤（Glean 线）| **partial+（memory 真 / connector ACL mirror 双入口）** | memory tier 预过滤真 live 且 fail-closed；OpenViking prompt injection path + retriever external layer 都有 item-level ACL mirror。剩余：Feishu/Drive/Office 全 read model ACL ingest、生成后复检仍未达 Glean 线 |
| **O1** invocation_spans | 租户 PG span trace-tree + reader + Prometheus；DecisionTrace 持久非内存 | **verified_live（span）/ partial+（decision linkback）** | span 层端到端真 live，10 join keys + FORCE RLS + reader + metrics 全核实；DecisionTrace linkback 已可落到 chat/session_feedback join key。剩余：DecisionTrace 仍是 JSONL，不是 PG RLS trace table |
| **C2** CJK + cache anchor | CJK token + canonical assistant anchor + frozen prefix 稳定 | **verified_live（formatter bug fixed）** | CJK + frozen prefix 稳定**成立**且 live；原 Anthropic 单 text block unwrap 丢 `cache_control` 已修复并有单块无签名回归 |
| **cache-anchor** | 单确定性 cache 写点 + provider 正确 passthrough | **verified_by_unit_path / live-provider trace pending** | frozen-prefix system block 正确；assistant-turn anchor 对 Anthropic/Qwen/MiniMax formatter 路径已保留 `cache_control`，剩余只是缺真实 provider payload trace |
| **MCP-authz** | URL userinfo + token query passthrough 硬门 | **verified_live** | 双咽喉 fail-closed 真 live；诚实 not_exposed RFC9728/8707/PKCE（非伪造，自声明缺）|
| **A2A** | per-agent card + profile 诚实 not_exposed | **verified_live** | card + profile 真 live + access-guarded + 诚实标记 not_exposed，无 fake support |
| **D1（学习脑 = M2 重复条目）** | 见 M2 | 见 M2 | 见 M2 |

**汇总（当前工作树）：18 个 milestone 中，在 ≥95% 对抗门槛下仍未完整存活的 active gap = 5 个主类**：M1（live behavior report 未运营）、M4（counter retention 未全量）、S1（只覆盖 replay-safe lane）、G1（microVM 生产 trace 未证）、P1/D11（仍非 Glean 全 connector ACL）。O2 decision linkback、ID1 desktop soft-delete、RLS owner-bypass、save_skill 自授权、D7 runtime replan reminder 已从 active blocker 移除。C2/cache-anchor 已从 active blocker 中移除，但真实 provider payload trace 仍是更高置信证据缺口。完整 verified_live 的 = K1、K2、W1（workflow lane）、MCP-authz、A2A、O1（span 层）、D12（span 层）、D13。

---

## 5. 铁律闸门 — 外部行为 eval 状态

**裁定：MECHANISM-ONLY。当前不允许任何「超越」claim。**

硬证据（报告作者独立复核确认）：

1. `app/evals/baselines/core_behavior_v1.json` 携 `commit_sha="pending-e2-live-run"`、`provisional:true`、六场景全 `score_p50=0.0`、`transport:"pending"`；`git log` 仅一个 seed commit（`3dd14578`，E1 hardened baseline），从未被真跑覆盖。
2. **per-PR merge-blocking 门是静态的**：`.github/workflows/harness-ci.yml:58-59` `--mode internal --fail-under 90` → `run.py:590-592 _internal_scenario_report` → `task_eval.py:449-468 evaluate_task_readiness`（纯静态 tool-surface + prompt-substring 契约，score 100/0），**零 invoke_agent**——今天没有任何 live 东西阻断 merge。
3. **真 live 路径从未端到端执行**：`hive_live_runner.py:473-474`（真 `from app.runtime.invoker import invoke_agent`）→ 仅经 schedule/dispatch nightly 入口。当前 workflow 已从旧 `exit 0` skip 修成缺 secrets 时写 `behavior_eval_evidence.v1` 并非零退出；但仍没有一次带真实 secrets 的端到端 `invoke_agent` 行为报告覆盖 provisional baseline。`record_behavior_eval_run`（G1 ledger bridge）零生产 caller。
4. live wiring 是真代码（非 stub）且门是 fail-closed（require-live + `behavior_eval_passed` 拒 provisional baseline 作通过证据），但产出零真测量。

**后果（§9 铁律）**：从未测过 Hive-vs-hermes 行为 delta，故任何「超越 hermes / surpassed / SOTA 夺冠」claim **被禁止、为未验证 Speculation**。代码库正确遵守此律（`docs/external-behavior-eval-ci.md:460` 明文「已超越 仍是未验证 Speculation」，无文档作裸 surpass-claim）。**解锁路径**：配 Railway eval secrets + tenant/agent/model → 跑一次 live runner 经治理 rebaseline 门覆盖 provisional baseline → 跑 hermes live 交叉对比产生真 delta → 理想地把 live 子集提升进 per-PR 层使 live 回归阻断 merge。

---

## 6. ≥95% 置信结论清单（满足 LIVE 入口追踪 ∧ 无 material 证伪）

- **内核治理咽喉不可绕过**：8 生产 caller + 3 自定义 executor 全部 wrap-then-call `ToolRuntimeService.execute()`，无 live 旁路（`service.py:277-336`；唯一 `hive_live_runner` 旁路仅 eval-only）。
- **CC 内核循环对齐且局部超越**：单一 `invoke_agent` → for-round 循环 + 治理 + `LoopGuard` 三通道 + round-pressure，结构匹配 CC 并叠加 CC 没有的语义循环检测（`engine.py:2533,2247`；`reminder_scheduler.py:291`）。
- **压缩主路径满足 AI-Native L1**：完整历史 + 主模型 + 20K 输出 + 真 usage anchor，机械 fallback 仅 observable（`memory_service.py:443-481`；`conversation_summarizer.py:483-484`；`engine.py:3083-3096`）。
- **K1 LLM 重试 + 529 fallback CC-aligned 且 live**：10 次重试矩阵 wired 进全 provider，529 vs 429 split 正确（`llm_client.py:449-486`；`llm_error_policy.py:47-58`；`engine.py:2972-3013`）。
- **K2/thinking 签名无伪造**：仅 reasoning_content ∧ signature 才发 thinking block，无 synthetic 伪造（grep 确认），beta header + round-trip 闭合（`llm_client.py:142-151,1848`）。
- **W1 workflow 完成去重是真 Temporal 式**：run-level FOR-UPDATE + idempotency_key，per-step/leaf hash-gated replay「executor 不调用」（`workflow_runtime_service.py:1006-1038`；`workflow_engine.py:480-488`）。
- **MCP authz 双咽喉 fail-closed 禁 token passthrough**：URL userinfo + token query keys 在 client init 前被拒（`mcp_authz.py:38-71`；`mcp_client.py:28`；`web_mcp.py:1067`）。
- **A2A/interoperability 诚实标记 not_exposed**：card + profile live + access-guarded，不支持面自声明 not_exposed 无 fake support（`interoperability.py:53-63,116-133`）。
- **O1 invocation_spans span 层端到端 live**：唯一生产 wiring + 三 span fire + 10 join keys + FORCE RLS + reader + Prometheus（`invoker.py:890`；`engine.py:3875,2657,941`；`invocation_span.py:33-55`；`admin.py:295`；`main.py:627`）。
- **D10 agent 身份 + 运行时生命周期门 live**：sponsor NOT NULL FK + before_flush 兜底 + 工具前 abort + 遗留 backfill（`agent.py:30-31,178-198`；`permissions.py:33-40`；`engine.py:2060`；`agent_identity_lifecycle_0613.py`）。desktop hard-delete blocker 已关闭；跨所有 deletion entrypoint 的统一 soft-delete conformance 仍在 §7。
- **铁律闸门：当前禁止任何「超越 hermes」claim**——eval baseline provisional、per-PR 门静态、live invoke_agent 从未执行（`core_behavior_v1.json`；`harness-ci.yml:58-59,111-114`）。

---

## 7. 未达 95% / 待取证（结论 + 精确取证缺口）

| 结论 | 当前置信 | 为何不达 95% | 达到 95% 所需的精确证据 |
|---|---|---|---|
| 自进化自动晋升臂在生产工作（M1/M3/D3）| ~55% | behavior report 字段与 gate 已接、`save_skill` 自授权已关，但没有真实 trusted live report；`record_skill_execution` 仍未覆盖 web-chat 主路径 | 配置 secrets 跑 live behavior eval 覆盖 provisional baseline；把 skill execution telemetry 接进 live tool/kernel/runtime；一个真 agent workspace 的 ledger 显示从有机数据到 `decision='patched'` |
| M4 计数器驱动保留/驱逐（读侧）| ~45% | `usage_heat` 与 skill counter grace 已接，但 dream cap/eviction 未完整消费 reinforcement/harmful counters | dream cap/eviction 读 `reinforcement_count`/`harmful_count`，并有测试钉死「50× 强化条目不被 recency 驱逐」|
| S1 subagent/delegation 重启可恢复读不重执 | ~60% | replay-safe lane 已保留并可 resume；mutating delegation/subagent 仍不允许跨重启重放 | 对 mutating lane 加 per-step/leaf completion journal（匹配 workflow_engine），或把产品 claim 明确收窄为 read-only/replay-safe lane |
| D7 进度账本重规划 + workflow_completed 被消费 | ~62% | Progress Ledger reminder 已是 runtime policy，但仍靠模型响应 reminder，不是 orchestration hard transition；workflow_completed 进入 mailbox，但缺 live multi-agent eval | 跑 live multi-agent eval；如要 Magentic-One 强等价，再把 `needs_replan` 升为程序级 orchestration transition |
| O2 反馈 link decision/<id> | ~75% | linkback 已 wired，但 DecisionTrace 仍 JSONL，缺 PG RLS/tamper-proof trace table 与真实 UI feedback trace | 将 DecisionTrace 升为 tenant-scoped PG append-only table，补 UI feedback → decision_trace_id → T3 source refs 的 production trace |
| D8 Anthropic assistant cache anchor | ~88% | formatter bug 已修；剩余不是代码缺口，而是没有真实 Anthropic payload trace artifact | 抓一次真实 Anthropic request payload，证明单块无签名 assistant anchor 的 `cache_control` 送到 provider |
| D9 microVM 生产隔离生效 | ~70% | provider env allowlist/evidence 已有单测；仍仅对 fake SDK 证明 microVM，committed 默认值 = local_os_sandbox | Railway 上设 `HIVE_CODE_EXEC_PROVIDER=vercel_sandbox` 跑 agent execute_code，抓真 uname/network-denied trace（in-repo 或 CI artifact）|
| ID1 sub-agent 软删级联保留历史 | ~82% | desktop hard delete 已修；仍缺所有 agent deletion entrypoint 的统一 conformance sweep | 对 Web API、HR/system creation/deletion、desktop、auto-provision 做统一 soft-delete conformance test matrix |
| D11 knowledge 半按 principal 预过滤 | ~62% | OpenViking prompt 注入与 retriever external layer 都有 ACL mirror；仍缺 source ACL ingest 与全 connector read model 覆盖 | Feishu/Drive/Office 等 connector read model 写入 ACL metadata；补生成后复检 |
| 任意 SOTA 维度「在生产成功运行」（非仅 wired）| — | ~10/17 cell 自述仅 code/test/wiring 级、无 runtime trace；fail-soft 无聚合告警 | 跑 live invoke_agent + 抓真 Postgres span 行/真 529 fallback/真重启 reconcile/真 PL3 strip 的生产日志或 trace |
| 「Hive 超越 hermes」 | 禁止（Speculation）| 从未测过行为 delta | 见 §5 解锁路径（配 secrets → live 跑 → rebaseline → hermes 交叉对比）|

---

## 8. 完整性说明 — 本审计未覆盖的面（来自完整性 critic）

本审计是 17 个**功能维度**的原子审查；以下横切/入口面**无任何 cell 取主体**，是已知的覆盖盲区，其中第 1 项是最大的单点遗漏：

1. **运行时 RLS / DB 级租户隔离兜底（原最大遗漏，P0 已补框架）**。原审计发现 fresh bootstrap + owner connection 下只有少数表 FORCE RLS，现已改为 `RLS_FORCED_TENANT_TABLES = RLS_TENANT_TABLES + additional`，migration `force_all_tenant_rls_0615` 对 agents/chat_sessions/chat_messages/memory/audit/tasks/runtime/workflow/coordination/plugin 等 tenant 表 ENABLE+FORCE，测试钉死 bootstrap 常量与 migration `_FORCE_TABLES` 覆盖一致。剩余盲区不是“无 DB 后盾”，而是还缺一组 adversarial DB integration：以生产 owner role + `app.current_tenant_id` 跑代表性 SELECT/INSERT/UPDATE，证明每个高危表在漏 app-level `WHERE tenant_id=` 时仍被 PostgreSQL policy 拦住。
2. **渠道集成入站安全 & per-agent 隔离（零 cell）**。9 个渠道（Feishu/Slack/DingTalk/WeChat/Teams/Telegram/Email/Discord）各摄入外部不可信 payload 并解析到 agent+tenant。抽查发现 Feishu 有 HMAC 验签（`feishu.py:745`）、Slack 有，但**未核实其余渠道是否验签、入站消息能否跨解析到别租户 agent、2 个 bare-session pre-auth 路径（telegram/webhooks）是否 fail-close**。渠道是主要不可信入口面、完全未审。
3. **多租户不变量作为独立维度（CC-baseline / Goal-2 基石）**。RLS FORCE 框架已补，但仍需要把“tenant isolation adversarial DB tests”作为独立 cell，而不是隐含在各功能维度里。
4. **HR agent 创建管线 + soul refinement L1 预算（零 cell）**。CLAUDE.md 文档化了完整 LLM soul-refinement 创建管线（`_refine_soul_inputs`）；未核实其输出预算（L1 充分性）、fallback 路径、生成的 soul 是否在创建时租户/owner-bound。D10 只验了 `ensure_agent_identity` 从 `hr.py:1334` 调用，未审 soul 内容生成质量/预算。
5. **prompt-cache frozen-prefix 跨非-web 入口与渠道源的字节稳定性（部分）**。CC-context 与 D8 验了 web-chat 路径的 cache 边界，但 frozen prefix 跨 heartbeat/trigger/delegation/channel 调用（各自以不同 `active_packs` 建 SessionContext）的字节稳定性**未交叉核实**——per-source 的 frozen prefix 差异会静默污染自主运行的 cache，正是第一轮审计抓到的「dynamic suffix 毁 cache」回归类。

---

*报告作者独立复核的承重证伪点中，已退休：RLS owner-bypass/FORCE 覆盖缺口、desktop 硬删、`behavior_report` 永久 None、`save_skill` active 自授权、`load_skill` 显式路径 16K 截断、Anthropic 单块 unwrap 丢 `cache_control`、behavior eval 缺 secrets 时 `exit 0` skip、D7 完全无 Progress Ledger/team mailbox。仍 active：live behavior eval baseline provisional、mutating subagent/delegation 非全量 restart replay、真实 production Vercel microVM trace 缺失、full connector source ACL ingest 缺失。*
