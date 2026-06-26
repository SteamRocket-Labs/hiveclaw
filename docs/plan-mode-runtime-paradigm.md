# Plan Mode 运行时范式收敛：从隔离 Planner 到主循环内联规划

> 本文是 `docs/plan-mode-design.md`（总设计）和 `docs/plan-mode-agent-authored-planning.md`（谁写计划的修正）的**下一阶段修正**。
>
> 前序文档解决了 **「计划内容必须由 agent 撰写，而不是 deterministic skeleton 填表」**。本文解决下一层问题：**「agent 应该在哪里、以什么形态撰写计划」**——把规划从隔离 RPC 子调用搬回主对话循环，统一三套分裂的范式，并对标 Claude Code / Codex 强化运行时注入机制。
>
> 治理层（`agent_plan_requests` 账本、`PlanModeGate`、`plan_version/plan_hash` 加密确认、生产 cutover）**全部保留不变**。本文只改"计划如何被生产"，不改"计划如何被治理"。
>
> 关联文档：
> - `docs/plan-mode-design.md` — 总设计、数据模型、状态机、安全不变量、API、UI
> - `docs/plan-mode-agent-authored-planning.md` — agent-authored 原则修正（本文的前序）
> - `docs/plan-mode-agent-work-ledger.md` — 执行期 Work Ledger（与本文的 plan artifact 分工，见 §10）

---
## 0. TL;DR


**根因（一句话）**：

> Claude Code 和 Codex 让**主 agent 待在主循环里、用完整能力、对话式渐进地边想边写**——产物是叙述性思考。Hive 在自动触发 / 工具拦截路径上把规划降级成**一次性隔离 RPC 子调用 + 严格 JSON 产物**（`max_tool_rounds=8`、`channel="internal"`、隔离 session）——产物是填表。**形态决定风格：填表永远不像思考。**


**本文主张**：**收敛到单一范式**——主循环内联规划。

1. Plan Mode 升级为一等的 **runtime 状态**（不再是 `metadata` 字典里的临时标记）。
2. 每轮注入 **full / sparse 两档 plan-mode reminder**（对标 Claude Code），并在 compaction 后保活。
3. 起步阶段先保持 `exit_plan_mode` 的 structured fill 作为治理输入；**plan markdown 文件**作为后续阶段引入，且必须有精确路径白名单。
4. **淘汰隔离 RPC 子调用 planner** 作为有用户在场的主路径；其 v4 prompt 精华迁移进主循环 reminder。
5. 规划过程对用户 **可见、可流式**（淘汰 `channel="internal"` 黑箱）。
6. 治理层一行不动。

---

## 1. 背景与基线：前序文档对了什么

`docs/plan-mode-agent-authored-planning.md` 已经确立了一个正确且重要的不变量（其 §1）：

> Plan Mode 的 substantive plan content 必须由 agent authored planner 产生。系统只负责创建 planning envelope、限制 planner 权限、校验 schema、保存版本、展示确认、执行 handoff。

这是对的，且已经落地为 `agent_plan_planner.py`（`PLANNER_PROMPT_VERSION = "agent_plan_v4"`，迭代 4 版，prompt 质量已经相当高）。**本文不否定这个不变量，而是补上它遗漏的一维**：

| 维度 | 前序文档已解决 | 本文要解决 |
|---|---|---|
| **谁写计划** | ✅ agent（不是 skeleton 填表） | （保持） |
| **agent 在哪写** | ❌ 隔离 sandbox（`source="plan_mode"`, `channel="internal"`） | 主对话循环 |
| **写多久** | ❌ `max_tool_rounds=8` 硬上限 | 与主对话同等的探索预算 |
| **产物形态** | ❌ 一次性严格 JSON | Phase 4A 先 structured exit fill；Phase 4B/4C 后渐进式 markdown 文件 + 事后抽取 JSON |
| **用户可见性** | ❌ 黑箱，只见最终卡片 | 规划过程可流式可见 |
| **运行时状态** | ❌ 无（一次性 RPC） | 每轮 reminder 维持 plan 状态 |

一句话：**前序文档把规划的"作者"改对了（agent），但把规划的"工位"放错了（小黑屋）。**

---

## 2. 核心判断：范式分裂是根因

### 2.1 三方范式对照

| 维度 | Claude Code | Codex | Hive 目标 |
|---|---|---|---|
| **Plan mode 本质** | `permissionMode='plan'` 运行时状态位 | `<collaboration_mode>` developer 消息 | runtime 状态（§6.1） |
| **谁规划** | 主 agent，**完整能力** | 主 agent，**完整能力** | 主 agent，完整主循环能力 + 只读工具面 |
| **计划形态** | 渐进式写的 **markdown 文件**（唯一可写） | 对话式收敛出 `<proposed_plan>` 块 | 先 structured exit fill；后续渐进式 markdown 文件（§6.3） |
| **状态保持** | **每轮** `<system-reminder>`（full/sparse/subAgent 三档） | developer 消息，可中途切换 | 每轮 full/sparse reminder（§6.2） |
| **审批出口** | `ExitPlanMode` 工具 = 审批请求（禁止用文本/AskUser 旁路问） | `<proposed_plan>` + developer 消息结束 mode | `exit_plan_mode` 工具（§6.5，已存在） |
| **compaction 保活** | plan 文件 compaction 后自动重注入 | remote compact 重注入 canonical context | typed plan state + Phase 4B 后 plan 文件重注入（§6.2） |
| **治理强度** | 弱（纯行为，可违抗） | 中（mode 只能 developer 切） | **强**（加密确认 + gate，保留） |

Claude Code / Codex 的共同点：**规划是主 agent 思考的自然产物，发生在主循环里，对用户可见，靠每轮注入维持状态。** Hive 的护城河（加密确认 + 纵深 gate）比两者都强——**这部分不动**——但规划的"工位"必须搬回主循环。

### 2.2 为什么"填表"必然"不像"

`agent_plan_planner.py` 的 RPC 范式有六个结构性约束，每一个都把产物推离"思考"、推向"填表"：

1. `max_tool_rounds=8`（`agent_plan_planner.py:297`）——复杂任务还没探索完就被截断。
2. `max_output_tokens=4000`（`:298`）——计划被迫压缩。
3. 输出强制 `{plan_json, plan_markdown}` 严格 JSON（`_build_planner_user_prompt`）——agent 先想"怎么填满 15 个字段"，而不是"这个任务该怎么做"。
4. `channel="internal"`（`:282`）——用户看不到探索过程，只看到渲染后的卡片。
5. 隔离 session（`source="plan_mode"`）——不是用户对话的延续，丢失对话语境的连贯性。
6. 一次性调用——没有运行时状态，没有"我正在规划"的持续意识。

对比：interactive plan mode（已存在）没有 1/2/4/5/6 这些约束。**问题从来不是"agent 不会规划"，而是"我们把会规划的 agent 关进了不适合规划的笼子"。**

---

## 3. 现状全景：三套并存的范式（带 file:line）

### 3.1 路径 A — Interactive Plan Mode（主循环范式，✅ 方向正确）

```text
web 用户显式说"计划模式" / 长任务
  -> web_chat_runtime._maybe_handle_plan_mode_entry()        [web_chat_runtime.py:609]
  -> _activate_interactive_plan_mode()                        [web_chat_runtime.py:434]
       设置 runtime_session_context.metadata["plan_mode"] = {"active": True, ...}
  -> 返回 None（不生成计划，不阻塞）
  -> agent 在主对话循环正常运行
       _interactive_plan_mode_suffix 注入 system_prompt_suffix [web_chat_runtime.py:914]
  -> 工具被只读闸门限制                                         [tools/service.py:487 _interactive_plan_mode_readonly_block]
  -> agent 完成规划后调 exit_plan_mode 工具                    [tools/handlers/plan_mode.py:141]
  -> ensure_awaiting_plan_from_fill()                          [plan_mode_service.py:302]
  -> generate_plan(use_agent_planner=False)                    [plan_mode_service.py:393, 用 agent 提供的 fill]
  -> status=awaiting_confirmation -> plan card
```

**这条路径几乎就是 Claude Code 范式**：agent 在主循环、只读、自己写、`exit_plan_mode` 提交审批。它证明了主循环范式在 Hive runtime 里**可行且已部署**。

它与 Claude Code 的差距（本文要补强的）：
- 没有 full / sparse 分档，没有专门的 compaction 保活。
- plan mode 状态存在 `SessionContext.metadata["plan_mode"]` 无类型字典里（`session.py:19`），不是一等字段。

### 3.2 路径 B — RPC 子调用 Planner（隔离范式，❌ "不像"的根因）

```text
agent 调 set_trigger / 自动触发被 gate 拦
  -> PlanModeGate.check() 返回 needs_plan                      [plan_mode_gate.py:81]
  -> PlanModeService.ensure_awaiting_plan()                    [plan_mode_service.py:224]
  -> tool_args_to_plan_fill() 把 tool args 转 seed             [plan_mode_core.py:505]
  -> generate_plan(use_agent_planner=True)                     [plan_mode_service.py:393]
  -> _run_planner() -> DefaultAgentPlanPlanner.plan()          [plan_mode_service.py:360 / agent_plan_planner.py:254]
  -> 隔离 invoke_agent(source="plan_mode", channel="internal",
       max_tool_rounds=8, max_output_tokens=4000)              [agent_plan_planner.py:265]
  -> 解析 JSON -> _apply_generation 合并 skeleton + 校验        [plan_mode_service.py:514]
  -> plan_hash = compute_plan_hash(merged)                     [plan_mode_service.py:634]
  -> status=awaiting_confirmation
```

这条路径用于 **tool 拦截 / 自动触发**（用户没有显式进 plan mode，是 agent 的动作触发了 gate）。它是"填表式"产物的来源（§2.2）。



### 3.4 范式分裂的后果

1. **体验不一致**：同一个"规划"概念，web 显式触发走主循环（较好），tool 拦截走隔离子调用（填表），用户感知割裂。
3. **维护成本**：`use_agent_planner=True/False` 两条 generate_plan 分支（`plan_mode_service.py:393`），认知负担。
4. **质量上限被钳制**：自动触发场景永远拿不到主循环的探索深度。

---

## 4. 目标范式：主循环内联规划（单一范式）

### 4.1 一句话定义

> Plan Mode 是 agent 主对话循环的一个 **runtime 状态**。进入该状态后，agent 用**完整主循环能力**（不阉割轮数、不阉割输出、保留对话连续性）在主循环里探索，但工具面仍是**只读 + 审批出口**；规划过程对用户**可见可流式**；每轮注入 plan-mode reminder 维持"只读 + 正在规划"的状态；agent 调 `exit_plan_mode` 提交审批。系统从 `exit_plan_mode` 的 structured fill 生成 `plan_json`、计算 hash、落账本、要求用户**加密确认**；后续可把 markdown plan 文件作为同一契约的写作载体。治理边界与今天完全一致。

### 4.2 三个不变量

- **INV-1 规划在主循环**：substantive 规划必须发生在 agent 主对话循环里，不得降级为隔离子调用。（修正前序文档的工位错误）
- **INV-2 治理不变**：`plan_json` schema 校验、`plan_hash` 加密绑定、`PlanModeGate` fail-closed、用户确认锚定真实 user_id——全部保留（`plan_mode_core.py:909/392`）。
- **INV-3 形态是叙述性规划，不是同步填表**：第一阶段仍可让 `exit_plan_mode` 提交 structured fill，但 agent 的规划必须发生在主循环里，并以用户可读的 plan markdown/steps 表达。后续引入 plan 文件时，`plan_json` 才从文件或 fill 抽取为治理契约。

---

## 5. 与现有范式的取舍

| 现状组件 | 处置 | 理由 |
|---|---|---|
| Interactive plan mode（路径 A） | **保留 + 升级为主路径** | 已是正确范式，补强注入机制即可 |
| `exit_plan_mode` 工具 | **保留为唯一审批出口** | 已实现（`handlers/plan_mode.py:141`），对标 Claude Code ExitPlanMode |
| `agent_plan_planner.py`（RPC 子调用） | **降级为退化 fallback**（见 §6.6），有用户在场的主路径淘汰 | 其 v4 prompt 精华迁移进主循环 reminder |
| `tool_args_to_plan_fill`（`plan_mode_core.py:505`） | **保留为 seed 输入** | 作为 planner 的 evidence，不作为最终计划（前序文档已确立）；有用户在场时用于进入 interactive planning 的上下文 |
| `build_plan_skeleton`（`plan_mode_core.py:418`） | **保留为 schema envelope / 抽取目标** | Phase 4A 用于 structured fill 的字段骨架；Phase 4C 用于从 plan 文件抽取 plan_json |
| 治理层全套 | **一行不动** | 护城河 |

**注意**：这不是"删代码推倒重来"，是"把主路径从 B 切到 A，强化 A，B 退化保底"。已部署的治理与 cutover 不受影响（§14）。

---

## 6. 关键机制设计

### 6.1 Plan Mode 作为一等 runtime 状态

**现状**：plan 状态藏在 `SessionContext.metadata["plan_mode"]` 无类型字典（`session.py:19-41`，`web_chat_runtime.py:434` 写入）。`AgentInvocationRequest` 和 `SessionContext` 都没有 typed 字段（`invoker.py:94`）。

**目标**：升级为一等结构，让主循环、prompt builder、gate 都能可靠读取。

```python
# runtime/session.py — 新增 typed 状态（草案）
@dataclass(slots=True)
class PlanModeState:
    active: bool = False
    plan_id: str | None = None              # 关联 agent_plan_requests 行
    intent_type: str | None = None
    plan_file_path: str | None = None       # workspace 内渐进写的 plan 文件
    entered_round: int = 0                  # 用于 full/sparse 判定
    reminded_full: bool = False             # 首轮 full 注入后置 True
    source: str = "web_chat"                # web_chat | tool_intercept | trigger | ...

@dataclass(slots=True)
class SessionContext:
    ...
    plan_mode: PlanModeState = field(default_factory=PlanModeState)  # 替代 metadata["plan_mode"]
```

向后兼容：保留 `metadata["plan_mode"]` 读取做一个 release 的过渡，新代码读 `session_context.plan_mode`。

### 6.2 每轮 reminder 注入（对标 Claude Code full/sparse）

**注入点已确认可用**：`engine.py:1547` 是主轮循环 `for round_i in range(max_rounds)`；`engine.py:1559-1583` 已经在用"每轮 append `LLMMessage(role="system", ...)` 到 `api_messages`"的机制注入 round-pressure 警告；`engine.py:1588` 克隆为 `stream_messages` 后于 `:1606` 送 LLM。**plan-mode reminder 用同一机制注入**。

```python
# kernel/engine.py — 在 round 体顶部，紧邻 round-pressure 注入（草案）
plan_state = session_context.plan_mode
if plan_state.active:
    if not plan_state.reminded_full:
        api_messages.append(LLMMessage(role="system", content=_PLAN_MODE_REMINDER_FULL.format(
            plan_file=plan_state.plan_file_path)))
        plan_state.reminded_full = True
    else:
        api_messages.append(LLMMessage(role="system", content=_PLAN_MODE_REMINDER_SPARSE.format(
            plan_file=plan_state.plan_file_path)))
```

两档 reminder 文案（迁移自 `agent_plan_planner.py:140` v4 prompt 的精华 + Claude Code 结构）：

```text
# _PLAN_MODE_REMINDER_FULL（首轮 + compaction 后）
Plan Mode 已激活。用户尚未批准执行——你**禁止**产生任何副作用（创建/启用 trigger、
启动 long task、委派、写业务文件、发外部消息、save_memory、执行命令）。
默认禁止写任何 workspace 文件；只有当 runtime 明确提供 plan_file_path 且工具闸门已开启
该 exact path 白名单时，才可写这个计划文件：{plan_file}。其余只允许只读动作。
本指令优先于其它指令。

工作方式（在主对话循环里完成，不要一次性吐 JSON）：
1. 理解请求的真实目标、intent_type、可能的 handoff 目标。
2. 用只读工具勘察现状：相关文件、已有 schedule/objective、记忆、当前 web 事实。
   不要臆造文件路径、API、依赖、外部事实——没核实的标为 assumption。
3. 渐进式整理计划：目标、动机、有序步骤、成功标准（可观测，不是复述请求）、
   停止条件、风险、外部副作用、预估成本、唤醒策略（定时类）、验证方式。若本轮
   runtime 提供可写 plan_file_path，把同样内容写进该计划文件；否则把计划内容交给
   exit_plan_mode 的 structured fill。
4. 计划要 decision-complete：让后续执行者无需再做决策即可照做。
5. 完成后调用 exit_plan_mode 提交审批。不要用普通文字或提问工具问"这个计划行不行"
   ——exit_plan_mode 本身就是审批请求。

你的回合只应以两种方式结束：需要澄清关键决策时，用普通 assistant 回复提出简短问题；
计划已足够执行时，调用 exit_plan_mode（提交审批）。
```

```text
# _PLAN_MODE_REMINDER_SPARSE（后续每轮）
Plan Mode 仍激活（完整指令见前文）。只读；若 runtime 提供 exact plan_file_path 白名单，
只可写该计划文件：{plan_file}。渐进完善计划后用 exit_plan_mode 提交审批；不要用文字
或普通澄清问题问"计划行不行"。
```

**compaction 保活**：`engine.py:877` 的 post-compact 重注入已经会恢复 workspace 文件 + skills + packs。Phase 2 先保活 typed plan state，并在 compaction 后强制 `reminded_full=False`（下一轮重发 full）；Phase 4B 再把 exact plan 文件引用加入重注入清单（对标 Claude Code 的 `plan_file_reference`）。

**注意**：reminder 走 `role="system"` 注入 `api_messages`（每轮新鲜），**不再**塞进 `system_prompt_suffix`（5000 char 拥挤通道）。这同时给 interactive plan mode 腾出 suffix 预算。

### 6.3 渐进式 plan 文件 ↔ plan_json 抽取

**Phase 4A（推荐起步）**：先不强制 workspace plan 文件。`exit_plan_mode` 工具继续接收 agent 提供的结构化 `fill`（现状 `handlers/plan_mode.py:194` 已如此），同时要求 `plan_markdown` 是用户可读的叙述性计划。系统用 `build_plan_skeleton` 做 schema envelope，校验 fill，计算 hash。**改动最小，复用现有 `use_agent_planner=False` 路径。**

**Phase 4B（文件写作载体）**：引入 agent workspace 下的计划文件，路径如 `workspace/plans/{plan_id}.plan.md`。Plan Mode 期间它是**唯一**允许写入的 workspace path，但必须满足：

- exact path 来自 typed `PlanModeState.plan_file_path`，不能让 agent 自填。
- 工具闸门做 normalize + `relative_to(workspace_root)` 校验，禁止 path traversal。
- 只允许 `write_file` / `edit_file` 命中该 exact path。
- `fs_write` 只可在 `mode=write|edit` 且 path 命中 exact path 时放行；`mode=delete` 永远禁止。
- 不开放 `workspace/plans/*` 这类目录级宽泛白名单。


无论 4A/4B/4C，`plan_hash = compute_plan_hash(merged)`（`plan_mode_core.py:392`）和加密确认链路不变；hash 绑定的仍是 normalized `plan_json`，不是未校验的原始 markdown。

### 6.4 工具约束：统一为 mode-based 只读闸门

**现状有两个闸门**（`tools/service.py`）：
- `_interactive_plan_mode_readonly_block`（`:487`）——interactive 模式下拦非只读工具，不碰 DB。
- `_plan_mode_gate_block`（`:543`）——调 `PlanModeGate.check`，gate 已确认/未确认动作。

**目标**：以 typed `session_context.plan_mode.active` 为主判据，并在工具执行链路中镜像到当前 ContextVar，统一只读裁决。原因是 `ToolRuntimeService.execute()` / `execute_direct()` / `execute_approved()` 并不天然持有 `SessionContext`，而当前 interactive gate 已经依赖 `plan_mode_runtime_context` 的 ContextVar。

Plan Mode 激活时：
- 允许：只读上下文工具（`read_file`/`fs_read`/`list_files`/`glob_search`/`grep_search`/`web_search`/`web_fetch`/`search_memory`/`list_triggers`/`list_objectives`/`get_current_time` 等）。
- 允许：`tool_search` / `load_skill` 这类只改变 prompt/tool visibility、不会执行用户目标的规划辅助工具；是否允许 `firecrawl_fetch` / `xcrawl_scrape` 由 read-only policy 明确列出。
- Phase 4B 后允许：对 exact `plan_file_path` 的 `write_file`/`edit_file`，以及 `fs_write mode=write|edit` 命中 exact path。
- 拦截：其余所有写/发/删/委派/执行工具，返回只读违规提示（复用 `:487` 文案）。
- `exit_plan_mode`：始终允许（这是出口）。

不要直接复用 `PLANNER_ALLOWED_TOOLS` 作为最终清单：它是隔离 RPC planner 的历史工具面，当前 interactive allowlist 还包含 `load_skill`、`tool_search`、`firecrawl_fetch` 等差异项。应新增一个中心化 policy（例如 `PLAN_MODE_READONLY_TOOLS` + `is_plan_mode_write_allowed(tool_name, args, state)`），让 interactive mode、fallback planner、测试都引用同一裁决函数，避免两份清单继续漂移。

### 6.5 `exit_plan_mode` 作为唯一审批出口

已实现（`handlers/plan_mode.py:141`），对标 Claude Code ExitPlanMode。强化点：
- reminder（§6.2）反复强调"审批只能走 exit_plan_mode，不能用普通文字问计划行不行"——堵住 agent"我换个说法问用户行不行"的缝。澄清问题仍可用普通 assistant 回复，但它不是审批。
- `exit_plan_mode` 成功后，由 web_chat runtime / tool callback 清 `plan_mode.active=False`，并把待确认计划回注入主对话（对标 Claude Code 审批请求的 tool_result）。用户真正批准后，再通过现有确认 API 进入 confirmed handoff。

### 6.6 RPC 子调用 planner 的去留

`agent_plan_planner.py` 的 `DefaultAgentPlanPlanner`：

- **主路径淘汰**：web chat / 有用户在场的场景，一律走 §6.2 主循环范式，不再起隔离子调用。
- **tool-intercept 有用户在场时切 interactive**：当前 `_attach_intercepted_plan()` 会同步 materialise awaiting plan。新范式下，若工具拦截发生在 live chat session 内，应把 intercepted tool args 作为 typed `PlanModeState.seed` 注入主循环，让 agent 继续规划并通过 `exit_plan_mode` 产出确认卡；不要在 tool gate 内直接跑 RPC planner。
- **保留为退化 fallback**：纯无人值守自动触发（如 trigger 到点需要规划但无人在线、纯后台 objective 推进）确实没有"主对话循环"可附着。这种场景可保留一个非交互规划调用，但应：
  1. 复用 §6.2 的 reminder 文案（不再维护独立 v4 prompt 的全文）。
  2. 放宽 `max_tool_rounds`（8 → 与主循环同档，如 40）。
  3. 去掉 `max_output_tokens=4000` 钳制（让计划该多长多长）。
  4. 先走 §6.3 Phase 4A 的 structured fill；Phase 4B 启用后才写 plan 文件，且仍受 exact path 白名单保护。
- **v4 prompt 不浪费**：`_planner_system_prompt()`（`agent_plan_planner.py:140`）的"fact discipline / quality bar / output contract"段落是高质量资产，迁移进 §6.2 的 `_PLAN_MODE_REMINDER_FULL` 与抽取器校验规则。

---

## 7. 职责边界表（更新版）

| 模块 | 应做 | 不应做 |
|---|---|---|
| `web_chat_runtime` plan entry | 置 `plan_mode.active`，启动主循环规划，流式规划过程 | 起隔离子调用；同步阻塞等 JSON |
| `engine.py` 主循环 | 每轮注入 full/sparse reminder；compaction 保活 plan state，Phase 4B 后保活 plan 文件 | 把 plan 指令塞进 frozen prefix |
| `tools/service.py` 闸门 | 以 typed plan state + ContextVar 镜像做只读裁决；集中维护 read-only policy 与 exact path 写白名单 | 维护两份漂移的 allowlist；把 write 工具整体放行 |
| `exit_plan_mode` 工具 | 唯一审批出口；提交 structured fill / 后续触发抽取 | 自动确认；旁路 hash |
| `PlanModeService` | 创建账本、校验 plan_json、版本化、落库、hash；Phase 4C 后可做 markdown 抽取 | 用 skeleton 冒充 agent 计划 |
| `PlanModeGate` / `plan_mode_core` | 校验 confirmed plan/version/hash，fail-closed | 判断计划质量 |
| `agent_plan_planner`（退化） | 仅无人值守 fallback，复用主循环 prompt | 当 web chat 主路径 |
| frontend plan card | 展示规划过程 + 确认/改/拒 | 静默确认 |

---

## 8. 逐文件改造清单

> 标注：🟢 保留不动　🟡 升级　🔴 主路径淘汰/退化

| 文件 | 处置 | 改动要点 |
|---|---|---|
| `runtime/session.py:19` | 🟡 | 新增 `PlanModeState` typed 字段，过渡期兼容 `metadata["plan_mode"]` |
| `kernel/engine.py:1547-1606` | 🟡 | round 体注入 full/sparse plan reminder（复用 `:1559` 机制） |
| `kernel/engine.py:877` | 🟡 | post-compact 先保活 typed plan state；Phase 4B 后重注入 plan 文件；compaction 后重置 `reminded_full` |
| `runtime/prompt_builder.py:421` | 🟡 | plan reminder 移出 `system_prompt_suffix`（腾预算）；改走 engine 每轮注入 |
| `services/web_chat_runtime.py:434/609/914` | 🟡 | `_activate_interactive_plan_mode` 写 typed 状态；suffix 注入改 engine 注入 |
| `tools/service.py:487/543` | 🟡 | 两闸门统一为 typed state + ContextVar 判据；新增中心化 read-only policy；Phase 4B 才加 exact path 写白名单 |
| `tools/handlers/plan_mode.py:141` | 🟢/🟡 | `exit_plan_mode` 保留为出口；成功后由 runtime 清状态 + 回注入待确认计划 |
| `services/plan_mode_service.py:393` | 🟡 | Phase 4A 继续接 structured fill；Phase 4C 才引入 markdown 抽取；`use_agent_planner` 分支收敛为 fallback |
| `services/agent_plan_planner.py` | 🔴 | 主路径不再调用；保留为无人值守 fallback，放宽轮数/输出，复用主循环 prompt |
| `services/plan_mode_core.py:392/418/505/909` | 🟢 | hash / skeleton / fill / validate_confirmation 全部不动 |
| `services/plan_mode_gate.py:81` | 🟢 | gate 决策核心不动 |

---



- 已知的 synthesis 死结：`## Key Findings` 强制"每维度一个 `###` 子节"（COVERAGE IS MANDATORY）与"INTEGRATION NOT SUMMARIZATION"直接打架，导致"6 维度拼接、缺贯穿论点"（历史 RC11-RC15）。
- 外推主张（下一篇细化）：把 synthesis 也视为"主循环里的一次分析写作"，覆盖检查从"写作时结构强制"挪到"事后独立 critic agent 核查"，让 INTEGRATION 不再被 COVERAGE 钳制。


---

## 10. 与 Work Ledger 的关系

`docs/plan-mode-agent-work-ledger.md` 的 Work Ledger 与本文的 plan artifact**职责不同，必须分离**：

| | Plan artifact（本文；Phase 4A 为 `exit_plan_mode` structured fill，Phase 4B 后可为 plan 文件） | Work Ledger |
|---|---|---|
| 阶段 | 规划期（确认前） | 执行期（确认后） |
| 内容 | 用户可确认的计划 | agent 执行进度/findings/failures/verification |
| 治理 | 加密确认边界 | agent 私有工作状态，不能扩张确认边界 |
| 注入 | Plan Mode 每轮 reminder + compaction 保活 typed state；Phase 4B 后保活 plan 文件 | 执行期每轮注入摘要（建议同样改进，见下） |

**附带建议**（可纳入 work ledger 文档）：执行期 Work Ledger 也应该像 plan state 一样纳入每轮动态注入和 compaction 保活；但这属于 Work Ledger 后续增强，不能扩张用户确认边界。

---

## 11. 落地阶段

### Phase 0：文档定稿（本文）+ 测试夹具
- 本文拍板。
- 写失败测试先定义目标行为：plan mode 激活时主循环每轮收到 reminder；typed state 能兼容 `metadata["plan_mode"]`；只读闸门统一判据；`exit_plan_mode` 是唯一审批出口；治理 hash 不变。

### Phase 1：PlanModeState 一等字段
- `session.py` 加 `PlanModeState`，兼容旧 `metadata`。
- `web_chat_runtime` 写 typed 状态，同时在工具执行期间镜像到 `plan_mode_runtime_context` ContextVar。

### Phase 2：每轮 reminder 注入 + compaction 保活
- `engine.py` round 体注入 full/sparse。
- post-compact 重置 `reminded_full=False`，并保活 typed plan state。
- reminder 移出 `system_prompt_suffix`。

### Phase 3：只读闸门统一
- `tools/service.py` 两闸门收敛为 typed state + ContextVar 判据。
- 新增中心化 `PLAN_MODE_READONLY_TOOLS` / `is_plan_mode_write_allowed()`；不要直接复用 `PLANNER_ALLOWED_TOOLS`。
- Phase 3 默认仍禁止所有 workspace 写入。

### Phase 4A：structured fill 主路径收敛
- `exit_plan_mode` → `ensure_awaiting_plan_from_fill()` → `generate_plan(use_agent_planner=False)` 作为有用户在场的主路径。
- `plan_hash` / `validate_confirmation` / `PlanModeGate` 链路验证不变。

### Phase 4B：plan 文件写作载体（可选增强）
- 创建 exact `PlanModeState.plan_file_path`，如 `workspace/plans/{plan_id}.plan.md`。
- 只放行 `write_file`/`edit_file`/`fs_write mode=write|edit` 命中 exact path；禁止 delete 和目录级白名单。
- compaction 后重注入 plan 文件引用并重发 full reminder。

### Phase 4C：markdown → plan_json 抽取（可选增强）— **决议：跳过（accepted skip, 2026-06-02）**

> 原设计：系统读 plan 文件并抽取治理 `plan_json`，抽取失败进入 `planning_failed`。

**决议：不实装。** 确认契约保持为 `exit_plan_mode` 的 structured fill（`handlers/plan_mode.py` 要求 title/objective/plan_markdown/steps/success_criteria/stop_conditions → 直接生成 fill → `plan_json` → `plan_hash`）。Plan 文件（4B）是 planning workspace，**不是 approval 的 source of truth**。

理由：
- 4A structured fill + 4B plan 文件 + Phase 6 PlanCard 已覆盖主要体验：agent 可渐进写 plan 文件，最终用 structured `exit_plan_mode` 出确认卡，PlanCard 已展示 assumptions/open_questions。
- md→json LLM 抽取引入额外模型调用、不确定解析、新的 `planning_failed` 分支，收益仅「agent 少填一次 JSON 参数」，不值得。
- 关键：LLM 抽取会把确认契约变成**二次解释产物**，削弱现有 `plan_hash` / `validate_confirmation` 的确定性边界。

**Follow-up 触发条件**：仅当 telemetry 显示 agent 反复写出好 markdown 却不调用带 structured args 的 `exit_plan_mode` 时，才重新考虑。

**届时的护栏（若做务实版）**：只能是**窄 fallback**——`plan_markdown` 缺失且 plan 文件存在时，把文件内容作为 **preview**；steps/success_criteria/stop_conditions **仍必须由 structured args 提供**。绝不让 plan 文件成为第二个 source of truth。

### Phase 5：主路径切换 + RPC 退化
- web chat 默认主循环范式。
- live chat 内的 tool-intercept 不再同步跑 RPC planner，而是把 intercepted tool args 注入 interactive Plan Mode。
- `agent_plan_planner` 降为无人值守 fallback，放宽轮数/输出，复用主循环 prompt。

### Phase 6：前端规划过程可见
- plan card 支持 `planning` 流式态、assumptions/open_questions、revise 重进规划。

---

## 12. 测试矩阵

| 场景 | 期望 |
|---|---|
| web 显式进 plan mode | `plan_mode.active=True`；主循环每轮注入 reminder（首轮 full，后续 sparse） |
| plan mode 下调 set_trigger | 被只读闸门拦，返回只读违规 |
| Phase 3 plan mode 下 write_file 到任意文件 | 拦截 |
| Phase 4B plan mode 下 write_file 到 exact plan 文件 | 允许 |
| plan mode 下 write_file 到业务文件 | 拦截 |
| Phase 4B plan mode 下 `fs_write mode=delete` 到 plan 文件 | 拦截 |
| agent 用文字问"计划行不行" | reminder 已禁止；以 `exit_plan_mode` 为唯一出口（人工/eval 核查） |
| compaction 发生在 plan mode | typed state 保活；下一轮重发 full reminder；Phase 4B 后 plan 文件引用被重注入 |
| `exit_plan_mode` 提交 | 进 `awaiting_confirmation`；`plan_hash` 与确认链路与今天一致 |
| 用户确认 | 只消费同一 `plan_id+version+hash`（`validate_confirmation` 不变） |
| live chat tool-intercept 触发规划 | 进入 interactive Plan Mode，不跑 `channel="internal"` RPC planner |
| 无人值守 trigger 触发规划 | 走 fallback；仍 fail-closed；Phase 4B 后才写 plan 文件 |
| schema 不合格 | `planning_failed`，不执行 |

---

## 13. 验收标准

1. web chat 的 plan mode 规划全程发生在主对话循环（无 `channel="internal"` 隔离子调用）。
2. plan mode 激活期间，主循环每轮消息序列中可观测到 plan reminder（full→sparse）。
3. compaction 后 typed plan state 仍在上下文，agent 不丢失计划进度；Phase 4B 后 plan 文件引用也保活。
4. 只读闸门以 typed state + ContextVar 镜像裁决，read-only policy 中心化，无两份漂移 allowlist。
5. `exit_plan_mode` 是唯一审批出口；agent 旁路提问被范式约束。
6. `plan_hash` / `validate_confirmation` / `PlanModeGate` 行为与改造前逐字节一致（回归测试证明）。
7. 自动触发 fallback 路径仍 fail-closed，不绕过任何治理。
8. Phase 4B 的 plan 文件写白名单只允许 exact path，且 `fs_write mode=delete` 被明确拦截。

---

## 14. 风险与回滚

**生产现状**：Plan Mode 已部署，做过生产 cutover（316 个历史 trigger grandfathered）。本改造**只改计划生产路径，不改治理与已有数据**。

- **治理零改动** → 已确认/已 grandfathered 的计划不受影响。
- **灰度**：Phase 5 主路径切换用 feature flag。开发/测试环境先开；生产默认保持旧 RPC fallback，等 Phase 0-4 验收后再按环境打开，并保留一键回退 RPC 子调用。
- **回滚单位**：每个 Phase 独立可回滚；Phase 1-4A 是纯增强（加状态、加注入、统一闸门、收敛 structured fill），不破坏现有 interactive 路径；Phase 4B/4C 和 Phase 5 是行为切换点。
- **fallback 永在**：无人值守 RPC 路径保留，避免"没有主对话循环可附着"的场景失能。

---

## 15. 非目标

- 不把 Plan Mode 变成 `Agent.execution_mode` 持久人格。
- 不允许 agent 自我确认计划。
- 不绕过 capability approval / ActionPreflight / Memory Control Plane。
- 不把所有低风险同步动作升级成 Plan Mode（触发规则沿用 `plan-mode-design.md` §5）。

---

## 16. 待决问题（请拍板）

1. **plan 文件上线时机**：Phase 4A 先只做 structured fill 是否可以拍板？建议可以，Phase 4B/4C 作为后续增强。
2. **无人值守 fallback 的轮数上限**：放宽到与主循环同档（如 40）会增加无人场景成本，是否要单独设一个中间档（如 20）？
3. **plan 文件位置**：`workspace/plans/{plan_id}.plan.md` 是否与现有 workspace 约定冲突？需要核对 `ensure_workspace` 布局。
4. **reminder 语言**：full/sparse reminder 用中文还是英文？（agent system prompt 主体语言、目标用户语言会影响产出语言一致性）

---
