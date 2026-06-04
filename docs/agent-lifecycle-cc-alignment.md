# Agent 生命周期全面对标 Claude Code — 调查报告（讨论稿）

> 2026-06-04。方法：5 个并行调查 agent 覆盖 CC 核心机制详解 doc 的十个方面（压缩=第一章已单独完成，见 `compaction-cc-alignment.md`），主 agent 按 AI-Native Design Law 四问镜头校准汇总。
> **本报告是讨论稿——只诊断不动手。** 个别判定标 ⚠️ 表示主 agent 已校准 subagent 的误判；标 🔍 表示建议实施前再核实。
> 评判镜头 = CLAUDE.md「AI-Native Design Law」四问：①输入视野完整 ②输出预算充足 ③提示词达基准质量 ④机械只做可观测兜底。
> CC 三大设计支点（doc 十二章）作为参照系：cache 第一性 / **软约束 > 硬约束** / isMeta 统一注入总线。

## 0. 执行摘要

**总体结论：Hive 的基础设施和提示词工程多处反超 CC，但「机械决策替代 LLM 决策」作为系统性模式出现在 4 个核心位置——这是压缩问题（[-40:] 截断）的同构复现，证实了把 AI-native 定为最高法律的必要性。**

| 方面（CC doc 章） | 总体判定 | 一句话 |
|---|---|---|
| 一、压缩 | ✅ 已修复 | P0-P3 全对齐（compaction-cc-alignment.md） |
| 二、系统提示词注入 | 🟢 大体对齐+部分反超 | 14 sections/防注入/cache 边界强；裁剪静默是真 gap |
| 三、Plan Mode | 🟢 核心反超 | agent 主循环规划唯一路径+DB canonical+fail-closed 纵深 > CC；激活告知是 gap |
| 四、DR 与多 Agent 编排 | 🔴→⏸️ 冻结 | planner 纯模板、LLM 零参与维度拆解；synthesis prompt 反而 > CC。**2026-06-04 用户拍板：DR 整体重做、管道迁移 workflow，本轮不处理（见 §1 A1 标记）** |
| 五、自主模式 | 🔴 核心违规 | proactive 决策由 preflight 规则机械做；缺统一自主语义框架 |
| 六、Session 启动 | 🟢 对齐 | server 形态天然差异；invoker 串行构建可并行化（小项） |
| 七、Query 主循环 | 🟡 部分 | loop guard 一刀切；流式不边流边执行；frozen prefix 缓存反超 CC |
| 八、工具执行与权限 | 🟡 部分 | 拒绝消息无教学文本（直击③）；hook allow 无二次检查（安全）；企业审批反超 |
| 九、子 Agent | 🟡 部分 | "派生完整员工" vs "spawn 轻量 worker" 概念混淆；worker prompt > CC |
| 十、配置/Skill | 🟡 部分 | catalog 超预算整体失明；无条件激活/fork 隔离；按需加载哲学本身更 AI-native |
| 十一、记忆进阶 | 🟡 部分 | 激活评分纯机械权重；heartbeat 输入截断（压缩同款问题）；蒸馏 prompt 远超 CC |

## 1. 系统性主题（跨方面的同构问题）

### 主题 A：机械决策替代 LLM 决策（L1 直接违规，4 处）🔴

压缩的 `[-40:]` 修掉了，但同一模式在四处复现：

| # | 位置 | 现状 | 证据 | CC 对照 |
|---|---|---|---|---|
| A1 | **DR 研究规划** ⏸️ | depth→lane 固定模板映射，LLM 零参与维度拆解；注释自认 "LLM-assisted planning can be layered on later" | `services/deep_research/planner.py:18-55` | coordinator prompt 驱动，agent 自主拆维度（doc §4.2：四阶段是 prompt 不是状态机） |

> ⏸️ **A1 冻结（2026-06-04 用户拍板）**：Deep Research 全部内容将整体重做、管道迁移到 workflow（P14 入口壳已就位，leaf 能力下沉时一并按 AI-native 法律设计——LLM 拆维度、四问全过）。本轮不修旧 planner。其余 DR 专属 gap（worker 并行特化代码、digest 600 字上限等）同此冻结；**非 DR 专属**的 subagent/workflow 通用项（D6 fan-out token 池、D7 轻量 worker、D8 异步重入）不受影响，仍走轴 1/轴 2 排期。
| A2 | **Proactive 自主决策** | ✅ **已修复（2026-06-04）**：preflight 从"决策者"降为"边界提供者"——DO-cleared 候选**不再由系统自动 fire signal**，而是作为决策输入进 heartbeat prompt（"Cleared — your call" + preflight 评估 + 边界），agent 在自己的 run 里判断是否值得做、怎么做；治理保持系统所有（ASK/ESCALATE 的 checkpoint 自动创建=外向动作需人审、REFUSE 边界 enforce）。markdown 头部明示"judgment is yours, within boundaries (enforced regardless)"。符合"plan 来自 agent、治理归系统"。**证据**：`services/proactive_employee_loop.py`（DO 分支 sentinel 注册+fire 删除、markdown 决策输入式重写）；tests 语义反转 DO 测试（emissions==[]/无 signal/决策框架断言），checkpoint/REFUSE 测试保留；全量 3697 passed。附带：`evals/self_evolution_bakeoff.py` 特征匹配从锁定 `timeout_seconds=1.5` 改为语义匹配（A3 调超时不再破坏证据检查） | 自主决策交给模型，代码只搭管道 |
| A3 | **记忆激活评分** | ✅ **已修复-轻档（2026-06-04）**：实施中发现比诊断更严重——`_rerank_semantic_items` 是孤儿函数、`retrieve()` 的 `rerank_model_config` 是 dead parameter，**LLM rerank 生产路径从未运行**（激活 100% 机械）。修复=①接线：semantic 池 > 阈值（5）时 LLM rerank 真实运行于 retrieve() 路径（替换 semantic 子集，机械序为 fallback）②候选预览 150→400 chars（reranker 判断语义不是标题）③超时 1.5→3.0s ④降级 debug→warning+`memory_rerank_fallback` metric。**证据**：`memory/retriever.py`；tests/memory/test_retriever_rerank_wiring.py +2（接线/小池跳过），memory+memory_service 248 passed。重档（embedding）与 memory-claude-mem-borrow 计划合并另行 | 超越机会 |
| A4 | **Loop Guard 一刀切** | ✅ **已修复（2026-06-04）**：warn-before-abort——首次到阈值返回 `severity="warn"` 诊断（含模式详情 + 三条自纠指引：intentional 申明/换方法/停止重试报告错误），同 pattern 只警告一次；计数继续涨到 abort 阈值（warn×1.5：identical 5→8、failure 4→6、text 3→5、total 100→150、failed 12→18）才硬停。**证据**：`kernel/loop_guard.py` 重构（`_escalate`/`_PatternCheck`/`_WARN_GUIDANCE`），engine 4 个消费点 warn 注入 system 消息+`loop_guard_warning` 事件继续循环（`engine.py` `_inject_loop_guard_warning`）；tests/kernel/test_loop_guard.py 9 passed（含 warn→去重→abort 升级 3 个新测试），kernel+runtime 599 passed | 软约束哲学：stop hook blockingError 把"还没干完"拼回 messages 让模型继续 |

> CC doc 12.2 的原话：「把大量行为决策**外包给模型的指令遵循能力**，代码只负责搭管道」。A1/A2 是把本该 LLM 做的判断写死成了规则；A4 是有了硬约束却没配软约束前置。

### 主题 B：拒绝/警告类反馈缺教学意图（四问③，4 处）🔴

CC 哲学：每次拒绝都是教学机会——告诉模型为什么 + 下一步怎么调整。Hive 的对应反馈全是终态字符串：

| # | 位置 | 现状 | 证据 | 修法方向 |
|---|---|---|---|---|
| B1 | **工具拒绝消息** | ✅ **已修复（2026-06-04）**：新增 `_teaching_block_message` 模板（工具名+原因+capability+zone+next steps），改造 5 类消息——public zone block、capability denied（普通+dangerous 两处）、delegation token rejected、approval required（含 capability+approval ID+等待期间可做什么）。**证据**：`tools/governance.py` `_teaching_block_message` + 5 处替换；tests/tools/test_governance.py 断言改为教学要素式（"What you can do instead"/"Meanwhile you can"），tools+architecture 261 passed、kernel+runtime 599 passed | 模板化：工具名+触发的 capability+zone+建议 |
| B2 | **轮次压力警告** | ✅ **已修复（2026-06-04）**：抽出纯函数 `_build_round_pressure_warning`，80%/最后 2 轮警告携带实际数据——已用轮数/总轮数、工具调用总数、失败数、上下文 token 估算（loop_guard 计数 + estimate_tokens_from_chars），原 Objective Ledger 指引保留。**证据**：`kernel/engine.py`；tests/kernel/test_round_pressure_warning.py 2 passed，kernel 全量 119 passed | 注入实际数据像 CC 的 token budget nudge |
| B3 | **Plan Mode 激活告知** | ✅ **已修复（2026-06-04）**：新增 `_plan_mode_activation_notice`（被拦工具名+intent+为什么进入+下一步 exit_plan_mode+禁止直接重试），两个拦截激活点在激活成功后立即注入该 system 消息。**证据**：`kernel/engine.py`；tests/kernel/test_plan_mode_reminder.py +2 测试（命名被拦动作/sparse 兜底），kernel+tools 380 passed | 激活时注入说明消息 |
| B4 | **自主模式语义框架缺失** | ✅ **已修复（2026-06-04，二次修正）**：`build_dynamic_prompt_suffix` 新增 `source` 参数注入 `## Autonomous Work` 段（wake context/bias toward action/authority unchanged/pacing/state recording）——CC `# Autonomous work` 的 Hive 化；kernel 5 个调用点接线。**⚠️ 用户纠正（同日）**：首版错误地把 heartbeat 也纳入注入集合——heartbeat 是蒸馏器（T2→T3 curation，"librarian"），语义已由 identity heartbeat 模板 + HEARTBEAT.md SOP 完整覆盖且 SOP 明文禁止外向动作，通用段的 "bias toward action"/"external via plan/checkpoint" 对它既重复又矛盾。修正=`_AUTONOMOUS_SOURCES` 仅 {trigger}，heartbeat 测试语义反转为 omits。**证据**：`runtime/prompt_builder.py`；tests/runtime/test_prompt_builder.py（trigger 注入/heartbeat 不注入/live chat 不注入），runtime+kernel+heartbeat 639 passed | 按 source 特化，不是同一段文本 |

### 主题 C：输入视野截断（四问①，压缩 [-40:] 的残余同款，3 处）🟡

| # | 位置 | 现状 | 证据 |
|---|---|---|---|
| C1 | **heartbeat 输入截断** | ✅ **已修复（2026-06-04）**：核实后违规点是"无条件 per-message 24K 截断"（总量未超 80K 预算也剪；截断标记本身已可观测）。修复=full-fidelity 快速路径：总量 ≤ 预算时零截断原样返回，超预算才进入 per-message cap → compact → defensive pass（同压缩 P0 哲学）。**证据**：`services/heartbeat.py` `_compact_heartbeat_runtime_messages`；tests/services/test_heartbeat.py 语义反转 1 测试（单条 72K<80K 总预算→不截）+ 超预算保护测试，29 passed | `services/heartbeat.py` |
| C2 | **skill catalog 超预算整体丢弃** | ✅ **已修复（2026-06-04）**：核实后 `registry.render_catalog` 本身已有三级降级（full→truncated→names-only，对齐 CC）；真 gap 在 `_enforce_frozen_prefix_budget` 层——修复=永不失明：leftover<200 时放最低可见性路标（"skills are still available: call load_skill…"）而非静默丢弃；trimmed catalog 尾部加路标（"more skills exist: list skills/ or load_skill…"）。**证据**：`runtime/prompt_builder.py` `_CATALOG_OMITTED_NOTICE`/`_CATALOG_TRIMMED_SUFFIX`；tests/runtime/test_prompt_builder.py +2，runtime 487 passed | `runtime/prompt_builder.py` |
| C3 | **裁剪静默** | ✅ **已修复（2026-06-04）**：核实 frozen prefix tail-trim 已有 notice；补齐三处——①memory snapshot 裁剪标记带取回路径（"use search_memory to retrieve more"）②`_trim_block` 通用 marker 从裸 `...` 改为 "(trimmed to fit context budget)" 且 **marker 计入预算**（调用方尺寸契约严格成立）③active tool groups 截断加短 marker。**证据**：`prompt_sections/memory.py`、`prompt_builder.py` `_TRIM_MARKER`、`prompt_sections/active_tool_groups.py`；tests +2（marker 路标/预算契约），runtime+kernel 610 passed | — |

### 主题 D：能力缺口（真 gap，但非法律违规）🟡

| # | 缺口 | 证据/对照 | 备注 |
|---|---|---|---|
| D1 | 流式不边流边执行 + 并发上限 4（CC 10） | 🟡 **部分完成（2026-06-04）**：并发上限 4→10 已对齐 CC（仅 parallel-safe 工具进并发批，上界关乎 API 压力而非安全；**证据**：`engine.py` `_PARALLEL_SEMAPHORE_LIMIT=10`，test_parallel_tool_batch.py 峰值断言随常量自适应，kernel 121 passed）。**边流边执行未做**：StreamingToolExecutor 等价物需重构 `_stream_with_cancel` 流式架构（流中解析 tool_use+即时执行+保序回灌），属架构级大项需单独设计，非本轮 patch 范围 | 性能项 |
| ~~D2~~ | ~~hook allow 后无二次 deny 检查~~ | **✅ 验证为误判（2026-06-04）**：实际顺序 = kernel PRE_TOOL_USE hook（engine.py:388-399，可改 args/block）→ execute_tool → ToolRuntimeService.execute 内部跑完整 governance（service.py:283-298，使用 hook 修改后的 effective_args）→ 执行。hook 不 block 时 governance 必然执行，CC 的"hook 不能绕过 deny"天然成立。subagent 混淆了 engine.py hook 行号与 governance.py:388-396（capability gate fail-closed 块） | 无需修复 |
| D3 | 权限无 mode 概念（acceptEdits/dontAsk/auto） | governance 全固定流程 | 与 L3 中台的 per-agent 策略可结合设计 |
| D4 | approval 同步阻塞 vs CC 异步竞速 | `governance.py:515-538` | 企业审批本就该等；但可探索"先继续别的工作" |
| D5 | skill 无 paths 条件激活、无 fork/inline 隔离执行 | `skills/parser.py` 无对应字段 | CC 的条件激活本身是机械常态路径（有讽刺性）——Hive 的 load_skill 显式调用反而更 AI-native，但完全没有自动提示也损失体验 |
| D6 | subagent fan-out 无 token 池分配 | `orchestrator.py:592-622` 仅 Semaphore | 轴 1 P4/轴 2 已排期 |
| D7 | "派生完整员工" vs "spawn 轻量 worker" 概念混淆 | `orchestrator.py:640-680` | 轴 1 P2-P3 已排期 |
| D8 | 子 agent 完成无自动重入（Signal 未接通 delegation） | `orchestrator.py:1237-1311` | 轴 1 P4 已排期 |
| D9 | 团队/组织级共享记忆缺失（CC TEAMMEM 对照） | — | L3 中台场景的合理需求，可另立项 |
| D10 | 操作者可维护的分层指令文件（CC CLAUDE.md 三级级联对照）⚠️ | — | 校准：CLI 的文件级联不适用，但"公司级→部门级→agent 级 instructions"是中台等价物，值得讨论 |

### ⚠️ 已校准的 subagent 误判（不计入 gap）

- ~~"Git status 注入缺失 🔴"~~ → Hive 非 CLI 编码工具；底层洞察（环境实时状态感知）已部分由 focus.md/objective 投影覆盖，必要性待讨论
- ~~"信任对话关卡缺失 🔴"~~ → Hive 有 approval flow + Checkpoint + ActionPreflight，覆盖等价场景
- ~~"Headless 支持缺失 🔴"~~ → Hive 天然 server/headless
- 🔍 "auto_dream 实际程序化不走 LLM" → 与已知事实冲突（dream 有 LLM consolidation 路径 + soft dream 机械维护双轨），实施前核实

## 2. Hive 反超清单（保持并继续投资）

| 项 | 证据 | CC 状态 |
|---|---|---|
| **提示词工程**：synthesis（5 禁止模式+evidence matrix）、delegated worker（4 good+3 bad examples）、HEARTBEAT.md/DREAM.md（decision matrix+反例）、EXTRACT_PROMPT | `reasoner.py:784-849`、`orchestrator.py:186-307`、templates/ | CC 的 consolidation prompt 是纯文本无反例 |
| frozen prefix 跨轮缓存 | `engine.py:1496-1576` | CC 每轮重建 system prompt |
| recovery_manifest 结构化恢复 | `engine.py:1671-1707` | CC 只有 transcript 文本 |
| Loop Guard 的存在本身 | `loop_guard.py` | CC 无显式防环（只有 round 上限） |
| T0 backfill replay（no-lose 提取） | `extract_agent.py:15-18` | CC 会话结束没跑就丢 |
| PL 密级 + principal_stack 激活隔离 | `write_gate.py`、`activation.py` | CC 仅 OAuth 粗粒度 |
| delegation token 能力绑定 | `orchestrator.py:160-166` | CC 仅工具白名单 |
| ActionPreflight 5 轴风险评估（机制本身；决策权归属是 A2） | `action_preflight.py` | CC 无企业审批 |
| Plan Mode：DB canonical + 5 层 fail-closed + agent-authored 唯一路径 | `agent_plan_requests`、plan-mode 文档 | CC 磁盘文件+权限模式 |
| workflow journal + replay + admission | workflow-source-capability §9 | CC 的 WorkflowTool 源码已删 |
| memory form 强制校验 / 四层金字塔 / 权重分桶 | `md_store.py`、`t2_store.py` | CC 格式靠建议 |

## 3. 修复路线建议（待讨论，未拍板）

**第一优先：主题 A（机械替代 LLM）**——与压缩同级的法律违规：
- ~~A1 DR planner~~ → ⏸️ 冻结：随 DR 整体重做时按 AI-native 法律设计（LLM 拆维度），本轮不动
- A2 proactive 决策 → preflight 从"决策者"降为"边界提供者"，DO/ASK/ESCALATE 判断交给 agent（喂给它 preflight 的 5 轴评估作为输入）
- A3 记忆激活 → 权重模型保留为初筛，加语义层（embedding 或放宽 LLM rerank 触发条件+超时）；与 memory-claude-mem-borrow 计划合并考虑
- A4 loop guard → 先软后硬：首次触发注入诊断文本让模型自纠，N 次后才 abort

**第二优先：主题 B（反馈教学化）**——纯提示词工程，低风险高收益，可一个 PR 打包：B1 拒绝消息模板 + B2 轮次警告带数据 + B3 plan 激活告知 + B4 统一自主语义段。

**第三优先：主题 C（视野截断）**——heartbeat 截断修复（同压缩 P0 手法：完整序列化+超窗才机械兜底）+ catalog 降级策略 + 裁剪标记。

**第四优先：主题 D 按依赖排**——D2（安全）应尽快；D6/D7/D8 已有轴 1/轴 2 排期；D3/D9/D10 需要产品决策。

## 3.6 蒸馏器专项核查（2026-06-04，用户提出 heartbeat≠trigger 后的追加核查）

背景：用户抓到 B4 把 Autonomous Work 段错误注入 heartbeat（修正于 ebf423aa——heartbeat 是蒸馏器非 worker，语义归 HEARTBEAT.md SOP）。随后对三个 SOP-driven 蒸馏器按四问全面核查：

| 蒸馏器 | LLM 接线核实 | 四问结论 | 行动 |
|---|---|---|---|
| **heartbeat**（T2→T3） | ✅ KAIROS persistent session 真跑 | SOP prompt 远超 CC；C1 已修总量截断；section caps（T2 24K/16K、T3 8K）为合理分段预算且截断带标记 | 无需再动 |
| **dream**（T3→soul） | ✅ `_dream_llm_consolidate` 主路径真接线（auto_dream.py:1286 Step 1，审计 agent 的"程序化"判定为**误判**）；降级有 `record_autonomous_llm_call` metric + audit event | 🔴 两处违规已修：①输入截断在主路径（T3 每文件 4K、soul 3K——consolidator 基于残片决定 soul promotion）→ **full-fidelity 优先**（总量 ≤48K chars 不截，超预算才 per-section cap + 带文件路径的可观测标记）②输出 `max_tokens` 3000→8000（决策 JSON 含 promotions/rewrites/dedups/reasoning，3000 饿死） | ✅ 已修 — **证据**：`services/auto_dream.py` `_DREAM_INPUT_TOTAL_BUDGET_CHARS`；tests/services/test_auto_dream.py 语义反转（5K soul+6K T3 总量内不截）+ 超预算保护测试，dream 全系 83 passed，全量 3703 passed |
| **extract**（T0→T2） | ✅ hot path LLM + pattern fallback | 增量场景（cursor-based，单次远不到 `max_messages=120`）+ T0 backfill 兜底；单条 2500/2000 截断为防御性合理；输出 1000 tokens 对 ≤8 条单行 T2 条目够用 | 定性合理，标注即可 |

附注：dream 的 Step 2（pattern-based feedback promotion）在 LLM 成功后仍 always-run（"safety net"，auto_dream.py:1304）——LLM 决策与机械 promotion 写同一 soul 的潜在重复属低风险（LLM 整理后 T3 重读 + promote 有重复阈值），观察项不修。

## 4. 附录：各方面调查详情

完整的逐方面判定表、四问检验、gap 清单见 5 个调查 agent 的原始输出（本文件为校准后汇总）。各方面的关键证据锚点：

- **提示词注入**：`runtime/prompt_builder.py`（三层架构+预算）、`prompt_sections/`（14 sections）、`runtime/context_engine.py`（context_block 包装）
- **Plan Mode**：`kernel/engine.py:879-1070`（reminder+激活）、`tools/handlers/plan_mode.py`（exit_plan_mode）、`docs/plan-mode-design.md`
- **DR/编排**：`services/deep_research/planner.py`（模板）、`reasoner.py:784-849`（synthesis prompt）、`workflow_definition.py`
- **自主模式**：`services/trigger_daemon.py`、`heartbeat.py`、`proactive_employee_loop.py`、`auto_dream.py`
- **主循环**：`kernel/engine.py:1432-2400`（handle+round loop+流式+PTL）
- **工具权限**：`tools/governance.py`（2 层+approval）、`core/capability_gate.py`（CAPABILITY_MAP）
- **子 Agent**：`agents/orchestrator.py`（delegate+profiles+prompt）、`subagent_definition.py`
- **配置/记忆**：`skills/`（parser/registry）、`memory/`（retriever/activation/write_gate）、`services/extract_agent.py`、templates/HEARTBEAT.md、DREAM.md
