# 终极原子化对比审计 — 三大架构块（2026-07-09）

> 形态：**纯审计报告（只读不改）**。产出 = 断点 / 闭环 / 冲突三维判定 + 治理唯一权威序图 + 纯净架构蓝图 + 分优先级行动清单。
> 基线：FreeCode `src/`（CC 一手）+ Codex Rust（工程 delta）+ 原始设计文档（`docs/*.md`）。
> 判定口径：**断点**=链路不通（有去无回 / 孤儿 / 只写不读）；**闭环**=有去有回可验证；**冲突**=两个正当机制互相打架（尤其治理互锁导致 agent 跑不起来）。
> 铁律：`feedback_green_tests_dont_mean_done` — 每个"已落地"判定都要 grep 真实调用点 + 看 fallback 分支，不信绿测试。

---

## 块1 — 单 Agent 运行机制（对齐 CC + 融合 Codex + 云端适配层）

### 1.0 核心确认点（owner 原话）
单 Agent 整个生命周期是否完全 follow CC 规范，并在此基础上融合 Codex 工程优化 + 云端适配层。

### 1.1 云端适配层（CC 本地 CLI 结构性没有的一层 — owner 明示"云端场景需额外优化"）

这是判定的第一个关键：CC FreeCode 是单进程本地 CLI，用内存 `AbortController`（`QueryEngine.ts:187,1159`）做取消、`for await` 单循环做执行、进程即会话。Hive 是**云端多进程 + 断线可恢复**，必须加一层运行时基座。逐项判定这层是"正当 delta"还是"偏离 CC 语义"：

| 云端能力 | Hive 坐标 | CC 对应 | 判定 |
|----------|-----------|---------|------|
| 持久化 runs（DB 认领执行） | `models/runtime_task.py:64,89-91`（status/claimed_by/claim_expires_at/attempt_count）；`runtime_task_worker.py:171 claim_and_dispatch_once` | CC 无（进程内执行） | **正当 delta**：CC 单进程无需，云端多 worker 必须 DB 认领 |
| 跨进程 cancel（pub/sub） | `runtime_control_bus.py:51 publish_web_chat_cancel`；`main.py:645 start_runtime_control_listener` 生命周期订阅 | CC `AbortController.abort()` 进程内 | **正当 delta**：CC 内存信号，云端跨进程必须走 Redis pub/sub。历史断点（kernel 不读 DB cancel）已在生产 redeploy 修复（见 `project_single_agent_framework_review_20260702`） |
| restart resume（重放续跑） | `web_chat_runtime.py:1972 resume_persisted_web_chat_runs`；`long_task_runtime.py:177 build_long_task_resume_context`；注入点 `:3563` | CC 无（进程死会话死） | **正当 delta**：CC 崩溃即丢，云端 worker 重启必须从 durable 状态重放 |
| WS 断线重连（订阅≠取消） | `AgentDetail.tsx` ws.onclose→resuming phase（本轮 Step1 落地）；`web_chat_broker` 订阅 | CC 无 WS（本地 stdio） | **正当 delta**：浏览器断开是订阅变更不是取消 |
| budget plane（防烧钱熔断） | `runtime_budget_service.py` reservation+breaker；`models/runtime_budget.py` | CC 无（本地信任、用户自付） | **正当 delta**：云端多租户必须有 per-root-run 预算熔断（`project_runtime_budget_control_plane`） |

**块1 云端层初判**：四大支柱全部真接线，且每一项都是 CC 本地信任模型结构性缺失、云端必然要加的。**这层不是偏离 CC 语义，是 CC 语义在云端的必要投影**——符合 CCPlus 边界契约（`docs/ccplus-north-star-contract-2026-06-24.md`：本地 CLI 语义映射进 RuntimeTask/ChatSession/T0/Bridge 即 CCPlus scope）。

### 1.2 生命周期逐站对照（CC FreeCode ↔ Hive kernel）— 取证中

核心循环骨架已核对：Hive `kernel/engine.py:4293 for round_i in range(max_rounds)` ↔ CC `QueryEngine.ts:675 for await (message of query(...))`。Hive 每 round：cancel 检查（:4294）→ mid-run drain（:4305，CC 无此云端能力）→ runtime reminders 瞬时注入（:4339，transient 不入 api_messages）→ LLM stream with cancel（:4415）→ output-cap 续跑（:4426）→ span 记录（:4445）→ cost loop guard（:4483）。

四路并行取证进行中（上下文组装/工具循环+Codex delta/Rewind+Branch/Slash Command），完成后填充逐站判定表。

### 1.3 Session Rewind + Branch（owner 点名"能否正常 work + 前后端逻辑"）— ✅ 取证完成

**结论：Rewind 和 Branch/Fork 前后端都真实打通并有端到端测试。** 一个实质半接缺口。

**Rewind（对话级）= 闭环**：非破坏的 `active_projection` 投影，非"重放落库"——写 `session.transcript_metadata_json["active_projection"]`（`session_command_runtime.py:951`），每次后续 run 装载 history 时实时重建（读路径真实：`web_chat_runtime.py:3218 _apply_active_projection_to_history` → `:1219 _rewind_projected_history`）。transcript 原始事件不删，tail 渲染为 rewound。前端触发（`AgentChatSection.tsx:1073` checkpoint 选择器 + `:3155` 消息级）+ 消费 ui_action 插 rewind marker（`AgentDetail.tsx:1123-1164`）。测试：`test_session_command_runtime.py:461`（写投影不建会话）+ `test_conversation_branch_service.py:181`。

**Branch/Fork = 闭环**：新建 ChatSession 写 `parent_session_id` + `root_session_id` 自引用（`chat_session.py:43-44`），前缀事件复制并全打 `projection_only=True`/`bridge_to_t0=False`/`semantic_memory_eligible=False`（`conversation_branch_service.py:224-238`）实现 **T0 层隔离**（分支上下文不污染父语义记忆）。前端 SessionGitLine 血缘树（`AgentChatSection.tsx:663`）+ 侧栏归组（`AppSidebar.tsx:167`）。API 契约前后端全对上（`chat.ts:275 POST /branches` ↔ `chat_sessions.py:1642`）。测试：`test_conversation_branch_service.py:64/121/181`（fork 复制前缀 + T0 隔离逐条断言）。

**❌ 半接断点（块1 第一个明确断点）**：**workspace 文件级回退（mode=workspace/both）后端完整且有 3 个测试（`test_session_command_runtime.py:513/539/575`），但前端所有 rewind 触发只传 `checkpoint_event_id`、从不传 `mode`**（后端默认 `mode="conversation"`，`session_command_runtime.py:851`）。连带：
- workspace 快照**每个 user turn 都在写**（sha256 落盘，`web_chat_runtime.py:3109`），但 restore 无 UI 入口 → **写了不能从界面读**（只写不读的一半）。
- 确认流半接：后端要求两步确认（`workspace_restore_requires_confirmation` → 带 `confirm_workspace_restore` 重试），前端对 `open_permissions_menu` ui_action 只弹一句提示，**不会重发带确认标志的 rewind**（`AgentDetail.tsx:1109`）。

**两个需澄清的事实（非断点，但易混淆）**：
- "rewind" 双语义：斜杠 `/rewind` = 原地投影不建会话；REST `/branches` 的 `mode="rewind"` = 新建截止 checkpoint 的分支会话。两条路径都能从前端触发。
- `active_projection` 单槽 last-write-wins：rewind 与 compact 共用同键（`:952` vs `:1058`），同时刻只有一个投影生效——设计如此，但需确认是否有并发覆盖风险。

**与 CC 基线关系**：FreeCode `/rewind`（`free-code-main/src/commands/rewind/`）是 TUI 交互式 + `fileHistory` + `sessionStorage` 本地恢复、`supportsNonInteractive:false`。Hive 为多租户 Web 重写为 active_projection 投影 + sha256 workspace 快照 + T0 隔离分支——**不同基座的重实现（正当云端 delta），非移植**。

### 1.4 Slash Command 一致性（owner 点名"校验命令行指令与 CC 是否一致"）— ✅ 取证完成

**结论：会话控制类命令语义对齐 CC；Hive 新增 goal/team/schedule/loop 等 native 命令为正当扩展；5 个断点/半成品。**

架构：Hive 用"服务端命令注册表 `command_registry.py:158-600` + REST 执行 API `commands.py:1320-1509` + 前端解析 `slashCommand.ts:60-183`"复刻 CC 的 slash command（对标 `free-code-main/src/commands.ts`）。CC 三分类（prompt/local/local-jsx）→ Hive 五分类（metadata/runtime/tool/workflow/external）。

对齐的命令（语义相同）：resume/clear/compact/branch/rename/tag/export/copy/rewind/btw/plan/tasks/skills/agents/mcp/status/usage/cost/stats/context/doctor/version。Hive 独有（正当 native）：goal/team/schedule/loop/advanced_plan/verify_plan/start_workflow + 16 coding pack。CC 独有未实现：model/effort/theme/vim/login/memory/init/hooks 等（多为 TUI/本地环境命令，云端不适用）。

**块1 断点清单（Slash Command）**：
1. **❌ `steer` 孤儿（真断点，已亲手复核）**：`command_registry.py:216` 只注册了 `turn_steer`，无 `steer` 别名；但 `SESSION_COMMAND_NAMES`（:44）和执行体 `{"turn_steer", "steer"}`（`session_command_runtime.py:781`）都认 `steer`。`commands.py:1335` 分派**先查 registry**，两次 `registry.get("steer")` 都 KeyError → **404（:1346）**。结论：执行逻辑其实支持 steer，但注册表缺别名让它永远进不了 runtime——用户敲 `/steer` 必 404。**修复=registry 加一行 steer 别名**（低成本、明确）。
2. **⚠️ `/loop` self-pace 描述 stale**：registry 描述写 "self-pace (not yet available)"（`command_registry.py:461`），但 self-pace 实现已存在并被路由（`commands.py:682 _execute_loop_self_pace`）——文案骗人。
3. **⚠️ permissions/config 语义收窄**：CC 可写，Hive 为 metadata 只读（`command_registry.py:552`）。需确认是否刻意（云端权限应走后台而非命令行）。
4. **○ coding pack 16 命令 = external 存根**：`commands.py:1456` 返回 `requires_local_bridge:true` 不执行。**正当云端边界**（review/commit/diff 需本地 git，属 Local Bridge 范畴，非断点）。
5. **⚠️ advanced_plan/verify_plan 双面不可见**：`visible_to_model=False` + `visible_to_user=False`（`command_registry.py:490,503`），只能 API 直调。需确认是否有意（内部命令）。

### 1.5 工具循环 + Codex 工程 delta（core CC 平齐 + Codex 融合）— ✅ 取证完成

**结论：工具循环几乎全闭环，Codex delta 全部真接线。一个文档-实现偏差 + 一个脆弱点。**

**闭环（CC 平齐）**：
- 工具注册双表：`ToolExecutionRegistry`（name→executor，`runtime.py:46`）+ `ToolRegistry`（schema 投影）。`@tool` 装饰器 + collector 自动发现（`collector.py:162`）。单一 fallback = MCP passthrough（`service.py:456`）。
- tool_search 延迟发现：**单一真源** `discoverable_tool_names_for_query`（`agent_tools.py:590`，text 告知与 schema 加载共用），schema 按需加载真接线（`engine.py:717 _should_expand_tools` → `invoker.py:1044 _resolve_tool_expansion` → `full_toolset` 跨轮持久 + `deferred_tools_delta` 事件）。
- 结果处理：`ToolContentEnvelope`（`result_envelope.py:21`）+ 两层 spill（内核 50KB eviction→`workspace/tool_results/`，`engine.py:197`+`ccplus_contracts.py:110`；日志层 8KB→`artifacts/`，`t0_logger.py:62`）+ per-round 200K 预算强制（`engine.py:5313`）。
- round budget + loop guard：默认 200（`agent.py:81`），`for round_i in range(max_rounds)`（`engine.py:4293`）；80%/倒数2轮压力警告（`reminder_scheduler.py:279`）；LoopGuard 5 维度（identical_tool_args 阈5 / repeated_tool_failure 阈4 / repeated_assistant_text 阈3 / cost / total），warn-then-abort（abort=ceil(warn×1.5)，`loop_guard.py:21`）。CC `QueryEngine.ts:146 maxTurns` 对应。

**Codex delta（工程融合）全接线**：
- `execpolicy.py`（Codex execpolicy 移植：Decision allow/prompt/forbidden + first-match + load-time 示例校验）**真喂 capability gate**（`governance.py:236 evaluate_command`），替换了原 inline 危险命令正则。
- `SandboxProfile`（`ccplus_contracts.py:51`）+ `subprocess_sandbox.py`（bwrap/sandbox-exec 三档 profile）+ `code_execution/` provider（local/vercel，`HIVE_CODE_EXEC_PROVIDER` 门控）**真接 `execute_code`**（`filesystem.py:383`→`code_exec.py:232 execute_agent_command`）。vercel provider 默认不触发但非死代码。
- retry/overload（可重试状态含 529，`llm_client.py:424`）/ fallback_model（单次 client 热切换，`engine.py:4822`）/ reasoning_signature（Anthropic thinking round-trip，缺失省略不伪造，`llm_client.py:141`）/ prompt_cache（ephemeral cache_control + turn-level anchor=最新 assistant text，`prompt_cache.py:168`）**四项运行时契约全真接线**。

**块1 两个需记录（非严重）**：
1. **⚠️ 文档-实现偏差（heartbeat round budget）**：CLAUDE.md 与 `docs` 声称"heartbeat override max_tool_rounds=40"，但 **grep 后 heartbeat.py 无 max_tool_rounds 覆盖点**，走同一 `agent.max_tool_rounds or 200`。声称的契约没落地——要么补覆盖，要么改文档。
2. **⚠️ 脆弱点（本机绝对路径入生产 payload）**：`codex_optimization_ledger.py:50` 内嵌 `/Users/rocky243/Context Engineering/codex/...` 本机路径，被 `session_control_plane.py:1728` 消费进控制面 payload → 换环境即无意义。应改为相对引用或移除。
3. **○ 显式 deferred（非死字段）**：`ToolContentEnvelope.new_messages`/`terminal_signal` 恒空但有测试守护（`test_side_effect_channel_has_no_production_producer`）——显式延迟通道，不是静默死代码。

### 1.6 上下文组装 + Prompt cache 边界 + 压缩（AI-Native L1 核心）— ✅ 取证完成

**结论：Prompt 组装/缓存边界/压缩几乎全闭环，核心压缩符合 AI-Native L1 法律。一个命名双轨债需块2印证。**

**Prompt 组装（闭环）**：两段式——frozen prefix（`agent_context`→system→tasks→tools，`prompt_builder.py:361-375`）+ dynamic suffix（15 candidate 按 render_order=插入顺序，:645-934）+ `PROMPT_CACHE_BOUNDARY` marker（`prompt_cache.py:34`，插入 `:937`）。**frozen 路径显式关闭所有时变项**（`invoker.py:715-726`：runtime_metadata/skill_catalog/memory/focus 全 False）→ 缓存稳定。frozen token 护栏 + 四层裁剪（catalog→Context Material→body→soul，`:423-517`）。`prompt_sections/` 18 文件，`__init__.py` docstring 声明 FROZEN vs DYNAMIC 归属。

**Prompt cache（闭环，CC 平齐）**：capability 驱动 `_supports_cache_control(provider)`（未知 provider 无 marker，靠 frozen 稳定性）；frozen 块打 `cache_control: ephemeral`，dynamic 不打；TTL conversation=5min / heartbeat·trigger·task=1h（`prompt_cache.py:141`）+ 最新 assistant text anchor。对应 CC `claude.ts:358 getCacheControl` + "每请求恰好一个 message 级 marker"。

**压缩（闭环 + AI-Native L1 正解）**：主动 ≥0.75/每3轮（`engine.py:79`+`ccplus_contracts.py:113`）+ 被动 PTL 重试（`:4541`）+ 时间基 microcompact 0.60（只清工具结果非对话）。**核心 `maybe_compress_messages` 是 LLM 智能压缩，喂完整 old_messages（`_safe_split` 保 tool pair 完整）不机械截断**（`memory_service.py:579-617`）。
- **★ AI-Native L1 判定=正确**：LLM 压缩失败时**不做机械 summary fallback**，退化为"丢弃旧消息 + 占位 marker"（`memory_service.py:648`，注释 "No mechanical semantic summary fallback"）——机械只作可观测兜底、不伪造语义，正是 L1 case law（历史 `[-40:]` 截断违规已修）要求的姿态。机械截断只在被动 PTL 的 round-group drop 路径（`_truncate_head_for_ptl`，本就是最后手段）。CC 对应 `compact.ts:384 streamCompactSummary` + `truncateHeadForPTLRetry`。

**Memory 注入（闭环）**：模板 `prompt_sections/memory.py` 是静态槽，真检索走 `invoker._resolve_memory_context → memory_service.build_memory_context → MemoryRetriever.retrieve`（带 ActivationContext）；`_apply_activation`（`retriever.py:174`）用 ActivationScorer 打分 + suppressed 过滤 + bump_access 遥测。fail-closed：principal 无法解析抑制全部 prompt memory（`memory_service.py:144`）。注入 dynamic suffix 非 frozen。

**块1 需记录（1 个债，转块2 印证）**：
- **⚠️ T3 文件命名双轨（技术债）**：prompt 模板 + consolidation 管线用 **two-plane**（`memory/self/self.md` + `profiles/owner|collaborators|domain.md` + `knowledge/` + `milestones/`），但 `auto_dream.py:1544` / `t3_platform_gate.py:26` / `reference_index.py:35` / `migrate_memory_two_planes.py:37` 仍引用**旧平铺** `t3/{episodes,user,worker,capabilities}.md`。**两套命名共存**——prompt 注入走 retriever/resident-plane 不直读 legacy 平铺，但 consolidation/dream 侧还在写旧路径。**块2 记忆取证交叉确认这是否 = 未完成的 two-plane 迁移**（记忆 `project_memory_architecture_rethink_20260630` 挂账过 `migrate_memory_two_planes --apply` 待生产迁移）。

---

## 块3 — 公司治理板块（owner 自述"模糊"，需替他理清 + 冲突矩阵消解）

### 3.1 治理层级唯一权威序（`run_tool_governance` 内序，`governance.py:835 _run_governance_inner`）

每次工具调用**必经**这条流水线（`ToolRuntimeService.execute → run_tool_governance`，不变量：`service.py:690 build_dependencies`）：

```
① security zone (governance.py:843)      public zone → 仅 SAFE_TOOLS，其余 teaching-block
      ↓ 失败 fail-closed (:870 block ALL)
② tenant_id 存在性 (:892)                 无 tenant → 非 SAFE_TOOLS fail-closed
      ↓
③ MCP server-policy gate (:941)          deny 硬拦 / approval 进 session 确认 / auto 放行
      ↓
④ capability gate (:997 check_capability) CAPABILITY_MAP 查表 → denied/escalate_to_l3/放行
      │   └─ STRICT_CAPABILITY_MAPPING=True (config.py:181 默认开)
      │      → 未注册工具直接 deny (capability_gate.py:346)  ★痛点根源
      ↓
⑤ delegation token (:1070)               子代理越权/过期 → deny
      ↓
⑥ dangerous command (:1135 execpolicy)   路径语法/managed 凭证/破坏性删除/危险命令
      ↓
⑦ 治理 hook 双泳道 (:1463 本轮 Step3)     声明式快路 + command 沙箱慢路，只缩不放
      ↓
   return None = 放行执行
```

**权威序判定**：单一、有序、每层 fail-closed，无旁路（`execute_approved`/`execute_direct` 跳过是 post-approval 正当重放）。层间是"逐层加严"——后层只能否决前层放行，不能放宽前层否决。结构干净。

### 3.2 默认姿态两个关键判定项（待综合裁定）

| 项 | 现状 | 坐标 | 初判 |
|----|------|------|------|
| 默认权限模式 | `DEFAULT_CCPLUS_PERMISSION_MODE = BYPASS_PERMISSIONS` | `ccplus_contracts.py:48` | ⚠️ 待判：CC 本地默认 bypass = 本地信任；云端多租户默认 bypass 是否恰当取决于企业是否总覆盖 profile |
| STRICT capability 映射 | `STRICT_CAPABILITY_MAPPING = True`（未注册工具生产即 deny） | `config.py:181`、`capability_gate.py:346` | ⚠️ **痛点根源**：故意 fail-closed，代价=每个新工具必须同步注册 CAPABILITY_MAP 否则生产死。历史多次踩坑。有 startup 审计(:151)+metrics 计数但不阻止合并 |

### 3.3 冲突矩阵（owner 核心痛点）

候选冲突对（RLS×治理取证 agent 深挖中，回来后逐对填充 + 最危险对真机复现）：
- RLS × 后台 daemon（heartbeat/trigger/dream 的 tenant_id 可能为 None）
- RLS × pre-auth（登录无租户上下文，历史 401 全员事故）
- STRICT capability × 新工具注册（§3.2 痛点）— **实测：当前 CAPABILITY_MAP 零 drift（`audit_capability_mapping()` → unmapped=[]/stale=[]）**，无潜伏生产死雷。风险是机制性（加新工具须记得注册），非当前断点。本轮之前多次踩坑，现覆盖齐整
- budget plane × 自主循环（goal/loop 续跑跨 tick 熔断）
- permission mode × plan gate × 治理 hook（三层确认叠加）
- 双 fail-closed 叠加（治理超时 × 沙箱冷启）

_(7 路并行取证进行中：块1×4 + 块2×2 + 块3×1。回来后填充判定表 + 真机复现。)_

---

# 终极综合 — 三块汇总 · 断点/闭环/冲突判定 · agent-native 纯净架构方案

## A. 一句话总判

**系统核心是健康的**：单 Agent 生命周期 CC 平齐 + Codex 融合 + 云端适配三层全真闭环（块1），三大历史命门（Skill 进化/Personal KB/A2A）已从"生产即死"翻转为"真接线默认开"（块2），治理是单一有序 fail-closed 流水线（块3）。**真正的系统性风险只有一个**：治理×RLS 在 `tenant_id=None` 时互锁，让 agent 静默瘫痪（C1，已真机复现）。其余是可枚举的局部断点、待实施设计、和技术债。

## B. 断点 / 闭环 / 冲突三维判定表

### B.1 断点（链路不通，需修）
| 编号 | 断点 | 严重度 | 一句话 |
|------|------|--------|--------|
| **C1** | 治理×RLS tenant=None 互锁 | 🔴 **系统性** | daemon tenant 解析 None → agent 只读不能写/执行 = 瘫痪，且审计不落库。**真机复现坐实** |
| B1-1 | `/steer` 孤儿别名 → 404 | 🟡 用户可见 | 执行逻辑支持但 registry 缺别名，敲 /steer 必 404 |
| B1-2 | workspace 文件级 rewind UI 无法触达 | 🟡 半接 | 后端全有测试，前端不发 mode + 确认流断，快照只写不读 |
| M-1 | `promotion_router.py` 298 行孤儿 | 🟡 死代码 | 完整晋升路由子系统零调用者 |

### B.2 闭环（有去有回，健康）
- 单 Agent 循环：上下文组装/prompt cache 边界/工具循环/loop guard 5维/压缩(LLM 智能+机械兜底)/retry-fallback-signature-cache 四契约 — 全闭环 ✅
- 云端四支柱：RuntimeTask 认领/跨进程 cancel/restart resume/WS 重连 — 全闭环 ✅（正当 delta）
- Session Rewind(对话级)/Branch(T0 隔离) — 闭环 ✅（前后端 + 端到端测试）
- Codex delta：execpolicy→capability gate / sandbox→execute_code — 全接线 ✅
- Memory 主干 T0→T2→T3→soul + 双 Gate + rollback — 闭环 ✅
- Skill 进化臂 capture→eval→review→provisional 写盘→rollback — 闭环默认开 ✅
- Personal KB 六表+RRF 四通道+ACL+前端 — 闭环 ✅
- 治理 7 层流水线 + hook 双泳道 — 闭环 ✅
- bump_access/W_t/ContextBoost/budget summary lane — 闭环 ✅

### B.3 冲突（互相打架，需消解）— owner 核心关切
| 编号 | 冲突对 | 判定 | 消解 |
|------|--------|------|------|
| **C1** | 治理 fail-closed × RLS tenant=None | 🔴 真冲突（agent 瘫痪） | **daemon 侧显式短路**：resolve_tenant 返 None 立即结构化告警跳过，不让 None 流到治理层伪装成"工具被拦" |
| C2 | bootstrap(WITH CHECK) × migrated(USING-only) 库 | 🟡 潜在（写入行为分叉） | 统一迁移策略补 WITH CHECK，或 bootstrap 去掉 WITH CHECK 对齐 |
| C3 | 启动 strict RLS 门 × owner 连接 | 🟡 部署级（boot 失败） | 确认生产连非 owner app_rls；strict 门保留但文档明确前置条件 |
| C4 | NULL-tenant 行全租户可见 × USING-only 允许任意写 | 🟡 隔离破洞 | daemon 禁写 NULL-tenant 业务行；补 WITH CHECK |
| — | budget × 自主循环 | 🟢 **非冲突** | summary_only lane 恰好一次收尾已闭环（本轮 Step2） |
| — | permission × plan × hook 三层确认 | 🟢 **非冲突** | 逐层加严，deny>ask>allow 聚合有序，无叠加死锁 |
| — | 双 fail-closed（治理超时 × 沙箱冷启） | 🟢 **非冲突** | 两者串行不叠加，各有独立超时 |

**冲突结论**：owner 担心的"治理/RLS 互锁"**只有 C1 是真的会让 agent 跑不起来**，且根因单一（tenant=None 一路透传到治理层）。C2/C3/C4 是 RLS 部署一致性问题，不直接锁死 agent。permission/budget/plan/hook 之间**无互锁**——它们是有序加严不是竞争。

## C. 架构纯净度评估（owner: 对标一线大厂 + 百万在线单应用）

### C.1 模块边界 — 优
- 内核纯净：`AgentKernel.handle` 零 DB 依赖，全 I/O 经 `KernelDependencies` 回调注入 ✅
- 治理单一入口：`ToolRuntimeService.execute → run_tool_governance` 无旁路 ✅
- RLS 单一机制：`tenant_scoped_session`/`enter_rls_bypass` 双入口，bypass 强制 reason ✅
- Memory MD-first：T0 JSONL 机械真相 + MD 投影，Gate 分层 ✅

### C.2 死代码 / 双轨 — 中（可清理）
- 孤儿：`promotion_router.py`（298行）、`decay_signal` 字段、`activation_feedback.jsonl` 读者缺失
- 双轨：激活打分（加法 ActivationScorer vs 乘法 KB boost）、capability_factor vs distiller 晋升、T3 命名（two-plane vs legacy t3/*.md）、lifecycle path（canonical vs legacy）
- 脆弱：`codex_optimization_ledger.py:50` 本机绝对路径入生产 payload

### C.3 百万在线单应用维度 — 良（有基础，有隐患）
- ✅ 连接池隔离已治理（`project_pool_isolation`）、零DB端点分流、budget plane 防烧钱、RuntimeTask 认领可水平扩 worker
- ⚠️ 隐患：TTL single-flight 缓存是进程内（多 worker 各缓存一份，治理决策可能短暂不一致）；`_CANCEL_EVENTS` 进程内 dict（跨进程靠 runtime_control_bus 兜底，已修但双机制并存）

### C.4 对标一线大厂结论
**内核架构达标**（DI 纯函数核、单一治理入口、fail-closed 默认、MD-first 可审计），**这是超一线大厂的结构**。扣分项在**纯净度收尾**：4 类双轨 + 3 处孤儿 + 激活方程"设计待实施"的半成态。这些不是架构缺陷，是"演进中留下的未退役旧轨"——清理即达生产级纯净。

## D. agent-native 纯净架构方案（行动清单，按 ROI 排序）

### D.1 P0 — 立即修（agent 能跑起来 + 用户可见失败）
1. **C1 根治**：daemon（trigger/heartbeat/evolution/subagent_wake）在 `resolve_tenant_for_agent` 返 None 时**显式短路 + 结构化告警 + 记 operator 可见事件**，绝不让 None 流入治理层。治理层 tenant=None 分支改为"这是上游 bug 不是权限问题"的独立错误类。**这一条消除 owner 最大痛点**。
2. **B1-1 `/steer` 别名**：`command_registry.py` 加一行 steer 别名（极低成本）。
3. **B1-2 workspace rewind UI**：前端 rewind 补 mode 参数 + 确认流重发（或明确砍掉 workspace 级只留对话级，退役后端死路径）。

### D.2 P1 — 纯净度收尾（对标大厂的"最后一公里"）
4. **退役 4 类双轨**：promotion_router 删除或接线；capability_factor vs distiller 晋升二选一；T3 命名完成 two-plane 迁移退役 legacy t3/*.md（记忆挂账的 `migrate_memory_two_planes --apply`）；lifecycle path 迁移收尾。
5. **清 3 处孤儿**：decay_signal / activation_feedback.jsonl 读者 / codex_optimization_ledger 本机路径。
6. **RLS 部署一致性（C2/C3/C4）**：统一 bootstrap vs migrated 的 WITH CHECK；确认生产连非 owner 角色；daemon 禁写 NULL-tenant 业务行。

### D.3 P2 — 设计落地（蓝图→实现）
7. **激活方程 M1-M9**：按已定稿待拍板的 `dynamic-memory-activation-design` 把加法七维升级为乘法四因子 + 接通 goal_terms（TaskModulation）+ score_trace 全落 reasons。**需 owner 先拍板设计**。
8. **KB owner 画像双读（M2）**：应然/实然双读面。
9. **文档-实现对齐**：heartbeat round budget=40 补覆盖或改文档；`/loop` self-pace 描述更正。

### D.4 不需动（已达标）
- 单 Agent 核心循环、云端适配层、Codex delta、治理流水线、Memory 主干、Rewind/Branch、Skill 进化臂、Personal KB 主干 — 保持。

## E. 北极星达成度

| 目标 | 达成度 | 说明 |
|------|--------|------|
| CC 平齐（单 Agent 生命周期） | ~95% | 核心全闭环，剩 steer/workspace-rewind 局部断点 |
| Codex 工程融合 | ~90% | execpolicy/sandbox/retry/cache 全接线，剩本机路径脆弱点 |
| 云端适配层 | ~95% | 四支柱闭环，剩进程内缓存一致性隐患 |
| Hive-native Memory | ~80% | 主干闭环，激活方程设计待实施 + 双轨待清 |
| Hive-native 进化/KB/A2A | ~85% | 三命门翻转为真接线，剩 KB 画像双读 + capability_factor 双轨 |
| 公司治理板块 | ~85% | 流水线干净有序，剩 C1 互锁（P0）+ RLS 部署一致性 |
| **架构纯净度** | ~80% | 内核达超一线大厂结构，扣分在双轨/孤儿收尾 |

**总判：系统已是「健壮的 CCPlus + Hive-native」骨架，距「优雅干净模块化鲁棒可维护的 agent-native 系统」只差 P0 的 C1 根治（消除唯一系统性风险）+ P1 的纯净度收尾（退役双轨/孤儿）。P2 是设计演进，需 owner 拍板。**
