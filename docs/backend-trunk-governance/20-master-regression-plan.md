# 主干修复完成后的总回归计划

## 1. 为什么必须单独做总回归

因为主干修复的危险不是“单个功能坏了”，而是：

- 上游主干修复让下游整体偏移
- 每条主干单测都过，但连起来后语义不一致

所以总回归不是可选项，而是主干治理完成的必要环节。

---

## 2. 回归分层

### R0 局部主干回归

每条主干修完后立即执行。

### R1 全量主干回归

所有主干修完后执行。

### R2 系统级分支回归

全量主干回归通过后，再验证各分支接入没有断裂。

---

## 3. R1 全量主干回归范围

### 3.1 统一执行

- websocket / chat 入口
- task executor
- trigger 执行
- delegation worker

### 3.2 工具运行时

- registry
- governance
- direct execute
- tool profiles

### 3.3 prompt/memory

- memory injection
- prompt snapshot
- task/coordinator/delegation prompt contracts

### 3.4 session/message

- web session
- channel session
- A2A session
- trigger internal session

### 3.5 自主触发

- cron
- once
- interval
- poll
- webhook
- on_message

### 3.6 协作与委派

- A2A
- async delegation
- runtime task lifecycle

---

## 4. 推荐测试集

这部分不是最终唯一答案，但必须包含类似分组。

```bash
cd /Users/rocky243/vc-saas/hiveclaw/backend

pytest tests/architecture
pytest tests/runtime
pytest tests/agents
pytest tests/services/test_agent_message_runtime.py
pytest tests/services/test_collaboration_service.py
pytest tests/services/test_workspace_sync.py
pytest tests/services/test_relationships_file.py
pytest tests/test_memory_integration.py
pytest tests/api/test_desktop_agents.py
pytest tests/api/test_advanced_handover_api.py
ruff check app tests
```

如果有新增 architecture tests，应一并纳入。

---

## 5. R2 系统级分支回归

在 R1 通过后执行。

至少覆盖：

1. Web chat
2. 一条 agent delegation
3. 两条渠道入口
4. Desktop 子代理链路
5. 一条 MCP/Pack 激活链路

---

## 6. 通过标准

R1 通过标准：

- 架构测试全部通过
- 主干相关功能测试全部通过
- 旧路径搜索结果符合预期

R2 通过标准：

- 关键分支接入主干后可用
- 没有新的平行语义长出来

---

## 7. 失败处理原则

如果总回归失败：

1. 不进入分支修复
2. 回到对应主干继续修
3. 必须更新主干文档和风险台账

