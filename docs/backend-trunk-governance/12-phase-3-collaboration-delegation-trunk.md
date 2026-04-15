# Phase 3: 协作与委派主干收口

## 1. 本阶段目标

彻底钉死三个语义：

1. `send_message_to_agent` = 同步 A2A 协作
2. `delegate_to_agent` = 异步委派执行
3. `RuntimeTask` = 唯一委派句柄

---

## 2. 当前问题

历史上容易混在一起的几个东西：

- A2A 和 delegation 共用运行时但语义没完全分开
- 业务 `Task` 参与过 runtime delegation
- 协作 API 曾走过旧模型

这类问题的危险在于：

- 表面接口像是两个，底层却还是一团
- 审计/hook/metadata 会错

---

## 3. 唯一保留模型

### 保留

- `send_message_to_agent`
- `delegate_to_agent`
- `RuntimeTask`
- `orchestrator`

### 清退

- 业务 `Task` 充当 runtime 委派句柄
- A2A 冒充 delegation
- 协作 API 继续保留旧语义桥

---

## 4. 执行步骤

### W1 建架构测试

新增：

- `backend/tests/architecture/test_collaboration_trunk.py`

断言：

1. delegation 只写 `RuntimeTask`
2. A2A 的 `interaction_type` 不是 `delegation`
3. 协作 API 不再写业务 `Task`

### W2 盘点所有协作入口

执行：

```bash
rg -n "send_message_to_agent|delegate_to_agent|runtime_task|\\bTask\\b|collaboration_service" backend/app
```

### W3 固化 metadata / hooks 契约

需要定死：

- `interaction_type`
- `delegation_*`
- `agent_message_*`

禁止：

- 同字段兼表示两种语义

### W4 固化 runtime task 边界

必须写进测试：

- 业务 `Task` 用于业务任务板
- `RuntimeTask` 用于委派生命周期

### W5 清理兼容桥

删掉：

- 旧协作 API 中对业务 `Task` 的 runtime 使用
- 误导性文案和旧工具名残留

### W6 局部回归

至少覆盖：

- A2A 一次请求-一次回复
- async delegation 创建/查询/取消
- 审计日志
- hooks 行为

---

## 5. 风险与下游影响

### 对 T4 的影响

- A2A session source/channel 不能漂

### 对分支层的影响

- 前端高级协作页
- Desktop 子代理逻辑

控制：

- Phase 3 结束后先做协作域集成回归，再开放分支修复

---

## 6. 退出条件

1. delegation 只有 `RuntimeTask`
2. A2A 与 delegation hooks/metadata 完全分离
3. 协作 API 全量接入 runtime 主线
4. 局部回归通过

---

## 7. 当前执行入口（2026-04-14）

Phase 3 在本轮的起手动作不是直接改代码，而是先做证据盘点。

### 本轮先确认

1. `send_message_to_agent` 真实写入了哪些模型
2. `delegate_to_agent` 是否仍然把业务 `Task` 当 runtime 委派句柄
3. `RuntimeTask` 的创建、查询、取消是否已经构成唯一主线
4. orchestrator / collaboration API 是否仍保留旧桥接语义

### 本轮预期产物

1. 一份真实入口清单
2. 一组 Phase 3 架构红测试
3. 第一轮收口目标文件列表

---

## 8. 第一轮实际进度（2026-04-14）

### 已确认的旧桥断点

1. `backend/app/services/collaboration.py::send_message_between_agents`
   之前仍走 event bus / file inbox，不在 runtime A2A 主线上。
2. `send_message_to_agent`
   之前只按名字模糊找目标 agent，没有 `target_agent_id` 精准路由。
3. `send_message_to_agent`
   tool schema 仍保留 `task_delegate`，与 `delegate_to_agent` 的唯一委派主干冲突。

### 第一轮已完成

1. 已新增 Phase 3 架构测试：
   - `backend/tests/architecture/test_collaboration_trunk.py`
2. 已新增/补强行为测试：
   - `backend/tests/services/test_collaboration_service.py`
   - `backend/tests/services/test_agent_message_runtime.py`
   - `backend/tests/services/test_prompt_contracts.py`
3. `backend/app/services/collaboration.py`
   已把 message 路径收口到 `_send_message_to_agent`，不再写 event bus / file inbox。
4. `backend/app/services/agent_tool_domains/messaging.py`
   已支持 `target_agent_id` 精准路由。
5. `backend/app/services/agent_tool_domains/messaging.py`
   已明确拒绝 `msg_type="task_delegate"`，强制引导到 `delegate_to_agent`。
6. `backend/app/tools/handlers/communication.py`
   已移除 `send_message_to_agent` 的 `task_delegate` schema 选项。
7. `backend/app/templates/system_skills/delegation-guide/SKILL.md`
   已去掉把 agent message 描述为 fire-and-forget 的旧说法。

### 当前判定

Phase 3 当前状态应视为：

1. delegation 主线仍是 `delegate_to_agent -> delegate_async -> RuntimeTask -> orchestrator`
2. advanced collaboration message 旧桥已并回 `send_message_to_agent`
3. A2A 与 delegation 的主干边界比之前清楚，但 audit/hook/前端表达仍需继续清点

### 第二轮目标（紧接第一轮）

1. `collaboration:*` audit action 是否要继续保留旧表达，还是统一映射到更清晰的 A2A / delegation 事件
2. hook metadata 是否还有字段混用或缺失
3. `advanced API` 返回体与前端调用面是否还保留旧 message bridge 心智

---

## 9. 第二轮实际进度（2026-04-14）

### 已完成

1. `backend/tests/services/test_collaboration_service.py`
   已补 audit 契约红测试，明确要求：
   - delegation 审计动作为 `collaboration:delegation`
   - A2A message 审计动作为 `collaboration:agent_message`
   - `details` 内必须显式带上 `interaction_type` 与 `route`
2. `backend/tests/architecture/test_collaboration_trunk.py`
   已把旧的 `action=f"collaboration:{msg_type}"` 判定为不允许继续存在的分叉语义。
3. `backend/app/services/collaboration.py`
   已完成审计语义收口：
   - `delegate_task()` → `action="collaboration:delegation"`
   - `send_message_between_agents()` → `action="collaboration:agent_message"`
   - 两者都补上与 runtime 一致的 `interaction_type`
   - 两者都显式记录主干路由：`runtime_delegation` / `runtime_agent_message`

### 已核清但暂不改动

1. `backend/app/agents/orchestrator.py`
   当前 `session_metadata` 已明确区分：
   - `interaction_type="delegation"`
   - `interaction_type="agent_message"`
   因此 hook 主线没有继续沿用旧桥接语义。
2. `backend/app/runtime/hooks_setup.py`
   当前只对 `DELEGATION_END` 写 `behavior_type="delegation"`，没有把 A2A message 误写成 delegation。
   这说明 hook 侧边界当前是清晰的，暂时不需要为 Phase 3 再引入新兼容层。
3. `frontend/src`
   当前未发现 `/agents/{id}/collaborate/*` 的直接消费方。
   也就是说 `advanced API` 目前更多是后端暴露面，不是一个正在被前端放大的旧分叉。

### 当前结论

Phase 3 到这里可以确认三层语义已经基本对齐：

1. tool / runtime 层：`send_message_to_agent` vs `delegate_to_agent`
2. orchestration / hook 层：`agent_message` vs `delegation`
3. collaboration service / audit 层：`collaboration:agent_message` vs `collaboration:delegation`

剩余工作不再是“协作桥是不是走错路”，而是继续往更深层主干推进，检查 runtime task、prompt 注入、memory 汇合处是否还有旧体系残留。

---

## 10. 第三轮实际进度（2026-04-15）

### 新发现的深层断点

1. `backend/app/agents/orchestrator.py`
   虽然 `interaction_type` 与 session metadata 已能区分：
   - `delegation`
   - `agent_message`
2. 但 `delegate_to_agent(..., interaction_type="agent_message")`
   之前进入 runtime 时仍会复用 delegation 的两段核心语义：
   - `Delegated Task Brief`
   - `Delegated Worker Mode`

### 风险

1. 表层 A2A 已叫 `agent_message`
   但底层 prompt 仍把它当 delegated worker。
2. 这样会让：
   - 协作语义
   - prompt 语义
   - memory/tool 限制文案
   继续处于“接口已分家、执行提示词仍混用”的半收口状态。
3. 这类问题很隐蔽：
   - 不一定立刻报错
   - 但会让 A2A 回复风格、边界认知、后续 prompt/memory 调优继续受 delegation 旧心智污染

### 本轮已完成

1. 已新增红测试：
   - `backend/tests/agents/test_orchestrator.py`
   - 明确要求 `interaction_type="agent_message"` 时：
     - request message 必须是 `Agent Message Brief`
     - 不能再出现 `Delegated Task Brief`
     - `system_prompt_suffix` 不能再自动拼入 delegated worker prompt
2. `backend/app/agents/orchestrator.py`
   已完成 prompt 分流：
   - delegation 继续使用：
     - `Delegated Task Brief`
     - `Delegated Worker Mode`
   - agent message 改为使用：
     - `Agent Message Brief`
     - 仅保留调用方显式传入的 A2A prompt suffix
3. 这意味着 Phase 3 现在又少了一层“metadata 已区分，但 prompt 仍复用 delegation”的隐形旧桥。

### 针对性验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest backend/tests/agents/test_orchestrator.py -q
```

结果：

- `18 passed`

### 当前判断

Phase 3 现在开始不仅在：

1. API / audit / hook metadata

上完成分流，

也开始在：

2. runtime prompt contract

上真正把 `agent_message` 与 `delegation` 分开。

接下来继续排查的重点应转向：

1. `RuntimeTask` 持久化 metadata 是否还缺少足够明确的主干契约
2. resume / recovery 路径是否仍默认回落到 delegation 心智
3. A2A 与 delegation 在 memory/recall 侧是否还有共享旧假设

### 第三轮继续推进：恢复链契约也已补齐（2026-04-15）

1. 在把 runtime prompt 分流之后继续下钻，发现第二个更隐蔽的断点：
   - `RuntimeTask.metadata_json`
     之前不会持久化 `interaction_type`
   - `resume_persisted_async_delegations()`
     恢复 request 时也默认回落到 `delegation`
2. 风险：
   - 即使前面已经把 live runtime 的 A2A / delegation prompt 分开
   - 一旦经过 restart / resume 链路
   - 交互语义仍可能被静默吃掉，再次回到 delegation prompt
3. 本轮已完成修复：
   - `backend/app/agents/orchestrator.py`
     现在会把 `interaction_type` 一起写入 `RuntimeTask.metadata_json`
   - `resume_persisted_async_delegations()`
     现会按 metadata 中的 `interaction_type` 重建 `AgentDelegationRequest`
4. 已补测试：
   - `backend/tests/agents/test_orchestrator.py`
     新增断言：
     - restart-safe payload 必须持久化 `interaction_type`
     - resume 后若 metadata 标记 `agent_message`
       则恢复出来的 prompt 必须继续是 `Agent Message Brief`
5. 结果：
   - Phase 3 现在不仅 live path 分流了
   - restart / recovery path 也开始遵守同一份交互契约

### 扩展回归结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/agents/test_orchestrator.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  backend/tests/architecture/test_collaboration_trunk.py \
  backend/tests/services/test_prompt_contracts.py \
  -q
```

结果：

- `47 passed`

### 第三轮继续推进：A2A 的 hook / T0 / recall 语义也已开始独立（2026-04-15）

1. 在补完 runtime prompt 与 restart/recovery 契约后继续下钻，又发现一处“上游已分家、下游仍混写”的断点：
   - `runtime/invoker.py`
     之前在发 `SESSION_START / SESSION_CLOSE` hook 时，
     不会完整透传 `session_context.metadata`
   - `runtime/hooks_setup.py`
     在 `SESSION_CLOSE / SESSION_IDLE` 时，也一律把会话写成 `chat`
   - `services/t0_logger.py`
     与 `services/session_recall.py`
     也都还不认识 `agent_message` 这个独立行为类型
2. 风险：
   - A2A 在 runtime 内已经不是 delegation 了
   - 但一到 hook / T0 / recall 下游
   - 又会被重新压回普通 chat
   - 这样会让协作主干在“日志、回忆、后续提炼”层继续保留旧混合心智
3. 本轮已完成修复：
   - `backend/app/runtime/invoker.py`
     现在会把 `session_context.metadata` 一并透传给 `SESSION_START / SESSION_CLOSE`
   - `backend/app/runtime/hooks_setup.py`
     已开始按 `interaction_type` 把 `agent_message` 写成独立 T0 行为类型
   - `backend/app/services/t0_logger.py`
     已新增 `agent_message` formatter
   - `backend/app/services/session_recall.py`
     现会一起检索：
     - `chat-*.md`
     - `agent_message-*.md`
4. 已补测试：
   - `backend/tests/runtime/test_invoker.py`
   - `backend/tests/services/test_t0_logger.py`
   - `backend/tests/services/test_session_recall.py`
   - `backend/tests/test_memory_integration.py`
5. 结果：
   - Phase 3 不仅在 API / prompt / recovery 层完成分流
   - 也开始在 hook / T0 / recall 这些下游观察面上真正分流

### 本轮针对性验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_recall.py \
  backend/tests/test_memory_integration.py \
  -q
```

结果：

- `94 passed`

### 第三轮继续推进：delegation 的 T0 双写也已收掉（2026-04-15）

1. 在把 A2A 的 hook / T0 / recall 语义拆开后继续盘点，又发现 delegation 侧仍有一个残余重复面：
   - `SESSION_CLOSE / SESSION_IDLE`
     之前会把 delegation 子会话按普通 `chat` 落一份 T0
   - `DELEGATION_END`
     又会再落一份 canonical `delegation` T0
2. 风险：
   - 同一段委派执行在 T0 层形成：
     - `chat`
     - `delegation`
     两份观察面
   - 这会让内部委派 transcript 再次混进普通 chat 视图，也会给后续 recall / memory 观察制造歧义
3. 本轮已完成修复：
   - `backend/app/runtime/hooks_setup.py`
     现在对 `interaction_type="delegation"` 的子会话会跳过 `SESSION_CLOSE / SESSION_IDLE` 的普通 T0 写入
   - delegation 只保留：
     - `DELEGATION_END`
     这一条 canonical delegation 观察面
4. 已补测试：
   - `backend/tests/test_memory_integration.py`
     现明确要求 delegation session close 不得再落普通 chat T0
5. 结果：
   - 协作主干现在不仅把 A2A 从 chat 里拆出来
   - 也把 delegation 从 chat/T0 双写里收回到单一观察面

### 本轮针对性验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest backend/tests/test_memory_integration.py -q
```

结果：

- `36 passed`

### 第三轮继续推进：session memory / extractor source 也已保留交互语义（2026-04-15）

1. 在前面把：
   - prompt
   - recovery payload
   - hook / T0 / recall
   三层都拆开以后，继续顺着记忆链往下盘点，又发现一个更隐蔽的回退点：
   - `build_session_memory_payload_from_messages()`
     之前仍优先读取 `metadata["source"]`
   - `_extract_on_response()`
     之前调度 extract 时也仍直接用 `ctx.source`
2. 这会导致一个断层：
   - 上游明明已经显式区分
     - `interaction_type="agent_message"`
     - `interaction_type="delegation"`
   - 但到了 session memory / extract source 这一层，A2A 仍会重新退化成通用 `agent`
3. 风险：
   - `agent_message` 在记忆层再次被吞回 generic source
   - 后续若继续基于 source 做 recall / extraction policy / memory bucket 判断，会重新长出“上层已分流、下层又合流”的隐形旧桥
4. 本轮已完成修复：
   - `backend/app/services/session_memory.py`
     现会优先用 `interaction_type` 生成 `SessionMemoryPayload.source`
   - `backend/app/runtime/hooks_setup.py`
     新增 memory source 归一化逻辑：
     - `RESPONSE_COMPLETE`
     - `PRE_COMPACTION`
     - `SESSION_CLOSE`
     三条 memory 入口都先按 `interaction_type` 取 source
   - 因此：
     - A2A 会保留为 `agent_message`
     - 其他路径才回落到 `ctx.source` / fallback source
5. 已补红绿测试：
   - `backend/tests/services/test_session_memory.py`
     明确要求 `interaction_type` 必须覆盖 generic `source="agent"`
   - `backend/tests/test_memory_integration.py`
     明确要求 `_extract_on_response()` 调度 extraction 时必须带 `source="agent_message"`
6. 结果：
   - Phase 3 现在不仅在 prompt / recovery / T0 层把 A2A 与 delegation 拆开
   - 也在 session memory / extractor source 层封住了再次回退到 generic `agent` 的断点

### 本轮针对性验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_session_memory.py \
  backend/tests/test_memory_integration.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_recall.py \
  backend/tests/agents/test_orchestrator.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  backend/tests/architecture/test_collaboration_trunk.py \
  -q
```

结果：

- `135 passed`

### 第三轮继续推进：T2 source bucket 也已补齐协作语义（2026-04-15）

1. 在把 `agent_message` 修到 session memory / extractor source 后继续下钻，发现 T2 还有最后一层隐形断点：
   - `backend/app/memory/t2_store.py`
     的 `_AUTONOMOUS_SOURCES`
     之前只认识：
     - `delegation`
     - `trigger`
     - `scheduler`
     等 autonomous source
   - 但还不认识：
     - `agent_message`
     - `agent`
2. 这意味着即使上游已经把 A2A source 保住了：
   - `agent_message`
   进入 T2 以后仍会被 `_source_bucket()` 默认落入：
   - `system`
   而不是：
   - `autonomous`
3. 风险：
   - 协作链在 memory weighting 层再次和 delegation 分叉
   - `agent_message -> extract -> append_t2_entries -> compute_t2_weight`
     这条链会得到错误权重
   - 后续 recall / ranking / memory snapshot 会继续带着错误的优先级判断
4. 本轮已完成修复：
   - `backend/app/memory/t2_store.py`
     已把：
     - `agent_message`
     - `agent`
     一起纳入 `_AUTONOMOUS_SOURCES`
5. 已补红绿测试：
   - `backend/tests/memory/test_t2_store.py`
     现明确要求 `compute_t2_weight("feedback", "agent_message") == 0.70`
   - 红阶段实测之前返回的是：
     - `0.85`
     说明它之前确实被误判为 `system`
6. 结果：
   - Phase 3 现在不仅把 A2A 与 delegation 在 runtime / hook / recall / extractor 层对齐
   - 也把它们在 T2 weighting 这一层重新收回到同一套协作主干语义

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/memory/test_t2_store.py \
  backend/tests/services/test_extract_agent.py \
  backend/tests/services/test_session_memory.py \
  backend/tests/test_memory_integration.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_recall.py \
  backend/tests/agents/test_orchestrator.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  backend/tests/architecture/test_collaboration_trunk.py \
  -q
```

结果：

- `190 passed`

### 第三轮继续推进：协作会话也已接入 internal workflow distillation（2026-04-15）

1. 在把 `agent_message` 从 runtime 一路打通到 T2 之后，继续往下游消费端盘点，又发现一处下游仍停留在旧主干假设：
   - `backend/app/services/skill_distiller.py`
     的 `_INTERNAL_SESSION_SOURCES`
     之前只包含：
     - `heartbeat`
     - `trigger`
     - `task`
   - 但没有：
     - `agent`
2. 这意味着：
   - 协作主干现在已经正式通过：
     - `source_channel="agent"`
     存储 agent-to-agent 会话
   - 但 internal workflow distillation 仍把这类会话排除在外
3. 风险：
   - 协作链上的真实内部 workflow 无法进入 skill distillation 主干
   - 系统会形成“协作 runtime 是一套正式主干，但下游 workflow 归纳/蒸馏仍只认识 heartbeat/trigger/task”的认知断层
4. 本轮已完成修复：
   - `backend/app/services/skill_distiller.py`
     已把 `agent` 纳入 `_INTERNAL_SESSION_SOURCES`
5. 已补红绿测试：
   - `backend/tests/services/test_skill_distiller.py`
     现明确要求 internal session sources 必须包含 `agent`
   - 红阶段实测之前集合只有：
     - `heartbeat`
     - `trigger`
     - `task`
6. 结果：
   - 协作主干现在不仅在 runtime / memory / T2 weighting 上是正式主干
   - 也开始被下游 skill distillation 当作正式内部 workflow 来源消费

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_skill_distiller.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  backend/tests/agents/test_orchestrator.py \
  backend/tests/memory/test_t2_store.py \
  backend/tests/test_memory_integration.py \
  -q
```

结果：

- `78 passed`

### 第三轮继续推进：T0 / recall 的 source 字段也已彻底脱离 generic agent（2026-04-15）

1. 在把协作主干一路推到下游 consumer 后继续盘点，又发现 recall 层还有一个剩余断点：
   - `backend/app/services/t0_logger.py`
     虽然已经把 A2A 写成：
     - `type: agent_message`
     但 frontmatter 里的：
     - `source`
     之前仍是：
     - `agent`
   - `backend/app/services/session_recall.py`
     对外返回的命中来源字段又正是这个 `source`
2. 这意味着：
   - 上游明明已经把 A2A 行为类型拆成：
     - `agent_message`
   - 但到了 recall 消费面，命中结果仍会重新显示成：
     - `agent`
3. 风险：
   - `type` 已分流但 `source` 仍合流
   - 新旧消费端若只看 recall result 的 `source`
     仍会把 A2A 和 generic agent source 混为一谈
4. 本轮已完成修复：
   - `backend/app/services/t0_logger.py`
     新写出的 A2A T0 文件现在会明确写：
     - `source: agent_message`
   - `backend/app/services/session_recall.py`
     新增归一化逻辑：
     - 即使旧日志仍是 `type=agent_message + source=agent`
     - recall 对外也会统一返回 `source=agent_message`
5. 已补红绿测试：
   - `backend/tests/services/test_t0_logger.py`
     现明确要求 agent_message T0 文件必须写出 `source: agent_message`
   - `backend/tests/services/test_session_recall.py`
     现明确要求旧 frontmatter 仍写 `source: agent` 的历史 A2A 日志，
     recall 返回时也必须归一化成 `agent_message`
6. 结果：
   - 协作主干现在不仅在 runtime / memory / distillation 层独立
   - 也在 T0 / recall 的最终对外消费字段上彻底脱离了 generic `agent`

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_recall.py \
  backend/tests/test_memory_integration.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  backend/tests/agents/test_orchestrator.py \
  -q
```

结果：

- `126 passed`

### 第三轮继续推进：agent session consumer 的对端映射也已修正（2026-04-15）

1. 在把 A2A 的 source / recall 字段彻底拆开后继续往外围 consumer 看，又发现会话展示层还有一个 agent session contract 断点：
   - `backend/app/api/chat_sessions.py`
     在列出 `source_channel="agent"` 的协作会话时
   - `peer_agent_id / peer_agent_name`
     之前直接取：
     - `session.peer_agent_id`
     - `session.peer_agent_id` 对应的名称
2. 这会导致一个错误：
   - 如果当前请求的是协作会话里的“被消息方”
   - 返回的 `peer_agent_id / peer_agent_name`
     实际上会指向自己，而不是对端 agent
3. 风险：
   - agent session consumer 层的“对端”概念和 canonical agent-pair session 语义错位
   - 前端/管理端如果基于 `peer_agent_*` 做跳转、标记或筛选，会直接拿到错误对象
4. 本轮已完成修复：
   - `backend/app/api/chat_sessions.py`
     现在会相对于当前请求的 `agent_id`
     正确计算协作会话的对端 agent
5. 已补红绿测试：
   - `backend/tests/api/test_chat_sessions_permissions.py`
     新增测试明确要求：
     - 当请求方是会话中的 peer 侧 agent
     - 返回值中的 `peer_agent_id / peer_agent_name`
       必须指向 source 侧 agent
6. 结果：
   - 协作主干现在不仅在 runtime / memory / recall 侧边界清晰
   - 也在 session consumer / 管理视图这一层对齐了“谁是对端”这个核心 contract

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/api/test_chat_sessions_permissions.py \
  backend/tests/api/test_activity_chat_history_sessions.py \
  backend/tests/api/test_messages_permissions.py \
  backend/tests/architecture/test_chat_sessions_channel_contract.py \
  -q
```

结果：

- `14 passed`

### 第三轮继续推进：activity chat history 也已对齐 canonical agent session 语义（2026-04-15）

1. 在修完 `chat_sessions` 的对端映射后继续盘点外围 consumer，又发现：
   - `backend/app/api/activity.py`
     的 `_list_agent_conversations()`
     虽然已经能正确识别对端 agent
   - 但它在读取：
     - message_count
     - last_message
     时，之前仍直接使用当前请求方的 `agent_id`
2. 这和协作会话的真实落盘方式不一致：
   - agent pair session 的消息是按 canonical session agent 写入
   - 也就是：
     - `sess.agent_id`
     而不是 peer 侧当前查看者的 `agent_id`
3. 风险：
   - 当当前请求方位于 peer 侧时
   - `activity` 会错误地按请求方 `agent_id` 去查 `chat_messages`
   - 导致协作会话的：
     - message_count
     - last_message
     直接变成空值或错误值
4. 本轮已完成修复：
   - `backend/app/api/activity.py`
     现在对 agent 协作会话会显式使用：
     - `sess.agent_id`
     作为 canonical message owner 来读取统计和最后消息
5. 已补红绿测试：
   - `backend/tests/api/test_activity_chat_history_sessions.py`
     新增测试明确要求：
     - 当请求方是 peer 侧 agent
     - `_get_session_message_stats()` 与 `_get_last_session_message()`
       必须使用 canonical `session.agent_id`
6. 结果：
   - `chat_sessions`
   - `activity`
   - `messages`
   这三条 agent session consumer 链现在开始共同对齐 canonical agent-pair session contract

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/api/test_activity_chat_history_sessions.py \
  backend/tests/api/test_chat_sessions_permissions.py \
  backend/tests/api/test_messages_permissions.py \
  backend/tests/architecture/test_chat_sessions_channel_contract.py \
  -q
```

结果：

- `15 passed`

### 第三轮继续推进：session recall 的 DB fallback 也已支持 peer 侧 agent session（2026-04-15）

1. 在修完：
   - T0 / recall 的 `agent_message` source 归一化
   - `activity` 的 canonical session message 读取
   之后继续往 recall 深处盘点，又发现 DB fallback 仍残留一层旧假设：
   - `backend/app/services/session_recall.py`
     的 `_search_session_history_db()`
     之前只筛：
     - `ChatSession.agent_id == agent_id`
   - 同时还额外要求：
     - `ChatMessage.agent_id == agent_id`
2. 这会导致两个问题：
   - peer 侧 agent 的协作会话直接不会进 DB fallback
   - 即使 session 已通过 peer 关系命中，只要 message 落在 canonical `session.agent_id` 下，当前请求方是 peer 侧时仍会被过滤掉
3. 风险：
   - live path / T0 path 已经正确
   - 但只要 recall 落到 DB fallback
   - peer 侧协作历史就会再次消失，形成“主路径正确、回退路径断裂”的隐藏断点
4. 本轮已完成修复：
   - `backend/app/services/session_recall.py`
     现在会把：
     - `(ChatSession.agent_id == agent_id) | (ChatSession.peer_agent_id == agent_id)`
     作为 session 过滤条件
   - 同时移除了：
     - `ChatMessage.agent_id == agent_id`
     这层对当前请求方的错误绑定
   - transcript fallback 也同步移除了同样的 message 归属误绑
5. 已补红绿测试：
   - `backend/tests/services/test_session_recall.py`
     新增测试明确要求：
     - DB fallback SQL 必须包含 `chat_sessions.peer_agent_id`
     - 同时不得再强绑 `chat_messages.agent_id`
   - 红阶段已确认旧实现不满足这个 contract
6. 结果：
   - 协作主干现在不仅在：
     - runtime
     - T0
     - consumer
     这些正路径上对齐
   - 也开始把 DB fallback 这条回退路径收回到同一套 canonical agent-pair session 语义里

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_session_recall.py \
  backend/tests/test_memory_integration.py \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_memory.py \
  backend/tests/memory/test_t2_store.py \
  -q
```

结果：

- `88 passed`

### 第四轮继续推进：DB fallback 的 agent channel 排除与 source 漂移也已收口（2026-04-15）

1. 在上一轮把：
   - `peer_agent_id`
   - `ChatMessage.agent_id` 误绑
   这两层修掉之后，继续复盘 `_search_session_history_db()`，又发现还有两个更隐蔽的残留断点：
   - `_EXCLUDED_CHANNELS` 里仍然包含 `agent`
   - DB fallback 返回的 `source` 仍直接暴露原始 `agent`
2. 这说明上一轮虽然把 SQL 的 session/message owner 关系修正了，但 DB fallback 仍然存在两层未完全收口的问题：
   - SQL 层仍会把 `source_channel=\"agent\"` 的协作会话整体排除掉
   - 即使未来某些路径命中 DB fallback，对外返回的 source 语义仍会和已经修好的 T0 recall 分叉成：
     - `agent_message`
     - `agent`
3. 风险：
   - 表面看上去：
     - T0 recall 已正确
     - DB fallback 也已支持 peer 侧 session
   - 但实际上只要落到 DB fallback：
     - agent channel 仍可能被整体挡掉
     - recall consumer 仍会重新看到旧的 generic `agent` source
   - 这会形成一种更隐蔽的“条件已修、标签仍漂、回退口仍半断”的主干假闭环
4. 本轮已完成修复：
   - `backend/app/services/session_recall.py`
     现已把 `_EXCLUDED_CHANNELS` 收紧为真正的内部执行渠道：
     - `heartbeat`
     - `trigger`
     - `task`
     - `dream`
   - `source_channel=\"agent\"`
     不再被 DB fallback 误伤排除
   - 同时 DB fallback 在组装 recall hit 时，现会把协作会话的 source 统一归一化为：
     - `agent_message`
5. 已补红绿测试：
   - `backend/tests/services/test_session_recall.py`
     现进一步明确要求：
     - 编译后的 DB fallback SQL 的 `source_channel NOT IN (...)` 不得包含 `agent`
     - peer 侧协作 session 的 recall hit 必须返回 `source=\"agent_message\"`
   - 红阶段已确认旧实现仍会把 `agent` 放进排除集合中，因此测试真实失败
6. 结果：
   - Phase 3 在 recall 这条链路上，现在不仅修正了：
     - session 归属
     - message owner 归属
   - 也修正了：
     - fallback channel gating
     - fallback source contract
   - 同一条 A2A 协作历史现在无论走：
     - T0 recall
     - DB fallback
     对外都只剩一套 `agent_message` 语义

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_session_recall.py \
  backend/tests/test_memory_integration.py \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_memory.py \
  backend/tests/memory/test_t2_store.py \
  backend/tests/architecture/test_prompt_memory_trunk.py \
  -q
```

结果：

- `89 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/session_recall.py \
  backend/tests/services/test_session_recall.py
```

结果：

- `All checks passed`

### 第九轮继续推进：tool domain 的 OpenClaw A2A 发信入口也已补回 canonical transcript（2026-04-15）

1. 在把：
   - `api/gateway.py::send_message()`
   - `api/gateway.py::report_result()`
   - `api/gateway.py::_send_to_agent_background()`
   这些 gateway / OpenClaw 协作入口收回 canonical transcript 之后，继续顺着同一条主干往下扫，又发现工具域里还残着一条平行旁路：
   - `backend/app/services/agent_tool_domains/messaging.py::_send_message_to_agent()`
   - 当 target agent 是 `openclaw` 时，
     旧实现仍然只做：
     - `GatewayMessage(status="pending")`
   - 不会先写：
     - canonical `ChatSession`
     - outbound `ChatMessage(role="user")`
     - `GatewayMessage.conversation_id`
2. 风险：
   - 这样会形成一条新的“入口层双轨”：
     - gateway API 入口已经有 canonical transcript
     - tool domain / collaboration service 入口却还是 queue-only
   - 同一类 OpenClaw A2A 请求，
     会因为入口不同而出现：
     - 一条链有 transcript
     - 另一条链只有 gateway queue
     的结构偏移
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
   - `_send_message_to_agent()` 在 OpenClaw target 分支里，现也会：
     1. 解析 source agent participant
     2. `find_or_create_agent_pair_session(...)`
     3. 写入 outbound `ChatMessage(role="user")`
     4. 把 `GatewayMessage.conversation_id` 绑定到同一条 canonical session
     5. 回填 `chat_session.last_message_at`
   - 这条路径的 owner user 也已与 gateway OpenClaw 发信主干对齐为：
     - `target.creator_id or source.creator_id`
4. 结果：
   - `gateway API`
   - `messaging tool domain`
   两条 OpenClaw A2A 发信入口现在开始共享：
   - 同一条 canonical `agent_pair_session`
   - 同一份 `ChatMessage` transcript substrate
   - 同一个 `GatewayMessage.conversation_id`
   不再因为入口不同而各写一套 substrate
5. 已补红绿测试：
   - `backend/tests/services/test_agent_message_runtime.py`
     新增断言：
     - OpenClaw target 分支必须写入 canonical outbound `ChatMessage(user)`
     - `GatewayMessage.conversation_id` 必须绑定 canonical session
     - `chat_session.last_message_at` 必须同步更新
   - 红阶段已确认旧实现真实失败：
     - 只创建了 `GatewayMessage`
     - 没有 `ChatMessage`
6. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  -q
```

结果：

- `12 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py
```

结果：

- `All checks passed`

### 扩展回归结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  backend/tests/api/test_gateway_agent_transcript.py \
  backend/tests/agents/test_orchestrator.py \
  -q
```

结果：

- `35 passed, 10 warnings`

### 第十轮继续推进：OpenClaw queue 的 sender/content contract 也已与 gateway 主干对齐（2026-04-15）

1. 在补齐 tool domain 的 canonical transcript 之后继续下钻，又发现同一条 OpenClaw A2A 发信链上还留着一处更细的漂移：
   - `backend/app/services/agent_tool_domains/messaging.py::_send_message_to_agent()`
   - OpenClaw target 分支虽然已经会写：
     - canonical `ChatMessage(role="user")`
     - canonical `GatewayMessage.conversation_id`
   - 但旧实现仍然把 queue payload 写成：
     - `sender_user_id=source_agent.creator_id`
     - `content=f"[From {source_name}] {message_text}"`
2. 风险：
   - `backend/app/api/gateway.py::send_message()`
     的 agent-to-agent queue contract 现在已经是：
     - 只保留 `sender_agent_id`
     - `content` 写原始消息正文
     - 不再把 source owner user 混进 `sender_user_id`
   - 如果 tool domain 入口继续写旧 contract，就会导致：
     - 同样是 OpenClaw A2A queue
     - gateway 入口和 tool domain 入口出现两套 sender 身份解释
     - `/gateway/poll` 与 transcript consumer 侧重复出现 sender drift / content prefix drift
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
   - OpenClaw target 分支现在改为：
     - `GatewayMessage.sender_agent_id = from_agent_id`
     - `GatewayMessage.sender_user_id = None`
     - `GatewayMessage.content = message_text`
     - `GatewayMessage.conversation_id = canonical agent-pair session id`
4. 结果：
   - `api/gateway.py`
   - `agent_tool_domains/messaging.py`
   两条 OpenClaw A2A queue 写入入口现在不仅共享：
   - 同一条 canonical transcript
   - 同一个 `conversation_id`
   也共享：
   - 同一份 sender identity contract
   - 同一份 raw content contract
   不再出现“一条入口写 canonical transcript，但 queue payload 仍带旧 sender/content 心智”的半收口状态
5. 已补红绿测试：
   - `backend/tests/services/test_agent_message_runtime.py`
   - 明确要求 OpenClaw target queue payload 必须满足：
     - `sender_user_id is None`
     - `content == 原始 message_text`
   - 红阶段已确认旧实现真实失败在：
     - `sender_user_id` 仍被写成 source owner
6. 本轮验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py \
  -q
```

结果：

- `12 passed, 10 warnings`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_collaboration_service.py
```

结果：

- `All checks passed`
7. 本轮补扫结果：
   - 已继续盘点 `backend/app` 内其余 `GatewayMessage` 写入点
   - 当前未再发现第二处 agent-to-agent queue 把：
     - `sender_user_id`
     - `[From ...]` 前缀 content
     重新写回 payload 的同类回流


### 第七轮继续推进：OpenClaw-to-OpenClaw 直连旁路也已补回 canonical transcript（2026-04-15）

1. 在把：
   - gateway/native-agent 补偿路径的 `agent_message` metadata
   - call_llm 的 close-hook metadata 透传
   这些语义补齐之后，继续往同一条 OpenClaw 旁路下钻，又发现一个更底层的断点：
   - `backend/app/api/gateway.py::send_message()`
     在 `target_agent.agent_type == "openclaw"` 的直连分支里
     之前只写：
     - `GatewayMessage`
   - `backend/app/api/gateway.py::report_result()`
     在 `msg.sender_agent_id` 回包分支里
     之前也只写：
     - `GatewayMessage`
2. 这意味着虽然：
   - canonical agent-pair session 已经创建
   - queue routing 也已经使用 canonical `conversation_id`
   但真实的协作请求/回复内容却没有回落到 `ChatMessage` transcript
3. 风险：
   - 队列层看起来是通的
   - 但历史会话读取、chat history consumer、后续 recall / consumer 视角里
   - 这条 OpenClaw-to-OpenClaw 旁路仍然会留下“session 存在，但 transcript 为空或半空”的假闭环
4. 本轮已完成修复：
   - `backend/app/api/gateway.py::send_message()`
     在 OpenClaw-to-OpenClaw 直连分支里，现已额外写入：
     - `ChatMessage(role="user")`
     到 canonical `conversation_id`
   - `backend/app/api/gateway.py::report_result()`
     在 agent-to-agent 回包分支里，现已额外写入：
     - `ChatMessage(role="assistant")`
     到同一 canonical `conversation_id`
   - 也就是说：
     - 队列继续用 `GatewayMessage`
     - transcript 主干继续用 `ChatMessage`
     两条面现在重新对齐
5. 已补红绿测试：
   - 新增：
     - `backend/tests/api/test_gateway_agent_transcript.py`
   - 明确要求：
     - OpenClaw-to-OpenClaw 出站请求必须写入 `ChatMessage(user)`
     - `/gateway/report` 回包必须写入 `ChatMessage(assistant)`
     - 两者都必须挂到 canonical `conversation_id`
   - 红阶段已确认旧实现真实失败
6. 结果：
   - Phase 3 现在不仅在：
     - runtime
     - hook
     - recall
     - backfill
     - gateway metadata
     上收口
   - 也把 OpenClaw-to-OpenClaw 旁路的 transcript substrate 补回到了统一主干

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/api/test_gateway_agent_transcript.py \
  backend/tests/api/test_gateway_conversation_contract.py \
  backend/tests/api/test_websocket_call_llm.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/test_memory_integration.py \
  backend/tests/services/test_agent_message_runtime.py \
  -q
```

结果：

- `56 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/api/gateway.py \
  backend/tests/api/test_gateway_agent_transcript.py
```

结果：

- `All checks passed`

### 当前最新执行索引（2026-04-15）

- 第五轮：dream / T0 backfill 链从 `chat-only` 假设收回到 `agent_message`
- 第六轮：gateway / OpenClaw native-agent 补偿路径补齐 `agent_message` metadata 与 close-hook 透传
- 第七轮：OpenClaw-to-OpenClaw 直连旁路补回 canonical `ChatMessage` transcript
- 第八轮：gateway transcript sender identity 与 `participant_id` contract 已与 canonical `chat_sessions/messages` 对齐

### 第八轮继续推进：gateway transcript 的 sender identity / participant contract 也已接回主干（2026-04-15）

1. 在把：
   - gateway/native-agent 补偿路径的 `agent_message` metadata
   - OpenClaw-to-OpenClaw 直连旁路的 canonical transcript
   这些断点补上之后，继续下钻 history consumer，又发现更细的一层断裂：
   - `backend/app/api/gateway.py::send_message()`
     在 OpenClaw-to-OpenClaw 直连分支里虽然已经写入 `ChatMessage(role="user")`
     但没有带：
     - `participant_id`
   - `backend/app/api/gateway.py::report_result()`
     在 agent-to-agent 回包分支里虽然已经写入 `ChatMessage(role="assistant")`
     也没有带：
     - `participant_id`
   - `backend/app/api/gateway.py::_send_to_agent_background()`
     对 OpenClaw-to-native 的 transcript 写入也同样缺少：
     - source/target agent 的 `participant_id`
   - `backend/app/api/gateway.py::poll_messages()`
     在拼 history sender 时仍优先用：
     - `user_id -> User.display_name`
     - `assistant -> 当前 agent.name`
     没有优先走 canonical `Participant.display_name`
2. 这会形成一个很隐蔽但很实质的断层：
   - transcript 表面上已经存在
   - 但 sender identity 仍然不是 canonical participant contract
   - 同一条 agent session 在：
     - `gateway/poll`
     - `chat_sessions`
     - `messages`
     - recall / memory consumer
     之间仍可能出现不同 sender 语义
3. 风险：
   - OpenClaw 侧看到的 history sender 可能偏成 owner user、空值，或只剩当前 agent 名称
   - OpenClaw-to-native 与 native A2A 会话会出现“两套 transcript substrate、一套有 participant、一套没有”的结构偏移
   - 这类偏移会继续污染：
     - session viewer
     - activity / message list
     - memory / recall 的后续消费解释
4. 本轮已完成修复：
   - `backend/app/api/gateway.py`
     新增 `_get_agent_participant_id(...)`，统一从 canonical `Participant(type="agent", ref_id=...)` 解析 agent participant
   - `send_message()`
     在 OpenClaw-to-OpenClaw transcript 写入里，现已补齐：
     - `ChatMessage.participant_id = source_agent_participant_id`
   - `report_result()`
     在 agent-to-agent 回包 transcript 写入里，现已补齐：
     - `ChatMessage.participant_id = current_agent_participant_id`
   - `_send_to_agent_background()`
     在 OpenClaw-to-native transcript 写入的 user / assistant 两侧，现已分别补齐：
     - source agent participant
     - target agent participant
   - `poll_messages()`
     在 history sender 解析时，现会优先使用：
     - `Participant.display_name`
     只有 participant 不存在时才回退到：
     - `User.display_name`
     - `agent.name`
5. 已补红绿测试：
   - `backend/tests/api/test_gateway_agent_transcript.py`
     现进一步明确要求：
     - OpenClaw-to-OpenClaw 出站 transcript 必须带 source `participant_id`
     - `/gateway/report` 的 agent reply transcript 必须带当前 agent `participant_id`
     - OpenClaw-to-native 背景 transcript 的 request/reply 两侧都必须带 canonical participant
     - `/gateway/poll` history sender 必须优先使用 `Participant.display_name`
   - 红阶段已确认旧实现真实失败：
     - transcript 虽存在，但 `participant_id` 为 `None`
     - history sender 仍不是 canonical participant sender
6. 结果：
   - Phase 3 现在不仅补齐了：
     - session
     - transcript
     - metadata
   - 也补齐了协作会话最终对外暴露时的：
     - sender identity contract
   - gateway 协作链与 canonical `chat_sessions/messages` 读取面现在重新只剩一套 participant 语义

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/api/test_gateway_agent_transcript.py \
  backend/tests/api/test_gateway_conversation_contract.py \
  backend/tests/api/test_websocket_call_llm.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_collaboration_trunk.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_session_recall.py \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_session_memory.py \
  backend/tests/memory/test_t2_store.py \
  backend/tests/test_memory_integration.py \
  backend/tests/architecture/test_prompt_memory_trunk.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/architecture/test_legacy_agent_tools_allowlist.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/services/test_agent_tools_executor_dispatch.py \
  backend/tests/tools/test_service.py \
  -q
```

结果：

- `128 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/api/gateway.py \
  backend/tests/api/test_gateway_agent_transcript.py
```

结果：

- `All checks passed`

### 第五轮继续推进：dream / T0 backfill 链也已脱离 chat-only 旧假设（2026-04-15）

1. 在把：
   - live runtime T0
   - recall T0
   - recall DB fallback
   这几层收口之后，继续沿着历史补录链往下查，发现 `auto_dream -> backfill_recent_chat_logs()` 仍残留旧主干假设：
   - 只扫描 `chat-*.md`
   - 只查询 `ChatSession.agent_id == 当前 agent`
   - 只查询 `ChatMessage.agent_id == 当前 agent`
   - 无论 session 实际类型是什么，都统一回填成 `behavior_type="chat"`
2. 这意味着协作主干虽然在 live path 上已经分流完成，但在“历史无 T0 时的补录链”上仍然会重新断开：
   - peer 侧 agent session 可能根本扫不到
   - 即使扫到，也会被压回 `chat-*.md`
   - 已存在的 `agent_message-*.md` 也不会被视作已补录，存在重复回填风险
3. 风险：
   - 表面上：
     - runtime / hook / recall 都已经认识 `agent_message`
   - 但只要进入 dream 期的补录路径：
     - 协作历史仍会重新回退到 chat-only 语义
   - 这会导致“一套系统在 live path，另一套系统在修复/补录 path”的隐形双轨
4. 本轮已完成修复：
   - `backend/app/services/t0_logger.py`
     现在会同时把：
     - `chat-*.md`
     - `agent_message-*.md`
     视作已存在的 T0 substrate
   - backfill session 查询已扩成：
     - `ChatSession.agent_id == agent_id`
     - `ChatSession.peer_agent_id == agent_id`
   - backfill message 查询已移除：
     - `ChatMessage.agent_id == agent_id`
     这层 canonical owner 误绑
   - 对 `source_channel="agent"` 的 session：
     - 现会按 `behavior_type="agent_message"` 回填
     - 写出 `agent_message-*.md`
     - 不再把协作 session 错写成普通 `chat`
5. 已补红绿测试：
   - `backend/tests/services/test_t0_logger.py`
     新增测试明确要求：
     - peer 侧 agent session 也必须能被 backfill 扫到
     - backfill SQL 不得再按 `ChatMessage.agent_id` 过滤消息归属
     - agent session 必须补成 `agent_message-*.md`
     - 已有 `agent_message` 文件必须被视为已补录，不能重复生成第二份
   - 红阶段已确认旧实现真实失败
6. 结果：
   - Phase 3 现在不仅把协作主干在：
     - runtime
     - memory
     - recall
     上收口
   - 也把：
     - dream 触发后的历史补录链
     收回到了同一套 `agent_message` canonical 语义里

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_t0_logger.py \
  backend/tests/services/test_dream_phase6.py \
  backend/tests/test_memory_integration.py \
  backend/tests/services/test_session_recall.py \
  -q
```

结果：

- `98 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/t0_logger.py \
  backend/tests/services/test_t0_logger.py
```

结果：

- `All checks passed`

### 第六轮继续推进：gateway / OpenClaw 补偿路径的 agent_message metadata 也已接回主干（2026-04-15）

1. 在把：
   - runtime A2A
   - recall fallback
   - dream backfill
   这些路径收口之后，继续往 gateway / OpenClaw 补偿链盘点，又发现一处“入口像协作、关会话时却掉回普通 session”的旧断点：
   - `backend/app/api/gateway.py::_send_to_agent_background()`
     之前虽然已经：
     - 走 canonical agent-pair session
     - execution identity label 明确写了 `(agent_message)`
   - 但调用 `call_llm()` 时只传了：
     - `session_source="gateway"`
     - `session_channel="gateway"`
   - 没有显式把：
     - `interaction_type="agent_message"`
     带进 session metadata
2. 继续往下看又发现第二层问题：
   - `backend/app/api/websocket.py::call_llm()`
     在 `auto_close_session=True` 时会自己补发 `SESSION_CLOSE`
   - 但之前只附带：
     - `reason`
     - `channel`
   - 没有继承 `session_context.metadata`
3. 这会导致一个隐形断裂：
   - 即使某条补偿路径以后补传了 `agent_message` metadata
   - 到 `SESSION_CLOSE` / hook / T0 / memory 这一层仍可能被再次丢失
   - 形成“invoke path 知道是协作，close path 又忘了是协作”的半断主干
4. 本轮已完成修复：
   - `backend/app/api/gateway.py`
     现会为 native-agent 补偿路径显式创建：
     - `SessionContext(session_id=..., source="gateway", channel="gateway", metadata={...})`
   - metadata 内已明确带上：
     - `interaction_type="agent_message"`
     - `agent_message=True`
     - `agent_message_parent_agent_id`
     - `agent_message_parent_session_id`
   - `backend/app/api/websocket.py::call_llm()`
     在 `auto_close_session` 补发 `SESSION_CLOSE` 时，现会把：
     - `session_context.metadata`
     一并透传到 close hook metadata
5. 已补红绿测试：
   - `backend/tests/api/test_websocket_call_llm.py`
     新增测试明确要求：
     - `auto_close_session=True` 时
     - `SESSION_CLOSE.metadata` 必须保留 `interaction_type="agent_message"` 等 session metadata
   - `backend/tests/architecture/test_session_message_trunk.py`
     新增契约明确要求：
     - `gateway.py` 必须显式出现 `agent_message` interaction metadata
     - 必须通过 `SessionContext(...)` 传入 runtime
   - 红阶段已确认旧实现真实失败
6. 结果：
   - Phase 3 现在不仅把协作语义收口在：
     - live runtime path
     - recall path
     - backfill path
   - 也把：
     - gateway / OpenClaw 的 native-agent 补偿路径
     - 以及 call_llm 的 close-hook metadata path
     一起拉回到了同一套 `agent_message` 主干

### 本轮补充验证结果

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/api/test_websocket_call_llm.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/api/test_gateway_conversation_contract.py \
  backend/tests/test_memory_integration.py \
  backend/tests/services/test_agent_message_runtime.py \
  -q
```

结果：

- `54 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/api/websocket.py \
  backend/app/api/gateway.py \
  backend/tests/api/test_websocket_call_llm.py \
  backend/tests/architecture/test_session_message_trunk.py
```

结果：

- `All checks passed`
