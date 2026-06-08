# Session Loop 对标 CC — 完整方案（6 决策点一次解决）

> 状态：**🏁 方案设计完成（2026-06-08），6 决策点全拍，待实现**。诊断底稿见 `docs/session-loop-cc-alignment.md`（病灶 A-J）。三条线详细设计：`plan-mode-axis-design.md`(A/E/G-H) / `multi-agent-mainline-F-design.md`(F) / `prompt-tools-design.md`(I/J)。
>
> **▶ RESUME（compact 后从这里继续）**：进入实现，按落地顺序 **G/H.2+3 → E → I+J → A → F → G/H.1**，每组一个完整 PR、红测先行、真 PG（Testcontainers）、零 MVP 债。**第一站 = G/H.2+3**（`long_task` 一词三义收敛 + `tool_action` handoff 断链→`continue_current_session`，详见 `plan-mode-axis-design.md` G/H 节）。
>
> 交付纪律（仓库 CLAUDE.md「一次改完·禁 MVP·零债」）：每个决策点的设计必须**完整 scope up front**——测试、边界、错误路径、schema 迁移、legacy 回填、生产清理、可观测，全部一次到位。

## 组织脊柱 — CC 两轴、无调度器（实证基线）

CC 没有"模式调度器"。四形态分两轴，本方案的所有改动都服务于"把 Hive 那个不该存在的代码调度器拆掉，回归 CC 两轴"：

- **权限/模式轴**：Plan Mode 是用户控制的模式（用户 `shift+tab`/命令进入，或 AI 用 `EnterPlanMode` 工具**请求**、用户批准才进）。
- **工具轴**：Sub-agent（`spawn`）、Workflow（`start_workflow`）是**纯工具**，AI 在 ReAct loop 里自主调用，prompt 教它何时用。
- **普通 ReAct** = 永远在跑的底座。

判断归模型（L1），触发/批准归用户（零强加），治理是 L2 叠加（复用 Plan Mode 批准，不另起特判）。

## 6 决策点锁定方向

| 决策点 | 方向 | 关键不变量 |
|--------|------|-----------|
| **A** entry | 用户控制进入 + AI 用 `EnterPlanMode` 式工具请求（需批准）；砍 `auto`；判断挪进提示词 | 除用户显式/批准外，无路径激活 Plan Mode |
| **E** plan→执行 | `objective_trigger` 携带 `plan_markdown` 作为定时执行指令 | 计划正文 = 执行指令，单一交接 |
| **F** multi-agent | **push 对齐 CC**；统一 spawn/delegate、去机械信封；单一看板做跟踪 | CC 无认领；看板=跟踪非调度中枢；认领 defer 为 delta |
| **G/H** 模式边界 | 拆调度器：Plan=模式开关、Sub-agent/Workflow=纯工具；删散落特判 | 无统一 mode dispatcher；`long_task` 一词一义 |
| **I** 系统提示词 | 补 `tasks`/`tools` 段到 benchmark；修文档漂移；评 16K cap | 全段 benchmark 质量；soul 不被静默裁 |
| **J** MCP | MCP 工具进 `tool_search` deferred 面 | Skill/MCP/web/feishu 统一暴露心智 |

## 详细实现设计（三条线，已落盘）

| Track | 决策点 | 设计文档 |
|-------|--------|----------|
| **1 — Plan Mode 轴** | A entry + E plan→执行 + G/H 模式边界 | `docs/plan-mode-axis-design.md` |
| **2 — Multi-agent** | F push 对齐 + 统一派发 + 单一看板 | `docs/multi-agent-mainline-F-design.md` |
| **3 — Prompt & 工具暴露** | I 系统提示词 + J MCP→tool_search | `docs/prompt-tools-design.md` |

每个 doc 含：改动文件（函数级）/ 契约 schema / 测试计划（红测先行·真 PG）/ 迁移+回填 / 验收 / 硬骨头。

## 跨线硬骨头汇总（实现必读）

| # | 决策点 | 硬骨头 |
|---|--------|--------|
| ① | A | Hive 无 CC 同步终端批准 → 用「进入低门槛(read-only) + 执行高门槛(PlanCard)」替代 CC flip-时-批准 |
| ② | A | 无人值守 `request_plan_mode` 必须 fail-closed（无在场用户批准） |
| ④ | E | 注入点在 **trigger fire 时**(`trigger_daemon`)非 handoff 创建时；config 存 plan_id、fire 回读 plan row |
| ⑥ | G/H | `execution_mode` 是**两个概念**（DB agent.execution_mode 不改 / request cache-hint 改名 invocation_scope）；`invoker.py:229` 必须显式解耦 |
| 🦴 | F | DB `Task` 表**不能删**（supervision/api/task_executor 三活体消费者）→ 工具下线保留服务函数；status ENUM 在线迁移不可事务回滚（需停写窗口或边界翻译 fallback） |
| 🦴#1 | J | `agent_tools.py:525` 对未 backfill agent 会 **force-enable 非 default MCP 工具 = 权限提升** → 必须对 type==mcp 收紧 + RED 测 |
| 🦴#2 | J | `_tool_search`(text) 与 `_resolve_tool_expansion`(schema) **必须共用** `list_agent_mcp_deferred_tools`（防"面板说X运行做Y"） |
| 🔗 | I+J | tools.py 文本声称"MCP via tool_search" + 高 severity 契约 check 是 tripwire → **I 的 tools.py-text 与 J discovery 必须同时 land**（prompt 不能撒谎） |

## 落地顺序（一次完整交付，内部有序施工；每组一个完整 PR，无 MVP 债）

1. **G/H.2+G/H.3**（命名/断链收敛，纯重构 + 兼容别名，风险最低，先清地基）
2. **E**（trigger 注入 plan_markdown，独立可测，价值高）
3. **I + J**（系统提示词 + MCP→tool_search，强耦合同 PR）
4. **A**（删 auto + `request_plan_mode` 工具 + prompt 引导；依赖 Track 3 的 `plan_mode_guidance` + Track 1 eligibility helper）
5. **F**（multi-agent：派发对称 + 单一看板；依赖 G/H 的 manage_tasks 解绑）
6. **G/H.1**（`execution_mode` 改名，面最广、与 F 冲突面大，单独窗口最后做）

## ✅ 已拍板 + 核心不变量澄清（用户 2026-06-08）

**F 单一看板存储 = Work Ledger JSON（存 JSON / 呈现 MD）—— 已确认可行。**

**"MD-first"真义被校正（用户纠正我的误读）**：用户说"像写 MD 一样写 task"指的是 **目的——调用模型自己的分析能力去写整个 task 的内容（智能生成）**，不是 **格式——存成 MD 文件**。我之前盯着 JSON-vs-MD 的存储之争，是看错了层。

→ **F 核心不变量（实现必守）**：看板的 task **内容主体（subject/description/分析/上下文）必须由模型调用分析能力自由书写（智能生成）**；**禁止代码模板 / 机械结构化字段填充充当内容**。结构化（id/status/owner/JSON 存储）只是承载回写/调度的元数据外壳，不替代内容生成。这与 F 的派发判据（主 agent 智能生成派发指令）是**同一个 AI-Native L1 精神**——multi-agent 里 task 内容 + 派发指令都由模型智能生成，代码只承载与调度。

存储 JSON / DB `Task` 表保留 / J `:525` 收紧 —— 三个硬骨头均按各自设计文档执行。
