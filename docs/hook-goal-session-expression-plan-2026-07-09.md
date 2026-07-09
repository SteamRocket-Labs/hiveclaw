# Hook 治理 · Goal 收尾 · Session 表达 —— 三问统一设计（2026-07-09）

> 状态：**设计稿，待 owner 拍板**。三块各自独立可分批实施，但共享同一条"运行时相位（phase）"骨架，故合一文档。
> 证据来源：FreeCode `src/` 一手 hook 语义（CC 第一基线）+ 当前仓库代码 grep + 目标文档 `docs/session-rendering-overhaul-plan-2026-07-03.md` / `docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md`。
> 铁律遵循：一次改完·零技术债（`feedback_no_mvp_finish_completely`）；AI-Native 设计法律 L1/L2/L3；只缩不放的权限不变量；验收以代码 grep 为准（`feedback_green_tests_dont_mean_done`）。

---

## 0. 三问一句话

| # | 问题 | 一句话结论 | owner 需拍板 |
|---|------|-----------|-------------|
| 1 | Hook 能否让租户代码介入每次工具治理路径 | **能，但分级信任**：声明式策略钩子（进程内、无代码执行）覆盖企业管控 90%；可执行钩子（沙箱、fail-closed、只缩不放）作业务逃生舱 | 4 个决策点（§1.7） |
| 2 | Goal `summary_only` 收尾细化 | 预算熔断时**主动调度恰好一次收尾 turn**，注入收尾契约 prompt，写 `update_goal` 落状态，之后硬停 | 1 个决策点（§2.5） |
| 3 | Session 表达重构 | 不推倒重来：补**中间 timeline 的 turn 相位状态机 + activeTail 工作态 cell + 后端一条干净 phase 信号**；F1 stable-tail 投影治"Codex 半解" | 2 个决策点（§3.6） |

**三问的共同骨架**：都依赖一条**统一运行时相位（RuntimePhase）信号**——Hook 执行是相位的一个来源（`hook_evaluating`），Goal 收尾是相位的一个终态（`summarizing`），Session 表达是相位的消费者（渲染每个相位）。因此建议实施序：**先立 phase 骨架（§3 接缝1）→ 挂 Goal 收尾（§2）→ 挂 Hook 相位（§1）**，三者复用同一信号面，不各造轮子。

---

## 1. Hook 治理 —— 租户代码介入工具调用路径

### 1.1 为什么必须有（owner 立场）

Owner 明确：hook 是企业级 Agent 管理的支柱，缺了很多东西落不了地。两个用途：
1. **业务层**：通过 hook 做强制调用（如"写文件前必须先跑合规校验"）。
2. **企业后台管理层**：三层管理中的一层用 hook 实现——对应 CC 的 **policy-level managed hooks**（`hooksConfigSnapshot.ts:18-88`，企业 managed 层最高优先级、唯一可强制 `disableAllHooks`/`allowManagedHooksOnly`）。

这与 Hive North Star 目标 2（控制中台）直接吻合：managed hook = 公司对旗下所有 agent 工具行为的可编程管控点。

### 1.2 CC 一手语义（parity 基线，必须对齐的骨相）

FreeCode 证据（`freecode-hooks` 考证，file:line 见考证报告）：

- **事件面**：27 成员事件表；PreToolUse 最强（allow/deny/ask 改权限决策 + updatedInput 改入参 + additionalContext 注入）；聚合优先序 **deny > ask > allow**（`hooks.ts:2820-2847`）。
- **配置合并**：user→project→local→policy **拼接去重**（非覆盖），policy 层最高且唯一可强制（`settings.ts:798-801`）。
- **matcher**：空/`*`=全匹配；`/^[a-zA-Z0-9_|]+$/`=精确或 `|` 列表（`Write|Edit`）；否则当正则；额外 `if` 字段用 permission-rule 语法二次过滤。
- **四种 hook 类型**：`command`(shell) / `prompt`(小模型评估) / `agent`(Haiku verifier) / `http`(POST)。**hook 不只是 shell**。
- **CC 的两个不可照抄的姿态**：
  - **失败 fail-open**：超时/崩溃/坏输出一律**放行**（`hooks.ts:2473-2497`）。CC 是"你自己机器跑你自己命令"的本地信任模型。
  - **零沙箱**：命令直接 `spawn(cmd,{shell:true})`，默认透传完整 `process.env`（含密钥）（`subprocessEnv.ts:79-99`）。

### 1.3 Hive 的三个刻意 delta（相对 CC 的必然偏离）

Hive 是**多租户平台**，租户写的 hook 命令 = 任意 RCE。CC 此处无可抄之物，必须刻意偏离：

| delta | CC | Hive | 依据 |
|-------|-----|------|------|
| **D1 失败姿态** | fail-open 放行 | 治理型 hook **fail-closed**（挂/超时/坏输出→拒绝该工具调用） | 多租户治理不能因 hook 崩溃就放行；`run_tool_governance` 已是 fail-closed 超时模型（`governance.py:826`），一致 |
| **D2 沙箱** | 零隔离、透传密钥 | 可执行 hook 必走 `services/code_execution/` 沙箱（本地 sandbox-exec/bwrap，Railway vercel_sandbox），env 白名单、超时、资源上限 | Code Execution Provider 不变量（CLAUDE.md）；租户代码禁裸 subprocess |
| **D3 只缩不放** | allow 是真 grant，能绕过确认（`toolHooks.ts:372,382`） | 租户 hook **只能 deny/ask，不能 allow-grant**；复用 CC 的 deny>ask>allow 聚合序，但砍掉 allow 越权分支 | 权限不变量：hook 可加严不可放宽；平台既定权限门是地板，hook 不得穿透 |

**D3 的精确语义**（关键，避免误砍）：
- 租户 hook 返回 `deny` → 生效（加严）。
- 租户 hook 返回 `ask` → 生效（升级为需确认）。
- 租户 hook 返回 `allow` → **降级为"无意见"**（不改变平台既定决策；平台本来要弹框的仍弹框）。allow 不能把平台的 ask/deny 变成放行。
- **例外**：`managed`（企业 policy 层）hook 的 allow 是否保留 grant 能力？→ 这是 owner 决策点（§1.7-b）。企业自己对自己旗下 agent 授权，与租户第三方插件不同。

### 1.4 分级信任模型（核心设计）

不是"允许 or 不允许租户代码"的二元题，而是**按信任来源和执行形态分级**。两条泳道：

```
                       工具调用 (PreToolUse 相位)
                               │
              ┌────────────────┴────────────────┐
              ▼                                  ▼
   ┌──────────────────────┐        ┌──────────────────────────┐
   │  快路：声明式策略钩子    │        │  慢路：可执行钩子（逃生舱）  │
   │  DeclarativePolicyHook │        │  ExecutableHook           │
   ├──────────────────────┤        ├──────────────────────────┤
   │ · 无代码执行           │        │ · command/http 型         │
   │ · 进程内规则求值        │        │ · 必走沙箱                 │
   │ · 复用 execpolicy 引擎  │        │ · fail-closed             │
   │ · matcher + if 条件    │        │ · env 白名单、超时上限      │
   │ · deny/ask（+managed   │        │ · deny/ask only           │
   │   allow）              │        │ · 预算计量、span 落库       │
   │ · 微秒级、无沙箱开销     │        │ · 每次工具调用 +1 沙箱冷启   │
   └──────────────────────┘        └──────────────────────────┘
   覆盖企业管控 ~90% 场景            覆盖"必须跑租户代码"的业务 hook
```

**为什么分两路**（这是本设计的核心判断）：

1. **企业管控的绝大多数需求是"声明式的"**，不需要跑代码：
   - "Bash 工具禁止 `rm -rf`" → 声明式规则（已有 `execpolicy.py:DANGEROUS_COMMAND_RULES`）。
   - "write_file 到 `/etc/**` 必须审批" → matcher(`write_file`) + `if` path 条件 → ask。
   - "public zone agent 禁所有写工具" → 声明式（已有 security zone）。
   这些用**声明式策略钩子**在进程内求值，微秒级、零沙箱开销、零 RCE 面。这条路是 CC `if` 字段（permission-rule 语法）+ hookConfig matcher 的 Hive 强化版，**复用已建的 D3 execpolicy 引擎**（规则即数据、自校验、first-match）。

2. **只有真正需要"跑一段租户逻辑"的业务 hook 才进慢路**沙箱。例：hook 调租户内部合规 API（http 型）、hook 跑一段自定义校验脚本（command 型）。这类每次工具调用 +1 沙箱冷启动（~100-500ms），是真实成本，所以**默认关、显式开、且只给需要的 matcher**。

**这个分级同时解决了 owner 担心的两难**：
- "不允许很麻烦" → 声明式快路让企业管控**无需写代码**就能全覆盖，麻烦的部分（90%）根本不碰 RCE。
- "允许又有安全问题" → 真需要跑代码的 10% 走沙箱+fail-closed+只缩不放，安全面收敛到一条窄泳道，可被 policy 层 `allowManagedHooksOnly` 一键收死。

### 1.5 与现有治理管线的接缝

当前 `run_tool_governance` 内序（`governance.py:843+`）：
```
security zone → capability gate → approval flow
```
Hook 作为**第三/第四层**插入，位置精确：
```
security zone → capability gate → [声明式策略钩子] → approval flow → [可执行钩子（若启用）]
                                   ↑ 快路，纯函数                      ↑ 慢路，沙箱
```
- 声明式钩子在 capability gate 之后、approval 之前：它可以把"本来放行"升级为 ask/deny，符合"只缩不放"（在 gate 已放行的基础上只能加严）。
- 可执行钩子在最后（approval 之后、执行之前）：因为它最慢且最危险，放最后意味着前面能拦的都拦了，沙箱只处理真正到达执行边缘的调用。
- **PostToolUse 钩子**（工具已执行）走另一条路：不参与放行判定，只做 additionalContext 注入 / 结果改写 / 审计，对齐 CC `hooks.ts:643-649`。

### 1.6 数据模型与执行（已有地基）

- **注册表已建**：`ExternalExtensionHookRegistration`（`models/external_capability.py:153`，fail-closed 铸造、tenant+snapshot+qualified_name 唯一）。缺的是 **runtime 读者**（当前 grep：runtime/kernel/tools 零读者——这就是 `not_yet_supported` 的真相）。
- **事件枚举已建**：`HookEvent` 42 成员（`hooks.py:63`），PRE_TOOL_USE/POST_TOOL_USE 已 live-emit。缺 CC 的 PermissionRequest/PermissionDenied 等，按需补。
- **沙箱已建**：`services/code_execution/`（local_provider + vercel_provider）。可执行 hook 直接复用，不新造。
- **execpolicy 引擎已建**：`tools/execpolicy.py`（声明式规则、自校验），声明式钩子直接复用其求值器。

**要新建的**（一次改完清单）：
1. Hook 事件→治理管线的**消费桥**：`run_tool_governance` 里查该 agent 的 registration，按 matcher 过滤，分快/慢路求值。
2. 声明式钩子求值器（进程内，复用 execpolicy）。
3. 可执行钩子执行器（沙箱 + stdin JSON payload + exit/JSON 判定协议 + fail-closed + env 白名单 + span/预算）。
4. 判定聚合器（deny>ask>allow，D3 语义砍 allow grant）。
5. 多层配置合并（user/company/managed，policy 最高，拼接去重——对齐 CC）。
6. 全链 TDD：快路规则命中、慢路沙箱 deny、fail-closed（hook 挂→拒工具）、只缩不放（allow 不穿透平台 ask）、managed 强制、聚合优先序。

### 1.7 owner 拍板点（Hook）

- **1.7-a 慢路（可执行 hook）是否本轮就做，还是先只做声明式快路？**
  - 建议：**本轮做声明式快路 + 可执行慢路的 command 型**，一次到位（零技术债）。`prompt`/`agent`/`http` 三型作为后续显式可选面（文档标注，不装死）。
  - 若要更保守：先声明式快路（覆盖 90% 企业管控），慢路留下一轮——但按铁律，"先做一半"需要你明确豁免。
- **1.7-b managed（企业 policy 层）hook 的 allow 是否保留 grant 能力？**
  - 建议：**保留**。企业对自己旗下 agent 授权 ≠ 第三方租户插件越权。managed allow 可 grant（企业自担责），租户/项目层 allow 只降级为"无意见"。这与 CC 的 policy 层最高权语义一致。
  - 若要最严：一律砍 allow grant，连 managed 也只能 deny/ask——更安全但企业无法用 hook 做"自动放行低风险操作"。
- **1.7-c 失败姿态 fail-closed 的粒度**：hook 超时/崩溃时，是拒绝**该次工具调用**（严），还是**降级为 ask**（弹给用户/operator 决定）？
  - 建议：**治理型 deny/ask hook 挂 → fail-closed 拒绝该工具调用**（安全优先）；纯注入型 hook（PostToolUse/additionalContext）挂 → fail-open 放行（它本就不影响放行决策）。按 hook 的判定能力分姿态。
- **1.7-d 沙箱冷启动成本**：可执行 hook 每次工具调用 +1 沙箱冷启（~100-500ms）。是否接受？是否需要"沙箱预热池"优化？
  - 建议：**先接受裸成本 + 只对显式配了 matcher 的工具触发**（未配 matcher 的工具零开销）；预热池作为后续性能优化，不进本轮。

---

## 2. Goal `summary_only` 收尾细化

### 2.1 现状（已建的一半）

`runtime_budget_service.py` 已有 `fail_mode="summary_only"`：预算耗尽/熔断触发时，run 转入 `summary_only` 状态而非硬停——
- **放大型工作全部拒绝**（`_work_amplifying_amounts`：subagents/team_sessions/delegations/background_tasks/continuation_wakes 五维，`:258`）。
- **留了一条"非放大 invocation"通道**（`:543` 只拒放大操作，非放大放行）。
- 设计意图明确（`:358` 注释："leave a lane for one final summarizing invocation"）。

### 2.2 缺的一半（三个空缺）

1. **无人主动用这条通道**：run 转 `summary_only` 时没有任何代码去**调度那次收尾 turn**。通道只是"不拒绝"，不是"会发生"。
2. **模型不知道自己在收尾**：即使有 invocation 进来，prompt 里没有"预算已尽、禁开新工作、写收尾总结并调 `update_goal` 落状态"的契约。这违反 AI-Native L1（模型该有完整的任务态视野）。
3. **"放行一次"的一次没强制**：通道不会在用过后自动关死，理论上可反复走。

### 2.3 设计（收尾 wake）

复用 §3 的 phase 骨架，Goal 收尾是相位的一个**终态 `summarizing`**：

```
预算熔断/耗尽
   │
   ▼
run.status = summary_only  ──(现状到此为止)──
   │
   ▼ 【新增】
goal_continuation_service 检测到 run 转 summary_only
   │
   ▼
调度恰好一次 summary_only 续跑 turn（source=goal_continuation, mode=summarizing）
   │
   ├─ 注入收尾契约 prompt（模型可见：预算已尽/禁开新工作/写收尾/调 update_goal）
   ├─ 该 turn 内放大操作仍被 budget 拒（已有 :543）
   │
   ▼
收尾 turn 完成 → update_goal(status=complete|blocked, summary=...) 落库
   │
   ▼
run.status = summary_only → hard_stopped（关死通道，防反复）
   │
   ▼
前端渲染 summarizing 相位 → 完成后显示收尾报告（§3 消费）
```

### 2.4 接缝与实现（小工程，一次改完）

- **触发点**：`goal_continuation_service.py` 的 wake 消费点（`:267` 附近，A3 自主循环判定处）增一个前置分支：`if run.status == "summary_only" and not run.summary_turn_issued`。
- **收尾契约 prompt**：新增一段 vendor-neutral 收尾指令（对齐 A2 continuation prompt 的三审计段风格），注入 `mode=summarizing` 的续跑。
- **一次性保证**：run 加 `summary_turn_issued` 标志（或复用现有 terminal 流转），收尾 turn 完成后 `summary_only → hard_stopped`。
- **落状态**：收尾 turn 里模型调 `update_goal`（A1 已建的回写桥），status 终态化。
- **TDD 钉死**：①run 转 summary_only 恰好调度一次收尾 turn ②收尾 prompt 含禁放大契约 ③收尾后 run hard_stopped、再无续跑 ④放大操作全程被拒 ⑤收尾 turn 里 update_goal 落库生效。

### 2.5 owner 拍板点（Goal）

- **2.5-a 收尾 turn 失败怎么办**：收尾 turn 本身崩溃/超时，是重试一次还是直接 hard_stopped（放弃收尾）？
  - 建议：**重试一次**（收尾报告对 owner 有价值），二次失败则 hard_stopped 并记 `summary_failed`，前端显示"收尾未能生成"。避免无限重试烧预算。

---

## 3. Session 表达重构

### 3.1 诊断（session-audit 审计结论）

**不是推倒重来**。性能地基（`useSyncExternalStore`+rAF 合帧+memo）、右栏 Runtime Tables、Codex 表面语义（shimmer/exec 折叠/thinking headline）都是**可复用成品**。缺的是三样：

1. **中间 timeline 无 turn 生命周期状态机**：`isWaiting` 单布尔承载全部等待语义（`chatRuntime.ts:806-857`），没有 phase。
2. **后端 status 事件被前端丢弃**：后端大量 emit `status` 子阶段（`web_chat_runtime.py:2195+`），但前端 live WS 热路径只认 8 种事件（`AgentDetail:1725-1849`），子阶段被丢——"看不懂在干嘛"一半是前端没接。
3. **F1 stable-tail 投影未建**：`buildSessionThreadProjection`/`staticCells`/`activeTail` 在 `timelineModel.ts` 根本不存在（s6 计划纯纸面）——"Codex 一知半解"=学了皮相（表面语义）没学骨相（投影架构）。

**8 种等待态现状**：只有 ②tool 执行中、⑧断线重连达标；①等首 token、④budget/summary_only、⑤排队恢复、⑦goal 续跑间隙**完全零表达**；③审批、⑨plan 半成品。

### 3.2 owner 三批评归因（session-audit）

- **"看不懂在干嘛"** ← 中间流缺 phase 标签 + 后端信号没接 + 子任务在右栏 split-brain + 状态机缺失。
- **"等待状态非常差"** ← `isWaiting` 单布尔，8 态里 4 态没表达、2 态半成品。
- **"Codex 一知半解"** ← 表面学到（shimmer/exec-clip/done 折叠）、架构没学到（stable-tail 投影/turn 状态机/approval overlay）。

### 3.3 统一相位模型（三问的共同骨架）

定义一条 **RuntimePhase** 枚举，作为后端→前端的干净信号，同时被 Hook（§1）和 Goal（§2）复用：

```
queued          排队中（RuntimeTask 未认领）
resuming        恢复中（重启/断线后 replay）
starting        已认领、等模型首 token
thinking        模型思考中（thinking 流）
responding      模型输出中（text 流）
tool_running    工具执行中（+ 工具名/相位）
hook_evaluating 治理钩子求值中（§1 复用）★
compacting      上下文压缩中
awaiting_approval  等审批/plan gate
awaiting_budget budget/summary_only 降级中
summarizing     Goal 收尾中（§2 复用）★
continuation_gap Goal 续跑间隙（等下一次推进）
done / failed / cancelled  终态
```

★ = Hook 和 Goal 各贡献一个相位，证明三问共享骨架、不各造轮子。

### 3.4 最小重构面（session-audit 判断，4 接缝）

| 接缝 | 做什么 | 文件（file:line） | 解哪些差距 |
|------|--------|------------------|-----------|
| **接缝1 turn 状态机** | `isWaiting` 单布尔 → RuntimePhase 模型；后端收敛散落 status 成一条干净 live phase 信号（补 queued/resuming/budget/goal-gap/hook 相位） | `chatRuntime.ts:806-970` + `AgentDetail.tsx:1703-1850`（WS 热路径消费 status）+ `web_chat_runtime.py:2195+`（phase emit 收敛） | #1/#2/#4/#6/#7/#8 总闸 |
| **接缝2 activeTail 工作态 cell** | 新组件：run_started→首 token 间、tool 间、等待间渲染统一"进行中/等待中 + 相位文案 + 活秒表" | 挂 `AgentChatSection.tsx:3346/3977` renderConversationMessages | #1（等首 token 空白） |
| **接缝3 F1 stable-tail 投影** | `timelineModel.ts` 真建 `buildSessionThreadProjection{staticCells,activeTail,terminalAnswer,sideEffects,anchors}`；渲染切到投影 | `timelineModel.ts`（新增）+ `AgentChatSection:3353/3655` | #3（Codex 半解）+ 是 #2 载体 |
| **接缝4 中间↔右栏关联** | subagent/后台/waiter 在中间流插 marker + 与右栏 row 双向高亮 | `AgentChatSection:1619/1757/1889` | #5（split-brain） |

**可保留资产**（不动）：`sessionMessageStore.ts`（地基）、`RunDisclosureBlock.tsx`/`ThinkingDisclosure.tsx`（Codex 表面）、右栏 builders + `SessionRuntimePanel`、`session-tui-*` 密度 token、permission 队列渲染（升级为 overlay+三处同步）。

### 3.5 实施序建议

**接缝1 是总闸**（也是 Hook/Goal 复用的骨架），先做。然后接缝2（工作态可见性，直治"等待差+看不懂"），再接缝3（F1 投影，治"Codex 半解"），最后接缝4（split-brain）。接缝1+2 一次消掉 Top10 里七条。

### 3.6 owner 拍板点（Session）

- **3.6-a 重构范围**：本轮做**全部 4 接缝**（一次改完，达 Codex Desktop 级），还是先做接缝1+2（治标最猛的"看不懂+等待差"）、接缝3+4 下一轮？
  - 建议：**4 接缝一次到位**。按铁律零技术债，且接缝3（F1 投影）是接缝2 的架构载体，分开做会返工。
- **3.6-b 后端 phase 信号收敛**：现有散落的 `status` 事件是**重构成一条干净 phase 信号**（破坏性，改后端事件契约），还是**在前端做适配层**把散落 status 映射成 phase（不动后端）？
  - 建议：**后端收敛**。前端适配层是又一层"看不懂"的技术债；phase 是 Hook/Goal 也要用的公共信号，应在后端定义为一等契约。破坏性变更但一次做干净。

---

## 4. 统一实施序（三问合一）

```
第1步：立 RuntimePhase 骨架（§3 接缝1）        ← 后端 phase 契约 + 前端状态机，三问共用
   │
   ├─第2步：挂 Goal 收尾（§2）                  ← summarizing 相位，小工程，独立可测
   │
   ├─第3步：挂 Hook 治理（§1）                  ← hook_evaluating 相位 + 声明式快路 + 沙箱慢路
   │        （最大块，含 4 个拍板点）
   │
   └─第4步：Session 表达补全（§3 接缝2/3/4）    ← activeTail cell + F1 投影 + split-brain
```

三步之后，"看不懂在干嘛""等待状态差""Codex 半解"三批评同时消解，Hook 让企业管控落地，Goal 截断从"沉默死"变"有尊严收尾"。

## 5. 拍板结果（2026-07-09 owner 定，全部采纳建议）

| 编号 | 决策 | 结论 | 状态 |
|------|------|------|------|
| 1.7-a | 可执行 hook 本轮做到 command 型 or 只做声明式快路 | **声明式快路 + command 型慢路，一次到位**；prompt/agent/http 三型为后续显式可选面 | ✅ 拍板 |
| 1.7-b | managed hook 的 allow 是否保留 grant | **managed 保留 grant**（企业自担责，对齐 CC policy 层最高权）；租户/项目层 allow 只降级为"无意见"不穿透平台权限门 | ✅ 拍板 |
| 1.7-c | hook 失败姿态粒度 | 治理型 deny/ask hook 挂→**fail-closed** 拒该工具调用；纯注入型（PostToolUse/additionalContext）挂→fail-open 放行 | ✅ 采纳建议（实施级，可微调） |
| 1.7-d | 沙箱冷启动成本 | 接受裸成本 + **只对显式配了 matcher 的工具触发**（未配零开销）；预热池作后续性能优化不进本轮 | ✅ 采纳建议（实施级） |
| 2.5-a | 收尾 turn 失败处理 | **重试一次**，二次失败 hard_stopped 记 `summary_failed`，前端显示"收尾未能生成" | ✅ 采纳建议（实施级） |
| 3.6-a | Session 重构范围 | **4 接缝一次到位**（达 Codex Desktop 级；F1 投影是 activeTail 的架构载体，分开会返工） | ✅ 拍板 |
| 3.6-b | 后端 phase 信号 | **后端收敛为一等契约**（破坏性但做干净；phase 是 Hook/Goal/Session 三方公共信号，不做前端适配层技术债） | ✅ 拍板 |

**全部拍板完成。据此进入实施，实施序见 §4。**

## 6. 实施台账（证据行）

| 步 | 内容 | 证据 | 状态 |
|----|------|------|------|
| Step 1 | RuntimePhase 骨架：后端一等契约 `app/runtime/runtime_phase.py`（15 相位枚举 + `build_phase_event` + `RunPhaseEmitter` 去重/终态封口/广播失败不伤 run）；`web_chat_runtime.py` 收敛发射（queued→starting→thinking/responding/tool_running/compacting/awaiting_approval→done/failed/cancelled + finally 兜底 + 跨进程 cancel 兜底）；前端 `chatRuntime.ts` turn 状态机（`RuntimePhase` 类型 + `reduceRuntimePhase` live/replay 共用 + `phaseUi`/`uiForPhase` 单一真相派生，`SessionUiState` 升级为 phase 权威 + 双布尔派生视图）；`AgentDetail.tsx` WS 热路径新增 `phase` 一等分支 + 全部 8 处 `setSessionUiState` 调用点改为 `setSessionPhase`（发送=queued/断线重连=resuming/审批卡=awaiting_approval/abort=cancelled/stale=idle）；`AgentChatSection` 接收 `runtimePhase` prop（接缝 2-4 消费）。 | 后端 `tests/runtime/test_runtime_phase.py` 10 passed + `tests/services/test_web_chat_runtime.py` 98 passed（含 3 个新 phase 序列钉测：正常流 starting→thinking→tool_running→thinking→responding→done、异常流 finally 兜底 failed、审批 pause 停在 awaiting_approval 不冒 done）；前端 `chatRuntime.test.ts` 新增 6 用例（后端 phase 事件权威采纳/未知 phase 前向兼容忽略/replay 推导全表/终态可重开/派生布尔全表/replay 穿行）全绿，前端全量 84 files / 514 passed + `tsc --noEmit` 通过。 | ✅ |
| Step 2 | Goal 收尾（summarizing 相位 + 恰好一次收尾 turn + hard_stopped） | — | 施工中 |
| Step 3 | Hook 治理（声明式快路 + command 沙箱慢路 + 聚合 + 多层合并） | — | 待施工 |
| Step 4 | Session 表达补全（activeTail cell + F1 投影 + 中间↔右栏关联） | — | 待施工 |

**Step 1 附带修复**：主线既有红测 `AdminCompaniesSections.test.tsx`（上午 G 面 breaker 落地后 `AdminPlatformSection` 嵌入 `WorkspaceRuntimeBudgetsSection`，测试缺 QueryClientProvider + icons stub 不全）— 提供真实 QueryClient + 枚举图标 stub，2 passed。
