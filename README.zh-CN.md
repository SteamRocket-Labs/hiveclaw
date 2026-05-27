<div align="center">
  <h1>Hive</h1>
  <h3>开源数字员工平台 —— 拥有持久身份与长期记忆的多智能体协作系统</h3>
  <p><a href="README.md">English</a> | <strong>简体中文</strong></p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/react-19-61dafb.svg" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/postgres-15-336791.svg" alt="PostgreSQL"></a>
</div>

<br>

Hive 是一个可自部署的**数字员工**平台 —— 它构建的不是关掉浏览器就忘掉一切的无状态聊天机器人，而是有身份、能记忆、住在你工作群里、并能自主行动的 AI 同事。每个 Hive Agent 都拥有一份身份契约（`soul.md`）、一个私有工作目录、一套四层记忆体系、在你不在线时仍会持续"思考"和"做梦"的后台守护进程，以及一层让判断始终对齐 owner 与公司边界的 Memory Control Plane。

**Hive 与众不同之处：**

- **持久身份** —— 每个 Agent 都有一份 `soul.md`：它的角色、语气、边界与质量标准。它跨越对话、跨越会话、甚至跨越模型切换都不会丢失。
- **四层记忆金字塔 + 控制平面** —— 原始日志 → 学习提取 → 语义记忆 → 身份固化，同时由 owner/company 语境、隐私门、动态激活、决策轨迹与 replay 守卫来治理。无需手动配置 RAG。
- **Heartbeat 与 Dream** —— 后台守护进程在你不在时替 Agent 思考：整理它学到的东西、准备低风险跟进材料、决定哪些值得保留，并提出安全的身份/策略演化建议。
- **可恢复 Web Chat** —— Web 聊天回合现在作为后台 `RuntimeTask` 执行。刷新页面或临时断开只会断订阅，不会中断 Agent 正在做的事。
- **Office 工作台** —— Agent workspace 已支持通过 ONLYOFFICE 在浏览器里编辑 DOCX/XLSX/PPTX，并带签名回调与版本修订。
- **直接住在群聊里** —— 一等公民支持飞书/Lark、Slack、Discord、钉钉、企业微信、Microsoft Teams。同一个 Agent，同一份记忆，跨所有渠道。
- **对话式创建** —— HR Agent 通过 2–3 轮对话面试你，自动生成新员工。无需写 Prompt。
- **自主行动** —— 支持 cron、interval、webhook、轮询、消息事件触发。Agent 会主动起来工作，而不只是被动回答。
- **企业级治理** —— 安全分区、能力策略、人工审批流、多租户 PostgreSQL RLS 隔离、完整审计链。
- **60+ 内置工具** —— 文件读写、网页搜索、飞书办公套件、邮件、OfficeCLI/ONLYOFFICE 文档流、Deep Research，以及任意 MCP Server 一键导入。

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
                   |   后端（FastAPI 3.12）       |
                   +--------------+--------------+
                                  |
       +--------------+-----------+-----------+--------------+
       |              |                       |              |
   PostgreSQL      Redis              后台守护进程            Agent 文件系统
   (RLS, async)   (缓存, pubsub)     - Trigger（15s 一跳）    /data/agents/
                                     - 飞书 / 钉钉 / 企微        {agent_id}/
                                       / 微信长连接管理          soul.md
                                     - Heartbeat / Dream         focus.md
                                     - Evolution daemon          workspace/
                                                                 memory/
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

Kernel **不导入任何数据库代码** —— 所有 I/O 都通过注入回调完成。这意味着同一个 kernel 同时跑 Web 聊天、飞书 webhook、定时触发器、Heartbeat、Agent 间委派，上下文压缩、工具预算、Prompt 缓存语义完全一致。

Web Chat 是可恢复执行：浏览器 WebSocket 只是订阅后台 `RuntimeTask(task_type="web_chat_turn")`。页面刷新或临时断开后，后台 run 会继续，前端通过 active-run 轮询恢复状态。

## 记忆金字塔

这是让 Hive "不只是一个套了向量库的聊天机器人"的核心。

```
soul.md     ←  Dream         （4 小时 + 3 个会话门槛触发，T3 → soul 凝结）
   ↑
T3 语义记忆  ← Heartbeat      （每 45 分钟一次，T2 → T3 整理）
   ↑                           feedback / knowledge / strategies / blocked / user
T2 学习提取  ← Extract Agent  （每次回复后，T0 → T2 LLM 抽取）
   ↑
T0 原始日志  ← t0_logger      （游标式增量写入，会话空闲/关闭时落盘）
              保留 30 天
```

| 层级 | 存放位置 | 写入者 | 内容 |
|-------|-------|-----------|---------------|
| **T0** | `logs/YYYY-MM-DD/behavior/` | session hooks | 完整对话 MD —— 每条消息、每次工具调用、每个工具结果 |
| **T2** | `memory/learnings/*.md` | 提取 LLM | 原子化学习：事实、偏好、错误、模式 |
| **T3** | `memory/{feedback,knowledge,strategies,blocked,user}.md` | Heartbeat 守护进程 | 经过整理与去重的语义记忆 |
| **soul** | `soul.md` | Dream 守护进程 | 永久身份 —— 角色、语气、边界 |
| **focus** | `focus.md` | Agent 自身 + Heartbeat | 易变的运营优先级 |

**MD 文件即真相**。它们是普通 Markdown，你可以阅读、编辑、版本化，甚至在不同部署间复制。无需重建 embedding，无需迁移向量库。

记忆金字塔只是沉淀路径。真正决定 Agent 如何判断和行动的是 **Memory Control Plane**：

| 层级 | 作用 |
|-------|------|
| Principal stack | 显式区分公司、直接 owner、创建者/当前用户、委派 Agent，而不是把每个 prompt 都当作同等授权。 |
| 隐私与写入安全 | 记忆持久化前先分类；凭证直接拒绝，PII 可脱敏，长期条目必须带 evidence/lifecycle metadata。 |
| 动态激活 | 按当前目标、owner/company 相关性、open loop 压力、retention 分数和敏感度访问权来选择进入上下文的记忆。 |
| 决策轨迹与反馈 | 记录 Agent 为什么行动、询问、拒绝或升级，并把 owner feedback 反连到当时的 decision。 |
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
| API 路由 | 55 | Agents、auth、chat sessions、enterprise、channels、admin、Agent圈/plaza、triggers、office、deep research |
| ORM 模型 | 36 | 租户隔离 SQLAlchemy 模型，包含 runtime tasks、coordination、objectives、identity |
| 业务服务 | 130 | LLM 客户端、trigger/evolution 守护、渠道流、记忆、Office、治理、技能 |
| 工具处理器 | 60+ | filesystem · search · communication · email · feishu · office · memory · deep research · plaza · skills · triggers · hr · mcp |
| Kernel | 1 个无状态引擎 | 默认最多 200 轮工具 · 75% 上下文压缩阈值 · 50KB 单工具结果上限 |
| 数据库迁移 | 58 | Alembic，单 head 不可变约束 |
| 前端页面 | 16 + 25 子区块 | AgentDetail、Agent圈、公司后台工作台/设置、平台后台 |
| 前端 API | 25 个生产类型化领域适配器 | TanStack Query 管服务端状态、Zustand 管 UI 状态 |

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

它们是**Agent 框架** —— 你需要写代码去拼装的库。Hive 是**多 Agent 平台** —— 一个可自部署的产品。如果你想给同事一个 UI 来开 Agent、把它接到飞书、设置定时触发器、查看它的记忆、审批它的高风险动作 —— Hive 提供的就是 Agent 运行时之上的整套产品层。

### `soul.md` 是什么？

它是每个 Agent 工作目录根部的 Markdown 文件，描述**这个 Agent 是谁** —— 角色、主要服务对象、核心产出、操作风格、质量标准、边界、学习方式。和埋在代码里的 system prompt 不同，soul 是一等公民工件：可编辑、可版本化、UI 里直接展示。Dream 守护进程会随着 Agent 的成长更新它。Agent 的身份字面意义上活在文件里。

### 不用飞书 / Lark 行吗？

行。飞书集成最深（24 个办公工具、OAuth SSO、审批卡片）只是因为项目从那儿起步，但每个渠道都是可选的。你可以纯用 Slack、Discord，或者只用内置 web 聊天（`:3008`）跑 Hive。

### 能完全离线运行吗？

可以。把 LLM 提供商指向 vLLM / Ollama / SGLang 或任何 OpenAI 兼容端点即可。记忆管线、hooks、治理、触发器 —— 一切都在本地运行。

### 生产可用吗？

它已经在维护者自己团队的生产环境运行。但 1.0 之前 API 还不稳定 —— 次版本之间会有 schema 迁移（Alembic 处理）。多租户隔离、审计日志、密钥加密、审批流都已就位；当作一个年轻但认真的企业级产品来用。

### 怎么扩展？

按工作量从小到大三层：

1. **Skills（技能）** —— 带 frontmatter 的 Markdown 文件，Agent 按需加载。门槛最低，无需写代码。
2. **MCP Server** —— 通过 UI 导入任意 [Model Context Protocol](https://modelcontextprotocol.io) Server，工具自动注册为动态能力包。
3. **原生工具** —— 在 `backend/app/tools/handlers/` 加一个 handler，注册到 runtime，写一条治理规则。如果你的工具要用新型凭证或自定义流式协议，走这条路。

## 文档

- [`AGENTS.md`](AGENTS.md) —— 给 AI 编程助手的技术参考（命令、不变量、约定）
- [`ENGINEERING.md`](ENGINEERING.md) —— 完整架构：kernel、Prompt 装配、治理、记忆、部署
- [`CLAUDE.md`](CLAUDE.md) —— 给 Claude Code 会话的项目指引

## 致谢

依赖注入式 kernel 架构与四层记忆管线受 Claude Code 会话生命周期及更广义的 agent harness 思潮启发；飞书集成来自连续几个月在 lark-cli 上跑 Agent 的一手痛苦；`soul.md` / `focus.md` 的拆分来自一个观察 —— 当 Agent 的身份每次安装新工具就被改写一次，它会人格混乱。

## 许可证

[Apache License 2.0](LICENSE)
