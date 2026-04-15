# 主干清单与边界定义

## 1. 判定规则

主干必须满足：

1. 多入口共同依赖
2. 决定核心系统语义
3. 修改后会影响多个下游域
4. 双实现并存时一定引入隐性债务

分支必须满足：

1. 建立在主干之上
2. 只服务某个渠道、产品或集成场景
3. 不得定义独立核心语义

---

## 2. 主干总表

| 编号 | 名称 | 目标语义 | 唯一真源 | 当前主要文件 |
|---|---|---|---|---|
| T0 | 基础设施主干 | 配置、租户、安全、模型 | `config + database + core + models` | `config.py`, `database.py`, `core/*`, `models/*` |
| T1 | 统一执行主干 | agent 执行请求与内核 | `invoke_agent()` | `runtime/invoker.py`, `kernel/contracts.py`, `kernel/engine.py` |
| T2 | 工具运行时主干 | 工具元数据、治理、执行 | `ToolRuntimeService + ToolRegistry + surface/execution entry` | `tools/surface.py`, `tools/execution_entry.py`, `tools/*` |
| T3 | Prompt/Memory 主干 | prompt 组装、memory 注入、预算 | `agent_context + memory_service + prompt_sections` | `services/agent_context.py`, `services/memory_service.py`, `memory/*`, `runtime/prompt_*` |
| T4 | 会话与消息主干 | `ChatSession` / `ChatMessage` 生命周期 | 统一 session service/factory | `models/chat_session.py`, `services/channel_session.py`, 多入口写点 |
| T5 | 自主触发主干 | trigger / cron / once / poll / on_message / webhook | `AgentTrigger + trigger_daemon` | `models/trigger.py`, `api/triggers.py`, `services/trigger_daemon.py` |
| T6 | 协作与委派主干 | A2A 同步协作 + async delegation | `send_message_to_agent + delegate_to_agent + RuntimeTask` | `agents/orchestrator.py`, `agent_tool_domains/messaging.py`, `runtime_task_service.py` |

---

## 3. T0 基础设施主干

### 包含

- 配置读取
- DB engine / session
- 租户上下文
- 安全/权限/执行上下文
- 核心 ORM 模型

### 不包含

- 业务流程
- 渠道逻辑
- prompt 组装
- agent 执行 loop

### 边界

允许上层依赖 T0；不允许 T0 反向 import 高层业务。

### 完成标准

- 租户边界只有一套
- 关键实体不再出现重复影子模型
- ORM 关系与运行语义对齐

---

## 4. T1 统一执行主干

### 保留

- `AgentInvocationRequest`
- `invoke_agent`
- `AgentKernel`

### 不允许继续存在的平行实现

- websocket 自己维护 LLM/tool loop
- scheduler 自己维护独立 loop
- agent 协作链路私有 loop
- 渠道适配器私有 loop

### 包含

- request 组装
- kernel 调用
- fallback
- cancel
- tool rounds

### 不包含

- 业务 session 创建
- trigger 判定
- 具体工具治理规则

### 完成标准

- 所有执行入口最终都调用 `invoke_agent`
- 不再新增第二套 agent runtime

---

## 5. T2 工具运行时主干

### 保留

- `ToolRuntimeService.execute`
- `ToolRegistry`
- `governance`
- `handlers/*`

### 包含

- tool schema
- read-only / parallel-safe
- capability / approval / governance
- execution dispatch

### 不包含

- prompt 构造
- trigger 触发
- session 生命周期

### 当前危险边界

- 历史文档与分析仍可能把 `agent_tools.py` 写成现行入口
- 某些工具规则的“口头心智”仍可能滞后于 registry / surface 真源

### 完成标准

- 工具元数据唯一
- 执行唯一
- 治理唯一

---

## 6. T3 Prompt / Context / Memory 主干

### 保留

- `build_agent_context`
- `build_memory_context`
- `prompt_sections/*`
- `context_budget`

### 包含

- frozen prefix
- dynamic suffix
- memory retrieval
- section budgeting

### 不包含

- 业务 API
- 渠道消息持久化
- trigger fire 判定

### 当前危险边界

- 部分参数仍带 deprecated 语义
- memory/context 兼容读法较多

### 完成标准

- prompt 主路径唯一
- canonical memory 主路径唯一
- snapshot 测试稳定

---

## 7. T4 会话与消息主干

### 保留

- `ChatSession`
- `ChatMessage`
- 一个统一 session service/factory

### 必须清退

- 各入口直接 `ChatSession(...)`
- 各入口自己决定 `conversation_id`
- 各入口自己处理 legacy merge

### 包含

- 会话创建
- 会话归并
- conversation_id 真源
- source/channel 归一化

### 不包含

- trigger 何时执行
- agent 回答内容
- prompt/memory 预算

### 完成标准

- 只有一个 session 创建入口
- 所有会话消费者只“取 session”，不“造 session”

---

## 8. T5 自主触发主干

### 保留

- `AgentTrigger`
- `trigger_daemon`

### 必须清退

- `AgentSchedule`
- `api/schedules.py` 的独立存储语义
- `services/scheduler.py`
- `services/supervision_reminder.py` 的独立后台循环

### 包含

- cron
- once
- interval
- poll
- on_message
- webhook

### 不包含

- 具体 prompt 如何执行
- 具体 session 如何持久化

### 特别说明

`heartbeat.py` 可以保留“执行模式/辅助函数”，但不得再作为独立后台循环主系统。

### 完成标准

- 何时触发只有一套系统
- 后台循环只有一套

---

## 9. T6 协作与委派主干

### 保留

- 同步协作：`send_message_to_agent`
- 异步委派：`delegate_to_agent`
- 句柄：`RuntimeTask`
- 执行器：`orchestrator`

### 必须清退

- `Task` 承担 runtime delegation 语义
- A2A 冒充 delegation
- 协作 API 私下写旧模型

### 包含

- A2A 请求/响应
- async delegation
- runtime task lifecycle
- trace / metadata / hooks

### 不包含

- 业务待办板的语义
- trigger fire 判定
- 通用 session factory 逻辑

### 完成标准

- 协作与委派语义完全分离
- `RuntimeTask` 是唯一委派句柄模型

---

## 10. 分支列表

### B1 渠道分支

- Feishu
- Slack
- Discord
- Dingtalk
- WeCom
- Telegram
- WeChat Personal
- Teams
- Email

### B2 产品分支

- Desktop
- Plaza
- Notification
- Admin
- Enterprise

### B3 扩展能力分支

- Packs
- MCP registry/import
- Capability policy
- Feature flags
- Role templates

分支共同要求：

- 只允许调用主干契约
- 不允许定义自己的核心语义
