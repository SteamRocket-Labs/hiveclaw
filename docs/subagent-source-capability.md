# Subagent 源能力设计：让 Hive 成为"团队版 Claude Code"的协作地基

> **定位**：这是一篇聚焦**单一核心**的源能力设计文档——补齐 Hive 的 **subagent 体系**（多 agent 协作/委派/并行的底层能力）。
>
> **缘起**：deep research 产出质量长期不高，我们打了 RC11→RC15 一长串 prompt 补丁仍解不开（RC15 是 prompt 死结）。根因不是产物、不是提示词——是 **deep research 依赖的源能力本身残缺**。deep research 是组合产物；它脆，是因为它在手搓一个本该是平台源能力、但 Hive 还没有的东西。
>
> **两轴框架**：Hive 要成为"加强版团队 Claude Code",缺两根正交的源能力轴:
> - **轴 1 — Subagent 体系**（本文）：一个 agent 能声明式派生 / 并行 fan-out / 隔离 / 回收 / 治理一群轻量子 agent。
> - **轴 2 — 工作流编排**（独立讨论，本文只留接口）：代码拥有确定性控制流（pipeline / parallel / barrier / loop），而非 agent 即兴。借鉴 Claude Code 的 **Workflow 工具**范式。
> - **deep research = 轴1（并行子 agent）× 轴2（编排控制流）的一次组合**。补齐两轴，deep research 是它们的自然应用——水到渠成。
>
> 本文只钉死轴 1。轴 2 待专门讨论。

---

## 术语边界（v2 新增——先钉死，避免概念混层）

Hive 此前已收紧过 agent 语义，本文的"subagent"**专指其中一类**，不可与委派/身份混用：

| 术语 | 定义 | Hive 现状 | 本文 |
|---|---|---|---|
| **peer delegation** | 委派给一个**已存在的数字员工**（独立身份），解析 agent_id 后调用 | `delegate_to_agent`/`delegate_async`（调用方：messaging `message_to_agent`、plan-mode handoff） | ❌ 不收口、不改语义 |
| **spawned lightweight worker** | 派生一个**从属父、无独立身份**的轻量执行体（explorer/worker/critic） | ⚠️ 缺——DR worker 是手搓残缺版 | ✅ **本文要补的源能力** |
| **owned child identity** | 有父子**身份关系**的 agent（父 own 一个 child 数字员工） | 无 | ❌ 非本文范围 |
| **workflow step** | 工作流编排（轴2）里的一步，可引用上面任意一类 | 无（轴2 独立讨论） | 仅留接口 |

**铁律**：`spawn_subagent`（§5.1）**只**服务 **spawned lightweight worker**；peer delegation 保持独立入口（底层可共用 `invoke_agent` 底座，但对外语义/工具名分开），绝不用一个原语同时表示"派生从属 worker"和"委派给 peer 数字员工"。

---

## 0. TL;DR

1. **subagent 源能力 = 7 块骨架**（调研 Claude Code + Codex 源码提炼，剥开"md vs toml""软 vs 硬"的风格差异后，两家骨架一致）。
2. **Hive 现状**：3 块健康地基（context 隔离 / 结果回收 / 治理共享+防递归），4 块缺口（统一 spawn 入口 / 轻量子 agent type / 并行 fan-out 原语 / 异步完成重入）。
3. **deep research 的 worker fan-out 被坐实是"手搓残缺 subagent"**：私有 `asyncio.gather+Semaphore`、worker 是"被砍了工具的父 agent 克隆"（复用父灵魂/记忆，非轻量 type）、和 delegation 两套零共享、一串 RC/F 补丁堵 fan-out 没有资源配额导致的事故。
4. **设计主张**：把这 4 块缺口补成**平台级源能力**，**建在现有 3 块健康地基上**（不另起炉灶）；让 deep research 和 delegation 共用同一套 subagent 原语。
5. **取舍**：Hive 不照搬 CC/Codex。取 Codex 的**硬约束治理 + thread 隔离 + fork 旋钮**，取 CC 的**声明式定义 + 并行 fan-out + 结果回结论 + async 通知**，关键是引入**持久具名子 agent 实体**（explorer/worker/critic 类，`定义.md` + `记忆.md`；无数字员工身份层 soul/T3/dream，但**有自己的可进化 `记忆.md`**——CC 也有 agent memory（user/project/local 文件 scope），**Hive 的差异是 tenant-scoped + RLS + governed + audit-ready**，落控制中台护城河），并支持**两种调用**：主 agent 临时 spawn（对话中即兴）/ 工作流固化引用（持久定义里）。
6. **deep research = 轴1（持久 subagent 实体）× 轴2（固化工作流定义）的组合**：工作流固化引用 subagent，subagent 随每次执行把经验写回 `记忆.md` → 工作流产物越用越好、代码一行不改。

---

## 1. 调研：subagent 源能力 = 7 块骨架

Claude Code 和 Codex 在风格上**故意做反**，恰好暴露了哪些是"风格选择"、哪些是"不可省的骨架"：

| 维度 | Claude Code | Codex | 是骨架还是风格 |
|---|---|---|---|
| 子 agent 定义 | md body **替换** system prompt | TOML role **叠加**配置 | 风格（都要"声明式定义"） |
| 编排 | 可选 Coordinator（prompt 状态机，软） | 无 coordinator，agent 即编排者 | 风格 |
| 约束 | 软约束（每轮 reminder） | **硬约束（handler return Err）** | 风格（Hive 选硬，见 §3） |
| 隔离 | 换 prompt + 可选 worktree | 独立 thread + `fork_turns`(none/all/N) | 风格（都要"隔离+可控继承"） |
| 结果回流 | 回最后 text + `<task-notification>` | Mailbox + JSON，wait 只给信号 | 风格（都要"只回结论+异步重入"） |
| 递归 | 抽走 Agent 工具（可多层） | 默认 `max_depth=1` | 风格（都要"防递归"） |

**剥开风格，骨架 = 这 7 块（两家都有，缺一不可）：**

1. **单一 spawn 入口** —— 一个工具/函数统管派生（CC `Agent` 工具 / Codex `spawn_agent`），不是散落 API。
2. **声明式子 agent 定义** —— `type + 工具集 + model + 隔离级别 + maxTurns` 一份配置契约；尤其是**轻量任务专用子 agent**（explorer/worker/critic），不是派生整个重型实体。
3. **context 隔离 + 可控继承** —— 子 agent 独立 history，调用方可控决定继承多少父上下文（CC 换 prompt / Codex `fork_turns` 三档旋钮）。
4. **并行 fan-out 原语** —— 一次派生 N 个并行 + 收集（CC 单消息多 block + concurrency-safe / Codex `spawn_agents_on_csv` 并发 16~64）。
5. **结果回收 = 只回结论、父综合** —— 子 agent 中间 tool 过程**不灌**主 context（这是 fan-out 省 token 的本质），父负责综合；**never delegate understanding**。
6. **同步阻塞 + 异步通知两种模型** —— async fire-and-forget，完成后以消息**重入**父的下一轮（CC `<task-notification>` / Codex mailbox），父**不轮询**。
7. **治理父子共享、不可逃逸 + 防递归** —— 子 agent 必须过和主 agent **同一个**治理/sandbox 层，无法因为是 subagent 就绕过。

> **CC 关键参照**：`src/tools/AgentTool/AgentTool.tsx`（spawn+回收+并发标志）、`forkedAgent.ts`（隔离契约，最值得照搬）、`loadAgentsDir.ts`（定义 schema）、`constants/tools.ts`（防递归三层）。
> **Codex 关键参照**：`codex-rs/core/src/agent/control.rs`（spawn）、`config/mod.rs:1939`（`AgentRoleConfig`）、`session/input_queue.rs`（mailbox）、`protocol/src/protocol.rs:686`（`InterAgentCommunication`）。

---

## 2. Hive 现状：3 块健康地基 + 4 块缺口

用 7 块骨架当标尺核实 Hive（`backend/app/`，file:line 为实测）：

| # | 骨架块 | 状态 | 现状 + 证据 |
|---|---|---|---|
| 1 | 单一 spawn 入口 | 🟡 半成品 | 三条分裂路径都接 `invoke_agent`（`runtime/invoker.py:942`），但没收口：同步 `delegate_to_agent`（`agents/orchestrator.py:630`）、异步 `delegate_async`（`:1007`）、deep research 私有 `RuntimeResearchWorker.run`。 |
| 2 | 声明式子 agent 定义 | 🟡 半成品 | `_DELEGATION_TOOL_PROFILES`（`orchestrator.py:46-145`，4 个 profile）只声明**工具面+记忆策略**，缺 model/maxTurns/隔离级别/**type**；派生的是**整个目标数字员工**重新 invoke，不是轻量子 agent。 |
| 3 | context 隔离 + 可控继承 | ✅ 有 | 独立 `child_session_id` + `_build_delegation_brief`（`:500-528`）压成末 8 条 ≤4000 字 brief；隔离契约写死在 prompt（`:194`）。（继承量固定，非按需声明。） |
| 4 | 并行 fan-out 原语 | 🟡 半成品 | 全平台**唯一**真并行是 deep research 私有 `_run_worker_fanout`（`services/deep_research/orchestrator.py:592-622`，裸 `asyncio.gather+Semaphore(≤3)`）。delegation 侧无通用并行，只能逐个 `delegate_async` + 轮询。 |
| 5 | 结果回收（只回结论） | ✅ 有 | worker 中间过程不进父 context，只回 `intermediate_report` ≤600 字 digest（`worker.py:142-149`）；delegation 返回结构化 `AgentDelegationResult`（`orchestrator.py:441-483`）。 |
| 6 | 同步阻塞 + 异步通知 | 🟡 半成品 | 同步✅（`:838` await）+ 异步✅（`:1007` task_id）；但**异步是轮询不是重入**——父只能主动 `check_async_task`（`communication.py:162`），没有"子 done→自动唤醒父"闭环。Signal 原语存在（`coordination.py:100`）但没接成重入。 |
| 7 | 治理共享 + 防递归 | ✅ 有 | 子工具强制过 `ToolRuntimeService.execute→run_tool_governance`（`invoker.py:769`→`agent_tools.py:528`），无法绕过；防递归三重：`max_depth=2`（`:327`）+ per-trace 环检测 + worker profile 禁 delegate 工具 + `delegation_token`。 |

**工作流编排（轴 2）= 无 first-class 确定性引擎**（不是"能力为零"——下列 workflow-ish 机制存在，但都非代码控制流）：`FinanceWorkflowRunner` 已随 commit `c0ea7fe` 删除，grep 全 `app/` 无任何 `WorkflowRunner` 残留（已复核 ✅）；无 pipeline/parallel/barrier/loop 引擎；coordinator mode（`runtime/coordinator.py`）、skill workflow 蒸馏、deep research controller 是 workflow-ish，但 coordinator 仍 LLM 驱动、**不是代码拥有的确定性控制流**。`coordination.py` 的 Lease/Signal/Checkpoint 是 agent 间**协调/信令**原语（不是 subagent 派生体系），与轴 1 正交。

---

## 3. 核心判断：deep research 的 fan-out 是手搓残缺 subagent

坐实（证据见基线核实）：

1. **并行逻辑是 deep research 私有函数**（`_run_worker_fanout`），不是平台原语——delegation 想并行只能父 agent 循环 `delegate_async`+轮询。
2. **"worker" 不是轻量子 agent，是父 agent 克隆**：`RuntimeResearchWorker(agent_id=reasoner.agent_id, ...)`（`orchestrator.py:525`）复用父的身份/灵魂/记忆，只把工具面砍成 4 个只读 web 工具 + `max_tool_rounds=8`。这正是 §1 骨架 2 要避免的反模式。
3. **和 delegation 两套零共享**：`research_readonly` profile 的工具集和 worker allowed tools 几乎一模一样——重复造轮子的活证据。
4. **修复史印证残缺**：`worker.py` 里 RC1/RC2/RC3/F1/F2/F3 补丁（单源 12K 封顶、单 worker 8 源封顶、round-robin 取源防第一个 worker 吃满预算）——都是 fan-out 没有结构化资源配额导致的生产事故事后打补丁。**有源能力的 fan-out 会把"每个子 agent 的 token/源预算"做进契约，而不是一个个场景去堵。**

**一句话**：deep research fan-out = `asyncio.gather` 包 Semaphore，跑 N 个"被砍了工具的父 agent 副本"，靠手工 round-robin 配额 + reasoner 二次综合。能跑，但它是 §2 缺口 2+4 双缺位下的场景特化补偿，**不是源能力**。

---

## 4. 取舍：Hive 不照搬，取什么

Hive 的独特位置：已经有**比 CC/Codex 都强**的东西——数字员工（soul/skills/memory/自我进化）+ 治理控制中台 + deep research 引擎。所以补 subagent 不是抄，是"在已有内核上补缺的 4 块"：

| 取自 | 取什么 | 为什么 |
|---|---|---|
| **Codex** | 硬约束治理（handler `return Err`，治理父子共享不可逃逸） | 与 Hive"治理归系统、规划归 agent"哲学完全契合（`feedback_plan_from_agent_system_governs`）。子 agent 必须过同一 `ToolRuntimeService→run_tool_governance` 不变式。**这是控制中台该有的样子。** |
| **Codex** | thread 隔离 + `fork_turns`(none/all/N) 旋钮 | 比 CC 全有/全无更细；Hive 的 brief 继承量目前固定，应升级为按需声明。 |
| **CC** | 声明式子 agent 定义 + 并行 fan-out + 结果回结论文本 + async 通知重入 | 成熟，且与 Hive 的 hook + Redis pub/sub 架构对得上。 |
| **CC（关键）** | **持久具名子 agent 实体**（explorer/worker/critic 类，`定义.md`+`记忆.md`；无数字员工身份层 soul/T3/dream，但有自己可进化的 `记忆.md`，详见 §5.2） | 这是 Hive 当前 delegate **最大的形态错位**——现在派生子 agent = 把整个数字员工重新 invoke。 |

**地基复用（不另起炉灶）**：§2 的 block 3/5/7（context 隔离+brief、结果回结论、治理共享+max_depth+token）是 Hive 真实力。新源能力**建在这套治理+隔离地基上**。

---

## 5. Subagent 源能力设计

### 5.1 统一 spawn 入口（收口缺口 1）

把 **lightweight worker 派生**收口成**一个** spawn 原语 + 一个 spawn 工具（**只收 worker，不收 peer delegation**——见术语边界铁律）：

```python
# 草案：平台级 worker spawn（只派生 lightweight worker，不含 peer delegation）
async def spawn_subagent(
    parent: SubagentSpawnContext,          # 父 agent_id/user_id/trace_id/depth/token
    spec: SubagentSpec,                    # 见 5.2：声明式定义
    task: str,                             # 给子 agent 的任务（包成首条 user message）
    *, run_in_background: bool = False,    # 见 5.4：同步 vs 异步
    fork: Literal["none","brief","all"] = "brief",  # 见 5.3：上下文继承粒度
) -> SubagentHandle: ...
```

deep research 的 `_run_worker_fanout` 改为调它（它就是 lightweight worker spawn）。**`delegate_to_agent`/`delegate_async` 不并入**——它们是 peer delegation，保持独立入口；`invoke_agent` 仍是三者共用的底座，但 spawn 语义只收敛 worker 这一层。

### 5.2 子 agent = 持久具名实体（定义.md + 记忆.md）

**核心修正（讨论对齐 2026-06-02）**：子 agent **不是临时工**——它是**持久、具名、可进化的实体**，固化为两个文件，与 CC 的 agent `.md` 同构（CC 也已有 per-agentType memory，见下）：

- **`定义.md`** —— 记录这个子 agent 的一切：type、工具面、model、隔离级别、system prompt body。对标 CC `~/.claude/agents/<name>.md`。
- **`记忆.md`** —— 这个子 agent **自己的记忆**，随使用进化（§5.2.2）。**CC 也有 agent memory**（`agentMemory.ts`：user/project/local scope，per-agentType `MEMORY.md` + snapshot 同步，已复核 ✅）——所以"有记忆"**不是** Hive 独有。**Hive 的真差异 = tenant-scoped + RLS + governed（过 write gate）+ audit-ready，接入 Memory Control Plane**；落 North Star 的不是"有记忆"，是"企业级受治理的记忆"。

```python
# 定义.md 的 frontmatter → 这份契约（区别于"派生整个数字员工"）
@dataclass(slots=True)
class SubagentSpec:
    name: str                       # 具名实体: "market-research-explorer" | "code-critic"
    type: str                       # 类别: "explorer" | "worker" | "critic" | <自定义>
    allowed_tools: tuple[str, ...]
    excluded_tools: tuple[str, ...]
    model: str | None = None
    max_tool_rounds: int | None = None
    isolation: str = "session"      # session | brief | none（§5.3）
    has_own_memory: bool = True     # 持久【记忆.md】（schema 定全；MVP 切口 runtime 先恒走无 memory 路径，见 §8）
    parent_knowledge: str = "readonly"  # 只读父 knowledge，绝不写父
    soul: bool = False              # 无数字员工灵魂/身份演化（阉割的是身份层，不是记忆）
```

- **内置 type**：`explorer`（只读勘察、并行友好）、`worker`（限定工具的执行）、`critic`（只读审查/验证，对标 CC `verification` agent 的"只验不改"）。每个**具名实体**（如 `market-research-explorer`）= 一份固化的 `定义.md` + `记忆.md`。
- `_DELEGATION_TOOL_PROFILES` 升级为 `SubagentSpec` 预设；deep research worker 的 `RESEARCH_WORKER_ALLOWED_TOOLS` 收敛为一个具名 `explorer` 实体。
- **删掉原稿的 `long_term_memory=False`**：那是错误的"临时工"模型。子 agent **有**自己的 `记忆.md`；阉割掉的只是数字员工的**身份演化**（soul/T3/dream），不是记忆本身。（**目标态 vs 实现顺序**：spec 含 memory 字段，但 §8 切口 1 的 runtime 先走无 memory 路径，memory daemon 是最后一个切口。）

### 5.2.1 两种调用入口（都需要 —— 同一实体的两种用法）

同一个持久子 agent 实体，**两种调用方式**（正是 Claude Code 自己的 `Agent` 工具 + `Workflow` 工具两种用法）：

| 调用方式 | 入口 | 场景 | 执行层 |
|---|---|---|---|
| **临时 spawn** | 主 agent 在对话中调 `spawn_subagent`（轴1） | 对话中即兴（"并行探索 3 个方向"） | 运行时状态用完回收 |
| **工作流固化** | 固化的工作流定义**引用** subagent 当步骤（轴2） | deep research 等固定流程 | 同上 |

**关键闭环**：两种调用 → 同一实体 → **执行完都把经验写回它的 `记忆.md`**。临时和固化不是两个子 agent，是**同一可进化实体的两种用法**；固化工作流里的进化收益最稳（同类任务反复 → `记忆.md` 越用越厚 → 工作流产物自然变好，代码一行不改）。**这就是轴1（实体）× 轴2（固化引用）的接缝。**

### 5.2.2 记忆与「阉割版自进化」

子 agent 的"记忆"分三面，权限不同：

| 记忆面 | 内容 | 权限 | 存储 |
|---|---|---|---|
| 父知识 | 派生它的数字员工 knowledge | **只读** | 父 workspace（不写回，不污染身份） |
| 自己的 `记忆.md` | 作为这个具名子 agent 的经验积累 | **读 + 写（沉淀）** | 子 agent 实体目录（**纯 tenant-scoped**，待决1 已拍板 → §5.2.2 末） |
| 实例状态 | 这次运行时 | 用完即弃 | 不持久化 |

**阉割版自进化 = 砍身份层、留记忆层**：
- 数字员工：`T0→T2→T3→soul` + dream/heartbeat（身份演化，重）
- 子 agent：只保留**单层**「任务完成 → 提炼可复用经验 → 写回 `记忆.md`」（≈ 单层 T2 extraction，**无** T3 curation / soul promotion / dream consolidation）
- **铁律**：子 agent 只读父、读写自己的 `记忆.md`、**绝不写父的身份记忆**（否则一群 explorer 会搅乱数字员工的 soul/T3）。

**记忆边界（讨论拍板 2026-06-02）—— Workflow 装显性 SOP，subagent 只装隐性 How**：

Workflow（轴2）固化的是**显性知识**（SOP：分几步、每步派谁、怎么编排）。subagent `记忆.md` 只装 **SOP 装不下的隐性 know-how**（Polanyi 隐性知识"我们知道的比能说出的多"），且**只记 How（手艺）、不记 What（知识）**：

| 层 | 装什么 | 归属 |
|---|---|---|
| **Workflow** | 显性 SOP / 流程 / 编排 | 工作流定义（轴2，持久文件） |
| **数字员工记忆** | 身份 + 领域知识 **What**（这个人是谁、知道什么） | soul / T3 |
| **subagent `记忆.md`** | 执行手艺 **How**（隐性、领域特定）：① 源/工具可靠性校准 ② 判断/品味校准 ③ 失败模式·避坑 ④ 领域事实**地图索引**（指向性，非知识本身） | subagent 实体目录 |

**铁律**：subagent **只记 How、不记 What**——纯领域事实知识归数字员工 knowledge，subagent 顶多存"指向性地图索引"而非知识本身。否则"特定领域"会让 subagent 慢慢长成第二个数字员工，回到要避免的重型实体。**subagent 是会越用越熟练的手艺人，不是第二个分析师。**

**进化机制（拍板）**：**离线 daemon 批量扫**（待决2 ✅）这个**领域具名 subagent**（待决3 ✅ 领域特定，`market-research-explorer` ≠ `tech-explorer`，各扫各的、各记各领域手艺）的执行日志（T0）→ 提炼领域隐性 How → 写回它的 `记忆.md`。高频派生**不实时提炼**（太贵）。

**Memory Control Plane 不变量（v2 补，铁律）**：subagent memory 的 extraction/write **必须过 governed write gate**——复用 `memory/write_gate.py` 的隐私/敏感度分类 + lifecycle/evidence metadata（PL4 凭据拒写），或等价的 subagent-specific governed write。**离线 daemon 绝不直接拼 Markdown 写文件**，否则绕过 Hive 记忆写入不变量（与数字员工 T2/T3 走同一道闸）。

**待决1 已拍板（2026-06-02）→ 方案 B（只 tenant 级）**：`记忆.md` = **纯 tenant-scoped**，每租户独立积累、零跨租户流动（符合 Hive RLS 铁律，与本节离线 daemon 机制零摩擦，切口① MVP 成本最低）。**方案 A（平台级通用基线 + tenant 覆盖）搁置不碰——模糊地带太多**：① 基线来源是"平台出厂策展模板"还是"聚合租户日志"未定，后者直接撞多租户隔离铁律（跨租户 How 泄漏）；② 基线更新 vs 租户覆盖的 merge 冲突无解法；③ 不在轴 1 范围。**红线留痕**（若未来重启）：平台基线只能是平台出厂策展的只读模板（类比 `templates/HEARTBEAT.md`），绝不自动聚合租户执行日志。

### 5.3 context 隔离 + 可控继承（升级 block 3）

把现有固定的"末 8 条 brief"升级为按需声明（对标 Codex `fork_turns`）：
- `fork="none"`：只给 task（最干净，explorer fan-out 默认）
- `fork="brief"`：task + `_build_delegation_brief`（现状）
- `fork="all"`：task + 父完整近期 history（少用，重型委派）

### 5.4 并行 fan-out 原语（补缺口 4，deep research 最先受益）

一个平台级 fan-out，带**结构化资源配额**（直接消解 deep research 那串 RC/F 补丁）：

```python
async def fanout_subagents(
    parent: SubagentSpawnContext,
    jobs: list[SubagentJob],          # 每个 job: spec + task
    *, max_concurrency: int = 4,
    per_agent_budget: SubagentBudget, # token/源/round/超时——做进契约，不再手搓 round-robin
    on_partial_failure: Literal["isolate","abort"] = "isolate",  # 单子失败隔离
) -> list[SubagentResult]: ...
```

- 复用 §2 block 5（结果只回结论）：每个 job 回结构化 digest，中间过程不灌父 context。
- `deep_research._run_worker_fanout` 改为调它；`per_agent_budget` 取代 worker.py 里手工的单源/单 worker 封顶。
- 失败隔离对标 CC（单 subagent 失败降级为 partial，不炸整体）。

### 5.5 异步完成重入（补缺口 6）

接上现有 `coordination.py` 的 Signal，闭环"子 done → 投递 signal → 父被重新 invoke"，消灭父 agent busy-poll（记忆里 deep_research busy-loop / LoopGuard 正是这个坑）：
- 子 agent 完成 → `coordination` 投递完成 Signal（带子结果 digest）
- 调度层把 Signal 转成父的下一轮输入（fire-and-forget + notify），而非父主动 `check_async_task`。
- 对标 CC `<task-notification>` 重入 / Codex mailbox `trigger_turn`。

### 5.6 治理 + 防递归（地基 block 7，一行不动）

子 agent 强制过 `ToolRuntimeService.execute→run_tool_governance` + `max_depth` + per-trace 环检测 + `delegation_token` —— **现状已是 Hive 真实力，新原语必须继承这套，绝不开后门**。这是 Codex"硬约束治理"在 Hive 的已有体现。

---

## 6. 与 Deep Research 的关系：水到渠成的证明

补齐 §5 后，deep research 退化成 subagent 源能力的**一个普通应用**：

| deep research 现在（手搓） | 补源能力后（自然应用） |
|---|---|
| 私有 `_run_worker_fanout`（asyncio.gather+Semaphore） | `fanout_subagents(jobs=[explorer×N], per_agent_budget=...)` |
| worker = 砍了工具的父 agent 克隆 | 具名 `explorer` 实体（`SubagentSpec(soul=False, has_own_memory=True)`，自带可进化 `记忆.md`） |
| RC/F 一串配额补丁 | `per_agent_budget` 做进契约 |
| reasoner 一次性 synthesis（RC15 死结） | synthesis subagent + **独立 critic subagent**（覆盖检查从 prompt 强制挪到独立 agent，解 RC15） |
| 父 busy-poll worker 状态（LoopGuard 坑） | §5.5 完成重入 |

**deep research 不再需要自己造 reasoner/orchestrator——它只是"轴1 fan-out + 轴2 编排"的一次组合调用。** 这就是你说的水到渠成。

---

## 7. 工作流编排轴（轴 2）：留接口，待专门讨论

轴 2 与轴 1 正交，本文不展开，只钉接口：

- **现状**：Hive **无 first-class 确定性编排引擎**（finance 删后无通用 WorkflowRunner；coordinator/DR controller 是 workflow-ish 但 LLM 驱动、非代码控制流）。
- **范本**：借鉴 **Claude Code 的 Workflow 工具**（`pipeline` / `parallel` / `barrier` / `agent` fan-out / `loop-until-dry` / `phase`）——"代码拥有控制流、agent 当 worker"，与 Codex"agent 即编排"恰好相反。
- **接口**：轴 2 的编排步（如 deep research 的 plan→fan-out→synthesize→critic）调用轴 1 的 `fanout_subagents` / `spawn_subagent` 当 worker。即**轴 2 编排控制流，轴 1 提供被编排的 subagent**。
- **多租户/治理 blocker**：轴 2 落地 Hive 需解决 RLS、tenant 隔离、与现有治理层的关系（见 `project_workflow_determinism_hive`）。这些在轴 2 专门文档里钉。

> **下一次讨论轴 2 时**：以 Claude Code Workflow 工具的实现方式为蓝本，映射到 Hive 多租户 + 治理约束。

---

## 8. 增量切口（v3 重排——DR 接入抽离，subagent 先成完整源能力）

不要一次重写。按风险从低到高，每步独立可验证、可回滚。**v3 关键校准（用户 2026-06-02 拍板）**：不再让 deep research 当"边做边接"的验证驱动——**subagent 是第一性源能力，先把它做完整、做对、做成平台原语；deep research 是它的第一个应用，等源能力上线后再回头一次性改造**（原 v2 切口 2/3 的"DR 改调""DR critic 应用"从主线抽离，见下「后续阶段」）。**"完整"= 做到含持久实体 + 阉割版记忆进化（切口⑥），不停在无记忆临时工**（踩中 North Star Goal 1 自我进化）。

**纯 subagent 源能力主线（6 刀）：**

1. **切口①（最小核心）**：**runtime-only** `SubagentSpec`（契约）+ `fanout_subagents` + 只读 `explorer` type（§5.2+5.4）。内部 spawn：构造 `AgentInvocationRequest`→`invoke_agent`，继承治理（`delegation_token`+`tool_executor` 透传）+ 防递归（depth），enforce budget（rounds/timeout）。**不写 subagent memory**（spec 字段定全，runtime 恒走无 memory 路径）。纯运行时，零持久化。fork 实现 `none`。
2. **切口②**：`spawn_subagent` 单体入口 + 暴露成工具（§5.1）——主 agent 对话中临时 spawn（§5.2.1）。**只收 lightweight worker，peer delegation 不并**（见术语边界）。
3. **切口③**：`worker`/`critic` type 补全（§5.2 内置三 type 齐）+ fork 三档旋钮 `none/brief/all`（§5.3，brief 复用 `_build_delegation_brief`，all 传父近期完整 history）。
4. **切口④**：异步完成重入（§5.5）接 Signal，消灭父 busy-poll（LoopGuard 坑）。
5. **切口⑤**：持久 `定义.md`（实体固化，对标 CC `agents/<name>.md`）——具名实体 tenant-scoped 加载/解析/注册。
6. **切口⑥**：**tenant-scoped subagent `记忆.md` + 阉割版进化 daemon**（§5.2.2）——离线扫领域具名 subagent 的 T0 提炼隐性 How 写回 `记忆.md`，**必过 governed write gate**（`prepare_memory_write`，PL4 拒，rejected 即 abort 不 fallback raw；daemon 绝不直拼 Markdown）。源能力完整的最后一刀（风险最高、撞 Memory Control Plane）。

**——到此 subagent 是完整的持久可进化源能力。——**

**后续阶段（独立，不在本主线；等上面 6 刀全部上线后）：**

- **DR-A**：deep research `_run_worker_fanout` 改调 `fanout_subagents`——保留现有 RC/F 配额作 backstop（新旧并存验证，不推倒；DR 刚被 RC11-15 修到生产能跑），`per_agent_budget` 验稳后再撤旧补丁。
- **DR-B**：deep research synthesis 引入独立 `critic` subagent（§6），解 RC15（覆盖检查从 prompt 强制挪到独立只读 agent）。
- **轴2**：工作流编排（§7），借鉴 CC Workflow 工具，映射 Hive 多租户+治理（独立文档）。

---

## 9. 非目标 / 风险 / 不变量

- **非目标**：本文不做轴 2 工作流编排（独立文档）；不重写 deep research reasoner（只把它的 fan-out/worker 替换为源能力）；不动数字员工/soul/记忆体系。
- **不变量（绝不破）**：① 子 agent 必须过同一治理层（§5.6），绝不开后门；② 防递归（max_depth + 环检测 + token）保持；③ 结果回收"只回结论、不灌父 context"保持；④ 增量演进，每切口独立可回滚——deep research 刚被 RC11-15 修到生产能跑，不推倒。
- **风险**：轻量子 agent（无灵魂；切口①-⑤ 阶段无 memory 写回，切口⑥ 补 tenant 记忆进化）的产出质量是否够——靠 `critic` type 二次验证兜底；fan-out 并发资源（token/连接）需压测。

---

## 10. 实装状态追踪（v3，本轮全面实装）

| Phase | 切口 | 状态 | Commit |
|---|---|---|---|
| 0 | 固化决策（§8 v3 重排 + 本表） | ✅ done | `5ad301d` |
| 1 | ① runtime 契约 + fanout + explorer（无 memory） | ✅ done | `1155f46` |
| 2 | ② spawn_subagent 入口 + 工具暴露 + capability/discovery 闭环 | ✅ done | `47fdd57` + 本轮闭环 |
| 3 | ③ worker/critic type + fork 三档（none/brief/all） | ✅ done | `0f4067b` |
| 4 | ④ 异步完成重入（P0：Signal 完成通知+read-once 消费；调度层自动重入见后续） | ✅ done | `c1987fa` + 本轮闭环 |
| 5 | ⑤ 持久 定义.md + tenant path boundary + definition-driven spawn | ✅ done | `b825449` + 本轮闭环 |
| 6 | ⑥ tenant 记忆.md + 进化（P0：governed write+store+runtime 注入+distill writeback 入口；T0 扫描/周期调度见后续） | ✅ done | `90f325e` + 本轮闭环 |
| DR-A | deep research 接 fanout（保留 RC/F backstop） | ⏸ 后续 | — |
| DR-B | deep research critic 解 RC15 | ⏸ 后续 | — |
| 轴2 | 工作流编排（借鉴 CC Workflow） | ⏸ 后续 | — |
| C1 | 配置面：agent 级 store + 解析链 + 记忆跟随作用域（§12） | ✅ done（2026-06-05） | 本切口 commit |
| C2 | 配置面：7 端点 API（§12.7） | ✅ done（2026-06-05） | 本切口 commit |
| C3 | 配置面：AgentDetail Sub-agents tab（§12.8） | 📋 已设计待实施 | — |
| C4 | 配置面：Company Admin tenant 库管理（§12.8） | 📋 已设计待实施 | — |

---

## 11. Review 闭环记录（2026-06-03）

**结论**：v3 六刀的测试与 ruff 虽然通过，但还不能直接判定为完整源能力。review 发现的核心问题不是"代码不能跑"，而是几个平台边界没有闭合：工具面 fail-closed、capability governance、tenant path boundary、Signal 真消费、持久定义/记忆参与 runtime、以及预算/model 契约真实生效。

**必须闭环的修复项**：

1. **工具面 fail-closed**：unknown/custom subagent type 在没有显式 `allowed_tools` 时不得因空 allow-list 退化成父 agent 全工具面。
2. **capability mapping**：`spawn_subagent` 必须进入 `CAPABILITY_MAP`，严格能力映射开启时不能被误拒，也不能在 startup audit 里报 unmapped。
3. **tenant path boundary**：`定义.md` 与 `记忆.md` store 必须校验 subagent name，禁止 `../`、路径分隔符或其他越界写入。
4. **Signal 真消费**：`consume_subagent_signals()` 必须 read-once，不能重复返回同一完成通知。
5. **持久实体接入 runtime**：`SubagentDefinitionStore` 和 `SubagentMemoryStore` 不能只是测试 primitive；spawn 路径要能加载持久定义、注入 `记忆.md`，完成后走 governed distill/writeback。
6. **契约真实生效**：`SubagentSpec.model`、`SubagentBudget.max_source_chars/max_sources` 等字段要么实现，要么从契约/文档降级；本轮选择实现至少可验证的 model override 和 source capture budget。

**本轮修复记录（未提交）**：

- `resolve_subagent_tools()` 继续返回 allow/deny，但 `_spawn_one()` 对 unknown/custom type 的空 allow-list 明确 fail-closed；不会把空 allow-list 传进 kernel 形成"不过滤=父全工具面"。
- `spawn_subagent` 进入 `CAPABILITY_MAP`，ToolMeta 从 `safe` 改为 `sensitive`，并加入 `coordination_pack` / canonical tool surface；startup audit 不再把它报成 unmapped 或 functional orphan。
- `SubagentDefinitionStore` / `SubagentMemoryStore` 统一使用 `validate_subagent_name()`，拒绝 `../`、路径分隔符、空名和越界路径；tenant store helper 固定在 `AGENT_DATA_DIR/_tenants/{tenant_id}/subagents/{definitions|memory}`。
- `CoordinationRuntime.consume_signals()` 新增 read-once 语义，`consume_subagent_signals()` 改走 consume；同一 completion Signal 第二次读取返回空。
- runtime 新增 `spawn_subagent_from_definition()`；tool handler 支持 `definition_name` 从 tenant store 加载持久定义，并把 tenant `memory_store` 注入 spawn context。
- `_spawn_one()` 实现 `SubagentSpec.model` override（通过 `model_resolver`）、`SubagentBudget.max_source_chars/max_sources` source capture、`记忆.md` prompt 注入，以及成功后由注入 distiller 触发 governed `distill_and_record()` writeback。

**验证结果**：

- Red：新增测试后，目标子集先因缺少 `spawn_subagent_from_definition` 收集失败；full pytest 后续暴露 `spawn_subagent` discovery orphan/canonical surface 漏洞。
- Green：`source backend/.venv/bin/activate && pytest backend/tests/agents/test_subagent.py backend/tests/agents/test_subagent_definition.py backend/tests/agents/test_subagent_memory.py backend/tests/agents/test_subagent_async.py backend/tests/agents/test_subagent_spawn_tool.py backend/tests/services/test_capability_gate_policy_surface.py -q` → `64 passed`。
- 全量：`cd backend && source .venv/bin/activate && pytest -q` → `3371 passed, 7 skipped`。
- lint：`cd backend && source .venv/bin/activate && ruff check app tests` → `All checks passed!`。

**仍保留的真实后续边界**：

- 切口④仍是同进程/当前 coordination runtime 的 completion Signal read-once；"完成后自动唤醒父 agent 下一轮"还需要跨 worker wake-consumer loop / PostgreSQL-backed coordination 接线。
- 切口⑥已做到 runtime 记忆注入与 governed writeback hook；离线 daemon 的 T0 日志扫描、周期调度、LLM distiller 仍按 §10 后续边界处理。
- deep research 接入（DR-A/DR-B）仍未在本轮改造；本文主线继续只关闭 subagent 源能力。

---

## 12. 配置面与作用域（人类入口，v4 新增 —— 2026-06-05 用户拍板方向）

### 12.0 问题：仓库建好了，没有门

切口①-⑥ 落的是**运行时层 + 定义层**：`spawn_subagent` 能按 `definition_name` 从 tenant store 加载持久 `定义.md`，但**零 API、零前端**——没有任何人类入口能创建/查看/编辑 subagent 定义。唯一的"配置方式"是 agent 在对话中 inline spawn，或操作员手动往 `_tenants/<tid>/subagents/definitions/` 拼文件。本节补齐人类配置面。

### 12.1 作用域模型（CC 基线 + Hive delta）

CC 以**项目**为工作单元（项目级 `.claude/agents/` + 用户级 `~/.claude/agents/`，项目覆盖用户）；Hive 以 **agent** 为工作单元（agent workspace ≈ 项目空间）。映射：

| CC 基线 | Hive 对应 | 状态 |
|---|---|---|
| 对话内 `Task` 工具即兴 spawn | `spawn_subagent` inline spec | ✅ 切口② |
| **项目级** `.claude/agents/*.md`（跟项目走，日常主力） | **agent 级** `<agent_workspace>/subagents/*.md`（跟 agent 走） | 🆕 C1 |
| **用户级** `~/.claude/agents/*.md`（跨项目共享） | **tenant 级** 公司库 `_tenants/<tid>/subagents/definitions/`（跨 agent 共享、公司策展） | ✅ 切口⑤（存储）/ 🆕 C2+C4（门） |
| 项目级覆盖用户级 | **agent 级覆盖 tenant 级**（同名时） | 🆕 C1 |

Hive delta（CC 没有的）：① tenant 级是**组织策展**语义（控制中台），不是个人偏好；② 全部入口受多租户 RLS + capability gate + 审计治理；③ subagent 工具面 ⊆ 主 agent 工具面（只收窄不放宽，运行时已 enforce——配置面不新增权力，只开人类入口）。

### 12.2 agent 级定义（日常主力，C1）

- **路径**：`AGENT_DATA_DIR/<agent_id>/subagents/<name>.md`，格式与 tenant 级完全一致（同一 `parse_subagent_definition` / `render_subagent_definition`，零新格式）。
- **与 `skills/` 同构**：markdown 即配置、agent-legible。主 agent 可以用 `write_file` 自建/改自己的 `subagents/*.md`（self-evolution 闭环，与 save_skill 对称；workspace 工具治理已覆盖，`subagents/` 不在 P2 的 `memory/` 封禁范围）。
- **store**：复用 `SubagentDefinitionStore`（`base_dir = <agent_workspace>/subagents`），`validate_subagent_name` 路径边界校验原样生效。

### 12.3 tenant 级公司库（开门，C2+C4）

- 存储已有（切口⑤）。开两扇门：admin API + Company Admin UI。
- 公司管理员策展共享定义（如 `market-research-explorer`），全 tenant agent 可用。
- **治理叠加（后续，不在本轮）**：per-agent 可用性策略（哪些 agent 能用哪些 tenant 定义，对标 MCP server 的 per-agent override 模式）。MVP 先全 tenant 可用 = 现状语义不变。

### 12.4 解析链（无双路径）

```text
spawn_subagent(definition_name)
  → agent 级 store（<workspace>/subagents/<name>.md）   # 优先
  → tenant 级 store（_tenants/<tid>/.../definitions/）   # fallback
  → 都没有 → 报错并附两级可用定义列表
```

一条解析链、两个作用域；list 面（API/工具 discovery）合并去重、同名 agent 级胜、每行标 `scope: agent | tenant | builtin`。内置 type（explorer/worker/critic）在 list 中呈现为 `builtin` 只读行（可作新建模板），不落盘。

### 12.5 记忆跟随定义作用域（待决1 的延伸澄清，非推翻）

待决1 拍板"`记忆.md` 纯 tenant-scoped"时**只存在 tenant 级定义**。引入 agent 级定义后，对称规则：**记忆跟随定义的作用域**——

| 定义作用域 | `记忆.md` 位置 | 理由 |
|---|---|---|
| tenant 级定义 | `_tenants/<tid>/subagents/memory/<name>.md`（现状不动） | 公司共享实体，经验全 tenant 复用 |
| agent 级定义 | `<agent_workspace>/subagents/.memory/<name>.md` | 防跨 agent 串忆：两个 agent 各有同名私有定义时，手艺各自积累 |

tenant 隔离铁律不破（agent workspace 本就在 tenant 内）；governed write gate 不变量原样适用两处。

### 12.6 产品边界：subagent 配置 ≠ HR 雇人

正式 agent = 数字员工（HR 入职、org chart、soul、channel、长期身份）；subagent = 该员工的**手艺人分身**（无身份、记忆只记 How、任务级回收）。所以配置入口在 **AgentDetail 能力区**（给员工配工作方法），绝不在 HR/组织流程。延续术语边界（spawn 只收 worker，不并 peer delegation）。

### 12.7 API 面

```text
# agent 级（check_agent_access 守卫；写需 manage）
GET    /agents/{agent_id}/subagents              # 合并列表（agent+tenant+builtin，标 scope；同名 agent 级胜）
GET    /agents/{agent_id}/subagents/{name}       # 详情（定义全文 + 生效 scope + 记忆.md 存在性/摘要）
PUT    /agents/{agent_id}/subagents/{name}       # 创建/更新 agent 级定义（render→save，格式校验）
DELETE /agents/{agent_id}/subagents/{name}       # 删除 agent 级定义（tenant 级定义不受影响 → 删后回落 fallback）

# tenant 级（org admin 守卫）
GET    /enterprise/subagents
PUT    /enterprise/subagents/{name}
DELETE /enterprise/subagents/{name}
```

### 12.8 UI 面

- **AgentDetail 新 "Sub-agents" tab**（capability 组，与 Skills/MCP/Workflows 并列第四能力模块；Knowledge 维持只 deep-link 不管理的边界）：列表（name/scope/type/model/工具面摘要）→ 详情/编辑（markdown 编辑器 + frontmatter 校验）→ 从 builtin 模板新建。
- **Company Admin 新区段**：tenant 库同款列表/编辑。
- i18n en+zh 同步。

### 12.9 切口与验收

| 切口 | 内容 | Red tests | 验收 |
|---|---|---|---|
| **C1** 后端解析链 | agent 级 store + spawn/discovery 解析链（agent→tenant→builtin）+ 记忆跟随作用域 | 同名覆盖、fallback、删 agent 级回落、agent 级记忆不串 tenant、路径边界 | spawn_subagent(definition_name) 两级都能命中；list 合并标 scope |

**C1 实装证据（2026-06-05）**：`subagent_definition.py` 增 `SCOPE_*` 常量、`agent_subagent_root`/`definition_store_for_agent`（同 store 类换 base_dir，零新格式）、`resolve_subagent_definition`（agent 优先 → tenant fallback → None）、`list_subagent_definitions`（合并去重 agent 胜、builtin 只读模板行、损坏文件跳过并 warn 不空整表）；`subagent_memory.py` 增 `memory_store_for_agent`（`<workspace>/subagents/.memory/`，dot-dir 避开定义 glob）；spawn handler 改走解析链、响应带 `definition_scope`、not found 附合并可用列表（含 builtin 并指引 inline spawn）、记忆跟随定义作用域（inline spec 保持 tenant store 现状）、放宽"definition_name 必须有 tenant_id"（agent 级可独立解析）。TDD red→green：`tests/agents/test_subagent_scope_resolution.py` 8 例（同名覆盖/fallback/删除回落/记忆隔离含跨 agent/路径边界/list 合并+builtin 遮蔽）+ `test_subagent_spawn_tool.py` 新增 4 例（agent 胜+记忆跟随、tenant fallback、not found 双 scope 列表、无 tenant 解析 agent 级），先验证 collection error/4 failed 再实现转绿。回归：tests/agents/ + tests/tools/ 450 passed；§12.2 断言验证 = 工具层 `_write_file` guard 只封 `memory/`，`subagents/` agent 可自建。
| **C2** API | 7 端点 + 权限 | 多租户守卫（跨 tenant 404）、manage 写权限、格式非法 422、name 校验 | 前端可全 CRUD；非法 frontmatter 被拒带原因 |

**C2 实装证据（2026-06-05）**：新文件 `app/api/agent_subagents.py`（enterprise.py 已 1100+ 行不再塞）双 router：agent 级 4 端点（GET list 合并标 scope / GET detail 定义全文+生效 scope+记忆存在性/entry 计数摘要，builtin 名返回只读模板行 / PUT manage 守卫+parse 校验+frontmatter name 与 URL name 不一致 422 / DELETE 只删 agent 级、tenant 不受影响删后回落）+ tenant 级 3 端点（`/enterprise/subagents`，org_admin/platform_admin 守卫与 enterprise.py `_require_tenant_admin` 同语义）；PUT 收 markdown 全文走 runtime 同一 `parse_subagent_definition`（无第二 schema 无漂移）；`SubagentDefinitionStore.delete` 补齐（path guard 原样过）；main.py 注册 `/api`+`/api/v1` 双前缀实证 8 条路由。TDD red→green：`tests/api/test_agent_subagents_api.py` 14 例（合并 scope/detail 三态/PUT 创建/manage 403/坏 frontmatter 422/name mismatch 422/URL name 穿越拒/删除回落/删 tenant-only 404/跨 agent 404/enterprise CRUD/member 403），先 collection error 再实现转绿。回归：全量 3776 passed / 7 skipped（基线 3750+新增 26 精确吻合）；ruff+format 干净。
| **C3** AgentDetail UI | Sub-agents tab + adapter + i18n | 渲染/列表 scope 标记/编辑流 | 用户在 agent 详情页完成日常配置 |
| **C4** Company Admin UI | tenant 库管理 + i18n | admin 守卫渲染 | 管理员策展公司库 |

不变量复述：配置面**不新增任何运行时权力**——工具面收窄、capability gate、防递归、governed memory write 全部原样；本节只是给已有治理开人类入口。

---

> **状态**：设计稿 **v4**（2026-06-05：新增 §12 配置面与作用域，用户拍板"agent 级 + tenant 级双层、AgentDetail 第四能力模块 + Company Admin 公司库"方向；待文档拍板后实施 C1-C4）。v3（2026-06-02：DR 接入从主线抽离为后续阶段，subagent 先成完整源能力；"完整"= 到切口⑥含记忆进化）+ v2 内容（术语边界 + CC agent memory 事实纠正 + 切口拆分 + Memory Control Plane 闸 + §5.1 收窄）保留。取舍（§4）+ 主线切口顺序（§8 v3）+ 三待决已拍板冻结。轴 2（工作流编排，借鉴 CC Workflow 工具）作为独立后续。
