<div align="center">
  <h1>Hive</h1>
  <h3>AI Native 组织中台 —— 面向企业数字员工的 Agent-as-a-Service 控制平面</h3>
  <p><a href="README.md">English</a> | <strong>简体中文</strong></p>
</div>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/react-19-61dafb.svg" alt="React"></a>
  <a href="#"><img src="https://img.shields.io/badge/postgres-15-336791.svg" alt="PostgreSQL"></a>
</div>

<br>

Hive 是一个可自部署的 **AI Native 组织中台**。它给企业提供创建、授权、运行、观测和持续改进 AI 数字员工所需的控制平面。

Hive 不是聊天机器人外壳，也不只是一个 Agent 框架。Hive 把每个 Agent 当作有责任边界的组织成员：它有身份、记忆、工具、技能、工作区、运行时状态、权限、审计记录和进化路径。Hive 最大的价值是组织层面的：让企业可以把 Agent 当作公司的一部分运营，而不是散落在各处的 Prompt 窗口。

## 定位

Hive 可以用两个等价概念理解：

1. **AI Native 组织 SaaS**：面向工作流、记忆、权限和运行节奏都围绕 AI 员工重新组织的企业系统。
2. **Agent-as-a-Service 组织中台**：用于创建和治理数字员工的企业控制台，让 Agent 可以跨工具、渠道、文件、工作流和团队工作。

Hive 的北极星目标很明确：

- 建立具备企业级访问控制的自进化 Agent 基础设施。
- 建立公司级组织中台，让企业安全地规模化运营这些 Agent。

## 核心闭环

所有产品入口最终都会进入同一条 runtime loop：

```text
用户 / 触发器 / 渠道 / Agent
        |
        v
ChatSession + RuntimeTask
        |
        v
上下文组装
  身份 + 公司 + 会话 + 记忆 + 技能 + 工具 + 治理
        |
        v
AgentKernel 模型循环
        |
        v
ToolRuntimeService
  校验 + hook + 权限 + preflight + 执行 + 审计
        |
        v
Transcript / T0 证据 / 交付物 / runtime 状态
        |
        v
Memory、Skill、Workflow、治理反馈闭环
```

这是 Hive 的核心设计选择。Web Chat、渠道消息、触发器、Workflow、Subagent、Agent Team 成员和后台 continuation 不应该各自发明一套执行语义。它们都应该成为持久化的 session/runtime 对象，然后进入同一个 kernel 和同一套受治理工具层。

## Hive 提供什么

### 1. 数字员工

每个 Agent 都有：

- `soul.md` 身份契约。
- 私有 workspace，用于文件和交付物。
- 长期 memory vault。
- 已安装 Skill 和 Skill candidate。
- 工具与能力策略。
- owner、tenant、company 和 channel context。
- 持久 Session、Checkpoint、Branch 和 RuntimeTask。

目标不是保存更长聊天记录，而是让 Agent 成为稳定的组织行动者。

### 2. Session 原生 Runtime

Hive 的 Session 是一等运行时容器：

- `ChatSession` 保存对话表面。
- `RuntimeTask` 保存当前运行句柄。
- WebSocket 只是订阅者；关闭页面不应该杀掉后台运行。
- Checkpoint 是导航锚点；Rewind 和 Branch 是显式动作。
- Branch 创建新的 session lineage，而不是破坏原历史。
- Rewind 将当前 session 投影回选中 checkpoint 的状态。

前端 Session Workbench 要直接表达这些 runtime 状态：active run、工具、权限、压缩、checkpoint、child session、Agent Team 成员、Workflow 和后台任务。

### 3. 受治理的工具调用

所有工具都必须经过 `ToolRuntimeService.execute()`。工具层负责：

- JSON / input 校验。
- pre-tool、post-tool 和 failure hook。
- Session permission profile。
- capability 与 pack policy 检查。
- MCP policy 检查。
- 对外可见或敏感动作的 Action Preflight。
- runtime-owned context 注入。
- timeout、结构化错误、生命周期 frame 和审计记录。

原生工具、MCP 工具、deferred tools、Workflow 工具、Skill 加载工具、Subagent 工具、文件/工作区工具都共享这条治理路径。

### 4. 上下文组装

上下文是分层组装出来的 runtime 产物，不是一个无限膨胀的大 Prompt：

- Frozen prefix：身份、角色、操作契约、`soul.md`、公司信息、组织结构和稳定 prompt sections。
- Dynamic suffix：memory snapshot、memory navigation、检索结果、skill catalog、runtime metadata、权限、active tool groups、available deferred tools、channel/session 状态。
- User turn envelope：当前用户输入、附件、选择的 permission mode 和 session metadata。

这个拆分让身份层可以被 prompt cache，而记忆、技能、工具和运行时状态可以每轮更新。

### 5. Memory 与 Skill 进化

Hive 区分原始证据和已接受行为：

```text
T0 原始 Session 证据
  -> T2 reviewed segment packages
  -> T3 accepted semantic memory
  -> soul.md 与 skill candidates
```

Memory 写入必须经过 Memory Gate 和 Platform Gate。Skill 是渐进式能力胶囊：加载 Skill 只增加指令和参考资料；真正执行仍然走受治理的工具、Workflow、Subagent 或 sandbox runtime。Active Skill 变更通过 candidate package 和验证门晋升，不允许直接自我编辑上线。

### 6. 多 Agent 工作

Hive 支持几层多 Agent 执行：

- `spawn_subagent`：Session 内局部专家 worker，拥有隔离 prompt 和 child-session 状态。
- Agent Team：Session 内 team container；成员通过 `spawn_subagent(team_name + name)` 创建，并可以作为可进入的 Session 查看。
- Dynamic Workflow：结构化 workflow run，leaf 通常是 subagent-style worker，具备 preview、admission、run state 和状态投影。
- A2A-style collaboration：在组织边界允许时，提供跨 Agent 关系和消息协作表面。

这些不是同一个 UI 对象。Agent Team 成员可以进入完整 Session；Dynamic Workflow 默认更适合展示 run / phase / leaf 状态，只有 leaf 明确有 child session 时才进入子 Session。

### 7. 企业治理

Hive 是组织控制平面：

- PostgreSQL 多租户与 RLS。
- Agent owner 与 company context。
- Capability policy 与 pack policy。
- Session 级 permission profile。
- Approval 与 pending-tool frame。
- MCP 导入和执行授权。
- 针对敏感、外部可见、不可逆或跨公司边界动作的 Action Preflight。
- Invocation span 与 transcript event 作为审计证据。
- Company Admin 与 Platform Admin 控制面。

治理约束的是 Agent 能做什么，而不是替代模型思考，也不应该压缩 Agent 的上下文能力。

## 快速开始

```bash
git clone https://github.com/SteamRocket-Labs/hiveclaw.git
cd hiveclaw
bash setup.sh --dev
bash restart.sh
```

打开 http://localhost:3008，注册第一个用户，然后通过 HR / 创建员工流程创建第一个 Agent。

Docker：

```bash
cp .env.example .env
docker compose up -d --build
```

默认本地端口：

| 服务 | 端口 |
|------|------|
| Frontend | 3008 |
| Backend | 8008 |
| PostgreSQL | 5432 |
| Redis | 6379 |

## 开发命令

后端：

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8008 --reload
ruff check app/ --fix && ruff format app/
pytest
alembic upgrade head
```

前端：

```bash
cd frontend
npm run dev
npm run build
npm test
```

完整本地重启：

```bash
bash restart.sh
```

## 架构入口

| 层级 | 主要路径 |
|------|----------|
| API | `backend/app/api/` |
| Runtime 入口 | `backend/app/services/web_chat_runtime.py`, `backend/app/runtime/invoker.py` |
| Kernel | `backend/app/kernel/engine.py` |
| 工具治理 | `backend/app/tools/service.py`, `backend/app/tools/governance.py` |
| 上下文组装 | `backend/app/services/agent_context.py`, `backend/app/runtime/prompt_builder.py` |
| Memory | `backend/app/memory/`, `backend/app/services/memory_service.py` |
| Skill | `backend/app/skills/`, `backend/app/services/agent_tool_domains/workspace.py` |
| Workflow | `backend/app/runtime/workflow_*`, `backend/app/tools/handlers/workflow.py` |
| Agent Team | `backend/app/services/agent_team_runtime_service.py`, `backend/app/api/agent_teams.py` |
| 前端 Session UI | `frontend/src/pages/AgentDetail.tsx`, `frontend/src/pages/agent-detail/` |

完整工程路径见 [`ENGINEERING.md`](ENGINEERING.md)。AI 编程助手的开发规则见 [`AGENTS.md`](AGENTS.md)。

## 技术栈

| 领域 | 技术 |
|------|------|
| 后端 | Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2 |
| 前端 | React 19, TypeScript 5, Vite 6, React Router 7 |
| 状态 | PostgreSQL 15, Redis 7 |
| Runtime | Durable `RuntimeTask`, session transcript, stateless kernel, governed tools |
| 测试 | pytest, Vitest |
| 部署 | Docker Compose, Railway |
| 模型 | Anthropic, OpenAI, Gemini, DeepSeek, Qwen, MiniMax, Azure, OpenRouter, Zhipu, Kimi, vLLM, Ollama, SGLang, OpenAI-compatible endpoints |

## License

Apache 2.0。
