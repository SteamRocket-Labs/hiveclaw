# 运行时动态引导体系：机制 × 提示词（对标 CC attachment/reminder 层）

> 状态:**v0.1 审计定稿**(2026-06-05)。源码实证:CC `utils/attachments.ts` / `utils/messages.ts` / `constants/prompts.ts`;Hive `kernel/engine.py`。
> 关系:`docs/execution-mode-spectrum.md` 管 CC 引导体系的前两层——暴露架构 + 静态引导(§5 七原语决策序列、工具描述互指,T2 ✅ 已落地);**本文档管第三层:运行中的动态引导(reminder 注入)**。三层一起构成完整的"关键节点给模型判断信息"的体系。
> 流程(用户拍板,2026-06-05):**机制对齐 → 提示词对齐 → 全面 CC 对标**。本文档按此分 §5 三阶段路线。

---

## 0. 主旨与边界

CC 的引导体系是三层结构:

| 层 | CC 做法 | Hive 状态 |
|---|---|---|
| 静态系统提示 | 刻意做轻:`# Using your tools` 四条(dedicated-over-Bash 映射、任务管理一句话、并行引导、Agent 一句 when-to-use),选择哲学下放 | ✅ T2 落地(决策序列比 CC 总纲重,有意 delta,见 §4 P5) |
| 工具描述 | when-to-use / when-NOT / 互指网 | ✅ T2 对齐 |
| **动态 reminder** | **40+ attachment 类型,事件驱动,节流注入** | ⚠️ 机制存在(2026-06-03 切口②)但**从未对标过 CC 的注入哲学**——本文档主题 |

本文档回答三问:① Hive 动态引导**机制自身**有什么缺陷(§3,不依赖 CC 视角);② 提示词与 CC 的差距(§4);③ 对齐路线(§5)。

**边界**:`runtime/hooks.py` 的 15 事件总线是内部管线(记忆蒸馏/治理),不是 prompt 注入通道,不混入本议题;CC 的 file-watch/diagnostics 类 attachment 依赖 IDE/LSP 设施,在 §5 T-G3 登记但不在前两阶段。

---

## 1. CC 基线(源码实证)

### 1.1 attachment 管线

- **40+ attachment 类型**(`attachments.ts:296-611`),关键类:`todo_reminder`/`task_reminder`(任务工具)、`plan_mode`/`plan_mode_reentry`/`plan_mode_exit`、`critical_system_reminder`、`task_status`(任务状态变化)、`skill_listing`/`skill_discovery`、`nested_memory`/`relevant_memories`、`diagnostics`(lint/类型错误)、`edited_text_file`(外部文件变更)、hook 系×9。
- **注入形态**:attachment 渲染为 isMeta user message 包在 `<system-reminder>` 里(`wrapMessagesInSystemReminder`),**进入消息历史持久存在**——只追加不重排,天然 cache 安全;历史可回看,节流计数靠扫描历史实现。

### 1.2 task_reminder 解剖(本议题对齐锚点)

`attachments.ts:254,3212-3320` + `messages.ts:3685-3698`:

1. **双冷却节流**:`TURNS_SINCE_WRITE=10` + `TURNS_BETWEEN_REMINDERS=10`——最近 10 个 assistant 轮没用任务工具才提醒,两次提醒之间再隔 ≥10 轮。**不是每轮注入**。
2. **行为推断 gate**:倒扫消息历史数"距上次 TodoWrite 几轮"——从模型行为倒推要不要提醒,**不预判任务复杂度**。
3. **可用性/冲突让位**:任务工具不在 tools 数组 → 不提醒;Brief(更高优先通信通道)在场 → 整个 nag 让位,避免引导冲突(`#20467`)。
4. **状态快照**:reminder 自带当前任务清单(`#id. [status] subject` 逐行),模型零额外调用看到现状。
5. **防护句×2**:"This is just a **gentle reminder - ignore if not applicable**" + "Make sure that you **NEVER mention this reminder to the user**"。语气全程建议式(consider / if relevant)。
6. plan_mode attachment 节流:`TURNS_BETWEEN_ATTACHMENTS=5`。

### 1.3 哲学总结

CC 把动态 reminder 当**低频、事件驱动、带状态、可忽略**的旁路信号——核心引导住在工具描述里,reminder 只是"你好像忘了"的轻推。频率失控的重复文本会变成壁纸(模型学会忽略),所以节流不是省 token 的小优化,是**提醒有效性**的前提。

---

## 2. Hive 现状全图(Fact,2026-06-05 盘点)

### 2.1 动态引导通道清单

| 通道 | 位置 | 触发 | 频率 | 内容 |
|---|---|---|---|---|
| Plan Mode reminder | `engine.py:884-945` | plan 激活 | FULL 一次(+compaction re-arm ✓)→ **SPARSE 每轮** | 静态文本 |
| Work Ledger reminder | `engine.py:957-1016` | `work_ledger_enabled` metadata(invoker 复杂度预判)+ plan 互斥 | **每轮** | 静态文本,无快照,无防护句 |
| Round-pressure warning | `engine.py:966-996` | 80% 轮 + 最后 2 轮 | 各一次 | ✅ 带真实数据(B2 已对标 CC token nudge) |
| Loop guard warning | `engine.py:1706-1715` | 语义循环检测命中 | 事件驱动(先软后硬,A4) | 事件化文本 |
| DR routing reminder | `engine.py:774-808` | deep research 场景 | 条件 | 领域专用 |
| 工具结果 next_action | 各 handler | 工具返回时 | 随结果 | Hive 特色:结果内嵌引导(对位 CC hook_additional_context) |

### 2.2 注入机制(与 CC 的根本差异)

`api_messages` 在轮循环**外**构建一次(`engine.py:1841`),plan/ledger reminder 在 `for round_i` 循环**内** `append(LLMMessage(role="system", …))`(`engine.py:1899/1909`),**无去重、无清理**。

---

## 3. 机制层缺陷(自身审视,按严重度排序)

| # | 缺陷 | 实锤 | 后果 |
|---|---|---|---|
| **M1** | **reminder 逐轮堆积** | append 在循环内,数组跨轮持有,无 pop/去重(全文件仅 prompt-too-long 截断 `:2027` 和 compaction 恢复 `:2855` 两处整体重建) | 40 轮 heartbeat run 堆 40 条相同 ledger reminder(或 39 条 SPARSE plan reminder)≈3000+ token 纯重复;**壁纸效应**——重复越多模型越确信这段文本可忽略,提醒失效与 token 浪费同时发生 |
| **M2** | **堆积泄漏进记忆管线** | `_build_persisted_memory_messages`(`engine.py:185-198`)只 skip `api_messages[0]`,role=system reminder 全部进 persist(两个调用点 `:1480/:2269`) | T0/T2 蒸馏的输入被 reminder 噪音污染——蒸馏器读到的"会话"里混着几十条系统鞭策文本,影响 learnings 质量(违 L1 输入视野纯净) |
| M3 | **无节流基础设施** | reminder 函数是无状态纯函数,唯一状态是 `plan_state.reminded_full` 一个 bit;无"上次提醒在第几轮"的任何追踪 | 想加冷却就要逐 reminder 发明私有状态——机制缺一个统一的轮次计数/冷却设施 |
| M4 | **无统一调度层** | ledger×plan 互斥硬编码在 `_work_ledger_reminder_content` 内部(`:1010-1012`);round-pressure/loop-guard 与其他 reminder 同轮可叠加,无优先级概念 | 每新增一个 reminder 都要手工核对全部现有 reminder 的冲突关系,O(n²) 维护;CC 的 attachment 管线是注册组合式(`maybe()` 组合器) |
| M5 | **无状态快照能力** | reminder 是模块级字符串常量 | 无法做 CC 式"带当前任务清单"——提醒只能鞭策不能给料 |
| M6 | **零可观测** | 注入无 metric、无事件;reminder 不以独立形态落 T0(只以 M2 的噪音形态混进历史) | 操作者不知道某 session 注入了多少 reminder、模型是否响应;self-evolution 管线也无从学习"提醒是否有效" |
| M7 | **复杂度预判 gate 的 L1 张力** | `should_enable_work_ledger` 是 invoker 侧机械启发式(`invoker.py:1022`) | CC 的行为推断(从"模型用没用"倒推)不预判任务性质,更符合 L1"判断归模型"。gate 保留是拍板项(T1.2),但**预判+每轮**的组合放大了 M1;演进方向=gate 只决定"是否参赛",频率交给行为推断 |
| M8 | **冷却×compaction 语义未设计** | plan re-arm ✓ 已处理;但未来引入冷却计数后,compaction 重置怎么算没有答案 | CC 扫窗口计数,压缩后 attachment 消失自然重置;Hive 若用 scheduler 状态计数,需显式 reset 钩子(已有 `_reset_plan_reminder` 先例) |

## 4. 提示词层差距(对 CC,依赖 §3 机制修复的标注在列)

| # | 差距 | CC | Hive | 依赖 |
|---|---|---|---|---|
| P1 | 防护句缺失 | "gentle reminder - ignore if not applicable" + "NEVER mention this reminder to the user" | 两句都没有——reminder 可能泄漏进用户回复、简单场景误触发 | 无,纯文案 |
| P2 | 语气 | 建议式(consider / if relevant) | 指令式("Keep your work ledger current… use track_todo…") | 无,纯文案 |
| P3 | 状态快照 | 带任务清单 `#id [status] subject` | 纯鞭策文本 | M5 |
| P4 | 频率 | 任务 10+10,plan 5 | ledger 每轮,plan SPARSE 每轮 | M1/M3 |
| P5 | (有意 delta,非差距)静态层决策序列比 CC 总纲重 | 一句话+下放 | 七原语整段(executing_actions) | Hive 工具面宽(41 core+pack),一张决策地图必要——**保留,记录在案** |

## 5. 对齐路线(机制 → 提示词 → 全面对标)

### T-G1 机制对齐(地基,先行)

1. **堆积归零(M1)+持久化过滤(M2)**:reminder 改为**transient 注入**——每轮发送给 LLM 的请求中包含,但不滞留 `api_messages` 数组、不进 persist。
   - **设计分叉(待拍板)**:CC 是"持久进历史+节流控量"(它靠扫历史计数,且无记忆蒸馏管线);Hive 有记忆管线,reminder 进蒸馏是污染。**推荐 transient+scheduler 计数**:每轮构建请求时由调度器决定本轮注入哪些(冷却到了才有),注入只在当轮请求生效;计数状态放 session_context(与 `reminded_full` 同位)。cache:注入点在消息流尾部,前缀不变,安全。
2. **ReminderScheduler(M3/M4)**:注册式——每个 reminder 声明 `(触发条件, 冷却轮数, 优先级/互斥组)`,调度器统一计数与裁决;plan×ledger 互斥从硬编码迁入互斥组;round-pressure/loop-guard 保持事件驱动但纳入同一注册表(冷却=0)。
3. **快照装配(M5)**:reminder 支持 callable content(运行时读 ledger/objective 状态拼装),为 T-G2① 供能。
4. **可观测(M6)**:每次注入 emit 事件(type/round/字节数)进既有事件流;独立 metric 计数。
5. **compaction 语义(M8)**:scheduler 计数随 compaction reset(复用 `_reset_plan_reminder` 模式,全 reminder 统一)。

### T-G2 提示词对齐(站在 G1 上)

1. **ledger reminder**:双冷却(对齐 CC 10+10,数值可配)+ ledger 快照(当前 todos `[status] title` 清单)+ 防护句×2 + gentle 化改写;
2. **plan SPARSE 降频**(对齐 CC plan 节流 5 轮,FULL/re-arm 语义不动);
3. **round-pressure / loop-guard 文案**补防护句(数据部分已对齐,不动);
4. **M7 演进(待拍板)**:`work_ledger_enabled` 降级为"是否参赛"(eligibility),频率全交行为推断(N 轮没用 ledger 工具才提醒)——gate 不删(T1.2 拍板边界),语义收窄。

### T-G3 全面 CC 对标(40+ attachment 逐项 gap 盘点)

以 §1.1 清单为底,逐项登记 `have / planned / N-A`,各自独立成切口拍板——预判结论:`task_status`(Hive ledger 私有便签语义不同,可 N-A)、`diagnostics`/`edited_text_file`(依赖 LSP/file-watch 设施,独立议题)、`skill_discovery`(Hive skill 目录在静态 prompt,对位已存在)、`critical_system_reminder`(值得 planned)、`nested_memory`(Hive 记忆管线已覆盖,N-A)。**本阶段产出的是盘点表+切口清单,不在本文档预先实施。**

### 红测样例(T-G1/G2)

| # | 红测 |
|---|---|
| 1 | N 轮 run 后 `api_messages` 中 reminder 类消息 ≤ 调度器允许的注入次数(堆积归零) |
| 2 | persist 消息中不含任何 reminder 文本(M2 过滤) |
| 3 | 冷却:连续轮内同一 reminder 不重复注入;冷却到期后恰好一次 |
| 4 | ledger reminder 含当前 todos 快照;清单变化后快照跟随 |
| 5 | 防护句存在(ignore-if-not-applicable + never-mention) |
| 6 | plan×ledger 互斥经互斥组生效(行为不变性) |
| 7 | compaction 后 plan FULL re-arm 不回归 + scheduler 计数 reset |
| 8 | 注入事件可观测(emit 一次/注入) |

**DoD**:① M1/M2 归零有红测钉死;② reminder 全部经 scheduler 注册(无旁路 append);③ 提示词三要素(冷却/快照/防护句)落地;④ 全量绿;⑤ 本文档 §2/§3 更新为落地后新现状(证据闭环)。

## 6. 非目标

- 不删 reminder gate(`should_enable_work_ledger`,T1.2 拍板)——T-G2④ 只收窄其语义,且单独拍板;
- 不在本文档实施 T-G3 的任何新 attachment 类型(逐项独立拍板);
- 不动静态层(execution-mode-spectrum T2 已收口;P5 是记录在案的有意 delta);
- 不把 `runtime/hooks.py` 事件总线改造为 prompt 注入通道(职责不同);
- 工具结果内嵌 next_action 引导(Hive 特色)保留现状,不强行 CC 化。
