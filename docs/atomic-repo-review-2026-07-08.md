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
| 2026-07-08 | P1-10 Personal KB autonomous owner-scope agent grant | ✅ 已闭环 | Red: `test_ensure_agent_identity_seeds_owner_scope_personal_knowledge_grant` found no `KnowledgeGrant`. Green: `cd backend && source .venv/bin/activate && pytest tests/services/test_agent_identity_lifecycle.py -q` → `5 passed`; `pytest tests/services/test_personal_knowledge_service.py -q` → `24 passed`; new agent identity bootstrap now seeds owner-scope `search` grant for the agent when tenant + owner exist |
| 2026-07-08 | P1-11 Full regression contract sync | ✅ 已闭环 | Full backend: `cd backend && source .venv/bin/activate && pytest tests -q --tb=short` → `5513 passed, 206 skipped, 4 warnings`; backend ruff on changed files → `All checks passed!`; frontend targeted tests → `3 passed / 36 tests`; frontend build → success; `git diff --check` → clean |
| 2026-07-09 | §7.3.2 M0 动态记忆激活 eval 基线（先行门） | ✅ 已闭环 | Red: `tests/evals/test_memory_recall_baseline.py` collection error（harness 未建）; Green: `cd backend && source .venv/bin/activate && pytest tests/evals/test_memory_recall_baseline.py -q` → `6 passed`。基线锚定（真实 `search_wiki_pages` + `MemoryRetriever`+`ActivationScorer` 双 runner，零 mock 零 LLM 零 DB 写）：recall@k=0.833333 / MRR=0.857143，全部 ranked 顺序逐位钉死；确定性双跑一致；多跳证明（PPR 达 2 跳页而 BM25 不能）；2 个 headroom case（api-timeout recall=0 留 M2 BaseLevel、governance-2hop recall=2/3 留 M5 ContextBoost）。此 scorecard 即 Q-shrink 自验门（§7.3.3）：删 QKV 前后所有数字与顺序必须逐位不变 |
| 2026-07-09 | §7.3.1 Q-shrink QKV 精确退役 | ✅ 已闭环 | **自验门通过：删后 M0 全部分数与 ranked 顺序逐位不变（6 passed）——"QKV 非承载"实锤，退役安全**。净删 1,678 行（18+/1,696−）：`activation_router.py` 整删(380L)、`activation_query.py` 整删(397L，全部消费者同在死链)、`prompt_sections/activation_hints.py` 整删(86L)、`plane_read.py` B3 死读桥八函数链(163L)、`turn_envelope.py` qkv trace 构建器(126L)、`reference_index.py` activation_keys 投影(写侧+读侧 query/candidate_refs_for_keys，272L)、`context.py` router/query 字段簇(64L)、invoker/engine/prompt_builder 挂点。保留（施工图删/留表）：`ActivationCandidate/Score/Surface`(KB/subagent/skill/agent_tools 四处复用，HardMask 删)、`reference_counts` 反向索引、`activation_events.py` 全留(M3 原料，engine 事件链改直读 metadata turn/intent)、`ActivationScorer`、PPR/access_log/T2 activation_keys(LLM 撰写文件层)。rebuild 保留 `DROP TABLE IF EXISTS activation_keys` 清理存量并新增测试钉死。KB hint 真实路径保留（检索+record_activation_candidates+hint，router 死路径拆除）。验证：`pytest tests/memory tests/runtime tests/kernel tests/evals -q` → `1310 passed, 19 skipped`；生产 QKV 符号 grep 残留=0；ruff check 通过 |

## 0.2 闭环后独立复核（2026-07-09，主审逐 commit 抽查 09e20e95c..dcda97812，15 commits / 52 files）

**14/15 项实锤真实现**：P0-1 CAPABILITY_MAP（taxonomy:295 + gate 测试 :126 精确断言 + `audit_capability_mapping()` 执行级清零）；P0-2 strip 覆盖 hooks_setup 全部 7 个 T0 写入点（`_t0_safe_metadata` :247/339/380/455/489/526/577）；P0-3 HR 模板 sweep 实锤（SKILL.md:109-111 KB 引导真加、source_refs 示例全迁 `memory/knowledge/**` 零 t3 残留）；P1-3/9 revoke/deactivate/reject/supersede 真函数（trust_gate.py:189 / activation.py:73）；P1-4/6/7 前端真渲染（SessionNativeControls.tsx:79-80 读 cache/agent_cycle ledger 计数）——原"死观测面"复活；P1-10 grant 真种（agent_identity_lifecycle.py:59-61）。

**两个残留（同款"无调用者"反模式，登记为新债）**：
1. **P1-2 半闭环**：`process_import_jobs`（personal_knowledge_service.py:1985）**全仓零调用者**——无 daemon/API/scheduler 调度它。B2"同步内联"实质未变（upload.py:309 仍在请求内 `await ingest_source_bytes`），本批做的是有界化（上传硬上限）而非异步化。"批量消费入口"目前是带测试的孤儿方法。
2. **QKV K 侧写投影残留**：P1-8 删除了读侧死 API（MemoryRetriever gather），但 reference_index.py:116/139 的 activation_keys SQLite 全量 DROP+CREATE **写侧仍在**——读者清零后它成为更纯粹的只写不读，每次 T2/T3/explicit 写照付全量重建成本。应随 QKV"接活 vs 收缩"拍板一并处置。

---

## 0. 总判一览

| 领域 | 判定 | 一句话 |
|---|---|---|
| ① kernel 主循环 CC 对齐 | **✅ 扎实 CCPlus** | 5 维度全对齐或合规增强，无 CC 语义违背；债在 RTD 遥测台账层非内核 |
| ① RTD 决策台账层 | ✅ T3 死写入已闭环 | `context-usage`/Workbench 已读回核心 ledger；Codex dead append 已删；workflow/execution-shape 两项复核为误判 |
| ② QKV Attention Control | ✅ **已定方向（2026-07-09）：精确退役 + 演进为 Dynamic Recall Layer** | 拍板不接活；机械层外科手术式退役（§7.3.1 删/留清单），ActivationScorer 本体升级为统一激活方程（§7.3.2 / 设计文档），Hive-native 记忆演进主线（§7.3） |
| ③ Plugin：CC 市场适配 | ❌ **≈15%** | marketplace/source 拉取/materialize/`${CLAUDE_PLUGIN_ROOT}` 全零；adapter 是孤儿代码 |
| ③ Plugin：trust gate | ✅ 当前 trust chain 已闭环 / ⚠️ 市场适配未做 | skill/MCP import→stage→approve/reject→snapshot/catalog→activate/deactivate/revoke 真接线；新版本 approve 会 supersede 旧 snapshot/catalog；legacy plugins/install 复核为 builtin/local pack projection，不是 external-source trust-gate bypass |
| ④ Personal KB | ✅ 上线前硬断点已闭环 / ⚠️ 产品增强债仍在 | `search_personal_kb` 已注册 `agent.knowledge.read`；owner 搜索不再被 `agent_searchable` 误过滤；上传有硬上限；`KnowledgeIndexJob` 有批量消费入口；新 agent 自动获得 owner-scope Personal KB `search` grant 供自主态使用 |
| ⑤ HR Agent | ✅ P0 已闭环 | HR v5 模板已同步 Personal KB、work ledger、workflow/subagent 路由；旧 `memory/t3/` source attribution 示例已迁；现有 HR diff 已回归验证 |
| ⑥ Loop | ② 部分实现 | trigger 覆盖 cron 内核；缺模型自节奏 self-pace 链与 `/loop` 命令层；全量对齐清单见 §7.2-B |
| ⑥ 目标模式（/goal） | **基线=Codex `ext/goal`；Hive 半装配** | CC 无此功能（Fact）→ 权威基线转 Codex `/goal`（成熟自主目标循环）；Hive 数据模型逐字段对应但三处致命断点（完成回写桥缺/自主循环被双重阻断/记账空壳）；九件对齐清单见 §7.2-A |

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
- **B2 ✅ 已闭环** hints 区恒渲染空：dynamic suffix builder 仅在存在 actionable skill/tool/subagent hint 时写 `dynamic:activation:hints` ledger，Personal KB 继续走 `kb_hint` 旁路，不再渲染空 hints 段。
- **B3** router→memory 读桥死：plane_read.load_selected_memory_values（:228）零调用。
- **B4 ✅ 已闭环** memory 候选生产者全死：`retrieve_candidates` / `gather_t2_evidence_candidates` / `gather_t3_plane_candidates` / `gather_explicit_overlay_candidates` 已从 `MemoryRetriever` 退役；图谱与 `rg` 均确认删后无剩余符号/引用。
- **B5** K 侧 activation_keys 仍未承载 `MemoryRetriever.retrieve()`：B4 死读桥已删，`reference_index.py` 的 activation_keys 表仍作为 derived index / source-ref / repair script 合约存在；若要继续收缩，必须单独评估并迁移这些索引测试与维护脚本，而不是随 dead gather 一并误删。
- **B6-B8** tool/subagent 候选生产者死；qkv trace 构建器零调用；两个 credit 环只写不读（工具 activation events 零读者 hooks_setup.py:775；heat_delta/decay_signal 无消费者 session_feedback.py:202）。
- **B9 ✅ 已闭环** 原始 activation_events T0 truth-surface 泄漏：T0 boundary 只保留 `activation_feedback_summary`，原始 `activation_events` 已在 seal 前剥离；非 web 默认路径 open segment 不再把原始 router telemetry 写入 `memory/t0/.../events.jsonl`。

### 2.3 AI-Native 四问判决

hard mask（policy/acl/sensitivity/budget，activation_router.py:280-333）= 约束授权范围 = **L2 合法**。scoring（_multi_head_score:181-206）= 机械词重叠替代"哪些记忆相关"= **L1 领域**；更糟：semantic head（权重 0.4 最大）作用于恒空 concepts → **40% 打分权重永远为 0**，且 router 会用空-concept 词重叠分覆盖 KB 原本 FTS 相关分（:353）。当前因"非承载"被临时掩盖——**接线那天即 case-law 级 L1 违规**（对标 compaction `[-40:]` 案）。注：K 侧 activation_keys 是 LLM 在 T2 build 时撰写的（t2/prompts.py:109），这一侧是 AI-native 的。

### 2.4 处置建议 → **已拍板（2026-07-09）：精确退役 + 演进为 Dynamic Recall Layer**

原"要么接活要么退役"的二选一已有结论：**不接活、精确退役机械层、演进为新层**。完整删/留清单与执行顺序见 **§7.3（主线 2）**；此处只留已闭环的两项存档。

- **S1** ✅ hints 死注入已删（P1-1 空 hints shrink，prompt_builder + activation_hints）。
- **S2** ✅ dead gather 已删（P1-8）；剩余 activation_keys 投影退役并入 §7.3.1 Q-shrink（精确删投影、留 reference_counts 反向索引，删前后 M0 eval 分数应不变作自验门）。**不再接活**——机械 scoring 让位给 §7.3.2 的统一激活方程（ActivationScorer 本体升级，非重建平行层）。
- **S4（治理级）✅ 已闭环**：T0 seal 前 strip 原始 activation_events（只留 summary）堵 B9；policy dict 不再作为原始 activation event 进入 T0 truth surface。
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

- **B1 ✅ 已闭环** `search_personal_kb` capability gate 拒绝：已注册 `search_personal_kb -> agent.knowledge.read`，并补 capability audit / governance surface 测试；STRICT_CAPABILITY_MAPPING 下不再 fail-closed 拒绝。
- **B2 ✅ deterministic 部分已闭环 / ⚠️ UX 异步增强未做**：`process_import_jobs` 已能批量消费 queued/failed `KnowledgeIndexJob`；聊天上传新增 `HIVE_CHAT_UPLOAD_MAX_BYTES` / `HIVE_CHAT_IMAGE_UPLOAD_MAX_BYTES` 硬上限。剩余是产品体验层的后台 worker/队列化上传增强，不再是“无消费入口/无上限”的上线硬断点。
- **B3 ✅ 已闭环** owner 搜自己 KB 被 `agent_searchable` 误过滤：owner search statement 已去掉 `agent_searchable` 过滤；agent/非 owner 搜索仍保留该过滤。
- **B4 ✅ 已闭环** 自主态无自动授权：`ensure_agent_identity()` 会在 agent 有 tenant + owner/creator 时幂等种 `KnowledgeGrant(scope_type="person", resource_type="scope", grantee_type="agent", permission="search")`，heartbeat/trigger（user_id=None）可沿既有 agent grant 谓词读取 owner-scope Personal KB；无 tenant/owner 的旧对象不动。

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

### 6.2 目标模式（/goal）：权威基线 = Codex `ext/goal` crate（2026-07-09 修订）

CC 两基线无此命名功能（穷举 grep 零真命中，判定不变）。**Owner 澄清：目标模式即 `/goal`，权威基线转 Codex 源码。** codex-rs 考证结论（完整报告 scratchpad/goal-mode-codex-vs-hive-alignment.md，全部 file:line 可复核）：

**Codex `/goal` = 成熟的自主目标循环**，七维语义：
1. **入口**：`/goal set/clear/edit/pause/resume`（tui/slash_command.rs:122 "set or view the goal for a long-running task"，任务运行中可用）+ 三个 agent 工具 `get_goal`/`create_goal`/`update_goal`（ext/goal/src/spec.rs:9-11）。
2. **存储**：codex_state per-thread 单 goal，跨 session 持久（tool.rs:454-465）；`ThreadGoal{objective≤4000 chars, status, token_budget, tokens_used, time_used_seconds}`（protocol.rs:4019）；超大 objective 物化为 goal-objective.md 文件让模型 Read（tui/goal_files.rs）。
3. **注入**：隐藏 `<codex_internal_context source=goal>`，事件驱动非常驻（internal_model_context.rs:7；steering.rs:49-54）。
4. **驱动（关键）**：thread idle → continue_if_idle → try_start_turn_if_idle **自主循环直到终态，无次数上限**（runtime.rs:359-394；extension.rs:154-167）；turn_error→Blocked 防烧 token（extension.rs:305-312）。
5. **完成判定 = 模型自判 + `update_goal` 工具回写**（tool.rs:221-291）；5,261 字节 continuation.md 提示词含 Completion/Blocked/Fidelity 三个审计段。
6. **记账**：全链 token/time 真接线，预算耗尽 → BudgetLimited + steering（accounting.rs；extension.rs:326-403）；六态机 Active/Paused/Blocked/UsageLimited/BudgetLimited/Complete（protocol.rs:3993）。
7. **终止/恢复**：clear/complete/blocked/usage/pause；session resume 恢复 Active goal（runtime.rs:335-357）。

**与 CC /loop 的关系（Owner 问询结论）**：不是一回事，是正交两轴——/loop 是**时间调度轴**（何时再执行，无"完成"概念，终止是时间性的），/goal 是**目标驱动轴**（朝什么工作 + idle 自续跑直到目标终态，终止是状态性的）。Hive 映射恰好也是两套：trigger ↔ loop，Goal Mode ↔ goal，分开对齐不合并。

**Hive Goal Mode 对照判决：半装配脚手架**——数据模型逐字段对应 Codex ThreadGoal（objective/status/token_budget 甚至命名一致，"Codex-inspired" 是字面移植），should_continue_goal 与每-turn-一次续跑真接线，但**三处致命断点**使它尚不是真正的目标模式：
- **断点①完成回写桥缺失【最严重·L1 违规】**：agent 仅有 goal_start 工具，无 update_goal——模型说"目标完成了"没有任何路径写 status=complete，只剩确定性 terminal_reason 映射 + 用户手动 goal_stop。
- **断点②自主循环被双重阻断**：`completed_task_type != web_chat_turn` 拒 + `source == goal_continuation` 拒（goal_continuation_service.py:184,190-191）→ 每用户 turn 最多推进一步，`max_continuation_turns > 1` 永远达不到。
- **断点③记账空壳**：`tokens_used` 全仓零写入点，account_goal_tokens / mark_goal_blocked_if_repeated / objective_updated steering 全为死代码 → token_budget/BUDGET_LIMITED/time_budget 全是死字段；唯一真递增的是 continuation_count。另有 agent goal_start 落库桥断（requires_api_persist 无服务层消费者——agent 自主调 goal_start 实际不落库）。
- 对齐维（平台形态差异不算缺口）：存储（DB vs state_db 语义等价）、注入（都是续跑时注入非常驻）。合法 Hive delta 保留：多租户治理/RLS/CANCELLED 态/decision ledger/续跑不绕 T0——以及 **max_continuation_turns 硬上限 + Runtime Budget Control Plane**（Codex 本地无次数上限，Hive 多租户 Web 必须有 per-goal 熔断）。

### 6.3 Loop 的 Hive 对照

**Loop ② 部分实现**：trigger 系统（set_trigger cron/once/interval/poll/on_message/webhook/event_wait，triggers.py:21；trigger_daemon._should_fire:730-774）覆盖"周期触发"内核，且有 CC 没有的 failure_policy/preflight/restart-safe/budget daemon。三缺口：①**上下文延续语义相反**——CC 塞当前 session 队列，Hive 每次 fire 另起新 invocation/child_session（trigger_daemon.py:427）；②无模型自节奏 self-pace 工具（ScheduleWakeup/Sleep/tick 全仓零命中）；③无 `/loop` 自然语言命令层（docs/freecode-command-loop-feature-parity-audit-2026-06-22.md 自认）。全量对齐项见 §7.2-B。

---

## 7. 全量完成对齐清单（2026-07-09 重构）

> **Owner 指令（2026-07-09）：取消 P1/P2 分层——全部对齐项必须一次做完，不分期、不留债（一次改完纪律）。** 本节是唯一执行清单。条目间只有**执行依赖顺序**（G 前置），没有优先级分期；每条附验收判据。

### 7.1 已闭环存档

首轮 P0-1..P1-11 已于 2026-07-08 闭环 @ dcda97812（逐项验证见 §0.1，独立复核见 §0.2）：KB capability gate / QKV T0 泄漏 / HR 红测+模板 v5 / QKV 空 hints+死 gather / KB B3-B4 / Plugin trust gate 下半场（revoke/reject/supersede）/ RTD 读面接前端 / codex 死 append 删。全量 5513 passed。§0.2 两项残留（process_import_jobs 孤儿、K 侧写投影）滚入 §7.2-E。

### 7.2 全量对齐清单（全部必做）

**A. 目标模式（/goal）全对齐 — 基线 Codex `ext/goal`（九件，依据 §6.2 考证）**
| # | 项 | Hive 落点 | 验收判据 |
|---|---|---|---|
| A1 | `update_goal` agent 工具（模型自判完成回写桥，含 status/objective/summary 更新） | command_parity.py + 服务层落库；**必注册 CAPABILITY_MAP**（防重演 KB B1 STRICT 拒） | agent 在续跑中调 update_goal(status=complete) → DB status=complete + 循环停 + decision ledger 记录；governance 集成测试过 |
| A2 | continuation prompt 升 benchmark 质量 | prompts/goals.py:42（现 3 行）→ 对标 Codex 5,261 字节 continuation.md 的 Completion/Blocked/Fidelity 三审计段，vendor-neutral | prompt 含三审计段；prompt contract 测试钉住结构 |
| A3 | 打通自主续跑循环（idle→continue 直到终态） | 放开 goal_continuation_service.py:184,190-191 双重阻断，改用 continuation_count < max_continuation_turns 边界 | max_continuation_turns=5 的 goal 单用户 turn 后自主推进 5 步；每步过 budget guard；测试钉多步与熔断双路径 |
| A4 | token/time 记账接活 | account_goal_tokens 去死代码，invocation 结束累加 tokens_used/time_used_seconds | 续跑后 tokens_used>0；超 token_budget → status=BUDGET_LIMITED + 循环停 |
| A5 | `get_goal` agent 工具 | 同 A1 落点 | agent 可读当前 goal 状态与剩余预算 |
| A6 | 连续失败 → Blocked（对标 turn_error→Blocked 防烧） | mark_goal_blocked_if_repeated 去死代码，接 turn 终态 | 3 连败 → status=blocked + 循环停 + 审计记录 |
| A7 | objective 更新 steering（改目标后下轮注入更新提示） | objective_updated prompt 去死代码接注入链 | goal_update 后下一续跑 prompt 含新 objective steering |
| A8 | session resume 后 Active goal 恢复续跑资格 | 对标 runtime.rs:335-357；web chat 重连/RuntimeTask 恢复路径 | 重启后 Active goal 的下一 turn 仍触发续跑 |
| A9 | 修 agent goal_start 落库桥 | requires_api_persist 无服务层消费者——agent 自主调 goal_start 实际不落库 | agent 调 goal_start → DB 有行 + 前端可见 |
| 六态机 | status 枚举补齐 Active/Paused/Blocked/UsageLimited/BudgetLimited/Complete（+Hive 合法 CANCELLED） | models/agent_session_goal.py | 状态流转测试全覆盖 |

**B. Loop 全对齐 — 基线 CC /loop 两代（四件，依据 §6.1/6.3）**
| # | 项 | 验收判据 |
|---|---|---|
| B1 | `/loop` 命令层（命令面/agent 工具双入口的自然语言薄封装，落到 trigger） | `/loop 5m <prompt>` 创建 interval trigger 并立即先跑一次（对齐 loop.ts:67）；省略 interval 走 B2 |
| B2 | 模型自节奏 self-pace（ScheduleWakeup 等价工具：模型自定下次唤醒延迟 + stop 终止） | agent 可调 schedule_wakeup(delay,prompt)/stop；延迟受 clamp；每轮 prompt 回传续跑；受 budget plane 熔断 |
| B3 | 同 session 续跑模式（loop fire 可选注入当前 chat session 队列而非恒起新 invocation，对齐 CC cron 塞队列语义） | trigger 新增 delivery=same_session 选项；fire 时消息入该 session 下一 turn；REPL-busy 时排队不并发 |
| B4 | Loop 与 Goal 组合语义（loop 醒来时若有 Active goal，续跑走 goal 循环而非裸 prompt） | 同 session 内 loop+goal 并存时行为有测试钉死 |

**C. Plugin CC 市场全对齐 — 基线 FreeCode schemas.ts（六件，依据 §3）**
| # | 项 | 验收判据 |
|---|---|---|
| C1 | marketplace.json 索引解析（含官方市场仿冒/homograph 防护） | 真实 CC marketplace 仓库可被索引并列出插件 |
| C2 | plugin source 拉取（github/git/npm/url/file/directory） | 六种 source 各有集成测试；经 trust gate 评审后物化 |
| C3 | materialize 物化层 + `${CLAUDE_PLUGIN_ROOT}` 替换 | 插件文件落 workspace，hooks/mcp 中变量被替换为真实路径 |
| C4 | cc_plugin_adapter 接入运行时导入入口（孤儿转正） | 一个真实 CC 格式插件 repo 端到端：import→review→approve→activate→agent 真用起来（E2E 测试） |
| C5 | 格式矩阵补缺：manifest 字段（commands 内联 content/agents/skills/hooks 三形态/mcpServers path 形态）、目录+manifest 合并语义、userConfig `${user_config.KEY}` 替换、dependencies | §3.2 矩阵 ❌/⚠️ 项全转 ✅，各有解析测试 |
| C6 | command/hook 组件激活接线（去 fail-closed unsupported）或经 owner 确认标注永久 unsupported 并从 catalog 隐藏 | 装了的组件要么能用要么不出现在"已安装"列表 |

**D. Codex 工程 delta 完工（三件，依据 §1.3）**
| # | 项 | 验收判据 |
|---|---|---|
| D1 | SandboxProfile 四档 + network_access + writable_roots 真接进 subprocess_sandbox builder | 四档各有行为级测试（read_only 拒写/workspace_write 只许 writable_roots/网络开关生效）；Railway 仍锁 vercel_sandbox |
| D2 | 单命令 shell 升权流（被沙箱命令按需一次性申请升权，经审批） | 升权走 governance 审批；审批后仅该命令放行；审计记录 |
| D3 | 声明式 execpolicy 命令规则引擎替代 governance.py:232 手写危险 regex | 规则声明式可配；现有危险命令用例全过；结果仍喂 capability gate |

**E. 复核残留闭环（两件，依据 §0.2）**
| # | 项 | 验收判据 |
|---|---|---|
| E1 | process_import_jobs 接真实调度（daemon tick 或 upload 后异步派发），聊天上传管线真异步化——或明确改设计为同步有界并删孤儿方法 | 大文档上传即刻返回、后台完成索引；或方法删除 + spec §4 改写。禁止"方法存在无调用者"现状 |
| E2 | ~~QKV K 侧 activation_keys 写投影处置~~ → **已拍板并入 §7.3.1 Q-shrink**（精确删投影、留 reference_counts 反向索引） | 见 §7.3.1 删/留表 |

**F. 瘦身（一批，依据 §1.4）**
~1,990 LOC test-only 死模块 ×10 退役（连带测试）；台账工厂 7 文件合并 ~645→300L；薄文件内联；assembly-state 持久化字段裁剪（§0.2 DB bloat）。**注**：external_capabilities 三文件（cc_plugin_adapter/codex_plugin_adapter/context_projection）因 C4 接线**保留**，从退役清单移除。验收：全量测试绿 + 死模块 grep 零残留。

**G. 执行依赖（顺序约束，非分期）—— 两条并行主线 + 一个交汇点**

`docs/runtime-budget-control-plane-plan-2026-07-03.md` 的 reservation + circuit breaker **必须先落地**，然后 A3（goal 自主循环）与 B2（self-pace）才能放开——安全门不是 MVP 分期。

工作分成两条可**并行**推进的主线（互不阻塞，只在 M7↔A7 一处交汇）：

- **主线 1 — CC/Codex 全量对齐（§7.2 A-F）**：`G → A → B`；`C / D / F` 三组相互独立可并行；`E1`（Personal KB 异步化）独立。
- **主线 2 — Hive-native 记忆演进（§7.3）**：`Q-shrink（精确退役）→ M0（eval 红测锚基线，删 QKV 前后分数应不变）→ {M1-M3 BaseLevel 线 ∥ M4-M5 ContextBoost 线} → M6 填 goal_terms → M8 消费契约兑现`。
- **交汇点**：主线 2 的 `M7`（TaskModulation 智能级 attention set）依赖主线 1 的 `A7`（goal objective steering），共用一条链——A 系列落地后 M7 才收尾。

主线 2 的 M0-M6/M8 **不依赖 G**（dynamic recall 是确定性零 LLM，不烧钱），故可与主线 1 从头并行；唯一等待是 M7 尾随 A。整体收敛序：`G` 启动主线 1，`Q-shrink` 同时启动主线 2，两线并进，A7 完成时回收 M7，最后 F 瘦身收尾。

### 7.3 主线 2：Hive-native 记忆演进（已拍板 2026-07-09）

QKV 走向已拍板：**不接活、精确退役机械层，并演进为一层新的 Dynamic Recall Layer**（设计文档 `docs/dynamic-memory-activation-design-2026-07-09.md`）。这不是"整洁问题"，是一条与 §7.2 CC/Codex 对齐清单**并行的 Hive-native 记忆能力主线**。设计律：**Attention is for recall/ranking, not for truth**——T0/T2/T3/Memory Gate 仍独占真相，本层只决定"这一轮优先唤起什么"。

#### 7.3.1 QKV 精确退役（Q-shrink）——外科手术，不是一刀切

QKV 表面是 4 文件 1,269 行，但里面缠着被 KB/subagent/skill/T2-retention **复用的通用载体**与新方案要用的 **feedback 契约**。无脑全删会误伤一大片。精确删/留清单（全部 grep 证据）：

| 组件 | 行数 | 处置 | 依据（file:line） |
|---|---|---|---|
| `activation_router.py`（multi-head overlap scoring） | 380 | **删整个** | 唯一生产调用点 invoker.py:411 是"算了不用"死路径（record 后 :423 return 原始 candidates） |
| `activation_query.py` 机械 regex parser + 死 LLM seam | ~397 | **删机械部分** | invoker.py:304 + turn_envelope.py:293 同属死路径链；concepts 恒空 |
| `activation_candidates.py` 死 gather + `ActivationHardMask` | 部分 | **删** | P1-8 已删部分；HardMask 仅 router 用 |
| `reference_index.py` `activation_keys` 表投影（写侧 DROP+CREATE+INSERT + 读侧 `query_activation_keys`） | 部分 | **删** | 读侧 grep 零生产读者，纯只写不读 |
| — 分隔线：以下**保留**，删时误伤即回退 — | | | |
| `activation_candidates.py` 的 `ActivationCandidate/ActivationScore/ActivationSurface` | 保留 | **留**（宜搬中性模块） | KB(kb_candidates.py:14)/subagent(subagent_listing.py:13)/skill(skill_catalog_ranker.py:10) 三处复用 |
| `reference_index.py` 的 `reference_counts`（source_ref 反向索引） | 保留 | **留** | t2_retention.py:71 保留策略 + personal KB 在用，非 QKV 私有 |
| `activation_events.py`（`ActivationFeedback` heat_delta/decay_signal 契约） | 207 | **全留** | M3 FeedbackCredit 的原料契约；S4 已把它挡在 T0 truth surface 之外 |
| `ActivationScorer`（activation.py） | — | **留并升级** | 统一激活方程的宿主（不重建） |
| PPR/relation_graph、access_log/lifecycle、T2 activation_keys（LLM 撰写） | — | **留** | 分别是 ContextBoost/BaseLevel/K 的原料 |

净删约 700-800 行（router 全 + query 机械 + activation_keys 投影），而非 1,269 行全删。原 §7.2-E2 并入此表（不再是"随 G 拍板"）。

#### 7.3.2 Dynamic Recall Layer（H 组 = M0-M8）

统一激活方程 `Activation = Relevance(RRF,不动) × ContextBoost(W_t→PPR) × BaseLevel(频率+幂律衰减+credit) × TaskModulation(goal)`。**分件明细、验收判据、消费契约、W_t ACL 边界全部在设计文档，本文不复制**（避免双轨漂移）。摘要：M0 eval 红测门先行 → M1-M3 BaseLevel 线 ∥ M4-M5 ContextBoost 线 → M6 首次填 goal_terms → M7 智能级(依赖 A 系列) → M8 消费契约兑现(修 invoker.py:423 + score_trace)。

#### 7.3.3 关键自验门（删 QKV 与建新层的接缝）

**删 Q-shrink 前后，M0 的纯 RRF eval 分数应当不变**——因为审计结论是"QKV 非承载"（hints 恒空、router 算了不用）。若删后分数不变 → 实锤 QKV 确实没在承载，退役安全；若分数掉了 → 说明它其实在某处承载，**立即回退并重查**。这把"删旧"和"建新"用同一把 eval 尺子接上了缝。

---

## 8. 审计方法与证据

六路后台 agent 并行（部分自带子 agent 三层展开），全程 Grep/Read/Bash 直查源码 + 执行级验证（audit_capability_mapping() 实跑、测试套件实跑：external_capability 19 passed、HR 相关 70 passed、最终全后端 5513 passed / 206 skipped）；FreeCode/claude-code-org/codex-rs 三基线交叉；每结论 file:line 可复核。2026-07-09 增补：Codex `ext/goal` crate 专项考证（七维生命周期 + Hive 逐维对照，报告 scratchpad/goal-mode-codex-vs-hive-alignment.md）支撑 §6.2 与 §7.2-A。详细分报告：QKV 完整版存于审计 agent scratchpad（qkv-audit-final-report.md），其余以本文档为准。
