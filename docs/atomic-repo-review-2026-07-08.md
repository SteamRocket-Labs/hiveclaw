# Hive 全仓原子化 Review 总报告 — 2026-07-08

六路并行深审合成：①架构北极星（CCPlus）②Memory + QKV Attention Control ③Plugin 系统 × CC 插件市场 ④Personal KB ⑤HR Agent 兼容性 ⑥Loop / Target Mode 对标考证。全部对标 FreeCode 第一基线（/Users/rocky243/vc-saas/free-code-main）+ claude-code-org 交叉核对 + codex-rs delta。每项发现均有 file:line 证据，标注 Fact / Inference / Speculation。

> Closure status: execution started on current checkout `09e20e95c13c2f64fa6a009649c9463517b32310`.
> This document is updated after each completed item with the exact verification command and outcome.

## 0.1 闭环执行记录

| 时间 | 项目 | 状态 | 验证 |
|---|---|---|---|
| 2026-07-08 | P0-1 Personal KB capability gate | ✅ 已闭环 | `cd backend && source .venv/bin/activate && pytest tests/services/test_capability_gate_policy_surface.py -q` → `11 passed`; `audit_capability_mapping()` → `{'unmapped': [], 'stale': []}` |
| 2026-07-08 | P0-2 QKV activation_events T0 truth-surface leak | ✅ 已闭环 | `cd backend && source .venv/bin/activate && pytest tests/runtime/test_t0_to_t2_session_close.py tests/runtime/test_activation_events.py -q` → `20 passed` |
| 2026-07-08 | P0-3 HR red test + template sweep + existing HR diff | ✅ 已闭环 | Red: 3 targeted tests failed for v4/template/tool-set equality; Green: `cd backend && source .venv/bin/activate && pytest tests/tools/test_hr_handler.py tests/api/test_hr_agent_endpoint.py tests/services/test_agent_identity_lifecycle.py tests/services/test_prompt_contracts.py -q` → `70 passed` |
| 2026-07-08 | P1-1 QKV empty activation hints shrink | ✅ 已闭环 | Red: `test_dynamic_suffix_omits_empty_activation_hints_from_ledger` failed on empty `dynamic:activation:hints`; Green: `cd backend && source .venv/bin/activate && pytest tests/runtime/test_prompt_builder.py::test_dynamic_suffix_injects_activation_hints_and_records_ledger tests/runtime/test_prompt_builder.py::test_dynamic_suffix_omits_empty_activation_hints_from_ledger tests/runtime/test_activation_hints_section.py -q` → `4 passed` |
| 2026-07-08 | P1-2 Personal KB deterministic closure | ✅ 已闭环 | Red: owner search wrongly emitted `knowledge_documents.agent_searchable IS true`; oversized upload lacked `CHAT_UPLOAD_MAX_BYTES`; queued job worker method absent. Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_personal_knowledge_service.py tests/api/test_chat_upload_conversion.py -q` → `28 passed` |
| 2026-07-08 | P1-3 External capability trust gate revoke/deactivate | ✅ 已闭环 | Red: missing `revoke_external_capability_snapshot` / `deactivate_external_extension_for_agent` services and routes; Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_external_capability_trust_gate.py tests/services/test_external_capability_activation.py tests/api/test_external_capability_reviews_api.py -q` → `16 passed` |
| 2026-07-08 | P1-4 RTD context-usage observability wiring | ✅ 已闭环 | Red: missing frontend `getSessionContextUsage`, Workbench query, and context chip projection; Green: `cd frontend && npm test -- --run src/api/domains/ccParity.test.ts src/pages/session-workbench/SessionNativeControls.test.tsx src/pages/session-workbench/timelineModel.test.ts` → `3 passed / 36 tests` |
| 2026-07-08 | P1-5 RTD Codex optimization dead append shrink | ✅ 已闭环 | Red: `append_codex_optimization_ledger` / `codex_delta_can_override_semantics` still exported and persisted session metadata; Green: `cd backend && source .venv/bin/activate && pytest tests/runtime/test_runtime_context_composition.py::test_codex_optimization_ledger_is_control_plane_only tests/runtime/test_codex_substrate.py::test_codex_optimization_ledger_keeps_codex_as_additive_control_plane tests/services/test_session_control_plane.py::test_session_workbench_aggregates_turn_runtime_goal_and_team_state -q` → `3 passed` |
| 2026-07-08 | P1-6 RTD cache decision read surface | ✅ 已闭环 | Red: `context-usage` omitted `cache_decision_ledger`; Workbench panel did not read cache decisions. Green: `cd backend && source .venv/bin/activate && pytest tests/api/test_chat_sessions_permissions.py::test_get_session_context_usage_returns_context_diagnostics -q` → `1 passed`; `cd frontend && npm test -- --run src/api/domains/ccParity.test.ts src/pages/session-workbench/SessionNativeControls.test.tsx src/pages/session-workbench/timelineModel.test.ts` → `3 passed / 36 tests`; `cd frontend && npm run build` → success |
| 2026-07-08 | P1-7 RTD agent-cycle/context-artifact read surface | ✅ 已闭环 | Red: `context-usage` omitted `agent_cycle_decision_ledger` and `context_artifacts`; Workbench did not read either field. Green: `cd backend && source .venv/bin/activate && pytest tests/api/test_chat_sessions_permissions.py::test_get_session_context_usage_returns_context_diagnostics -q` → `1 passed`; `cd frontend && npm test -- --run src/api/domains/ccParity.test.ts src/pages/session-workbench/SessionNativeControls.test.tsx src/pages/session-workbench/timelineModel.test.ts` → `3 passed / 36 tests`; `cd frontend && npm run build` → success |
| 2026-07-08 | P1-8 QKV MemoryRetriever dead gather API retirement | ✅ 已闭环 | Red: `test_memory_retriever_exposes_only_live_retrieval_entrypoints` failed while `retrieve_candidates` existed. Green: `cd backend && source .venv/bin/activate && pytest tests/memory/test_retrieval_pipeline.py -q` → `12 passed`; `ruff check app/memory/retriever.py tests/memory/test_retrieval_pipeline.py` → passed; graph/`rg` confirmed removed `retrieve_candidates` / `gather_*` symbols have no remaining code nodes or references |
| 2026-07-08 | P1-9 External capability reject + version supersede | ✅ 已闭环 | Red: missing `reject_external_capability_review`; approve did not supersede older approved snapshot/catalog. Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_external_capability_trust_gate.py tests/services/test_external_capability_activation.py tests/api/test_external_capability_reviews_api.py -q` → `19 passed`; `pytest tests/services/test_plugin_install_service.py -q` → `16 passed` confirms legacy `/enterprise/plugins/install` remains builtin/local pack projection, not external-source trust-gate bypass |

---

## 0. 总判一览

| 领域 | 判定 | 一句话 |
|---|---|---|
| ① kernel 主循环 CC 对齐 | **✅ 扎实 CCPlus** | 5 维度全对齐或合规增强，无 CC 语义违背；债在 RTD 遥测台账层非内核 |
| ① RTD 决策台账层 | ✅ T3 死写入已闭环 | `context-usage`/Workbench 已读回核心 ledger；Codex dead append 已删；workflow/execution-shape 两项复核为误判 |
| ② QKV Attention Control | ⚠️ **半接线脚手架 / 收缩中** | 能稳定运行（fail-open + cache 正确）但未承载真实激活；T0 原始 activation event 泄漏已堵；空 hints 与 MemoryRetriever dead gather 已删，剩余为 Q 侧接活或继续收缩决策 |
| ③ Plugin：CC 市场适配 | ❌ **≈15%** | marketplace/source 拉取/materialize/`${CLAUDE_PLUGIN_ROOT}` 全零；adapter 是孤儿代码 |
| ③ Plugin：trust gate | ✅ 当前 trust chain 已闭环 / ⚠️ 市场适配未做 | skill/MCP import→stage→approve/reject→snapshot/catalog→activate/deactivate/revoke 真接线；新版本 approve 会 supersede 旧 snapshot/catalog；legacy plugins/install 复核为 builtin/local pack projection，不是 external-source trust-gate bypass |
| ④ Personal KB | ⚠️ owner 面 ~95% / ✅ deterministic gaps 已修 | `search_personal_kb` 已注册 `agent.knowledge.read`；owner 搜索不再被 `agent_searchable` 误过滤；上传有硬上限；`KnowledgeIndexJob` 有批量消费入口；自主态 grant 仍需权限产品拍板 |
| ⑤ HR Agent | ✅ P0 已闭环 | HR v5 模板已同步 Personal KB、work ledger、workflow/subagent 路由；旧 `memory/t3/` source attribution 示例已迁；现有 HR diff 已回归验证 |
| ⑥ Loop | ② 部分实现 | trigger 覆盖 cron 内核；缺模型自节奏 self-pace 链与 `/loop` 命令层 |
| ⑥ Target Mode | **CC 侧不存在此功能**（Fact） | Hive Goal Mode 是 Codex-inspired 原生 delta，单次续跑防 runaway，缺"模型自判完成"闭环 |

---

## 1. Agent 架构与北极星（CCPlus 达成度）

### 1.1 kernel 主循环：5 维度对齐核验（全 Fact）

| 维度 | 结论 | 证据 |
|---|---|---|
| 轮次预算 | 对齐 + 治理 delta | 自然停机同为"无 tool_use 终止"（engine.py:4892 / FreeCode query.ts）；Hive 恒套 200 硬帽 vs CC 顶层无上限（query.ts:1705），subagent 侧 200==200 精确对齐；Hive 额外轮次压力告警（engine.py:4295） |
| 流式 | 架构差非语义差 | 两侧均 per-delta（CC pull generator QueryEngine.ts:818 / Hive push callbacks engine.py:4146） |
| 压缩 | 语义对齐（满足 AI-native L1） | 均完整历史喂 LLM 摘要器（engine.py:5689 / autoCompact.ts）；Hive 75% vs CC ~93% 是有据调参；机械截断仅 PTL 兜底，**无 `[-40:]` 残留** |
| 工具裁剪 | 数值完全一致 | 50K 单工具 / 200K 单轮聚合（ccplus_contracts.py:110/111 == toolLimits.ts） |
| 工具并发 | 忠实实现 + 防御超集 | 只读并发 Semaphore 10（engine.py:5149）/ 写串行栅栏（engine.py:5164）与 CC partitionToolCalls 可观测等价；额外硬剔 destructive（engine.py:989） |

### 1.2 RTD 决策台账三层分类

统一反模式：**"决策台账"都是决策记录器而非决策者**——真决策在别处（异常类型 / enforce_plan_gate / governance deny 分支 / ContextPolicy 阈值），台账事后记录，绝大多数不读回。

**T1 真机制（误名"ledger"，实际驱动运行时）**：`context_budget.py`（召回条数/prompt 段上限/工具结果预算/模型路由全由它驱动）、`ccplus_contracts.py`（ContextPolicyV1 驱动压缩阈值 engine.py:76/80/85；PermissionProfileV1 驱动真权限）、`recovery_manifest.py`（主路径无条件 hydrate engine.py:3470 + 重放 pending tool frames :4220）、`workflow_admission.py`（越限 raise 即 run 不创建）、`context_candidates.py`（激活反向索引 join 键）。

**T3 只写不读（原审 6 项，当前均已闭环或修正）**：
1. ✅ `decision_ledger.py` agent_cycle_decision_ledger 字段已接 `context-usage` payload + Session-native controls agent cycle decision count（RTD-37）
2. ✅ `cache_decision_ledger.py` 已接 `context-usage` payload + Session-native controls cache decision count（RTD-21）
3. ✅ `context_engine.py` record_prompt_manifest_context_artifacts 已接 `context-usage` payload + Session-native controls context artifact count（RTD context artifacts）
4. ✅ `codex_optimization_ledger.py` append 路径已删除；`codex_delta_can_override_semantics` 恒 False 死函数已删除；Workbench 仍通过 `build_codex_optimization_ledger()` 输出 control-plane read model（RTD-36）
5. ✅ `dynamic_workflow.py` 内嵌 workflow_decision_entry 复核为误判：workflow runtime completion 会读写并更新 outcome/repair（`workflow_runtime_service.py`），不是零消费者（RTD-29）
6. ✅ `context_budget.py:346` build_tool_execution_shape_decision 复核为误判：`start_workflow` / `spawn_subagent` 返回 payload 直接暴露给模型/用户，并有工具测试钉死 warning/recommendation（RTD-30）

**T2 可观测非驱动（10 项）**：runtime_decision / schedule_decision / authorization_decision / failure_policy（纯遥测，真失败分支靠异常类型，真消费者是另一模块 llm_error_policy.py:129）/ runtime_reminder_candidate / subagent_decision_entry / subagent_return_contract / compaction_trace / tool_result_ledger / deferred_tools sink。

**观测面闭环状态**：`GET .../context-usage`（chat_sessions.py:1792）已接 `ccParityApi.getSessionContextUsage`、Session-native controls query、Chat header context chip fallback；workbench payload 里 `codex_optimization_ledger` 仍是 0 前端引用；8 个决策台账字段名前端仍 0 命中。

**架构 lane 断点结论（诚实）**：**无崩溃级断点**——RTD 台账层全部 inert（T2/T3）不改控制流。两个登记项：①sandbox 分档死契约是 latent 安全落差（"承诺的隔离没兑现"，若有代码信赖该分档即落差暴露）；②**assembly-state DB bloat**——RuntimeAssemblyState.persist()（context.py:153）每次 record_* 把全部 ~24 字段重写进 session.metadata → ChatSession.transcript_metadata_json（JSONB），列表有界（limit 50~200）非无限增长，但每 session 行携带一大坨几乎无人读的台账 = DB/序列化死重量。顶层 200 轮硬帽是 intended 治理 delta 非断点。

**旧记忆修正**（Fact）：`reconciliation_retry_allowed` 并非"从不 set"——runtime_task_service.py:310 在 audited retry contract 非空时置 True；真 gate 在 runtime_reconciliation.py:192。

### 1.3 Codex 工程 delta 评估

- codex_optimization_ledger 语义诚实（`codex_delta_can_override_semantics` 硬 False:131），已引入合规 delta：typed session/turn 契约、session_context_controller、compaction/turn 可观测性（最强项）。
- **头号缺口**：SandboxProfile 四档（ccplus_contracts.py:51）是**死契约**——subprocess_sandbox.py 仍二元/恒 deny-network/无 writable_roots 粒度。
- 最高价值可引入 3 项（不改 CC 语义）：①sandbox 分档 + network_access + writable_roots 接进 builder ②单命令 shell 升权流（Hive 零等价物）③声明式 execpolicy 命令规则引擎替代手写危险 regex（governance.py:232）。
- 越界排除清单：勿用 AskForApproval 换 PreflightDecision；勿用 Codex rollout SQLite 当 T0 真相；勿让 DangerFullAccess 上 Railway；勿移植 CollaborationMode/Personality。

### 1.4 瘦身清单

- **test-only 死模块 ×10，~1,990 LOC 可退役**（连带测试文件）：self_evolution_audit(94L) / proactive_employee_loop(205L) / charter_proposals(379L) / template_seeder(237L) / heartbeat_reflection_backfill(142L) / extract_queue_replay(113L) / office_workflow_examples(135L) / external_capabilities{cc_plugin_adapter 443L, codex_plugin_adapter 171L, context_projection 71L}（注意：后三者若走 Plugin 主线接线则不删，见 §3）。
- **台账工厂合并**：cache_decision_ledger + runtime_decision_ledger + authorization_decision（153L）并入 decision_ledger → 7 文件 ~645 LOC 收敛到 ~300L；薄文件 outcome.py(25L)/compaction.py(15L)/delegation.py(19L) 内联单一消费者。
- **双轨债**：pack policy 三面（pack_policy_service + pack_service + capability_group_policy facade + 死 is_pack_enabled shim@agent_tools.py:81 零 importer）；T2 writer 双轨（segment_package canonical vs extract_agent/t2_store legacy——留 admin backfill 用途明确即可）；T0 writer 双轨（ledger vs t0_logger——main.py:695 backfill 活，保留）。
- **假阳性别砍**：prompt_sections/*.py（__init__ 再导出，prompt_builder:353 消费）、skill_creator_files/scripts（工作区模板 standalone 跑）。
- **shim 三裁决**：0ef82fec8 plan_gate reason 兜底=无害死防御保留；22604de04 subagent compat=非债（启用自定义定义）保留；799492db0 is_pack_enabled=grep 死重导出，验证后删。
- **文档漂移**：CLAUDE.md 记忆治理表仍称 proactive_employee_loop 为接线的"Proactive steward loop"，实为 test-only 死码（heartbeat.py 从不 import）。

---

## 2. Memory 系统 + QKV Attention Control 层

### 2.1 总判

**能稳定运行，但不是它宣称的东西。** fail-open 完整、cache 语义正确（hints 与 kb_hint 均在 dynamic suffix，PROMPT_CACHE_BOUNDARY 之后，prompt_builder.py:951——不污染 frozen prefix ✅）。但 QKV 不是承载 memory/skill/tool/subagent 激活决策的注意力路由，而是**与既有检索/排序系统平行、绝大部分读侧死代码/空渲染的契约脚手架**。真实激活仍由改造前的 ActivationScorer + retriever + skill_catalog_ranker + deferred tool index 完成。唯一真接线承载路径是 Personal KB 一路，而其产出（activation hints 区）生产**恒渲染为空**，KB 提示实际走另一条 `kb_hint` 旁路。

FreeCode 全仓 attention/activation/router 零命中（Fact）——CC 无此层，QKV 是合法 Hive-native delta，但当前**复杂度与收益严重不成比例**。

### 2.2 断点（全 grep 证实）

- **B1** LLM Query Parser seam 死：maybe_parse_activation_query_with_llm（activation_query.py:157,183）零调用。Q 侧 100% 机械 regex，`concepts` 恒 []（:151）。
- **B2** hints 区恒渲染空：route_activation_candidates 全仓仅 1 调用点（invoker.py:411）且只喂 KB 候选；build_activation_hints_section 只渲染 skill/tool/subagent（activation_hints.py:73-78）→ 恒返回 ""。
- **B3** router→memory 读桥死：plane_read.load_selected_memory_values（:228）零调用。
- **B4 ✅ 已闭环** memory 候选生产者全死：`retrieve_candidates` / `gather_t2_evidence_candidates` / `gather_t3_plane_candidates` / `gather_explicit_overlay_candidates` 已从 `MemoryRetriever` 退役；图谱与 `rg` 均确认删后无剩余符号/引用。
- **B5** K 侧 activation_keys 仍未承载 `MemoryRetriever.retrieve()`：B4 死读桥已删，`reference_index.py` 的 activation_keys 表仍作为 derived index / source-ref / repair script 合约存在；若要继续收缩，必须单独评估并迁移这些索引测试与维护脚本，而不是随 dead gather 一并误删。
- **B6-B8** tool/subagent 候选生产者死；qkv trace 构建器零调用；两个 credit 环只写不读（工具 activation events 零读者 hooks_setup.py:775；heat_delta/decay_signal 无消费者 session_feedback.py:202）。
- **B9 ⚠️ 治理级泄漏**：invoker.py:1697 把原始 activation_events 注入 _hook_metadata → TURN_STOP → _t0_turn_stop（hooks_setup.py:447-457）→ seal_t0_session_segment，_clean_metadata 保留 list/dict（ledger.py:892）→ 原始 events 写入 `memory/t0/.../events.jsonl` segment_boundary。而 memory/t0/ 正是其自己声明的 forbidden_truth_surfaces（activation_events.py:187）。触发面：非 web 默认路径有 open segment 时（trigger/delegation/heartbeat/一次性）。

### 2.3 AI-Native 四问判决

hard mask（policy/acl/sensitivity/budget，activation_router.py:280-333）= 约束授权范围 = **L2 合法**。scoring（_multi_head_score:181-206）= 机械词重叠替代"哪些记忆相关"= **L1 领域**；更糟：semantic head（权重 0.4 最大）作用于恒空 concepts → **40% 打分权重永远为 0**，且 router 会用空-concept 词重叠分覆盖 KB 原本 FTS 相关分（:353）。当前因"非承载"被临时掩盖——**接线那天即 case-law 级 L1 违规**（对标 compaction `[-40:]` 案）。注：K 侧 activation_keys 是 LLM 在 T2 build 时撰写的（t2/prompts.py:109），这一侧是 AI-native 的。

### 2.4 处置建议（要么接活，要么退役——当前半接线是最坏状态）

- **S1** hints 死注入与 KB 旁路二选一：kb_hint 已够用，删空注入（prompt_builder.py:865-875 + activation_hints.py）。
- **S2** 退役 K 侧只写不读回路：✅ dead gather 函数已删；activation_keys 表投影仍保留为 derived index / source-ref / repair script 合约，后续若继续退役必须先拆 reference_index 合约与测试，若接读侧则必须先修 B1（LLM parser）+ 让 scoring 语义判断回归模型。
- **S4（治理级，立即）**：T0 seal 前 strip 原始 activation_events（只留 summary）堵 B9；policy dict 接入真实写前校验或删除。
- **S5** 若保留 SQLite 索引：加 busy_timeout + build-to-temp + atomic-rename（当前双进程可 database is locked，连接不 close 泄漏句柄）。
- 性能注记：KB 激活每 invocation 无条件 ~2 个 PG 查询（invoker.py:1623），热路径无同步 embedding ✅；reference_index 每写全量 DROP+CREATE 却无读者（为死读侧付全量写代价）。

---

## 3. Plugin 系统 × CC 插件市场

### 3.1 两件事分开评分

**A. CC 插件市场适配 ≈15%（Fact）**：定义 CC 插件"市场"的四大能力——marketplace.json 索引解析、plugin source 拉取（github/npm/pip/git）、materialize 物化、`${CLAUDE_PLUGIN_ROOT}` 替换——**全部零实现**（grep 全仓 0 命中）。唯一能解析 `.claude-plugin/plugin.json` + 五类组件的 `cc_plugin_adapter.py` 是**带单测的孤儿代码**（load_cc_plugin_bundle 零运行时调用者）。设计文档 docs/external-capability-trust-gate-plan-2026-06-26.md §0.1 自己诚实列出了未完成项，唯一淡化处是未点破 adapter 无调用者。

**B. trust gate 权限链 ✅ 当前链路闭环**：import→stage→approve/reject→snapshot→catalog→activate/deactivate/revoke 对单个 Skill 和 MCP server **真实接线**（trust_gate.py + activation.py；skill 走 SkillGuard 二次扫描 skill_installation.py，MCP 走 mcp_authz ✅）。三段权限分离正确：任意 user 可 stage（不落盘）→ 仅 admin approve/reject → 有 agent 权限者 activate/deactivate；新版本 approve 会 supersede 同 tenant/source/name 的旧 approved snapshot 与 catalog。

### 3.2 格式覆盖矩阵要点（基线 schemas.ts）

✅ 完整：plugin.json 三要素、标准 commands//agents//skills/ 目录、.mcp.json dict 形态、组件命名空间。⚠️ 部分：manifest commands 仅取 source 且与目录互斥（CC 是合并）、hooks 能解析但激活 fail-closed、userConfig 无 `${user_config.KEY}` 替换。❌ 缺失：author/homepage/dependencies 等字段、内联 content、agents/skills manifest 字段、output-styles、lspServers、settings、channels、`${CLAUDE_PLUGIN_ROOT}`、marketplace.json、source 拉取、仿冒防护。

### 3.3 生命周期缺环 + 权限风险

1. ✅ **revoke/deactivate/rollback 已闭环**：snapshot revoke 会隐藏 catalog；agent deactivate 会 inactive activation 并清理本地 skill/subagent 投影，MCP 标记 `manual_revoke_required`。
2. command/hook 激活 fail-closed unsupported（activation.py:80-99 else 分支）——解析了、catalog 了、永不接入运行时，用户会困惑"装了没用"。
3. ✅ **被拒 import 清理 + 版本 supersede 已闭环**：review 可被 admin reject，rejected review 不能再 approve；approve 新版本会将同 tenant/source/name 的旧 approved snapshot 与旧 catalog entry 标记 `superseded`。
4. ✅ **legacy 双轨旁路复核为误判**：`POST /api/enterprise/plugins/install` 只接 `plugin_key`，服务端 `load_manifest()` 仅读本地 pack manifests，`_assert_installable()` 只允许 `builtin/local` 且 remote source fail-closed；它是 legacy builtin/local pack projection，不是 external capability source/import 通道。命名仍旧，但不是 trust-gate bypass。
5. 【低】通用 POST /reviews 信任客户端 admission_notes（external_capabilities.py:63→trust_gate.py:149），客户端省略 note 可逃 blocked 判定；缓解=admin-gated + 前端不走此端点。
6. hook custom executor 修复（56539a934）**验证正确**（invoker.py:1038 传 emit_runtime_hooks=False，修复前 2 failed 后 2 passed，非绿洗）；残留脆弱点=靠 executor 签名约定，建议按 tool_call_id 跨-emit 去重升级为结构保证。

---

## 4. Personal KB（未上线，上线前必修清单）

### 4.1 断点

- **B1〔CRITICAL·执行铁证〕`search_personal_kb` 被 capability gate 拒**：工具在 registry.py:70 的 `_MEMORY` 运行时组但**不在 CAPABILITY_MAP**，governance.py:1014 无条件 check_capability，STRICT_CAPABILITY_MAPPING=True 下 fail-closed 拒绝（audit_capability_mapping() 原话确认）。整个 spec §5.2 agent 消费路径死；invoker.py:444 还在注入"用 search_personal_kb"提示词=自相矛盾。owner Web UI 端点直调 service 不受影响。**修法一行**（注册 CAPABILITY_MAP 如 agent.knowledge.read）。两个工具测试绕 governance 直调 handler 故 CI 没抓——应补一条真走 governance 的集成测试当修复门。
- **B2〔高〕管线同步内联非异步**：无 worker 消费 KnowledgeIndexJob；upload.py:302 在聊天上传请求内 await ingest（含逐段 LLM 抽取）→ 大文档阻塞请求；KB 路径无独立大小上限。
- **B3〔中〕owner 搜自己 KB 被 agent_searchable 误过滤**（search statement :418 无条件过滤，不分 owner）——文库列表能见、搜索不见，不一致。
- **B4〔中〕自主态无自动授权**：ACL 谓词仅 current_user==owner 放行（service:316）；heartbeat/trigger（user_id=None）需显式 agent grant 而无种植机制 → 即便 B1 修好自主 agent 检索仍得空。与 HR 审计的"创建时不种 grant"同根因，合并为一个拍板点。

### 4.2 缺失与债

向量通道整个缺失（segment 无 embedding 列，迁移测试证明是刻意——实为 text(tsv+ILIKE)+entity(ILIKE)+graph(1-hop) ~3 通道非 spec 四通道，实体对齐是字符串非语义）；引用边模型缺失（"N agent 同文件=1 doc+N 引用边"未实现，t0_refs 恒空）；幂等半实现（sha256 去重 ✅ 但重复 ingest 重烧 LLM，无快路径+无幂等测试）；无 per-channel 降级；前端 6 个 mutation 无 onError 静默失败、搜索空态 return null 吞掉；i18n 双语缺 10 键 + 硬编码中文来源标签；artifact 先于事务落盘留孤儿；upload 中途 db.commit() 事务边界不干净。

**真实现（不冤枉它）**：RLS day one（7 表 FORCE + 迁移测试断言精确 SQL）✅ 单 alembic head ✅ L1 抽取整段视野+租户模型 ✅ 提示行进动态后缀不破 cache ✅ QKV KB seam 真接线 ✅ 四模块前端真接 ✅ score_trace 完整 ✅。

---

## 5. HR Agent 兼容性

- **已闭环 diff：可提交状态已验证**。放宽窄口径正确（field 仍必填 hr.py:266、显式非法仍拒 :264、缺省默认+计 warning :261-282）；方向是数据质量改善（保留+标债 vs 整条丢弃）；版本 bump 驱动存量 resync 且有 .bak 备份。
- **无硬断点**：空记忆库 QKV 激活/retriever 优雅降级有测试；soul.md 当不透明整文注入零解析→结构不可能漂移；工具 seeding 与存量一致。
- **既存红测已修**：test_hr_tool_included_in_hr_tools_set（test_hr_handler.py:107）已改为核心 HR 工具 superset 断言，不再钉动态 provider 集合。
- **模板 sweep 已修**：HR 模板版本推进到 `hr-flow-v5-personal-kb-work-routing-2026-07-08`；SKILL.md 退役 `memory/t3/` source attribution 示例已迁到两平面知识路径；Personal KB、work ledger、workflow、subagent 路由已补齐。
- 文档漂移：CLAUDE.md + path-contract Ownership 表仍把 memory/t3/*.md 当 canonical（C7 后已两平面，t3_platform_gate.py:22 明标 legacy）。

---

## 6. Loop 与 Target Mode 考证（Fact 级结论）

### 6.1 CC 权威语义（两代）

- **第一代（FreeCode 快照）**：`/loop` = 纯 cron 包装（src/skills/bundled/loop.ts:74-92→CronCreate，省略 interval 默认 10m，**无 self-pace**；建完立即先跑一次 :67）；cron 把 prompt **塞进当前 session 命令队列**（isMeta/priority:later），仅 REPL idle 才 fire；recurring 7 天过期。真 self-pace 是分离的 ant-only `--proactive`（main.tsx:3833）+ `<tick>` 注入 + **Sleep 工具**（"nothing useful → MUST call Sleep"）。`autonomous-loop` 字面 sentinel 源码不存在。
- **第二代（现役 CC harness）**：`/loop` 统一 interval（CronCreate，sentinel `<<autonomous-loop>>`）+ dynamic（ScheduleWakeup，sentinel `<<autonomous-loop-dynamic>>`，模型自定 delaySeconds [60,3600]、每轮回传 prompt、stop:true 终止）= 把第一代 ant-only 自醒循环收编进 GA。

### 6.2 Target Mode：**CC 两基线均无此命名功能**（穷举 grep 零真命中；targetMode 命中全是 file/queue/permission mode 参数误命中）。最接近机制按序：--proactive 自主模式 > /loop dynamic > Plan Mode。

### 6.3 Hive 对照

- **Loop ② 部分实现**：trigger 系统（set_trigger cron/once/interval/poll/on_message/webhook/event_wait，triggers.py:21；trigger_daemon._should_fire:730-774）覆盖"周期触发"内核，且有 CC 没有的 failure_policy/preflight/restart-safe/budget daemon。三缺口：①**上下文延续语义相反**——CC 塞当前 session 队列，Hive 每次 fire 另起新 invocation/child_session（trigger_daemon.py:427）；②无模型自节奏 self-pace 工具（ScheduleWakeup/Sleep/tick 全仓零命中）；③无 `/loop` 自然语言命令层（docs/freecode-command-loop-feature-parity-audit-2026-06-22.md 自认）。
- **Goal Mode（Hive-native delta，② 部分）**：session-local（session_goal_runtime.py:1-5 自述 Codex-inspired），数据/工具/命令面/REST/前端/治理/审计全接线 restart-safe。关键性质：续跑决策是**确定性 Python**（should_continue_goal:155-185 判 budget/turn-cap，非模型自判）；**每用户 turn 后最多续跑一次、续跑不递归**（goal_continuation_service.py:184 + 测试钉死）——刻意防 runaway（呼应常春藤烧钱事故），但不等价 CC proactive 持续自主。缺口：goal_stop/goal_update 不是 agent 可调工具（仅命令面/REST），"目标是否完成"落在 prose 无治理回写桥 = L1 待加强。
- **建设判断**：两者都不是从零建——底座（trigger daemon + goal 续跑 + RuntimeTask + heartbeat）都在，缺的是"模型自节奏 + 自判完成"这条 L1 自主链 + `/loop` 命令薄封装。若要"无人值守持续自主"形态，**必须先落 runtime budget control plane**（docs/runtime-budget-control-plane-plan-2026-07-03.md）再放开，否则重演烧钱事故。

---

## 7. 统一优先级行动清单

### P0 — 生产断点 / 治理级（立即）
1. ✅ **Personal KB B1**：search_personal_kb 注册 CAPABILITY_MAP（一行）+ 补真走 governance 的集成测试当修复门。上线前置。已闭环：`search_personal_kb -> agent.knowledge.read`，capability audit 无 drift。
2. ✅ **QKV B9**：T0 seal 前 strip 原始 activation_events，堵 truth-surface 泄漏；policy dict 接真实校验或删。已闭环：T0 boundary 只保留 `activation_feedback_summary`，不落原始 `activation_events`。
3. ✅ **HR 既存红测**：test_hr_tool_included_in_hr_tools_set 改 superset 断言；已由目标红测转绿并纳入 HR 回归组。
4. ✅ **HR diff**：已完成并回归验证；HR v5 模板同步 Personal KB / ledger / workflow / subagent 路由，旧 `memory/t3/` 示例已清除。

### P1 — 结构性技术债（要么接活要么退役，禁半接线常态化）
5. **QKV 收缩**：✅ S1 删恒空 hints 注入（kb_hint 已够，builder 仅在有 actionable skill/tool/subagent hint 时写 ledger）；✅ S2 dead gather 函数已退役；activation_keys derived index 是否继续收缩需按 reference_index/source-ref/repair-script 合约单独处理，或拍板接活（接活先修 LLM parser + scoring 回归模型判断）。
6. ✅ **RTD T3 六项死写入**：context-usage 死观测面已接前端（Session-native controls + Chat header chip fallback）；`codex_optimization_ledger` dead append / 恒 False override 已删，保留 Workbench builder；`cache_decision_ledger`、`agent_cycle_decision_ledger`、`context_artifacts` 已接 context-usage + Workbench；`workflow_decision_entry` 与 `execution_shape_decision` 复核为误判（已有运行时/返回 payload 读者）。剩余只属于文件组织瘦身，不再是行为死写入。
7. ✅ **Plugin trust gate 下半场**：snapshot revoke/deactivate/rollback 已补（catalog 隐藏，agent activation inactive，本地 skill/subagent 文件清理，MCP 标记 `manual_revoke_required`）；admin reject + rejected review approval block + version supersede 已补；legacy `/enterprise/plugins/install` 复核为 builtin/local pack projection，不是 external-source trust-gate bypass。剩余 Plugin 大项仅属于 CC marketplace/source/materialize 能力建设，不再是当前 trust-gate 安全缺口。
8. **Personal KB B2-B4**：✅ B2 deterministic 部分：`process_import_jobs` 批量消费 queued/failed `KnowledgeIndexJob`，聊天上传新增 `HIVE_CHAT_UPLOAD_MAX_BYTES`/`HIVE_CHAT_IMAGE_UPLOAD_MAX_BYTES` 硬上限；✅ B3 owner 搜索去 `agent_searchable` 过滤，agent/非 owner 搜索仍过滤；B4 自主态 grant 需要权限产品拍板后实现。
9. ✅ **HR 模板 sweep**（B1 落后）：O1 stale t3 例子 + M1 KB 引导 + M2/M3 能力路由，已随 P0-3 同批 bump 到 HR v5。
10. **瘦身**：~1,990 LOC test-only 死模块退役（cc_plugin_adapter 等三个 external_capabilities 文件除外——若 Plugin 主线拍板接线则留）。

### P2 — 对齐增强（拍板后排期）
11. Codex sandbox 分档接线（SandboxProfile 死契约→builder）+ 单命令升权流 + execpolicy 规则引擎。
12. Loop 补齐：/loop 命令层薄封装；评估同 session 续跑模式；self-pace 工具（前置=budget control plane）。
13. Goal Mode 闭环：goal_stop(complete) 成 agent 工具 + prose→完成治理回写桥。
14. Plugin CC 市场主线：marketplace.json 解析 + source 拉取 + materialize + ${CLAUDE_PLUGIN_ROOT} + adapter 接线（文档 §0.1 已排序）。

### Owner 拍板点
- **KB day-one 自主访问**：新 agent 创建时是否种 owner-scope grant（HR 审计 Medium + KB B4 同根因）。
- **QKV 走向**：接活成真承载（需先修 Q 侧 LLM parser、scoring 让位模型）vs 收缩到"契约 + kb_hint + 治理 sidecar"。
- **无人值守持续自主**（Loop self-pace + Goal 多步续跑）：要不要这个产品形态；要则 budget control plane 前置。
- **Plugin 市场主线**：15% 的市场适配是否本期推进（决定 cc_plugin_adapter 等孤儿是接线还是暂留）。

---

## 8. 审计方法与证据

六路后台 agent 并行（部分自带子 agent 三层展开），全程 Grep/Read/Bash 直查源码 + 执行级验证（audit_capability_mapping() 实跑、测试套件实跑：external_capability 19 passed、HR 相关 7 passed）；FreeCode/claude-code-org/codex-rs 三基线交叉；每结论 file:line 可复核。详细分报告：QKV 完整版存于审计 agent scratchpad（qkv-audit-final-report.md），其余以本文档为准。
