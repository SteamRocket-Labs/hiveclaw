# Session / Workspace / HR 原子化闭环落地证据（2026-07-10）

## 1. 结论

本轮针对两个线上暴露问题完成了一次完整收口：

1. **文件已生成但无法交付**：代码执行生成的文件现在会进入结构化 artifact 证据通道，绑定当前 turn 的写入清单，并由 `ChatArtifact` 作为唯一交付记录供聊天和 Workspace 消费。
2. **HR Preview → Create 依赖模型复述长蓝图**：Preview 现在保存服务端 canonical draft；登录用户在 UI 对精确 version/hash 做确认；Create 只提交 `blueprint_id`，幂等身份由服务端 draft 派生，不再要求模型重写蓝图或生成重试 key。

同时完成了与这两条链路直接相关的 UI 信息分层、System HR Personal KB owner 绑定、空模型输出恢复、直接创建旁路封堵和 Company KB 未实现边界校正。

本文件是本轮新建的落地证据，不修改历史报告。

## 2. 原子化状态总表

| 能力 | 状态 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 |
|---|---|---|---|---|---|---|---|---|
| 代码执行文件交付 | 闭环 | `execute_code` / `run_command` 的 workspace 变更 | Agent、session、runtime task 与安全相对路径共同绑定 | code execution provider → `ToolContentEnvelope.artifacts` → kernel | tool result、完整 current-turn write manifest、`ChatArtifact`、内容快照 | DB 幂等约束；tool message 到 final message 只做 canonical row rebind；断线后由持久 run 恢复 | 聊天交付物与 Workspace 右栏共同读取 | 后端单测、全量回归、Workbench E2E |
| HR canonical 创建状态机 | 闭环 | System HR session 中的 Preview；登录用户确认 | tenant RLS + HR agent + requester user + session + exact version/hash | Preview 持久化 → API 确认 → Create 读取 canonical draft | `hr_creation_drafts`、确认审计、provisioning 状态、T0 创建事件 | lease、服务端幂等身份、completed replay、failed retry、已有 Agent 恢复 | HR 决策卡轮询并展示确认、创建、失败状态 | service/API/migration/tool/template/frontend tests + PostgreSQL 集成测试 |
| HR Personal KB principal 绑定 | 闭环 | System HR 发起 Personal KB tool call | 当前已认证 requester，而不是 System HR 的历史 creator | `search_personal_kb` / `read_personal_kb` / proposal 统一 owner 解析 | tenant-scoped query 与 tool result | 无 owner 时 fail closed | HR 仅把 Personal KB 当建议证据 | Personal Knowledge tool regression tests |
| HR 创建入口治理 | 闭环 | 普通用户创建意图 | 普通用户只能走 System HR；组织/平台管理员保留控制中台入口 | 标准用户直调 `/agents/` 返回 403 | HTTP 状态与审计路径 | 用户回到 HR session 重新发起 | Sidebar / Create 页面都新建 HR session | API governance test + frontend tests |
| Workspace 信息层级 | 闭环 | 当前 session 的 artifact 与 runtime item | 用户信息面与技术证据面分离 | 右栏上部 Deliverables，下部 Run status；技术详情显式打开 overlay | typed item 仍保留完整 schema/evidence | overlay 可关闭；原始 details 默认折叠 | 普通用户默认只看交付物和状态 | Vitest、Playwright desktop/narrow snapshots |
| 模型空终态恢复 | 局部闭环 | 模型无正文但已有 tool outcome | kernel 是唯一终态生成点 | 生成可行动的 `[LLM Error]`，不暴露 raw payload | collected tool parts 与 transcript 保留 | 用户可重试；完整错误留在技术详情 | 聊天不再显示无意义的 `[LLM returned empty content]` | kernel regression test |

“模型空终态恢复”标为局部闭环，是因为本轮保证了用户可恢复和证据不丢，但 provider 级自动续写策略仍属于后续全局 runtime 审计范围，不能由本轮局部修复伪装成完整闭环。

## 3. 文件交付链路

### 3.1 输入

- `backend/app/services/agent_tool_domains/code_exec.py`
  - 执行前后对 agent workspace 做文件指纹比较。
  - 忽略执行临时目录、Skill 工具目录、Git 与 Hive 内部目录。
  - 将新增/更新文件输出为 `ToolContentEnvelope.artifacts`。

### 3.2 权威与执行

- 所有执行仍通过既有 code execution provider，不引入 raw subprocess 旁路。
- `backend/app/kernel/engine.py` 从 tool envelope 读取 artifacts，并通过统一 `tool_session_write_paths()` 进入 session write evidence。
- `backend/app/services/chat_artifact_delivery.py` 只接受 `workspace/...` 安全相对路径，继续执行路径穿越、内部目录、文件存在性与快照检查。

### 3.3 证据与恢复

- `backend/app/runtime/session.py` 不再把当前 turn 写入证据截断为 10 个文件；该清单在下一 turn 开始时统一清空。
- tool result 首次持久化即可创建 `ChatArtifact`，因此模型尚未写 final text 时 Workspace 也能看到文件。
- final assistant 选中同一文件作为交付物时，只把同一 canonical row rebind 到 final message，不创建第二事实源。
- 内容快照、content hash、runtime task、session、agent 与 source 信息继续保留。

### 3.4 消费

- `backend/app/services/web_chat_runtime.py` 同时消费显式文件工具写入和 code-exec artifact manifest。
- Frontend 的 Workspace 右栏将这些记录放在 “会话交付物”，运行状态放在其下方。
- raw schema、UUID、typed data 和 evidence refs 默认不露出，只能通过每条运行项的技术详情按钮打开。

## 4. HR canonical 创建链路

### 4.1 输入与权威

- `preview_agent_blueprint` 只能由 `__system_hr__` 在当前登录用户自己的 chat session 中调用。
- Preview 结果由 `backend/app/services/hr_creation_service.py` 持久化为 `HrCreationDraft`。
- 确认 API 同时绑定 tenant、System HR、requester 和 draft；只有最初 requester 能确认或拒绝。
- 用户确认精确 `blueprint_version` 与 `blueprint_hash`；聊天中的“确认”文本不具备授权效力。

### 4.2 唯一执行入口

- `create_digital_employee` 的 schema 只接受 `blueprint_id`。
- 创建参数从数据库 canonical `blueprint_json` 读取；模型不能在 Create 阶段改写字段。
- 服务端以 `hr-draft:<blueprint_id>` 派生幂等身份，消除了模型生成或记住 idempotency key 的依赖。
- 普通用户对旧的直接 `/agents/` 创建入口被拒绝；组织/平台管理员入口作为控制中台能力保留。

### 4.3 证据与恢复

- 新表 `hr_creation_drafts` 记录 version/hash、确认人、确认时间、claim lease、attempt、created agent、provisioning、failure 和过期信息。
- RLS 被启用并强制执行；bootstrap strict table 清单同步更新。
- 重复调用已完成 draft 返回既有 Agent；未过期并发 claim 返回 `creation_in_progress`；过期 lease 或失败状态可继续同一 draft。
- Agent 核心记录落库后，draft 记录唯一 `created_agent_id`。若后续可选能力安装中断，重试会恢复既有 Agent，而不是重复创建。
- T0 evidence 失败不会删除已创建 Agent，但会在 provisioning 和 warnings 中留下机械证据。

### 4.4 消费与 UI

- `frontend/src/pages/agent-detail/HrBlueprintPreviewCard.tsx` 展示 mission、用户、输出、边界、访问范围、风险、知识债务、缺失 gate、安装与人工步骤。
- 卡片不展示 blueprint ID、hash 或 raw JSON。
- “确认并创建”先调用登录用户确认 API，再让 HR Agent 用唯一 blueprint 引用继续创建。
- 页面会轮询 confirmed / creating / provisioning 状态；聊天发送失败时仍可从已确认状态继续。
- Sidebar 的“新建数字员工”是动作按钮，每次创建新的 HR session，不再把 System HR 伪装成普通员工树节点。

## 5. Knowledge 边界

- Personal KB 仍然是 tool 调用，不进入原始上下文组装。
- System HR 使用 Personal KB 时，owner 是本次调用的登录用户，而不是共享系统 Agent 的创建者。
- Company KB 当前仍是**已知缺失**；本轮没有伪造实现。
- 旧输入 `supported_by_company_kb` 只做兼容降级，转为 `unknown_or_needs_company_source` 并产生 warning。
- HR 模板明确禁止把 Personal KB、Memory、通用知识、上传文件或模型推断包装成公司政策。

## 6. UI 信息分层

用户默认可见：

- 对话正文、可行动错误、审批卡和 HR 决策卡；
- 右栏顶部的最终/会话交付物；
- 右栏底部的运行状态、Workflow、Sub-agent / Team 等过程状态；
- Sidebar 的 Home、Agent、session 和“新建数字员工”。

仅按需可见：

- thread item schema、ID、correlation、typed data、evidence refs；
- 通过每条 item 的 `{}` 技术详情按钮打开 overlay；
- JSON details 默认折叠。

公司后台保留：

- 组织级 Agent 直接创建、资产登记、RLS、审计、权限与治理配置；
- 这些能力不进入普通 session 的默认信息面。

## 7. 验收证据

当前 checkout 上的最终结果：

```text
cd backend && source .venv/bin/activate && ruff check app tests
All checks passed!

cd backend && source .venv/bin/activate && pytest tests -q
6036 passed, 1 skipped, 5 warnings in 121.83s

cd backend && source .venv/bin/activate && alembic heads
hr_creation_drafts_0710 (head)

cd frontend && npm test -- --run
92 test files passed; 556 tests passed

cd frontend && npm run build
7061 modules transformed; build exit 0

cd frontend && npm run test:e2e -- e2e/thread-workbench.spec.ts --project=chromium
3 passed
```

## 8. 明确未声称完成的部分

- Company Knowledge Base 仍未建设，是已知缺失，不计作本轮回归。
- 本轮未执行生产部署；完成门是代码、迁移、测试、UI 回归与 Git 提交。
- 全项目其余体验断点由本实现提交之后的新一轮“Agent 实际使用体验原子化扫描”单独给出，避免把局部修复报告冒充全局结论。
