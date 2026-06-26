<div align="center">
  <h1>Hive</h1>
  <h3>开源企业数字员工操作系统 —— 自进化 Agent Runtime + 公司级控制中台</h3>
  <p><a href="README.md">English</a> | <strong>简体中文</strong></p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/react-19-61dafb.svg" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/postgres-15-336791.svg" alt="PostgreSQL"></a>
</div>

<br>

Hive 是一个可自部署的**企业数字员工操作系统**。它要解决的不是"如何再做一个聊天机器人"，也不是"如何写一个 agent workflow"，而是公司如何真正雇佣、授权、运行、审计、纠正和持续改进一批 AI 数字员工。

今天的 AI agent 市场有一个断层：模型越来越强，但模型厂商不提供完整公司治理；企业 SaaS 有权限和审计，但多数 agent 仍是静态配置或人在回路的离线优化；开源 agent framework 能帮开发者拼装流程，却不能直接让一家公司运营长期工作的数字员工。Hive 的定位就是这个交叉点：**运行时自进化的数字员工 + 公司级控制中台**。

## Hive 要成为的东西

Hive 的一等目标只有两个：

1. **自进化 Agent Runtime**：每个 Agent 都有身份、记忆、技能、工具、私有工作区和长期任务能力，并能从真实工作、用户反馈、会话结果和失败案例中持续改进。
2. **公司级控制中台**：企业可以统一管理这些 Agent 的身份、owner、权限、工具、预算、渠道、审批、审计、组织关系和数据边界。

这意味着 Hive 不把 Agent 当作一次性 Prompt，也不把治理当作外层 UI。Agent 的智能增长、记忆写入、技能晋升、外部行动和组织权限都必须进入同一套 runtime contract。

## 四个产品支柱

**1. 数字员工身份**

每个 Agent 都有一份 `soul.md` 身份契约、独立 workspace、长期 memory、技能目录、owner/company context、渠道配置和基于 trigger 的唤醒策略。它跨会话、跨模型、跨 IM 渠道延续自己的工作身份，而不是每次对话重新开始。

**2. 受治理的自我进化**

Hive 允许 Agent 学习，但不允许它靠自评决定自己变强。Response-complete extraction、fast reflection learning brain、Heartbeat、Dream、session feedback、skill distillation 和 patch-first skill candidate 都可以提出改进；真正持久化时必须经过 source evidence、hard verification、rollback metadata、audit record 和 replay/eval gate。自我进化不是"模型觉得自己对"，而是系统能证明这次改进没有污染记忆、欺骗 owner 或绕过公司边界。

**3. 公司级控制中台**

Hive 面向的是一家公司运营一批数字员工：Company Admin、Platform Admin、Agent Circle、HR Agent、工具注册、能力策略、审批流、多租户 RLS、审计日志、预算、组织结构、per-agent channel config、MCP authz、A2A-style Agent Card 和 interoperability profile 都是同一套控制面的一部分。

**4. Harness-grade Runtime**


## 两轮大改后的当前基线

**第一轮：把 Hive 从复杂 Agent 应用打成企业级 Agent Harness。**

第一轮审计发现的问题不是"缺几个功能"，而是失败路径和跨模块闭环不足：provider overload 一击毙命、长任务被进程重启打断、web chat 断线影响运行、工具执行边界不够硬、验证门有自评冒充硬验证的风险。整改后，Hive 的 runtime 基线变成：可恢复 `RuntimeTask`、统一 provider retry/fallback、DB-backed invocation trace、受治理工具执行、沙箱化代码执行、MCP 安全导入、Memory Control Plane 写入门和可审计 promotion path。

**第二轮：把可运行底座推进到 SOTA 数字员工能力。**

第二轮不再只对标 Claude Code 的 harness 基线，而是对标 Devin、Letta、ACE、Voyager、Temporal、Glean、Microsoft Entra Agent ID 等分散 SOTA。落地结果包括：`skill_guard` 硬验证门、fast reflection learning brain、patch-first 技能修补、ACE-style T3 reinforcement counters、Session Useful/Misleading feedback 生产入口、10 次 LLM status/network retry、529 fallback、workflow completion side-effect 去重、subagent/web-chat restart recovery、Anthropic interleaved-thinking header 和 signed thinking round-trip。

## 现在能做什么

- **创建数字员工**：HR Agent 通过 2-3 轮对话生成 `soul.md`、起始任务、工作边界和触发器。
- **接入工作现场**：同一个 Agent 可以住在 Web Chat、飞书/Lark、Slack、Discord、钉钉、企业微信、个人微信、Telegram、Email 和 Microsoft Teams。
- **长期记忆与学习**：T0/T2/T3/soul 四层记忆把原始行为、学习提取、语义记忆和身份固化分开治理；fast reflection 和 session feedback 不直接污染 T3，而是进入候选、ledger 和验证路径。
- **自主但可控地行动**：cron、interval、webhook、polling、message-event trigger 和 workflow 让 Agent 主动工作；外部可见、敏感、不可逆或跨公司边界的动作需要 preflight、approval 或 checkpoint。
- **企业级运营**：公司后台管理模型、员工、组织、工具、技能、配额、审批、审计、记忆和渠道；平台后台管理全局配置。
- **Office 与文档工作**：Agent workspace 支持浏览器内 DOCX/XLSX/PPTX 编辑，ONLYOFFICE callback 签名和版本修订保持文档链路可追踪。
- **模型平等与自部署**：Hive 不绑定某个模型或办公生态，可以接 Anthropic、OpenAI、Gemini、DeepSeek、Qwen、MiniMax、Azure、OpenRouter、Zhipu、Kimi、vLLM、Ollama、SGLang 或自定义 OpenAI-compatible endpoint。

> [!NOTE]
> Hive 完全可自部署。FastAPI + React + PostgreSQL + Redis，自带 Docker Compose，支持 14+ 种 LLM 提供商（Anthropic、OpenAI、Gemini、DeepSeek、通义千问、MiniMax、Azure、OpenRouter、智谱、Kimi、vLLM、Ollama……）。

## 快速开始

**一键启动（推荐）：**

```bash
git clone https://github.com/rocky2431/hive-agents.git
cd hive-agents
bash setup.sh --dev      # 自动配置 PostgreSQL、虚拟环境、前端依赖、初始化数据
bash restart.sh          # 启动后端（:8008）与前端（:3008）
```

打开 http://localhost:3008 ，注册第一个用户（自动成为平台管理员），然后跟 HR Agent 聊几句创建你的第一个数字员工。

**或使用 Docker：**

```bash
cp .env.example .env
docker compose up -d --build    # 全栈运行在 http://localhost:3008
```

> [!TIP]
> HR Agent 通过 2–3 轮对话创建新员工。告诉它你想要的角色（"一个负责处理账单升级的客户支持主管"），回答几个澄清问题，它会自动生成 soul 契约、初始任务列表和启动触发器。

## 工作原理

```
                   +-----------------------------+
                   |   前端（React 19 + Vite）    |
                   +--------------+--------------+
                                  |  /api  /ws
                   +--------------v--------------+
                   |   后端（FastAPI + Python）    |
                   +--------------+--------------+
                                  |
       +--------------+-----------+-----------+--------------+
       |              |                       |              |
   PostgreSQL      Redis              后台守护进程            Agent 文件系统
   (RLS, async)   (缓存, pubsub)     - Trigger（15s 一跳）    /data/agents/
                                     - 飞书 / 钉钉 / 企微        {agent_id}/
                                       / 微信长连接管理          soul.md
                                     - Heartbeat / Dream         workspace/
                                     - Evolution daemon          memory/
                                                                 logs/
                                                                 skills/
```

无论是 WebSocket 聊天消息、飞书 webhook、定时触发器，还是另一个 Agent 的委派调用，所有 Agent 调用都流经同一个**无状态 kernel**：

```
入口  →  invoker.py（解析依赖、组装 Prompt）
      →  kernel/engine.py（多轮 LLM 循环，依赖注入式）
      →  tools/service.py（受治理的工具执行）
      →  tools/governance.py（安全分区 → 能力闸门 → 审批流）
```

Kernel **不导入任何数据库代码** —— 所有 I/O 都通过注入回调完成。这意味着同一个 kernel 同时跑 Web 聊天、飞书 webhook、定时触发器、Heartbeat、Agent 间委派，上下文压缩、工具预算、Prompt 缓存、invocation trace、provider retry 与受治理工具执行语义完全一致。

Web Chat 是可恢复执行：浏览器 WebSocket 只是订阅后台 `RuntimeTask(task_type="web_chat_turn")`。页面刷新或临时断开后，后台 run 会继续，前端通过 active-run 轮询恢复状态。

## 记忆金字塔

这是让 Hive "不只是一个套了向量库的聊天机器人"的核心。

```
soul.md      ← Dream / Soul Writer     （reviewed soul.md.next，Platform Soul Gate exact commit）
   ↑
T3 语义记忆   ← T3 Consolidator         （LLM pitch + Memory Gate review + Platform Gate exact XML blocks）
   ↑                                     memory/t3/{episodes,user,worker,capabilities}.md
T2 Package   ← T0 -> T2 distillers      （summary.md / labels.md / review.md / manifest.json）
   ↑
T0 证据账本   ← session ledger           （append-only MD/XML events，segment-sealed resume boundaries）
               覆盖 chat、task、trigger、delegation、heartbeat、dream 的原始证据
```

| 层级 | 存放位置 | 写入者 | 内容 |
|-------|-------|-----------|---------------|
| **T0** | `memory/t0/sessions/<session_id>/segments/<segment_id>/source.md` | web chat、task executor、runtime hooks | append-only 原始 MD/XML events —— user、assistant、tool、task、trigger、delegation、heartbeat、dream 与 segment boundary |
| **T2** | `memory/sessions/<session_id>/segments/<t2_segment_id>/{summary.md,labels.md,review.md,manifest.json}` | LLM summary/label agents + 独立 Memory Gate review；Platform Gate 提交 package metadata | 每个 source session segment 对应一个 reviewed Segment Package，并用 `source_refs` 回指 T0 证据 |
| **显性记忆 Overlay** | `memory/explicit/<scope>/...` | `save_memory`，只处理用户明确要求记住的内容 | 立即可激活的 scoped overlay；后续只能通过同一条 T3 consolidation lane 并入 accepted T3 |
| **T3** | `memory/t3/{episodes.md,user.md,worker.md,capabilities.md}` | T3 Consolidator + Memory Gate + Platform Gate exact commit | 已收敛的语义 XML blocks：情景锚点、用户模型、worker 规则、能力/SOP 种子 |
| **Skill candidates** | `evolution/skill_candidates/<candidate_id>/` | `save_skill`、fast reflection、Skill Distiller | inactive `SKILL.md.draft` / `candidate_signal.md` packages；active skill 必须经过 Skill Gate promotion |
| **soul** | `soul.md` | Dream/Soul Writer，经 Soul Memory Gate + Platform Soul Gate | 永久身份 —— mission、voice、boundaries 和高稳定行为宪法 |

Heartbeat cadence 由配置驱动：`evolution_daemon` 每 `HEARTBEAT_TICK_SECONDS` 调度一次（默认 60 秒），可运行 Agent 按受平台托管的 `HEARTBEAT_DEFAULT_INTERVAL_MINUTES` cadence 进入资格判断（默认 120 分钟）。后续 Heartbeat tick 如果没有新的 T2 entries 会直接跳过。完整 Dream 是更慢的身份层操作：至少 24 小时，并满足 3 个会话或 2 次有效 Heartbeat。Soft Dream 只在 T3 压力上来时做确定性去重、容量缓压和 index refresh。

**面向人的记忆真相仍是 MD 文件**。Accepted T3 语义真相只包含上面四个 `memory/t3/*.md` 文件；`memory/wiki_map.md` 是唯一 generated navigation read model，不是第二套记忆库，也不是常驻 prompt 记忆。旧 `memory/learnings/*.md`、`understandings.md`、根目录 `memory/INDEX.md`、小写 `memory/index.md` 和 `.derived/t3_index.md` 都是兼容或已退役表面，不是 canonical runtime truth。legacy learnings extractor 默认 fail-closed，只允许在显式迁移环境变量 `HIVE_ENABLE_LEGACY_T2_BACKFILL=1` 下运行。默认不配置任何外部 T3 记忆增强程序。

记忆金字塔只是沉淀路径。真正决定 Agent 如何判断和行动的是 **Memory Control Plane**：

| 层级 | 作用 |
|-------|------|
| Principal stack | 显式区分公司、直接 owner、创建者/当前用户、委派 Agent，而不是把每个 prompt 都当作同等授权。 |
| 隐私与写入安全 | 记忆持久化前先分类；凭证直接拒绝，PII 可脱敏，长期条目必须带 evidence/lifecycle metadata。 |
| 动态激活 | 按当前目标、owner/company 相关性、open loop 压力、retention 分数和敏感度访问权来选择进入上下文的记忆。 |
| 决策轨迹与反馈 | 记录 Agent 为什么行动、询问、拒绝或升级，并把 owner feedback 反连到当时的 decision。 |
| 会话校准 | 持久化 useful/misleading feedback event，并让校准学习继续走 T2/T3 写入门。 |
| 记忆卫生 | 退役 legacy shadow store，隔离 dead stub，补齐缺失 lifecycle metadata，保持 Markdown 记忆为唯一真相源。 |
| 协调运行时 | 用 Lease、Signal、Checkpoint、Sentinel 让多 Agent 协作和 confirm-first 动作成为显式运行时对象。 |
| 主动员工循环 | Heartbeat 可以准备低风险有用材料，但对外可见动作必须走 Checkpoint，策略调参必须通过 replay evaluation。 |

完整设计和阶段证据见 [`docs/owner-steward-agent-memory-design.md`](docs/owner-steward-agent-memory-design.md)。

## 产品界面分层

Hive 现在分成三层界面：

| 界面 | 路由 | 用途 |
|---------|--------|---------|
| App | `/plaza`, `/agents/:id`, `/messages` | 日常 Agent 交互。`/plaza` 用户侧命名为 **Agent圈**。 |
| 公司后台 | `/enterprise/*` | 公司工作台、模型配置、记忆、HR、工具、技能、配额、用户、组织、审批、审计、邀请码。`/dashboard` 会跳转到 `/enterprise/dashboard`。 |
| 平台后台 | `/admin/*` | 平台管理员设置。 |

## 渠道集成

| 渠道 | 连接方式 | 能力 |
|---------|-----------|--------------|
| 飞书 / Lark | WebSocket + Webhook | 聊天、OAuth SSO、文档、Wiki、电子表格、多维表格、任务、日历、审批卡片 |
| Slack | Bot API | 聊天 |
| Discord | Bot Gateway | 聊天（可选 SOCKS5 代理） |
| 钉钉 | Stream SDK | 聊天 |
| 企业微信 | WebSocket + Webhook | 聊天（AES-CBC 加密） |
| 个人微信 | Stream bridge | 个人聊天桥接 |
| Telegram | Bot API | 聊天 |
| 邮件 | SMTP/IMAP 配置 | 发送、读取、回复 |
| Microsoft Teams | Bot Framework | 聊天 |

渠道配置是**按 Agent 维度**的，不同员工可以同时活在不同的工作 IM 里 —— 销售在飞书、研发在 Slack、运营在钉钉 —— 共用同一套 Hive 后端与租户。

## 架构总览

| 模块 | 文件数 | 说明 |
|-------|-------|-------|
| ORM 模型 | 43 | 租户隔离 SQLAlchemy 模型，包含 runtime tasks、coordination、objectives、identity、invocation spans、session feedback |
| 业务服务 | 163 | LLM 客户端、trigger/evolution 守护、渠道流、记忆、Office、治理、技能、trace、MCP authz、interoperability |
| Kernel | 1 个无状态引擎 | 默认最多 200 轮工具 · 75% 上下文压缩阈值 · 50KB 单工具结果上限 · trace spans · thinking signatures |
| 数据库迁移 | 79 | Alembic，单 head 不可变约束 |
| 前端页面 | 16 个页面入口 + 40 个嵌套页面/区块辅助文件 | AgentDetail、Agent圈、公司后台工作台/设置、平台后台 |
| 前端 API | 37 个领域适配器/测试/index 文件 | TanStack Query 管服务端状态、Zustand 管 UI 状态 |

更深入的技术细节请见 [`ENGINEERING.md`](ENGINEERING.md)（架构、不变量、运行时契约）与 [`AGENTS.md`](AGENTS.md)（给 AI 编程助手的开发参考）。

## 技术栈

| 组件 | 选型 |
|-----------|--------|
| 后端 | Python 3.12、FastAPI、SQLAlchemy 2.0 async、asyncpg、Pydantic v2 |
| 前端 | React 19、TypeScript 5、Vite 6、React Router 7、TanStack Query 5、Zustand 5 |
| 数据库 | PostgreSQL 15（含 RLS 行级隔离）、Redis 7 |
| LLM | Anthropic、OpenAI、Gemini、Azure、DeepSeek、通义千问、MiniMax、OpenRouter、智谱、Kimi、vLLM、Ollama、SGLang、自定义 OpenAI 兼容端点 |
| 数据库迁移 | Alembic |
| 代码检查 / 格式化 | Ruff（Python）、ESLint + Prettier（TypeScript） |
| 测试 | pytest（后端）、Vitest（前端） |
| 部署 | Docker Compose、Railway（`backend`、`frontend`、`Postgres`、`Redis`、`onlyoffice-documentserver`） |

## 常见问题

### 我为什么不直接用 LangGraph / AutoGen / CrewAI？

它们是**Agent 框架**，回答的是"开发者如何写一个 agent workflow"。Hive 是**企业数字员工操作系统**，回答的是"公司如何雇佣、授权、运行、审计、纠正和升级一批长期工作的数字员工"。

如果你只是要在代码里编排几个 LLM 节点，框架足够。如果你要让同事通过 UI 创建 Agent、把它接进飞书或 Slack、给它工具权限、设置触发器、查看它的长期记忆、审批高风险动作、审计每次外部行为、并让它在可验证边界内持续变强，Hive 提供的是框架之上的产品层和控制面。

### `soul.md` 是什么？

它是每个 Agent 工作目录根部的 Markdown 文件，描述**这个 Agent 是谁** —— 角色、主要服务对象、核心产出、操作风格、质量标准、边界、学习方式。和埋在代码里的 system prompt 不同，soul 是一等公民工件：可编辑、可版本化、UI 里直接展示。Dream 守护进程会随着 Agent 的成长更新它。Agent 的身份字面意义上活在文件里。

### 不用飞书 / Lark 行吗？

行。飞书集成最深（24 个办公工具、OAuth SSO、审批卡片）只是因为项目从那儿起步，但每个渠道都是可选的。你可以纯用 Slack、Discord，或者只用内置 web 聊天（`:3008`）跑 Hive。

### 能完全离线运行吗？

可以。把 LLM 提供商指向 vLLM / Ollama / SGLang 或任何 OpenAI 兼容端点即可。记忆管线、hooks、治理、触发器 —— 一切都在本地运行。

### 生产可用吗？

它已经在维护者自己团队的生产环境运行。当前主线已经过两轮 harness / SOTA 对标整改，具备 restart-resumable `RuntimeTask`、provider retry/fallback、DB trace、沙箱化代码执行、受治理记忆写入、硬验证 promotion path、多租户 RLS、审计日志、密钥加密和审批流。

但它仍是 1.0 前产品：API 和 schema 会继续演进，升级时应按 Alembic migration 和 release notes 执行。把它当作一个年轻但认真追求生产级闭环的企业系统，而不是一次性 demo。

### 怎么扩展？

按工作量从小到大三层：

1. **Skills（技能）** —— Agent 按需加载的渐进式能力胶囊。文件夹型 Skill 可以包含指令、引用资料、模板、脚本、eval、workflow 定义和 subagent 定义；加载 Skill 只增加上下文和指导，真正执行仍走受治理的 workflow、subagent/delegation 或 sandbox/code runtime。
2. **MCP Server** —— 通过 UI 导入任意 [Model Context Protocol](https://modelcontextprotocol.io) Server，工具会作为 deferred runtime tool group 被发现和启用。
3. **原生工具** —— 在 `backend/app/tools/handlers/` 加一个 handler，注册到 runtime，写一条治理规则。如果你的工具要用新型凭证或自定义流式协议，走这条路。

## 文档

- [`AGENTS.md`](AGENTS.md) —— 给 AI 编程助手的技术参考（命令、不变量、约定）
- [`ENGINEERING.md`](ENGINEERING.md) —— 完整架构：kernel、Prompt 装配、治理、记忆、部署
- [`CLAUDE.md`](CLAUDE.md) —— 给 Claude Code 会话的项目指引
- [`docs/harness-engineering-audit-2026-06-11.md`](docs/harness-engineering-audit-2026-06-11.md) —— Harness 审计、整改日志与验证证据
- [`docs/round2-sota-benchmark-2026.md`](docs/round2-sota-benchmark-2026.md) —— 第二轮 SOTA 对标与当前优化路线
- [`docs/self-evolution-sota-plan.md`](docs/self-evolution-sota-plan.md) —— 自我进化基石计划
- [`docs/agent-memory-purity-spec.md`](docs/agent-memory-purity-spec.md) —— 记忆纯净、生命周期与卫生契约

## 致谢

依赖注入式 kernel 架构与记忆管线受 Claude Code 会话生命周期及更广义的 agent harness 思潮启发；飞书集成来自连续几个月在 lark-cli 上跑 Agent 的一手痛苦；Hive 将持久身份放在 `soul.md`，把运行进展放在受治理记忆、work ledger、workspace artifact 和 trigger 唤醒策略里，避免临时工作反复改写 Agent 身份。

## 许可证

[Apache License 2.0](LICENSE)
