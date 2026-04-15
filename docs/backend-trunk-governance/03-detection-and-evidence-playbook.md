# 主干审计、检测与证据采集手册

## 1. 目标

这份手册只负责一件事：

> 在改代码之前，先用统一方法把“并存系统、重复写路径、平行执行器、兼容残留”扫出来，并形成可追踪证据。

---

## 2. 审计输出物

建议统一放到：

```text
docs/backend-trunk-governance/evidence/YYYY-MM-DD/
```

每轮至少产出：

- `inventory-execution.txt`
- `inventory-session.txt`
- `inventory-trigger.txt`
- `inventory-collaboration.txt`
- `inventory-legacy.txt`

如果暂时不落文件，至少要把命令和结果整理进修复 PR 描述或工作记录。

---

## 3. 通用扫描命令

### 3.1 执行入口扫描

```bash
cd /Users/rocky243/vc-saas/hiveclaw
rg -n "invoke_agent\\(|AgentInvocationRequest\\(" backend/app
```

用途：

- 找出所有 agent 执行入口
- 确认是否绕开统一执行主干

判定：

- 允许存在多个调用点
- 但不允许出现第二套独立 LLM/tool loop

### 3.2 调度/触发并存扫描

```bash
rg -n "AgentSchedule|start_scheduler|scheduler.py|supervision_reminder|AgentTrigger|trigger_daemon" backend/app
```

用途：

- 找出自主触发域里的新旧系统并存点

### 3.3 会话写点扫描

```bash
rg -n "ChatSession\\(|conversation_id=|source_channel=" backend/app
```

用途：

- 找出所有 session 直接创建点
- 找出所有 conversation_id 直接赋值点

### 3.4 协作语义扫描

```bash
rg -n "send_message_to_agent|delegate_to_agent|runtime_task|\\bTask\\b|collaboration_service" backend/app
```

用途：

- 找出 A2A / delegation / business task 混用点

### 3.5 legacy/compat 残留扫描

```bash
rg -n "deprecated|legacy|backward compat|compat|TODO|FIXME|single source of truth" backend/app
```

用途：

- 盘点所有兼容层与历史残留

---

## 4. 架构测试骨架

必须先建这些测试文件：

```text
backend/tests/architecture/test_execution_trunk.py
backend/tests/architecture/test_trigger_trunk.py
backend/tests/architecture/test_session_trunk.py
backend/tests/architecture/test_collaboration_trunk.py
backend/tests/architecture/test_no_legacy_paths.py
```

### 4.1 `test_execution_trunk.py`

要验证：

- 新执行入口最终使用 `invoke_agent`
- 没有新的私有 agent loop

### 4.2 `test_trigger_trunk.py`

要验证：

- 启动器只拉 `trigger_daemon`
- 不再拉 `scheduler`
- `AgentSchedule` 不再承担主流程语义

### 4.3 `test_session_trunk.py`

要验证：

- 非统一 session service 的模块不得直接创建 `ChatSession`

### 4.4 `test_collaboration_trunk.py`

要验证：

- delegation 只写 `RuntimeTask`
- A2A 不发 delegation hooks

### 4.5 `test_no_legacy_paths.py`

要验证：

- 被宣布删除的旧入口在代码搜索中为 0

---

## 5. 每条主干修复前必须收集的证据

### T5 自主触发

- 所有 `AgentSchedule` 读写点
- 所有 `start_scheduler()` 引用
- 所有 `supervision_reminder` 调用点
- 所有 `AgentTrigger` 写入点

### T4 会话与消息

- 所有 `ChatSession(...)` 创建点
- 所有 `conversation_id=` 赋值点
- 所有 `source_channel="agent"` / `"task"` / `"trigger"` / `"web"` 入口

### T6 协作与委派

- 所有 `RuntimeTask` 调用点
- 所有 `Task` 与协作交叉点
- 所有 delegation hooks 消费方

### T3 Prompt/Memory

- 所有 `build_agent_context` 调用方
- 所有 `build_memory_context` 调用方
- 所有 legacy memory wrapper

### T2 工具运行时

- 所有工具定义源
- 所有直接工具执行源
- 所有 governance 分流

---

## 6. 每轮修复的证据模板

每轮 PR/提交说明至少要回答：

1. 这轮修的是哪条主干？
2. 保留哪套系统？
3. 删除哪套系统？
4. 入口归一化了吗？
5. 写路径归一化了吗？
6. 哪些下游可能受影响？
7. 跑了哪些局部回归？

---

## 7. 最低执行命令集

每轮最少跑这些：

```bash
cd /Users/rocky243/vc-saas/hiveclaw/backend
pytest tests/architecture
ruff check app tests
```

外加该主干自己的局部测试集。

