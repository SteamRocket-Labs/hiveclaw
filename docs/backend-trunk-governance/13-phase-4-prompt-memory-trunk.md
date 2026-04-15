# Phase 4: Prompt / Context / Memory 主干收口

## 1. 本阶段目标

把 prompt 与 memory 的注入路径收口为一条可解释主线：

- `agent_context` 负责静态身份与目录
- `memory_service` / `memory/*` 负责检索与注入
- `prompt_sections/*` 负责 section 化输出
- `context_budget` 负责预算

---

## 2. 当前问题

风险不是“会报错”，而是：

- 注入重复
- 注入顺序不清
- deprecated 参数继续误导调用方
- 行为漂移难以察觉

---

## 3. 保留与清退

### 保留

- `build_agent_context`
- `build_memory_context`
- `prompt_sections/*`
- `context_budget`

### 清退

- 重复 memory 注入点
- 仅为兼容存在但仍被活跃调用的旧 wrapper
- 旧 prompt builder 别名的活跃新调用

---

## 4. 执行步骤

### W1 建快照测试

新增建议：

- `backend/tests/runtime/test_prompt_snapshot.py`
- `backend/tests/runtime/test_memory_injection_contract.py`

要覆盖：

- conversation mode
- task mode
- coordinator mode
- heartbeat/trigger mode
- delegation worker mode

### W2 盘点注入点

执行：

```bash
rg -n "build_agent_context|build_memory_context|prompt_sections|memory_context|memory_messages" backend/app
```

### W3 冻结职责

写进代码与测试：

- `agent_context` 不直接再注 canonical memory
- `memory_service` 不再承担身份目录拼接
- `prompt_sections` 不再偷偷读业务状态

### W4 收口 deprecated 参数

特别关注：

- `include_memory_file`
- `include_focus`

处理方式：

- 保留兼容一轮可以
- 但不允许新增调用方继续依赖

### W5 局部回归

至少跑：

- prompt snapshot
- memory integration
- context budget
- task/coordinator prompt contracts

---

## 5. 风险与下游影响

### 对 T5 的影响

- trigger/heartbeat 的 prompt 会变化

### 对 T6 的影响

- delegation worker prompt 会变化

控制：

- snapshot 对比
- targeted behavior regression

---

## 6. 退出条件

1. prompt 注入主路径唯一
2. memory 注入主路径唯一
3. deprecated 参数不再被新代码依赖
4. snapshot 与局部回归通过

---

## 7. 第一轮真实盘点（2026-04-14）

### 已确认的生产主干

1. `backend/app/runtime/invoker.py`
   是当前 prompt / memory 主干唯一生产入口协调层：
   - `_build_system_prompt()`
   - `_resolve_memory_context()`
   - `_resolve_retrieval_context()`
2. `backend/app/kernel/engine.py`
   负责真正把三块内容装进最终 system prompt：
   - `prompt_prefix`
   - `memory_snapshot`
   - `retrieval_context`
3. `backend/app/runtime/prompt_builder.py`
   当前生产主干实际使用的是：
   - `build_frozen_prompt_prefix()`
   - `build_dynamic_prompt_suffix()`
   - `assemble_runtime_prompt()`

### 已确认的边界

1. `build_agent_context`
   当前生产调用方只有 `runtime/invoker.py`。
2. `build_agent_runtime_context`
   当前生产调用方只有 `runtime/invoker.py`。
3. `build_memory_snapshot`
   当前生产调用方只有 `runtime/invoker.py`。
4. `build_runtime_prompt`
   当前没有任何生产调用方，仅剩 tests / evals 兼容价值。
5. `memory_service.on_conversation_start`
   盘点后确认没有任何生产调用方，现已删除。
6. `memory_service.on_conversation_end`
   盘点后确认也没有任何生产调用方，现已删除。

### 本轮新增护栏

1. 已新增：
   - `backend/tests/architecture/test_prompt_memory_trunk.py`
2. 该测试当前固定了以下约束：
   - prompt/memory 主干入口不允许从 `invoker` 外重新长出新调用面
   - `build_runtime_prompt()` 不允许被重新带回生产路径
   - `on_conversation_start()` 不允许重新出现新的生产调用
   - `on_conversation_end()` 不允许重新出现新的生产调用

### 当前判断

Phase 4 第一轮先不急着改实现，先确认“主干入口到底是不是唯一”。
目前答案是：生产入口已经基本单线化，真正需要继续处理的是：

1. legacy wrapper 是否要直接删除
2. `request.memory_context` 与 canonical snapshot 的关系是否仍有重复注入风险
3. retrieval section 里混入 runtime hints / memory recall / external knowledge 的边界是否还需要进一步拆清

### 已落地清理

1. `backend/app/services/memory_service.py`
   已删除未被生产使用的 `on_conversation_start()` wrapper。
2. `backend/app/services/memory_service.py`
   已删除未被生产使用的 `on_conversation_end()` wrapper。
3. `backend/app/runtime/prompt_builder.py`
   已修正注释，明确 `build_runtime_prompt()` 不是生产主干入口，避免继续误导后续修复判断。

### 第二轮起点

下一刀聚焦：

1. `request.memory_context`
   当前仍会在 `runtime/invoker.py::_resolve_memory_context()` 中与 canonical `build_memory_snapshot()` 结果直接拼接。
2. 这条兼容注入口的风险不是“会报错”，而是：
   - 旧调用方继续绕开 canonical memory 主干
   - 同一轮 prompt 同时混入 snapshot + manual memory，形成双注入
   - 后续很难判断 memory 漂移到底来自 retriever 还是来自调用侧塞入的额外块
3. 第二轮目标：
   - 先确认真实生产调用面
   - 再决定是直接删除、仅允许 websocket 兼容，还是改成显式的 compatibility metadata

---

## 8. 第二轮实际进度（2026-04-14）

### 已确认的真实调用面

1. `backend/app/api/websocket.py::call_llm()`
   原本仍保留 `memory_context` 参数并透传给 `AgentInvocationRequest`。
2. 但盘点 `gateway / trigger_daemon / feishu / websocket 主流程` 后确认：
   当前生产调用方没有任何一个主动传入非空 `memory_context`。
3. 也就是说它已经不是“还在被用的能力”，而是“仍挂在主链路上的兼容口”。

### 已完成清理

1. `backend/app/api/websocket.py`
   已删除 `call_llm()` 的 `memory_context` 参数与透传。
2. `backend/app/runtime/invoker.py`
   已删除 `AgentInvocationRequest.memory_context`
   并且 `_resolve_memory_context()` 不再把 manual memory block 与 canonical snapshot 直接拼接。
3. `backend/app/kernel/contracts.py`
   已删除 `InvocationRequest.memory_context`，避免 compatibility 字段继续穿透 kernel contract。

### 已新增护栏

1. `backend/tests/architecture/test_prompt_memory_trunk.py`
   现在额外要求：
   - `invoker.py` 不允许继续出现 `if request.memory_context`
   - `websocket.py` 不允许继续把 `memory_context` 透传进 runtime
   - `kernel/contracts.py` 不允许继续保留这个字段
2. `backend/tests/api/test_websocket_call_llm.py`
   已改为验证 `call_llm()` 生成的 request 不再暴露 `memory_context`
3. `backend/tests/runtime/test_invoker.py`
   已改为验证 runtime contract 不再暴露 manual memory 字段，system prompt 只吃 canonical snapshot

### 当前判断

这一轮之后，Prompt / Memory 主干进一步清晰：

1. frozen prefix 来自 `build_agent_context`
2. canonical memory snapshot 来自 `build_memory_snapshot`
3. retrieval context 来自 `build_agent_runtime_context + build_memory_context + fetch_relevant_knowledge`

剩下最值得继续收口的，不再是 manual memory，而是 retrieval 区块内部三类内容的边界是否还需要更明确拆分。

---

## 9. 第三轮观察点（2026-04-15）

### 新发现的主干漂移

1. `backend/app/services/session_recall.py`
   已经把 internal session 排除集合收口为：
   - `agent`
   - `heartbeat`
   - `trigger`
   - `task`
   - `dream`
2. 但 `backend/app/runtime/context_budget.py`
   的 cheap-route 禁用集合仍是：
   - `task`
   - `schedule`
   - `heartbeat`
   - `agent`
3. 也就是说，Phase 1 已经把 runtime 的内部触发主干从旧 `schedule` 收口到 `trigger`，
   但 model routing 侧仍残留旧枚举。

### 风险判断

这不是注释级问题，而是行为级问题：

1. `trigger` 会话可能被误判为允许 cheap-route
2. 低成本 fallback model 可能进入原本应保持主模型的自主触发链
3. 结果会表现为：
   - trigger 执行质量漂移
   - 上下游都“看起来正常”，但只有 runtime routing 还在吃旧语义

### 下一步修复目标

1. 先补 `backend/tests/runtime/test_context_budget.py` 红测
2. 固定：
   - `trigger` session source 不允许触发 cheap-route
   - `schedule` 旧枚举不应继续作为主干语义存在
3. 再改 `runtime/context_budget.py`，把内部 session source contract 与 Phase 1/Phase 2 当前真实主干对齐

### 本轮已完成（2026-04-15）

1. 已新增红测并确认问题真实存在：
   - `backend/tests/runtime/test_context_budget.py`
   - `backend/tests/architecture/test_prompt_memory_trunk.py`
2. 红测证明：
   - `session_source="trigger"` 时，cheap-route 之前确实会错误降到 fallback model
   - `runtime/context_budget.py` 源码里仍显式保留旧 `"schedule"` 枚举
3. `backend/app/runtime/context_budget.py`
   已完成修复：
   - `_NO_CHEAP_ROUTE_SESSION_SOURCES` 从 `{task, schedule, heartbeat, agent}`
     收口为 `{task, trigger, heartbeat, agent}`
   - `resolve_turn_model_route()` 的注释与真实主干语义重新一致
4. 这意味着：
   - Trigger 执行链重新固定在主模型路由上
   - Phase 1 的 `schedule -> trigger` 主干迁移，现已补齐到 Prompt / Routing 层
5. 本轮相关回归已通过：
   - `backend/tests/runtime/test_context_budget.py`
   - `backend/tests/architecture/test_prompt_memory_trunk.py`
   - 扩展回归：`96 passed`

### 第四轮补缝（2026-04-15）

1. 在修完 `context_budget` 后继续盘点，发现还有一处更深的 source contract 漂移：
   - `trigger_daemon.py` 调用 `call_llm(...)` 时未显式传 `session_source / session_channel`
   - `call_llm()` 会默认补 `web / web`
2. 这意味着：
   - 即使 cheap-route 禁用集合已经修到 `trigger`
   - 真正的 trigger runtime 之前仍可能因为 source 伪装成 `web` 而绕开那条护栏
3. 本轮已完成修复：
   - `backend/app/services/trigger_daemon.py`
     已显式透传：
     - `session_source="trigger"`
     - `session_channel="trigger"`
4. 已新增护栏：
   - `backend/tests/api/test_websocket_call_llm.py`
   - `backend/tests/architecture/test_session_message_trunk.py`
5. 结果：
   - Phase 2 的 session identity contract
   - Phase 4 的 runtime routing contract
   现在在 trigger 执行链上已经重新接成一条完整主干

### 第二轮追加收口

这一项现在也已向前推进一段：

1. `backend/app/runtime/invoker.py::_resolve_retrieval_context()`
   不再返回单纯的 `runtime + memory + knowledge` 裸拼接文本；
   现在会显式输出：
   - `## Runtime Context`
   - `## Relevant Memory Recall`
   - `## Knowledge`
2. `backend/app/runtime/prompt_builder.py::build_dynamic_prompt_suffix()`
   已支持识别“已经 section 化的 retrieval block”，不会再把它整体二次包进一个新的 `Knowledge` 外壳。
3. 结果是：
   - runtime hints 不再伪装成 knowledge
   - memory recall 不再伪装成 knowledge
   - external knowledge 仍保留 knowledge 区块提示语义

### 第二轮追加收口：memory source naming contract 对齐（2026-04-14）

1. `backend/app/memory/t2_store.py`
   里的 `_HUMAN_SOURCES` 已补入 `microsoft_teams`
2. 这次修复的原因不是“多一个渠道枚举”这么简单，而是：
   - 上游 session/channel contract 已统一成 `microsoft_teams`
   - 若 T2 memory 仍只认旧的 `teams`
   - 同一条 Teams 人类对话就会在记忆层被错误归到非 human bucket
3. 当前修复后：
   - `microsoft_teams` 与 `web / feishu / slack / wecom / dingtalk` 一样
   - 会按 human source 权重进入 T2
4. 已补测试：
   - `backend/tests/memory/test_t2_store.py`

### 第二轮追加收口：session recall internal-channel 边界补齐（2026-04-14）

1. `backend/app/services/session_recall.py`
   的 `_EXCLUDED_CHANNELS` 已补入 `task`
2. 这次修复的意义：
   - `trigger / heartbeat / dream / task` 这类内部执行 session
   - 不会再被 session recall 当成“可回忆的人类跨会话聊天记录”
3. 已补护栏：
   - `backend/tests/architecture/test_prompt_memory_trunk.py`
   - `backend/tests/services/test_session_recall.py`

### 第二轮再追加一刀

1. `backend/app/runtime/prompt_builder.py::build_runtime_prompt()`
   现已从生产模块删除。
2. 删除依据：
   - 全仓真实调用面只剩测试
   - 它保留的是“旧式整包 prompt builder”心智，会模糊当前真实主干
3. 当前测试面已改为直接验证：
   - `build_frozen_prompt_prefix()`
   - `build_dynamic_prompt_suffix()`
   - `assemble_runtime_prompt()`
4. 这意味着 Prompt/Memory 主干在代码表达上也与真实运行路径一致了，不再保留一个“看起来像主干、其实不是主干”的旧入口。

### 当前剩余尾巴

当前最明确的下一项是：

1. `backend/app/services/agent_context.py::build_agent_context()`
   仍保留：
   - `include_memory_file`
   - `include_focus`
2. 虽然这两个参数已经没有真实业务意义，但它们还挂在主干函数签名上，并且 `runtime/invoker.py` 仍在显式传 `False`。
3. 这类“永远只能传 False 的 deprecated 参数”会继续误导后续维护者，以为 agent context 还承担 memory/focus 注入职责。

### 这一项现在也已完成

1. `backend/app/services/agent_context.py`
   已删除：
   - `include_memory_file`
   - `include_focus`
2. `backend/app/runtime/invoker.py`
   已不再显式传这两个永远为 `False` 的兼容参数。
3. 当前含义已经更直接：
   - `agent_context` 只负责静态身份/目录/上下文材料
   - memory/focus 不再通过任何“看起来还能打开”的旧参数进入这条主干

### 第二轮最后一段清理

1. `backend/app/runtime/prompt_builder.py::build_frozen_prompt_prefix()`
   已删除无实际语义的 `memory_snapshot` 参数。
2. `backend/app/runtime/prompt_builder.py`
   已进一步删除两个仅为旧测试/旧导入存在的薄 wrapper：
   - `_compute_system_prompt_budget()`
   - `_render_active_packs()`
3. 当前 `prompt_builder.py` 剩下的就是主干真实构件：
   - `build_frozen_prompt_prefix()`
   - `build_dynamic_prompt_suffix()`
   - `assemble_runtime_prompt()`

### 当前阶段判断

到这里，Phase 4 的主干已经基本满足退出条件：

1. prompt 注入主路径唯一
2. memory 注入主路径唯一
3. 主干函数签名里的假开关、假参数、假入口已基本清空
4. prompt/memory 层的表达已经和真实运行路径基本一致

### 第三轮 persistence 收尾（2026-04-14）

1. `backend/app/memory/store.py`
   已删除 legacy `memory.json` 双写路径：
   - `replace_semantic_facts()` 不再回写 `memory.json`
   - legacy json 现在只保留“只读导入”职责，不再是活跃 persistence surface
2. `backend/app/api/memory.py`
   已不再直接读取 `memory.json`
   - agent memory API 改为通过 `PersistentMemoryStore.load_semantic_facts()` 读取当前主干数据
3. `backend/tests/architecture/test_prompt_memory_trunk.py`
   已新增护栏：
   - `api/memory.py` 不允许再直接依赖 `memory.json`
   - `memory/store.py` 不允许重新出现 `_write_legacy_json`
4. 当前含义已经更清楚：
   - prompt/runtime memory 读取面不再直接碰 legacy json 文件
   - `memory.json` 退化为历史导入介质，不再和当前主干并行运行

### 第四轮 fallback 收口（2026-04-14）

1. `backend/app/services/memory_service.py`
   已删除对 `FileBackedMemoryStore` 的生产依赖。
2. 当前 `build_memory_context()` 在 retrieval pipeline 异常时，
   会直接回到 canonical fallback：
   - `_load_session_summary()`
   - `_load_previous_session_summary()`
   - `_load_agent_memory()`
3. 这意味着：
   - 检索失败时仍保留当前 summary + agent memory 的兜底行为
   - 但不再把旧 file-backed wrapper 挂在生产主干上
4. `backend/tests/architecture/test_prompt_memory_trunk.py`
   已新增护栏：
   - `memory_service.py` 不允许重新出现 `FileBackedMemoryStore`
5. `backend/tests/services/test_memory_service.py`
   已新增行为测试：
   - retrieval 失败时必须走内部 canonical fallback
   - 不允许再实例化 legacy store
6. 这一刀的意义不是“功能变化”，而是进一步清除隐藏并行链路：
   - 运行时 memory 主干只剩 retrieval pipeline + canonical fallback
   - 旧 `FileBackedMemoryStore` 退到兼容/测试边界，不再属于生产主链路

---

## 9. 阶段收口与交接（2026-04-14）

### 退出条件复核

当前按本阶段最初定义复核：

1. prompt 注入主路径唯一：已满足
2. memory 注入主路径唯一：已满足
3. deprecated 参数不再被主干依赖：已满足
4. snapshot / runtime / integration 局部回归：已满足

### 当前保留结论

1. Phase 4 现在不再以“继续删 prompt/memory 旧接口”为主任务。
2. 后续只保留两类动作：
   - 跟随下游主干（尤其是工具运行时主干）做联动回归
   - 如果后续又出现新的 manual prompt/memory 注入口，再由架构测试拦下

### 与下一主干的关系

Phase 4 之所以现在可以交接给 Phase 5，是因为：

1. Prompt 主干已经明确，tool schema / tool execution 的变化不会再和旧 prompt wrapper 混在一起。
2. 接下来要查的是工具运行时有没有第二套执行器、第二套 metadata 来源、第二套 governance 旁路。
3. 如果 Phase 5 收口时改动了 tool profile、tool schema、tool result shape，仍需要回头补跑本阶段回归，确认 prompt 侧没有被新的 tool runtime 结构带偏。
