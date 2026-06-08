# Multi-agent 主链 F — 实现设计（push 对齐 CC）

> 状态：实现设计（grounding 完成，待拍板后落地）。本文不写代码，写"改哪里、怎么改、怎么验"。
> 锁定方向（不重开）：**F = push 对齐 CC，认领（claim）本轮 defer**。CC 的 `AgentTool` 即 push 模型——主 agent spawn → 传 prompt → 子 agent 隔离跑 → 返回 digest，无 claim、无共享作业表。Hive 对齐这个形态。
> CC 基线锚点：`src/tools/AgentTool/runAgent.ts`（push 派发）、`src/utils/tasks.ts:69`（`TASK_STATUSES = ['pending','in_progress','completed']`）、`src/utils/tasks.ts:76-89`（`TaskSchema {owner,status,blocks,blockedBy}`）、`src/tools/TodoWriteTool/`（轻量 todo）。

---

## 0. 现状三句话（grounding）

1. **派发不对称**：
   - `spawn_subagent`（无身份 worker）— `task` 由主 agent 自由文本**逐字透传**：`backend/app/agents/subagent.py:594-595`（`messages.append({"role": "user", "content": task})`）。✅ 对的。
   - `delegate_to_agent`（有身份同事）— `message` 在工具层先包成 `[Delegated by {name}] {message}`（`backend/app/services/agent_tool_domains/messaging.py:1132-1136`），但 `_delegate_after_cycle_check` 在 `backend/app/agents/orchestrator.py:844` 调 `_build_delegation_brief()`（定义 `orchestrator.py:515-543`），把它**丢弃并重写成固定三段式模板**（`## Delegated Task Brief` / `### Parent Context Snapshot` / `### Expected Return`），且子会话还叠加一个 ~150 行的 `_build_delegated_worker_prompt`（`orchestrator.py:195-317`）强制 `Completed/Evidence/Blockers` 三段返回。这是要消除的**机械信封**。

2. **两套看板并存**：
   | | DB `Task` + `manage_tasks` | Work Ledger + `track_todo` |
   |---|---|---|
   | 存储 | PostgreSQL `tasks` 表（`backend/app/models/task.py`）+ `tasks.json` 镜像（`backend/app/tools/workspace.py:317-340`） | 每-agent JSON（`AGENT_DATA_DIR/<id>/runtime_artifacts/.../work_ledger.json`，`backend/app/services/agent_work_ledger.py:75-91`） |
   | status 枚举 | `pending / doing / done`（`task.py:31`） | `pending / in_progress / completed`（`agent_work_ledger.py:105-111`，已把 `done`/`complete` 归一为 `completed`） |
   | 创建语义 | **create-即-execute**：`task_type=="todo"` 时 `asyncio.create_task(execute_task(...))`（`backend/app/services/agent_tool_domains/tasks.py:53-55`） | **写≠执行**：纯认知便签，never `governance="sensitive"`、never `plan_gate`（`backend/app/tools/handlers/work_ledger.py:1-22`） |
   | 治理 | `governance="sensitive"` + `plan_gate_action_kind="start_long_task"`（`backend/app/tools/handlers/tasks.py` ToolMeta） | `governance` 默认 safe；`read_ledger` 显式 safe |
   | 字段 | title/description/type/status/priority/assignee/due_date/plan_*/supervision_* | id/title/status/owner/blocks/blockedBy/evidence_refs + findings/failures/open_questions/verification/progress |
   | 已对齐 CC 的形态 | ❌（`doing`/`done` 偏离，create 耦合 execute） | ✅（status 三元组、owner、blocks/blockedBy 与 `tasks.ts` 完全一致） |

3. **派发↔看板已部分接线**（push + 状态镜像）：`spawn_subagent` / `delegate_to_agent` 都接受 `ledger_todo_id`，spawn 时 `assign_todo_owner`（盖章 owner + in_progress），完成时 `record_delegated_todo_status`（completed / 失败回 pending，`expected_owner` fail-closed）。代码：`subagent.py` spawn 路径 + `orchestrator.py:691-748`（`_stamp_ledger_todo_owner` / `_write_back_ledger_todo`）。但**看板有两套**，镜像只接到 Ledger 一套——这正是要"做干净、单一"的点。

**设计判据（北极星 + AI-Native L1）**：单一看板必须让模型**自由表达内容**（标题/描述/活动态由 LLM 写，不机械改写），看板只做状态镜像与认知脚手架，不当调度中枢、不驱动派发、不认领。

---

## 1. 改动文件清单（函数级）

### 1.1 派发对称化（消除 delegate 的机械信封）

**目标形态（对齐 CC `runAgent.ts`）**：主 agent 写的指令**逐字进入子会话第一条 user message**，外层只挂"轻量上下文"——即"谁委派的 + 期望返回"一两句，而非用模板**重写指令本身**。spawn 已经是这样；delegate 要变成这样，但保留 spawn=无身份 worker / delegate=有身份同事 的本质差异（delegate 仍签发 delegation_token、走有身份目标 agent 的 soul/记忆、保留 depth/cycle 防护）。

| 文件 | 函数 | 改动 |
|---|---|---|
| `backend/app/agents/orchestrator.py` | `_build_delegation_brief`（`:515-543`） | **退役"重写"语义**。改为：取主 agent 最新一条指令（`conversation_messages[-1]["content"]`，即工具层已构造的指令文本）**逐字**作为子会话 user message，不再折叠 8 条历史成 `## Delegated Task Brief` 三段式。仅当 `conversation_messages` 为空时回退到一句兜底。函数更名/收敛为 `_delegation_user_message()`，语义="透传指令"，非"合成简报"。 |
| `backend/app/agents/orchestrator.py` | `_build_delegated_worker_prompt`（`:195-317`） | **从"机械信封"降为"轻量上下文 + 同事身份提示"**。删除强制三段式 `<return_format>`/`<good_return_examples>`/`<bad_return_examples>`（~120 行）——这是 L1 违例（用模板框死了"如何返回"=框死了思考产物）。保留：`<isolation_contract>`（子会话隔离事实：看不到父对话、不能再委派——这是 harness 约束 L2，正当）、`<tool_policy>`（profile 的 tool_rule/memory_rule）。新增一句轻量 framing：`你正在作为同事处理 {parent_name} 委派的工作；完成后把结论与证据返回给委派方`（描述性，非格式模板）。 |
| `backend/app/agents/orchestrator.py` | `_delegate_after_cycle_check`（`:814-997`，调用点 `:844-849`） | 把 `delegated_brief = _build_delegation_brief(...)` 换成 `delegation_user_message = _delegation_user_message(request.conversation_messages)`；`combined_suffix` 仍叠 `request.system_prompt_suffix` + 瘦身后的 worker 上下文。`invocation.messages` 用透传文本。 |
| `backend/app/services/agent_tool_domains/messaging.py` | `_delegate_to_agent_async`（`:1095-1156`） | `conversation_messages` 的 `content` 由 `f"[Delegated by {source_agent.name}] {message_text}"` 改为**纯 `message_text` 逐字**；委派来源（`from_agent` / `parent_name`）改为通过结构化字段传递（`AgentDelegationRequest` 已有 `parent_agent_id`；新增携带 `parent_agent_name` 供 worker 上下文 framing 用），不再把来源**前缀拼进指令文本**。这样指令 = 主 agent 智能输出，来源 = 旁路元数据。 |
| `backend/app/agents/orchestrator.py` | `AgentDelegationRequest`（`:420-445`） | 新增 `parent_agent_name: str | None = None` 字段（worker framing 用），与既有 `parent_agent_id` 并列。`delegate_to_agent` / `delegate_async` 签名透传。 |

> **批判 / 硬骨头①（对称化的"度"）**：spawn 的 worker **无身份**（kernel 默认 soul，`standalone_system_prompt` 语义），delegate 的目标**有身份**（真实 agent 的 soul.md/记忆/relationships）。"对称"指的是**指令透传形态对称**（都逐字），不是"两者变成同一个东西"。worker_safe/research_readonly 等 `DelegationToolProfile`（`messaging.py:67-154` 的 5 个 profile）**保留**——这是 delegate 的能力边界治理（L2），与"指令透传"正交。删 `_build_delegation_brief` 的历史折叠是对的；但**不要**顺手删 `tool_profile` 机制（那会破坏有身份同事的权限分级）。

### 1.2 单一看板收敛

**决策：看板单一化为 Work Ledger（JSON，per-agent scoped），退役 `manage_tasks` 的 agent-facing todo 创建路径。** 理由见 §2.3。

| 文件 | 函数/符号 | 改动 |
|---|---|---|
| `backend/app/tools/handlers/tasks.py` | `manage_tasks` ToolMeta + handler | **从 agent 工具面退役**（不再注册为 LLM-callable）。`list_tasks` / `get_task`（DB 读）一并从默认工具面移除（它们是 DB Task 的读侧，看板归一后 agent 用 `read_ledger`）。DB Task 的 REST/supervision 仍在（见 §1.4），故**保留服务函数 `_manage_tasks`** 供 REST 复用，仅去掉"工具暴露 + create-即-execute"。 |
| `backend/app/services/agent_tool_domains/tasks.py` | `_manage_tasks`（`:17-93`） | **拆掉 create-即-execute**。把 `if task_type == "todo": asyncio.create_task(execute_task(...))`（`:53-55`）从这里移除。`_manage_tasks` 退化为纯 CRUD（供 REST `api/tasks.py` 调用；REST 自己在确认 plan 后显式触发 `execute_task`，见 `api/tasks.py:120-121` 已是分离的）。**新增 status 归一**：写库时 `doing→in_progress`、`done→completed`（见 §1.3 迁移）；或保留 DB 旧枚举但在边界翻译（见 §2.4 取舍）。 |
| `backend/app/tools/handlers/work_ledger.py` | `track_todo` ToolMeta | **升格为唯一看板工具**。description 去掉"To actually launch an autonomous job use `manage_tasks` instead"（`work_ledger.py:62-65`）——`manage_tasks` 已退役，改为"启动后台自治工作用 `delegate_to_agent`/`spawn_subagent`/`start_workflow`"。`track_todo` 仍是认知动作（写≠执行），CC `TodoWrite`+`TaskUpdate` 的合并体。 |
| `backend/app/runtime/prompt_sections/tools.py` | `:30-31` | 删 `manage_tasks`/`list_tasks`/`get_task`/`tasks.json` 引导文案；改为"用 `track_todo`/`read_ledger` 维护你的看板"。 |
| `backend/app/tools/governance.py` | `_STATIC_SENSITIVE_TOOLS`（`:29`）/`_STATIC_SAFE_TOOLS`（`:43-44`） | 移除 `manage_tasks`（sensitive 集）、`list_tasks`/`get_task`（safe 集）的静态登记。 |
| `backend/app/services/capability_gate.py` | `CAPABILITY_MAP`（`:45-47`）+ 退役清单 | 移除 `get_task`/`list_tasks`/`manage_tasks` 的能力映射（若它们彻底下线工具面）。`track_todo`/`record_finding`/`read_ledger`（`:54-56`）保留。 |
| `backend/app/tools/plan_gate_registry.py` | `_PLAN_GATED_TOOL_NAMES`（`:43-49`）+ `_manage_tasks_action_kind`（`:96-103`）+ `hard_gated_action_kind`（`:179`分支） | 移除 `manage_tasks` 条目与其 `start_long_task` 分支函数。**注意 Track 1 交叉点**：`start_long_task` action_kind 本身**不删**（REST `api/tasks.py:120` 与 manual trigger `:213` 仍用它做 plan gate），只删"`manage_tasks` 工具→`start_long_task`"这条工具侧绑定。 |
| `backend/app/templates/system_skills/delegation-guide/SKILL.md` | 决策表 + 文案 | 不涉及 `manage_tasks`（该 skill 只讲 delegate/message），但新增一行"看板：用 `track_todo` 记录派发出去的 todo + `ledger_todo_id` 关联"。 |
| `backend/app/kernel/runtime_guidance_catalog.py` | `:587`/`:637`（已 mapped to track_todo）、`:116`/`:898`（spawn/delegate schemas） | 校对引导话术，确认无 `manage_tasks` 残留指向。 |

### 1.3 派发↔看板接线（做干净、单一）

接线已存在，收敛为"单一看板 = Work Ledger"后，去掉所有指向 DB Task 的镜像歧义：

| 文件 | 函数 | 改动 |
|---|---|---|
| `backend/app/agents/orchestrator.py` | `_stamp_ledger_todo_owner`（`:696-721`）/ `_write_back_ledger_todo`（`:724-748`） | **保持现状**（已正确：Ledger 是观察面非控制面，失败 non-fatal）。仅确认它们写的是 Work Ledger（是），不触碰 DB Task（是）。 |
| `backend/app/agents/subagent.py` | spawn 路径 `ledger_todo_id` 接线 | **保持现状**（`subagent.py:143` 已 stamp）。 |
| `backend/app/tools/handlers/work_ledger.py` | `track_todo` 的 `owner` 字段 | 文档化"owner 写另一个 agent 的 id/name = 你把这条 todo 派给它的认知记号；真正派发仍要调 `delegate_to_agent`/`spawn_subagent` 并传 `ledger_todo_id`"。（`upsert_agent_work_ledger_todo` docstring `:473-479` 已有此语义，只需在工具 description 镜像一句。） |

> 接线契约（单一、清晰）：**主 agent → `track_todo(add, owner=X)` 建条目拿 `item_id` → `delegate_to_agent(..., ledger_todo_id=item_id)` 或 `spawn_subagent(..., ledger_todo_id=item_id)` 真派发 → runtime 自动 `assign_todo_owner`（盖章）→ 完成自动 `record_delegated_todo_status`（回写）**。看板永远是镜像，派发永远是 push，两者通过 `ledger_todo_id` 单点关联。

### 1.4 DB Task / tasks.json / ORM 存储处理

**不删 DB `Task` 表**——它有 3 个 agent-工具面之外的活体消费者，删表是破坏性事故：
- `backend/app/services/supervision_reminder.py:341-343`：督办 tick 读 `Task.type=="supervision"` + `status in ("pending","doing")`，发飞书提醒。
- `backend/app/api/tasks.py`：人类 UI 的任务 CRUD + 手动触发（`:120-121`、`:213-214`）。
- `backend/app/services/task_executor.py`：REST 确认 plan 后的 todo 后台执行。

处理方式：
| 文件 | 改动 |
|---|---|
| `backend/app/services/agent_tool_domains/tasks.py:53-55` | 删 `manage_tasks` 路径里的 `execute_task` 自动触发（agent 侧不再 create-即-execute）。REST 侧（`api/tasks.py`）的 `execute_task` 触发**保留**——人类在 UI 显式建任务并确认 plan，是合法的 push 入口。 |
| `backend/app/tools/workspace.py:317-340`（`_sync_tasks_to_file`）| `tasks.json` 镜像**保留但语义降级**为"DB Task（含 supervision + 人类建的 task）的只读快照文件"，不再是 agent 看板。agent 看板看 `work_ledger.json`。不删 `tasks.json` 避免破坏读它的现存代码（`filesystem.py`/`agent_seeder.py`/`agent_manager.py` 都引用）。 |
| `backend/app/models/task.py:31` | DB Task status 枚举**保留 `pending/doing/done`**（见 §2.4 取舍：边界翻译而非迁库），或做一次性归一迁移（见 §4）。supervision_reminder 的 `status.in_(["pending","doing"])` 查询若改枚举需同步更新（`supervision_reminder.py:343`）。 |

---

## 2. 契约 / Schema

### 2.1 统一后的派发工具参数

**`spawn_subagent`（无身份 worker）— 不变**（已 push、已透传）。参数保持：`task`（逐字透传，required）/ `type`(explorer|worker|critic) / `name` / `definition_name` / `max_tool_rounds` / `ledger_todo_id`。源：`backend/app/tools/handlers/subagent.py:_SPAWN_PARAMETERS`。

**`delegate_to_agent`（有身份同事）— 参数不变，语义变**：`message` 描述从"Precise task instructions"保持，但**运行时不再机械重写**——`message` 逐字成为同事的 user message。参数：`agent_name` / `message`（逐字透传，required）/ `max_tool_rounds` / `parent_session_id` / `tool_profile`(worker_safe|memory_readonly|review_readonly|research_readonly) / `ledger_todo_id`。源：`backend/app/tools/handlers/communication.py` delegate_to_agent ToolMeta。

**对称性契约（验收锚）**：spawn 与 delegate 都满足"主 agent 写的指令字符串 == 子/同事会话首条 user message 内容"（不含任何 `## Delegated Task Brief` / `[Delegated by X]` 前缀或三段式包装）。来源/期望返回作为**结构化旁路上下文**（`parent_agent_name` + isolation_contract），不污染指令本身。

### 2.2 单一看板数据模型（Work Ledger 的 todo 子集）

权威 schema = `agent_work_ledger.v1` 的 `todo_items[]`（`backend/app/services/agent_work_ledger.py:121-145` `_normalize_work_item`）：

```
TodoItem {
  id: string                       # uuid hex, 服务端生成
  title: string                    # 模型自由表达（= content = subject 三别名同值）
  description: string              # 模型自由表达
  status: "pending"|"in_progress"|"completed"   # ← CC tasks.ts:69 完全一致
  activeForm: string               # 进行时态展示串（CC TaskUpdate.activeForm 对齐）
  owner?: string                   # 派给谁（agent id/name）— 认知记号，非派发触发
  blocks: string[]                 # 此 todo 完成前不能开始的 todo ids（CC tasks.ts:84）
  blockedBy: string[]              # 必须先完成的 todo ids（CC tasks.ts:85）
  evidence_refs: string[]          # 证据路径/链接
  required: bool
  updated_at: iso8601
}
```

CC 对齐确认：`{owner, status, blocks, blockedBy}` 与 `src/utils/tasks.ts:76-89 TaskSchema` 逐字段对应；status 三元组与 `TASK_STATUSES` 逐值对应。Hive 多出的 `description/activeForm/evidence_refs/findings/failures` 是 CC `TodoWrite` + Hive 认知脚手架的并集，不冲突。

**status 归一（唯一枚举）**：全平台 agent 看板统一 `pending / in_progress / completed`。`_normalize_status`（`agent_work_ledger.py:98-111`）已把 `done`/`complete`/`running`/`in_progress` 都收敛到这三个值——**无需新代码，已经是单一枚举**。DB Task 的 `doing`/`done` 仅在 REST/supervision 边界存在，归一策略见 §2.4。

**MD-first 落地形态**：看板内容（title/description/activeForm/findings）由模型自由表达，**持久化用 JSON**（`work_ledger.json`），渲染给模型/UI 时可投影成 MD（`render_work_ledger_reminder_snapshot` `agent_work_ledger.py:855-900` 已产出 `#id [status] title` 的 MD 行；`render_work_ledger_resume_block` `:815-852` 产出 MD reboot 块）。即：**存储 JSON（结构化、可校验、并发安全），呈现 MD（模型友好）**——对齐 CC（CC 的 todo 也是结构化存储 + 文本呈现，不是裸 MD 文件）。

### 2.3 存储形态取舍（MD vs DB vs JSON）—— 硬骨头②

| 选项 | 优点 | 缺点 | 判定 |
|---|---|---|---|
| **A. 纯 MD 文件**（agent 直接读写 markdown 看板） | 模型最自由、零 schema | 无并发安全、无状态机校验、blocks/blockedBy 关系难维护、镜像回写要 parse MD（脆）、`ledger_todo_id` 关联无稳定主键 | ❌ 否决：派发回写（`record_delegated_todo_status` 按 `item_id` 定位 + `expected_owner` fail-closed）需要**稳定主键 + 原子写**，MD 给不了 |
| **B. DB（沿用 `tasks` 表）** | 强一致、可查询、RLS | 与 create-即-execute 历史耦合深、status 枚举要迁库、跨 worker 进程要 DB 往返、和"轻量认知便签"定位冲突（CC 的 todo 是会话内轻量态非企业实体） | ❌ 否决：看板定位是"push 模型下的状态镜像/认知脚手架"，不是企业级任务实体；上 DB 是过度工程，且 supervision/人类 task 已经在 DB，再塞 agent todo 会污染语义 |
| **C. JSON（Work Ledger，现状）** | 已有完整 schema + 归一 + 镜像接线 + reboot/snapshot 渲染；per-agent scoped（`AGENT_DATA_DIR/<id>/`，天然隔离，对齐 §8 invariant 5）；原子写（`_write_ledger` `:189-203`）；稳定 `item_id` 主键 | 不是 DB，跨节点共享要靠文件系统/对象存储（与 agent workspace 同位，已是既定架构） | ✅ **选 C**：看板内容自由表达（JSON string 值，模型随便写）+ 结构化骨架（id/status/blocks 可校验）+ MD 投影呈现。三全其美，且零新基建。 |

> **结论**：单一看板 = **Work Ledger JSON**，存储 JSON、呈现 MD、主键稳定、并发原子、per-agent 隔离。DB Task 退出 agent 看板角色，降级为"supervision + 人类 UI task"的专用表。

### 2.4 DB Task status 归一取舍

两条路：
- **路 A（边界翻译，推荐 MVP）**：DB 枚举保留 `pending/doing/done`，仅在**对外呈现/对内消费**的边界翻译成 `pending/in_progress/completed`。改动面小（不动迁移、不动 supervision 查询），但留了"两套枚举字符串"的认知债。
- **路 B（一次性迁移，彻底）**：`ALTER TYPE task_status_enum` 把 `doing→in_progress`、`done→completed`，全平台单一枚举。改动面：Alembic 迁移 + `task.py:31` 枚举定义 + `supervision_reminder.py:343` 查询 + `tasks.py` 的 `args["status"]=="done"` 判断（`tasks.py:71`）+ `_sync_tasks_to_file` + schemas + 前端 TaskOut 消费。

> **推荐路 B**（彻底归一，符合"无双存储残留/单一枚举"验收）。但**批判硬骨头③**：`task_status_enum` 是 PostgreSQL ENUM 类型，`ALTER TYPE ... RENAME VALUE` 是在线 DDL（PG 10+ 支持），但**不可在事务块内回滚**，且需停写窗口或 `ADD VALUE` + 双写过渡。存量数据要 `UPDATE tasks SET status='in_progress' WHERE status='doing'`。详见 §4 迁移。若窗口风险不可接受，降级路 A（边界翻译）作为 fallback。

---

## 3. 测试计划（红测先行，真实依赖 / Testcontainers 真 PG，不 mock 业务核心）

> 原则（CLAUDE.md）：Functional Core 无 mock，Imperative Shell 用 Testcontainers 真 PG。派发对称/看板归一是 Functional Core（纯函数 + 文件 IO），用 tmp data_root；DB Task 迁移/REST 是 Shell，用真 PG。

### 3.1 派发对称性（新测试）

`backend/tests/agents/test_dispatch_symmetry.py`（新建）：
- **RED**：`test_delegate_passes_instruction_verbatim` — 给 `delegate_to_agent` 一条指令 `"Audit auth/*.py and list token bugs"`，断言子会话首条 user message `content == "Audit auth/*.py and list token bugs"`（**当前会失败**：现状会被 `_build_delegation_brief` 包成 `## Delegated Task Brief\n...`）。用 `invoke` 注入捕获 `AgentInvocationRequest.messages`（参照 `test_subagent_spawn_tool.py:124` 的 `fake_spawn` 捕获法）。
- `test_spawn_passes_task_verbatim` — 已有等价（`test_subagent_spawn_tool.py:146` `captured["task"]=="investigate"`），补一条断言"spawn 与 delegate 对同一指令产出相同首条 user message 内容"。
- `test_delegate_no_three_section_envelope` — 断言子会话 prompt suffix **不含** `Completed:`/`Evidence:`/`Blockers:` 强制模板串、不含 `## Delegated Task Brief`、不含 `[Delegated by`。
- `test_delegate_keeps_source_as_metadata` — 断言 `parent_agent_name` 进了 worker framing 上下文（结构化旁路），而非拼进指令。
- `test_delegate_preserves_identity_semantics` — 断言 delegate 仍签发 `delegation_token`（`orchestrator.py:855`）、仍用目标 agent 的 soul（`agent_id=request.target.id`），证明"对称 ≠ 抹平身份差异"。

### 3.2 看板单一化 + status 归一

`backend/tests/services/test_single_board_ledger.py`（新建）：
- `test_track_todo_is_only_board_tool` — 断言工具注册表里 `manage_tasks`/`list_tasks`/`get_task` 不在 agent 默认工具面（`collect_tools()` 结果），`track_todo`/`read_ledger`/`record_finding` 在。
- `test_ledger_status_enum_single` — 喂 `done`/`doing`/`complete`/`running`，断言落盘全是 `pending/in_progress/completed`（覆盖 `_normalize_task_status`，已有逻辑，钉死契约防回归）。
- 更新 `backend/tests/services/test_capability_gate_policy_surface.py`（已在改动清单，git status 显示 M）— 移除 `manage_tasks`/`list_tasks`/`get_task` 的 CAPABILITY_MAP 断言。

### 3.3 create 不触发执行（核心不变式）

`backend/tests/services/test_create_does_not_execute.py`（新建，真 PG via Testcontainers）：
- `test_track_todo_add_never_executes` — `track_todo(add)` 后断言**无** `execute_task` / 无 `RuntimeTask` 创建 / 无 async task spawn（监控 `asyncio.all_tasks()` 或注入 spy）。这是 Work Ledger 的定义性不变式（`work_ledger.py:6` 注释承诺）。
- `test_manage_tasks_path_retired` — 断言 `manage_tasks` 不再作为工具可达；REST `api/tasks.py` 建 todo 仍触发 `execute_task`（人类显式 push 入口保留），证明退役的是 **agent 侧 create-即-execute**，不是人类侧。
- 更新 `backend/tests/services/test_task_executor.py` — 若它经 `manage_tasks` 触发，改为经 REST/直调 `execute_task` 触发。

### 3.4 派发↔看板接线（单一）

更新现有 `backend/tests/agents/test_orchestrator_ledger_todo.py` + `test_subagent_ledger_todo.py`（已覆盖 assign/write-back/fail-closed）：
- 补 `test_ledger_todo_id_targets_work_ledger_only` — 断言镜像写的是 `work_ledger.json`，**不写** DB Task（防"双看板都镜像"回归）。

### 3.5 需要更新的现存测试（status 显示 M 或受影响）

| 测试 | 为何更新 |
|---|---|
| `tests/services/test_capability_gate_policy_surface.py`（M） | 移除退役工具的能力断言 |
| `tests/tools/test_plan_gate_registry.py` | `_PLAN_GATED_TOOL_NAMES` 移除 `manage_tasks`，断言更新 |
| `tests/tools/test_plan_mode_tool_gate.py` / `test_plan_mode_policy.py` | `manage_tasks`→`start_long_task` 分支删除后的门控断言 |
| `tests/services/test_task_executor.py` | create-即-execute 触发点改 REST |
| `tests/services/test_agent_tools_core_surface.py` | 默认工具面集合变化（移除 list_tasks/get_task/manage_tasks） |
| `tests/services/test_tool_registry.py` / `tests/tools/test_collector.py` | 工具注册集合变化 |
| `tests/templates/test_skill_capability_alignment.py` | delegation-guide / prompt 文案变更 |
| `tests/services/test_prompt_contracts.py` / `tests/runtime/test_invoker.py` | `prompt_sections/tools.py` 文案删 manage_tasks |
| `tests/agents/test_orchestrator.py` | `_build_delegation_brief` 重命名/语义变更 → 更新对 brief 内容的断言 |
| `tests/services/test_a2a_prompt.py` | worker prompt suffix 瘦身后断言更新 |

---

## 4. 迁移 + legacy 回填

### 4.1 DB Task status 归一迁移（路 B）

Alembic 新迁移 `migrations/versions/xxxx_normalize_task_status_enum.py`：
```
# upgrade（在线，分步，避免长锁）
1. ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'in_progress';   # PG: 不可在事务内，autocommit
2. ALTER TYPE task_status_enum ADD VALUE IF NOT EXISTS 'completed';
3. UPDATE tasks SET status='in_progress' WHERE status='doing';
4. UPDATE tasks SET status='completed'  WHERE status='done';
# 旧值 'doing'/'done' 暂留枚举（PG 不支持 DROP VALUE）；新写入只用新值。
# entrypoint.sh 的 ALTER ... IF NOT EXISTS patch 同步加这两个 ADD VALUE（向后兼容启动）。
```
- `backend/app/models/task.py:31`：枚举 `Enum("pending","in_progress","completed", name="task_status_enum")`（`create_constraint=False` 容忍残留旧值）。
- `backend/app/services/supervision_reminder.py:343`：`status.in_(["pending","in_progress"])`。
- `backend/app/services/agent_tool_domains/tasks.py:71`：`if args["status"] == "completed"`（REST 透传 mapped status）。
- 前端 `TaskOut` 消费（`frontend/src/` 任务状态显示）：`doing`→`in_progress`、`done`→`completed` 文案 + i18n（en/zh 都改）。

> **回填**：`UPDATE` 两条即全量回填存量行。无孤儿——`tasks` 表所有行都有 status。

### 4.2 Work Ledger JSON — 无需迁移

Ledger 的 `_normalize_task_status` 早已把任何历史 `done`/`doing`/`running` 在**读取时**归一为新枚举（`agent_work_ledger.py:105-111`），且 `_build_legacy_long_task_ledger_view`（`:998-1094`）已处理 work_ledger.json 之前的 `plan.json`/`progress.jsonl` 旧产物。存量 JSON 文件无需批量重写——读时归一即向后兼容。

### 4.3 tasks.json — 无需迁移

`_sync_tasks_to_file` 每次 DB 变更覆写，status 归一迁移后下次 sync 自动产出新枚举。旧 `tasks.json`（含 `doing`/`done`）会被下一次 sync 覆盖；读它的代码（`filesystem.py` 等）只展示不判断枚举值，无破坏。

### 4.4 向后兼容矩阵

| 入口 | 迁移前 | 迁移后 | 兼容手段 |
|---|---|---|---|
| agent `manage_tasks` | create-即-execute | 工具下线 | agent 改用 `delegate/spawn/start_workflow`（真派发）+ `track_todo`（看板）；prompt 引导已更新 |
| 人类 REST 建 task | execute_task | 不变 | REST 入口保留 |
| supervision tick | `status in (pending,doing)` | `status in (pending,in_progress)` | 迁移同步改查询；ADD VALUE 保证启动不炸 |
| 存量 DB 行 | doing/done | in_progress/completed | UPDATE 回填 |
| 存量 work_ledger.json | 任意枚举 | 读时归一 | 已有逻辑 |

---

## 5. 验收标准（可验证判据）

1. **spawn/delegate 对称** — `test_dispatch_symmetry.py` 全绿：同一指令字符串，spawn 的 `content=task` 与 delegate 的子会话首条 user message 内容**逐字相同**；两者都无三段式信封/来源前缀。grep `_build_delegation_brief` 在 `orchestrator.py` 已无"合成三段简报"语义（函数被 `_delegation_user_message` 透传替代）。
2. **身份差异保留** — delegate 仍签发 delegation_token、走目标 agent soul；spawn 仍是无身份 worker。`test_delegate_preserves_identity_semantics` 绿。
3. **单一看板** — `manage_tasks`/`list_tasks`/`get_task` 不在 agent 工具面（`collect_tools()` 断言）；`track_todo`/`read_ledger`/`record_finding` 是唯一 agent 看板工具。grep agent-facing tool registry 无 DB-task 工具。
4. **status 单一枚举** — agent 看板全平台 `pending/in_progress/completed`；DB Task 迁移后无 `doing`/`done` 新写入（`SELECT DISTINCT status FROM tasks` 不含旧值的**新行**）。
5. **create 不触发执行** — `track_todo(add)` 后零 `execute_task`/零 RuntimeTask/零 async spawn（`test_create_does_not_execute.py` 绿）。`agent_tool_domains/tasks.py:53-55` 的 `asyncio.create_task(execute_task)` 已从 agent 路径移除。
6. **无双存储残留** — `ledger_todo_id` 镜像只落 `work_ledger.json`，不写 DB Task（`test_ledger_todo_id_targets_work_ledger_only` 绿）。
7. **push 链路生产真接线** — 端到端：主 agent `track_todo(add,owner=X)` → `delegate_to_agent(ledger_todo_id=item)` → worker 隔离跑（透传指令）→ 完成 `record_delegated_todo_status` 回写 completed。真 PG + tmp data_root 集成测试跑通（不是只测函数，测 invoke_agent 真路径）。
8. **回归全绿** — §3.5 所有受影响现存测试更新后通过；`pytest` 全量 0 failed。

---

## 6. 风险 / 依赖 / 交叉点

### 6.1 与 Track 1（G/H 模式边界）交叉

- `manage_tasks` 曾挂 `plan_gate_action_kind="start_long_task"`（`tasks.py` ToolMeta）。退役工具后，**`start_long_task` action_kind 本身不能删**——REST `api/tasks.py:120`（人类建 todo）+ manual trigger `:213` 仍依赖它做 plan gate。只删"`manage_tasks` 工具 → `start_long_task`"的工具侧绑定（`plan_gate_registry.py:43-49,96-103,179`），保留 action_kind 定义（`plan_mode_core.ACTION_KINDS`）。
- **风险**：若 Track 1 的 G/H 边界设计假设"`manage_tasks` 是 long_task 的工具入口"，需与 Track 1 对齐——现在 long_task 的 agent 入口收敛为 `delegate_to_agent`（async）/ `start_workflow`（确定性）/ `spawn_subagent`（worker），`manage_tasks` 不再是入口。**依赖**：确认 Track 1 不重新引入 agent-facing `manage_tasks`。

### 6.2 与 Track 3 交叉

- Track 3（若涉及看板 UI / 前端任务视图）：`TaskOut` schema 的 status 枚举变更（`doing`→`in_progress`、`done`→`completed`）需前端 i18n 同步（en.json + zh.json）。Work Ledger 看板若要上前端，复用 `build_agent_work_ledger_display_view`（`agent_work_ledger.py:1097`）已产出 chat-safe view。
- **依赖**：前端任务面板消费 `Task.status` 的组件需同步枚举翻译。

### 6.3 "认领 defer" 的扩展位（本轮不做，但留好）

- **不引入**任何 claim/lease-to-claim 的共享作业表语义。CC 的 `blockTask`/swarm claim（`src/utils/tasks.ts` 的 `blockTask`）**不移植**。
- 扩展位（未来做 claim 时的接入点，本轮只留注释/不实现）：
  - `TodoItem.owner` 字段已在（CC `TaskSchema.owner`）——未来 claim 可复用此字段从"认知记号"升格为"认领锁"，但**本轮 owner 仅是 push 时的镜像记号**，不参与调度。
  - `blocks`/`blockedBy` DAG 已在 schema——未来若做依赖驱动调度（claim 模型），数据结构现成；本轮**纯笔记，不排序执行、不触发**（`work_ledger.py` description 已明确"never triggers or orders execution"）。
  - 派发仍是 push（主 agent 直接 spawn/delegate）。未来 claim = 在此之上叠"worker 主动认领看板条目"的反向流，届时新增独立工具（如 `claim_todo`）+ 共享作业表，**不改**本轮的 push 链路。

### 6.4 其它风险

- **硬骨头③（ENUM 迁移在线 DDL）**：PG `ALTER TYPE ADD VALUE` 不可事务回滚；若生产无停写窗口，降级路 A（边界翻译，DB 保留旧枚举，仅消费侧翻译）。决策点：拍板时确认窗口可用性。
- **delegate prompt 瘦身回归**：删 `_build_delegated_worker_prompt` 的三段式可能让某些"指望 worker 返回结构化 Completed/Evidence/Blockers"的下游解析失效。排查：grep 解析 worker 返回的代码（如有按 `Completed:` 切分的），改为消费自由文本 digest（CC 模型——返回是 digest 非固定格式）。**这是 L1 修复的正向收益**：不再用模板框死 worker 思考产物。
- **supervision_reminder 枚举依赖**：`status.in_(["pending","doing"])` 是硬编码字符串，迁移漏改会导致督办静默失效（查不到任务）。验收必须覆盖 supervision tick（真 PG，建 supervision task → tick → 断言 fire）。
- **`tasks.json` 多消费者**：`filesystem.py`/`agent_seeder.py`/`agent_manager.py`/`workspace.py`/`messaging.py` 都引用——降级为只读快照时确认无写冲突（仅 `_sync_tasks_to_file` 写，其余读）。

---

## 附：改动文件总览（按子任务）

**派发对称化**：`agents/orchestrator.py`（`_build_delegation_brief`→`_delegation_user_message`、`_build_delegated_worker_prompt` 瘦身、`AgentDelegationRequest.parent_agent_name`、`_delegate_after_cycle_check` 调用点）、`services/agent_tool_domains/messaging.py`（`_delegate_to_agent_async` 去前缀）。

**看板收敛**：`tools/handlers/tasks.py`（manage_tasks/list_tasks/get_task 下线）、`services/agent_tool_domains/tasks.py`（`_manage_tasks` 去 execute_task、status 归一）、`tools/handlers/work_ledger.py`（track_todo 升格、description 改委派话术）、`runtime/prompt_sections/tools.py`、`tools/governance.py`、`services/capability_gate.py`、`tools/plan_gate_registry.py`、`templates/system_skills/delegation-guide/SKILL.md`、`kernel/runtime_guidance_catalog.py`。

**DB/存储**：`models/task.py`（枚举）、`migrations/versions/xxxx_normalize_task_status_enum.py`（新）、`services/supervision_reminder.py`（查询枚举）、`tools/workspace.py`（tasks.json 降级注释）、`entrypoint.sh`（ADD VALUE patch）、`frontend`（TaskOut 枚举 + i18n）。

**测试**：新建 `test_dispatch_symmetry.py` / `test_single_board_ledger.py` / `test_create_does_not_execute.py`；更新 §3.5 列出的 ~12 个现存测试。
