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
| A2 | **Proactive 自主决策** | DO/PREPARE_ONLY/ASK/ESCALATE/REFUSE 由 `preflight_service.evaluate()` 规则引擎决定，agent 不参与判断 | `services/proactive_employee_loop.py:68-108` | 自主决策交给模型（Autonomous work prompt + bias toward action），代码只搭管道 |
| A3 | **记忆激活评分** | 纯机械：keyword 重叠 + 固定权重（goal 0.25/owner 0.2/…），内容语义零参与；LLM rerank 仅候选>5 且 1.5s 超时即降级 | `memory/activation.py:39-74`、`memory/retriever.py:86-93` | （CC 此处也弱——这是超越机会而非对齐项） |
| A4 | **Loop Guard 一刀切** | 触发即硬 abort，模型收不到"你陷入了什么模式"的诊断，没有自纠机会 | `runtime/loop_guard.py:64-116`、`kernel/engine.py:1605-1634` | 软约束哲学：stop hook blockingError 把"还没干完"拼回 messages 让模型继续 |

> CC doc 12.2 的原话：「把大量行为决策**外包给模型的指令遵循能力**，代码只负责搭管道」。A1/A2 是把本该 LLM 做的判断写死成了规则；A4 是有了硬约束却没配软约束前置。

### 主题 B：拒绝/警告类反馈缺教学意图（四问③，4 处）🔴

CC 哲学：每次拒绝都是教学机会——告诉模型为什么 + 下一步怎么调整。Hive 的对应反馈全是终态字符串：

| # | 位置 | 现状 | 证据 | 修法方向 |
|---|---|---|---|---|
| B1 | **工具拒绝消息** | `"🔒 Tool blocked"` / `"🚫 Capability denied: {reason}"`，无 capability 名/安全区/next step | `tools/governance.py:196-233, 289-314` | 模板化：工具名+触发的 capability+zone+建议（request_approval/load_skill/换路径） |
| B2 | **轮次压力警告** | 硬编码"80% 轮数/剩 2 轮"里程碑，无数据 | `kernel/engine.py:1823-1847` | 注入实际数据（已调 N 工具/M 字符结果/预算余量）像 CC 的 token budget nudge |
| B3 | **Plan Mode 激活告知** | tool-intercept 自动激活时 agent 体验是"突然只读了"，没有"你刚才要做 X，系统要求先规划"的显式消息 | `kernel/engine.py:1016-1070` | 激活时注入一条说明消息（含被拦的 action artifact） |
| B4 | **自主模式语义框架缺失** | trigger context 只有 `Trigger: name\nReason: reason` 三行；heartbeat/trigger/proactive 各自为政，没有 CC `# Autonomous work` 那种统一段（pacing/first wake-up/bias toward action/terminal focus） | `services/trigger_daemon.py:977-1000` | source∈{trigger,heartbeat} 时统一注入自主工作语义段 |

### 主题 C：输入视野截断（四问①，压缩 [-40:] 的残余同款，3 处）🟡

| # | 位置 | 现状 | 证据 |
|---|---|---|---|
| C1 | **heartbeat 输入截断** | `_cap_heartbeat_message` 把喂给 curator 的消息截到 24K chars（head+tail），中段丢失——curator 基于残片做 T2→T3 决策 | `services/heartbeat.py:113-120` |
| C2 | **skill catalog 超预算整体丢弃** | frozen prefix 超 56K chars 时 catalog 全删，模型不知道有什么 skill 可用（应降级为"名字+描述"+提示用 load_skill） | `runtime/prompt_builder.py:217-262` |
| C3 | **裁剪静默** | memory 60% 预算裁剪、frozen prefix 删 section 都无"(truncated — 用 X 工具取全量)"标记 | `prompt_builder.py:265-277` 一带 |

### 主题 D：能力缺口（真 gap，但非法律违规）🟡

| # | 缺口 | 证据/对照 | 备注 |
|---|---|---|---|
| D1 | 流式不边流边执行 + 并发上限 4（CC 10） | `engine.py:1870-2400, :346` vs CC StreamingToolExecutor | 性能项 |
| D2 | hook allow 后无二次 deny 检查 | `governance.py:388-396` vs CC toolHooks.ts:325-326 | **安全项**——CC 明确 hook 不能绕过 deny |
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
