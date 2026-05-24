# Hive vs Mercury：Agent 架构对比

> ⚠️ **已过时更正（2026-05-23）**：本文多处把 Hive 描述为「无 loop detection / 50 轮硬上限 / 检索仅 word overlap」——这些**已不再属实**。当前代码：`kernel/loop_guard.py` 已接入（`engine.py:1870/1982/2055`）；`max_tool_rounds` 默认 **200**（`models/agent.py:63`）；检索为 MD-first + 加权 activation（`memory/activation.py:43-74`）；抽取有持久队列 + 重启 replay（`extract_queue.py`）。本文保留作历史对比快照；当前事实与路线见 `docs/self-evolution-sota-plan.md`。

> 范围：仅对比"agent 架构本身"，不涉及权限控制、多租户、企业相关层。
> 对比对象：`/Users/rocky243/Context Engineering/mercury-agent`（Mercury，TS/Vercel AI SDK）vs 本仓库（Hive，Python/FastAPI）。
> 方法：原子化扫描两边核心源码，按维度逐项对比，标注 `file_path:line_number`。

---

## 顶层定位差异

|  | Mercury | Hive |
|---|---|---|
| 形态 | 单 Agent CLI 工具（TypeScript ESM, ~12K LOC, 80 文件） | Agent 运行平台（Python，仅核心 5 文件就 7616 LOC，加 138 个 services） |
| LLM Loop | Vercel AI SDK 的 `streamText({ tools, maxSteps: 10 })` 包装 | 自实现 `AgentKernel.handle()`，2220 LOC，零 DB 依赖 |
| 入口模型 | Channel emit 事件 → Agent 监听 | 所有路径 funnel 到 `invoke_agent()` |
| 数据存储 | 用户家目录 `~/.mercury/`（YAML + MD + JSONL + SQLite） | 工作区文件 `workspace/*.md` 全 MD，无 DB shadow store |

Mercury 是"站在 Vercel SDK 上的 CLI agent"——agent loop 的核心（多步、工具调用协议、流式）全是 SDK 给的。Hive 是"从零写的 kernel + 一整套配套子系统"，复刻了 SDK 已有的能力，换来精细控制权。

---

## 1. Agent Loop（最核心差异）

### Mercury（`src/core/agent.ts`）

- `MAX_STEPS = 10`（agent.ts:242），SDK 内部决定何时停止
- **Loop detection 是亮点**——6 层防御（agent.ts:33-240）：
  - 25 次总工具调用硬上限
  - 12 次失败硬上限
  - 同工具同参 3 次中止
  - 同工具全失败 4 次中止
  - 5 次无 tool_call 的纯 reasoning 中止
  - 文本重复 3 次中止
- 无 context compaction（依赖 SDK），无 token budget 自管，无工具结果 size 控制

### Hive（`backend/app/kernel/engine.py`）

- 50 轮硬上限（更宽松）
- **Context compaction 是亮点**：
  - 主动：每 3 轮检查，>75% 利用率触发 LLM 摘要
  - 反应式：LLM 报 prompt-too-long 时重试 3 次，用截断策略
  - 微压缩：60min 默认 / 10min 高压时间窗清理老 tool result
- **工具结果分层**：单结果 >50K 字符溢出到 `workspace/artifacts/{tool_call_id}-{tool}.json`，per-round 200K 总预算
- **Provider-specific cache hints**：Anthropic / OpenAI / DeepSeek / Gemini 各自的缓存断点注入
- 没有 loop detection（靠 50 轮上限和 token budget 兜底）

### 诚实评价

Mercury 的 loop detection 设计成熟，Hive 几乎是裸奔的；但 Hive 的 context 工程明显比 Mercury 重一个量级——这是为了支持长会话 + 多渠道 + 大工具结果，Mercury 单 user 短会话不需要。

---

## 2. Memory 架构（最大哲学差异）

### Mercury：4 层异构存储

| 层 | 存储 | 写时机 | 读时机 |
|---|---|---|---|
| 短期 | `~/.mercury/memory/short-term/{convId}.json` | 每条消息 | 每次组 messages 时 |
| 长期 facts | `long-term/facts.jsonl` | `add()` | 关键词搜索（fallback 用） |
| 情景 events | `episodic/events.jsonl` | 每次交互 | 最近 N 条 |
| **Second Brain** | `second-brain.db`（SQLite + FTS5）| LLM 提取 fire-and-forget | `retrieveRelevant(query, max=5)` 加权排序 |

**Second Brain 是 Mercury 真正的工程亮点**（`user-memory.ts:1-469`, `second-brain-db.ts`）：

- **10 种记忆类型** + **2 scope**（durable / active）
- 加权排序：confidence 0.3 + importance 0.25 + durability 0.15 + age 0.2 + query match 0.1
- **自动 merge**（overlap ≥ 0.74）
- **极性冲突自动解决**（"prefers" vs "doesn't prefer" 按 confidence 仲裁）
- **老化淘汰**：active inferred 21d / active direct 42d / durable inferred 120d
- **晋升**：active 被强化 3 次后自动 → durable

### Hive：4 层 MD 金字塔（全文件，无 DB shadow store）

```
T0 raw logs → T2 learnings → T3 memory → soul.md
SESSION_CLOSE  RESPONSE_COMPLETE  Heartbeat (45min)  Dream (4h+3 sessions)
              (hot fire-forget +
               replay backfill)
```

- **T0 拆 3 类**（PR-1 后）：
  - `behavior/`（外部交互，可作为 T2 substrate）
  - `system/`（heartbeat、dream 的自我审计日志，仅审计）
  - `artifacts/`（>8K 工具结果溢出）
- **T2 双路径**：
  1. 热路径：`RESPONSE_COMPLETE` hook 触发 LLM 提取（cursor-based）
  2. Backfill 路径：`replay_messages_from_t0` 重放 behavior MD（idempotent）
- **T3 5 文件**：feedback / knowledge / strategies / blocked / user
- **Heartbeat（45min）+ KAIROS 持久 session**：跨 tick 保留对话历史，做 T2→T3 curation
- **Dream（4h + 3 sessions gate）**：T3 → soul.md 提升，flock 序列化防并发

### 诚实对比

| 维度 | Mercury Second Brain | Hive 4-layer MD |
|---|---|---|
| 检索 | FTS5 + 加权 score | word overlap + CJK char overlap + LLM rerank |
| 冲突处理 | 极性检测 + confidence 仲裁 | 无显式机制（靠 heartbeat LLM 重写时自然消解） |
| 演进 | 内部 tier promote（active→durable）| 跨层 promote（T2→T3→soul），更"人类化" |
| Soul 自演进 | **没有**（soul 是用户编辑的）| **有**（dream 自动写入 soul）|
| 自我审计 | 无 | system T0 日志记录 heartbeat / dream 的决策推理 |
| 工程复杂度 | 单点重投资（SQLite/FTS5/排序）| 全链路重投资（4 层 + 双周期任务 + 多 hook）|

这是两种完全不同的记忆哲学：

- **Mercury**：把 memory 当**结构化数据库**，靠 schema 和算法保证质量
- **Hive**：把 memory 当**人的笔记/反思/价值观沉淀过程**，靠时间周期和 LLM 重写演进

Mercury 的 Second Brain 在"短句事实"上更鲁棒（confidence、polarity、age 都有量化）；Hive 的 4 层在"长期身份漂移"上更深入（dream→soul 是 Mercury 完全没有的概念）。

---

## 3. Skill 系统

| | Mercury | Hive |
|---|---|---|
| 格式 | YAML frontmatter + MD body | 同 |
| 渐进披露 | catalog 在 prompt，body 由 `use_skill` tool 加载 | 同 |
| **关键差异** | skill 不绑定 capability | **skill 声明 `declared_packs`，加载时自动激活整套 capability pack** |

Hive 的 skill 是 capability 激活的入口（`invoker.py:563-602`），Mercury 的 skill 只是"提示词模板"。这是 Hive tool 系统的一个重要钩子。

---

## 4. Soul / Identity

### Mercury（`src/soul/identity.ts`）

- 4 个 MD：`soul.md` / `persona.md` / `taste.md` / `heartbeat.md`
- 每次只注入 `soul + GUARDRAILS + persona` ≈ 350 token baseline
- **用户手编辑导向，agent 不会改写**

### Hive（`prompt_sections/identity.py` + `workspace/soul.md`）

- **soul.md**（frozen prefix）：
  - Identity / Mission / Personality
  - **User Profile / Learned Behaviors / Core Strategies / Blocked Patterns**（dream 自动写入）
  - Boundaries / How I Learn
- **focus.md**（dynamic suffix）：当前任务/目标投影

### 关键差异

Mercury 的 soul = **用户手册**；Hive 的 soul = **用户手册 + agent 自传**。

---

## 5. Prompt 架构

### Mercury

- ~350 token baseline
- 无显式 section 划分
- 无 cache boundary 设计

### Hive：14 prompt sections（`runtime/prompt_sections/`）

- **Frozen prefix**（≤8K token，session 内复用）：
  - identity / system / tasks / executing_actions / tools / tone_style / skills_catalog / relationships
- **Dynamic suffix**（每轮重建）：
  - memory / scenario / active_packs / knowledge / environment / triggers
- 中间插 `PROMPT_CACHE_BOUNDARY` 标记 + provider-specific cache hint

这是 Hive 为 prompt cache 优化做的重投资。Mercury 完全没这个考虑（短会话 + 单用户，不需要）。

---

## 6. 入口 / Channel

### Mercury（`src/channels/base.ts`）

- 抽象 `Channel` 接口：`onMessage(handler)`, `send`, `stream`
- 2 个实现：CLI、Telegram
- agent 订阅 `channels.onIncomingMessage(...)`
- **没有"单一 invoke 入口"**

### Hive（`runtime/invoker.py`）

- 7+ 入口（WebSocket / Trigger / Heartbeat / Delegation / Feishu / Slack / Teams / DingTalk / WeChat）**全部** 构造 `AgentInvocationRequest` → `invoke_agent()`
- `execution_mode` 字段控制风险姿态：`conversation` / `coordinator` / `task` / `heartbeat`

### 评价

Mercury 是"事件流"模型，Hive 是"RPC 模型"。这直接决定了 Hive 能扩展到多渠道而 Mercury 加渠道得改 `agent.ts`。

---

## 7. Multi-Agent / Delegation

| | Mercury | Hive |
|---|---|---|
| 多 agent 协作 | **完全没有** | 有 `delegate_to_agent` 工具 |
| 防递归 | N/A | `core_tools_only=True` 让 worker 拿不到 delegate 工具 |
| 审计 | N/A | `ExecutionIdentity` ContextVar + `delegation_token` 链路追踪 |
| HR agent 创建 agent | N/A | 有（`hr_agent_template/`）|

这是 Hive 的"平台"维度，Mercury 不在这个赛道。

---

## 8. Hook 系统

### Mercury

没有 hook bus。

- 几个零散 callback：`setOnScheduledTask`, `onHeartbeat`, `onMessage`
- Memory 提取直接硬编码在 `agent.ts:896-898`

### Hive（`runtime/hooks.py`）：15 events + matcher

- **Tool**: `PRE_TOOL_USE` / `POST_TOOL_USE` / `POST_TOOL_FAILURE`
- **Session**: `SESSION_START` / `RESPONSE_COMPLETE` / `SESSION_IDLE` / `SESSION_CLOSE`
- **Compaction**: `PRE_COMPACTION` / `POST_COMPACTION`
- **Delegation**: `DELEGATION_START` / `DELEGATION_END`
- **Hive 特有**: `TRIGGER_END` / `HEARTBEAT_TICK_END` / `DREAM_END`
- **通知**: `MEMORY_EXTRACTED`
- Matcher 支持 `if="tool=feishu_*,source=heartbeat"` 这种过滤

### 意义

Hive 的整个 memory pipeline 是用 hook 驱动的：

- `RESPONSE_COMPLETE` → T2 提取
- `PRE_COMPACTION` → 紧急提取
- `SESSION_CLOSE` → T0 写入

Mercury 直接硬编码在 agent loop 里。

---

## 9. Provider 抽象

两边都做了多 provider + 自动 fallback。

- **Mercury**：8 providers（DeepSeek / OpenAI / Anthropic / Grok / Ollama Cloud/Local / Mimo / OpenAI-compat）+ `getFallbackIterator()` + `markSuccess()` 记住成功
- **Hive**：同样多 + provider-specific cache hints 注入

差异不大，Hive 多一个 cache hint 层。

---

## 10. 自我演进 / Lifecycle

### Mercury

- 显式状态机：`unborn → birthing → onboarding → idle ⇄ thinking → responding → sleeping → awakening`
- 5min throttle 的 heartbeat consolidation（用于 memory pruning）

### Hive

没有显式状态机，但有：

- `execution_mode` 4 种（conversation / coordinator / task / heartbeat）控制风险姿态
- Heartbeat（45min）+ Dream（4h + 3 sessions）双周期任务
- KAIROS 持久 session 跨 tick 保留思考连续性
- system T0（`heartbeat-*.md`, `dream-*.md`）记录"我为什么这么决策"——agent 自己的日记

### 诚实评价

- Mercury 的 lifecycle 是**工程状态机**（哪个状态做什么）
- Hive 的演进是 **agent 自传**（每次反思都留痕）

Hive 这块的设计深度高于 Mercury。

---

## 整体诚实评价

**Hive 比 Mercury 复杂大约 5 倍**，但这复杂度的成因可以分两类：

### A. 平台必需的复杂度（不在本对比范围）

- 多 agent 协作 / delegation
- 多渠道入口（Feishu / Slack / Teams / ...）
- 多 execution_mode

### B. 纯 agent 智能层面的工程深度（本对比的焦点）

| 维度 | 谁更深入 | 具体证据 |
|---|---|---|
| Loop 鲁棒性 | **Mercury** | 6 层 loop detection vs Hive 裸 50 轮上限 |
| Context 管理 | **Hive** | 主动+反应式压缩+微压缩+artifact 溢出 vs Mercury 依赖 SDK |
| Memory 结构化 | **Mercury** | Second Brain SQLite/FTS5 + 10 类型 + 极性仲裁 + 老化 vs Hive 全文件 |
| Memory 演进 | **Hive** | T2→T3→soul 三级 promote + dream 自动改 soul vs Mercury soul 仅人编辑 |
| 自我审计 | **Hive** | system T0 记录 heartbeat / dream 决策推理 vs Mercury 无 |
| Prompt 工程 | **Hive** | 14 sections + frozen/dynamic + cache boundary + provider hints vs Mercury 350 token 单段 |
| Skill→Capability | **Hive** | skill 激活整套 pack vs Mercury skill 只是模板 |
| Hook 平台化 | **Hive** | 15 events + matcher 驱动整个记忆管道 vs Mercury 硬编码 |
| Provider fallback | 平手 | 都有，Hive 多 cache hint |
| 单文件可读性 | **Mercury** | `agent.ts` 1200 行能看完整流程 vs Hive 要跨 5+ 文件 |

### 最尖锐的差距在两个方向

1. **Mercury 在"短会话单 agent 的工程鲁棒性"上更精炼**——loop detection、Second Brain 排序、provider iterator 都是反复打磨过的；Hive 在这些点上要么没做（loop detection），要么粗糙（memory ranking 是 word overlap）。

2. **Hive 在"长期 agent 自我演进"上做得更深**——dream 把 T3 提升到 soul.md、heartbeat 用 KAIROS 持久 session 跨 tick 思考、system T0 给 agent 留"决策日记"——Mercury 完全没有"agent 改写自己身份"这个概念，soul 永远是用户手编辑的。

---

## 可借鉴清单

如果只看"和 Hive 架构兼容、且 Mercury 做得更好可以拿来用"的点：

| 借鉴项 | 来源（Mercury）| 落地位置（Hive）| 优先级 |
|---|---|---|---|
| 6 层 loop detection | `agent.ts:33-240` | `kernel/engine.py` 加一层 detector | 高 |
| 极性冲突仲裁（"prefers" vs "doesn't"）| `user-memory.ts:572-603` | `services/extract_agent.py` 或 heartbeat curation | 高 |
| Memory confidence + age 加权排序 | `user-memory.ts:425-442` | `memory/retriever.py` 替代 word overlap | 中 |
| Active/Durable scope + 老化淘汰 | `second-brain-db.ts:305-342` | T3 文件加 metadata，老化推回 T2 | 中 |
| Provider fallback iterator + last-success | `providers/registry.ts:78-92` | Hive provider 选择层 | 低（Hive 已有部分）|

---

## 一句话总结

> Mercury 是**工程上更精致的单 agent CLI**，Hive 是**工程上更宏大的 agent 演进平台**——Mercury 在"agent 这一次怎么干好"上更扎实，Hive 在"agent 怎么变成它自己"上更深入。
