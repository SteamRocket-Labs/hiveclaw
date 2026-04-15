# Phase 5: 工具运行时主干收口

## 1. 本阶段目标

让工具系统只剩一套解释与执行机制：

- `ToolRegistry` 是元数据真源
- `ToolRuntimeService` 是执行真源
- `governance` 是审批/敏感动作真源

---

## 2. 当前问题

当前已经有统一框架，但仍要警惕：

- 上层桥接逻辑过厚
- 工具规则重复定义
- fallback 分支过多
- handler 与上层心智不完全一致

---

## 3. 保留与清退

### 保留

- `tools/registry.py`
- `tools/service.py`
- `tools/runtime.py`
- `tools/governance.py`
- `tools/handlers/*`

### 逐步削薄

- `services/agent_tools.py` 中的大量桥接与回退逻辑

### 清退

- 平行工具元数据定义
- 绕过 runtime service 的直接工具执行

---

## 4. 执行步骤

### W1 建架构测试

新增建议：

- `backend/tests/architecture/test_tool_runtime_trunk.py`

断言：

1. 工具定义通过 registry 输出
2. 敏感/只读/并行安全属性有唯一来源
3. 业务层不新增绕过 `ToolRuntimeService` 的执行路径

### W2 盘点工具元数据来源

执行：

```bash
rg -n "@tool\\(|display_name=|parallel_safe|read_only|sensitive|ToolRuntimeService|ToolRegistry" backend/app
```

### W3 盘点执行来源

执行：

```bash
rg -n "execute_tool\\(|execute_direct\\(|try_execute\\(" backend/app
```

### W4 收口 fallback

要求：

- fallback 只做兼容桥
- 不允许 fallback 继续长成第二执行器

### W5 局部回归

至少覆盖：

- 工具 schema 输出
- 并行安全判断
- governance 拦截
- approval path
- direct execution path

---

## 5. 风险与下游影响

### 对 T6 的影响

- delegation / A2A 的 tool profile 依赖工具系统

### 对渠道分支的影响

- 发送消息/发送文件等 handler 不能漂

控制：

- 保留行为契约测试
- 分域跑通信 handler 回归

---

## 6. 退出条件

1. 工具元数据唯一
2. 工具执行唯一
3. 治理唯一
4. fallback 不再承担主逻辑
5. 局部回归通过

---

## 7. 第一轮真实盘点（2026-04-14）

### 已确认的主干结构

1. `backend/app/services/agent_tools.py`
   当前仍是工具主干的总桥接层：
   - `get_combined_openai_tools()` 负责输出经 `ToolRegistry` 标准化后的 collected tool surface
   - `get_agent_tools_for_llm()` 负责按 agent / pack / provider 可用性裁剪工具集合
   - `execute_tool()` / `_execute_tool_direct()` 负责把执行请求交给 runtime service
2. `backend/app/tools/registry.py`
   当前已经是 schema 输出与只读/并行安全元数据的标准化入口：
   - `ToolRegistry.from_openai_tools(...)`
   - `to_openai_tools()`
   - `is_read_only_tool()`
   - `is_parallel_safe_tool()`
3. `backend/app/tools/service.py`
   当前已经是受 governance 控制的执行总入口：
   - `ToolRuntimeService.execute()`
   - `ToolRuntimeService.execute_direct()`
   - `ToolRuntimeService.execute_with_context()`
4. `backend/app/tools/runtime.py`
   提供第一类 executor registry：
   - `ToolExecutionRegistry`
   - `ToolExecutionRequest`
   - `ToolExecutionContext`

### 已确认的好消息

1. `get_agent_tools_for_llm()`
   无论走 DB 还是 fallback，都已经统一通过：

   - `ToolRegistry.from_openai_tools(...)`
   - `registry.to_openai_tools()`

   这意味着 tool schema sanitization 与 `read_only / parallel_safe` 元数据并没有在上游散成多份。
2. `execute_tool()`
   当前并不是自己调 handler，而是直接委托：

   - `ToolRuntimeService.execute()`

3. `_execute_tool_direct()`
   当前也已经统一委托：

   - `ToolRuntimeService.execute_direct()`

4. `runtime/invoker.py`
   默认工具调用已经统一回到 `services.agent_tools.execute_tool()`；
   即使存在 `request.tool_executor` 注入缝，也只是上层策略包装，不是新的底层执行器。
5. `heartbeat.py` 与 `agent_tool_domains/messaging.py`
   当前的自定义 `tool_executor` 都还是包装后再回调 `execute_tool()`，没有直接绕过 runtime service。

### 当前真实风险

1. `services/agent_tools.py` 仍然过厚。
   它虽然已经把真正执行下放给 `ToolRuntimeService`，但仍同时承担：
   - tool surface 组织
   - channel / Feishu / HR 工具裁剪
   - DB / fallback 选择
   - direct execution 入口
   - 大量 domain re-export
2. `tools/handlers/*`
   之前大量 handler 仍通过 `agent_tools` 下划线 re-export 间接调用 domain 函数，导致：
   - handler -> agent_tools -> domain 的桥接链过长
   - tests / monkeypatch 容易继续钉在旧桥上
3. 这意味着 Phase 5 在“执行面第二套系统”之外，还存在“handler bridge 过厚”的结构债务。

### 第一轮结论

当前可以先下一个判断：

1. 工具元数据主干已经比预期更清晰，问题不在 schema 真源缺失。
2. 工具执行主干虽然已经有 `ToolRuntimeService`，但当时 `agent_tools.py` 内的 direct fallback 仍然太厚，存在“第二执行器”风险。
3. Phase 5 第一轮应先补架构测试，再把“哪些地方允许调用 `execute_tool`、哪些地方不允许再长 direct bypass”钉死。

### 第一轮已落地收口（2026-04-14）

1. 已新增：
   - `backend/tests/architecture/test_tool_runtime_trunk.py`
2. 这轮测试已经固定：
   - `execute_tool` 生产调用点只能留在当前主干集合
   - `get_combined_openai_tools()` / `get_agent_tools_for_llm()` 都必须经过 `ToolRegistry`
   - `heartbeat / A2A messaging` 这类特殊 executor 只能包装 `execute_tool()`
   - `direct_fallback_executor` 不允许继续手写第一类工具分发
3. `backend/app/services/agent_tools.py`
   已完成两项收口：
   - `direct_fallback_executor` 已削薄为仅兜未知工具 / MCP passthrough
   - `get_combined_openai_tools()` 已切到 `ToolRegistry.from_openai_tools(...).to_openai_tools()`
4. 额外修正：
   - `backend/tests/services/test_tool_registry.py`
     已把 `send_channel_message` 对齐回真实 core tool contract，消除“生产主干已依赖、测试基线仍排除”的漂移
   - `backend/app/tools/handlers/mcp.py`
     已给：
     - `list_mcp_resources`
     - `read_mcp_resource`
     补上 `read_only=True`，让 collector 可以直接产出这两个 MCP 资源工具的只读元数据

### 当前判断

到这里，Phase 5 已完成第一轮真正的主干收口：

1. raw collected schema 不再直接泄漏为生产 tool surface
2. approved direct path 不再维持一套平行第一类工具执行器
3. 工具执行调用点已经有了架构级护栏
4. `list_mcp_resources / read_mcp_resource` 的只读属性已从静态 registry 托底回收到 decorator metadata

---

## 8. 第二轮继续收口（2026-04-14）

### 已完成的元数据主干收口

1. `backend/app/tools/registry.py`
   已删除：
   - `_STATIC_READ_ONLY_TOOL_NAMES`
   - `_STATIC_PARALLEL_SAFE_TOOL_NAMES`
2. 当前：
   - `READ_ONLY_TOOL_NAMES`
   - `PARALLEL_SAFE_TOOL_NAMES`
   都只由 `collector` 延迟解析得到，不再是“静态表 + collector”双来源拼接。
3. `ToolRegistry.from_openai_tools(...)`
   现已改为：
   - 优先吃 `category_overrides`
   - 否则吃 collector 的 seed metadata
   - 仅对未知工具才退回 `infer_category(name)`
4. 已验证：
   - collector seed category 与 `ToolRegistry` 中的 builtin tool category 失配数已从 `75` 降到 `0`

### 已完成的 handler bridge 收口

1. 以下 handler 已直接回连 domain，不再通过 `agent_tools` re-export：
   - `communication.py`（除 `send_channel_message / send_channel_file`）
   - `filesystem.py`
   - `plaza.py`
   - `triggers.py`
   - `email.py`
   - `feishu.py`
2. 对应测试也已同步切到真实 domain 路径，不再 monkeypatch 旧桥：
   - `backend/tests/services/test_feishu_handler_runtime.py`
3. 当前 `backend/app/tools/handlers/*` 中剩余的 `agent_tools` 依赖只剩两个：
   - `send_channel_message`
   - `send_channel_file`

### 第二轮新增护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在额外固定：
   - `registry.py` 不允许重新长出静态 `read_only / parallel_safe` 集合
   - category 不允许回到 `category=infer_category(name)` 这种单点静态猜测
   - `communication / filesystem / plaza / triggers / email / feishu` handlers 不允许重新依赖 `agent_tools` 的 domain re-export

### 当前判断

到这里，Phase 5 的主干已经继续前进了一大段：

1. tool metadata 的 `read_only / parallel_safe / category` 三个核心字段都已基本回到主数据驱动
2. 大部分 handler 已不再经由 `agent_tools` 做中间桥接
3. `agent_tools.py` 的剩余结构债务，已明显收缩为：
   - tool surface 裁剪与 DB/fallback 组合逻辑
   - `send_channel_message / send_channel_file` 这两个 channel delivery bridge

### 下一步

下一刀优先看剩余的 channel delivery bridge，而不是再回头改 metadata：

1. 判断 `send_channel_message / send_channel_file` 是否应该下沉到独立 domain
2. 如果下沉，就要补一组 channel delivery contract tests
3. 如果不下沉，就要明确把它们标注为 handler 层唯一允许保留的 `agent_tools` 桥接点

### 下一步

下一刀优先看元数据层，而不是再去动 handler：

1. 继续核查 `registry.py` 的静态 `read_only / parallel_safe` 集合
2. 当前真实集合对比已确认：
   - static read-only 独有值：`[]`
   - 说明 `read_only` 判定已经没有“只有静态表才知道”的工具
3. 下一步重点转向：
   - 评估是否可以删除静态 `read_only` 集合本身
   - 再判断 `parallel_safe` 是否也能进一步收敛
4. 在动这一步之前，先补一组针对 metadata 来源唯一性的测试，避免误删后影响 provider/tool catalog 行为

---

## 9. 第三轮继续收口（2026-04-14）

### 已完成的 channel delivery bridge 下沉

1. 新增：
   - `backend/app/services/agent_tool_domains/channel_delivery.py`
2. 以下内容已从 `backend/app/services/agent_tools.py` 迁出：
   - `channel_file_sender`
   - `channel_web_agent_id`
   - `channel_feishu_sender_open_id`
   - `_send_channel_file(...)`
   - `_send_channel_message(...)`
3. `backend/app/tools/handlers/communication.py`
   现已直接从：
   - `agent_tool_domains/channel_delivery`
   导入：
   - `_send_channel_message`
   - `_send_channel_file`

### 已完成的 import 面收口

以下调用侧已统一切到新 domain，不再从 `agent_tools` 取 channel context：

1. `backend/app/api/feishu.py`
2. `backend/app/api/telegram.py`
3. `backend/app/api/slack.py`
4. `backend/app/api/teams.py`
5. `backend/app/services/wechat_personal_stream.py`
6. `backend/app/services/agent_tool_domains/feishu_docs.py`
7. `backend/app/services/agent_tool_domains/feishu_calendar.py`
8. `backend/tests/api/test_telegram_channel.py`

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在进一步固定：
   - `agent_tools.py` 不允许再定义 channel delivery context var
   - `agent_tools.py` 不允许再定义 `_send_channel_file / _send_channel_message`
   - `communication.py` 不允许再从 `agent_tools` 导入这两个发送函数
   - `channel_delivery.py` 必须承载这组 channel delivery 符号

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `backend/app/services/agent_tool_domains/channel_delivery.py` 不存在

Green + 回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `89 passed`

静态检查：

```bash
ruff check backend/app/services/agent_tool_domains/channel_delivery.py \
  backend/app/services/agent_tools.py \
  backend/app/tools/handlers/communication.py \
  backend/app/services/agent_tool_domains/feishu_docs.py \
  backend/app/services/agent_tool_domains/feishu_calendar.py \
  backend/app/services/wechat_personal_stream.py \
  backend/app/api/feishu.py \
  backend/app/api/slack.py \
  backend/app/api/teams.py \
  backend/app/api/telegram.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/architecture/test_tool_runtime_trunk.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的 channel delivery 剩余桥已经被拿掉：

1. handler -> `agent_tools` 的最后一段 channel bridge 已消失
2. channel context 与 channel reply/send file contract 已有独立 domain
3. `agent_tools.py` 的角色进一步收缩为：
   - tool surface 组装
   - runtime service 入口
   - 少量 legacy re-export（待继续清理）

### 下一步

下一刀不再回头修 channel delivery，而是继续压缩 `agent_tools.py` 的“工具集合组装器”职责：

1. 盘点 `get_agent_tools_for_llm()` 的 DB / fallback / pack / provider 分叉面
2. 判断这些分叉哪些属于 canonical tool surface selection，哪些属于历史兼容
3. 先补架构测试，再继续把 `agent_tools.py` 从“厚 orchestrator”削成更薄的 runtime entry + selection facade

---

## 10. 第四轮继续收口（2026-04-14）

### 已完成的 tool surface 主干迁移

1. 新增：
   - `backend/app/tools/surface.py`
2. 该模块现承接：
   - `get_collected_tools()`
   - `get_combined_openai_tools()`
   - `get_agent_tools_for_llm()`
   - provider availability filtering
   - Feishu access filtering
   - DB / fallback / pack / provider 组合裁剪
3. `backend/app/services/agent_tools.py`
   当前已收缩为：
   - `ToolRuntimeService` facade
   - `execute_tool / _execute_tool_direct / _execute_tool_inner`
   - 薄包装的 tool surface facade
   - 少量 legacy helper facade / domain re-export

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在额外固定：
   - `app.tools.surface` 必须承载：
     - `get_combined_openai_tools`
     - `get_agent_tools_for_llm`
     - `_provider_available_tools`
   - `agent_tools.py` 不允许再承载：
     - provider availability 实现
     - unavailable tool filtering 实现
     - `_get_always_core_tools / _get_feishu_tools / _get_hr_tools`
   - `agent_tools.py` 上保留的 `_filter_feishu_tools_for_access / _agent_has_feishu*`
     只能是 facade，必须直接委托给 `app.tools.surface`

### 测试调整

1. `backend/tests/services/test_agent_tools.py`
   已改成：
   - selection 行为测试直接命中新主干 `app.tools.surface`
   - `agent_tools.py` 只保留 facade delegation tests
2. `backend/tests/tools/test_hr_handler.py`
   已把 `_get_hr_tools` 的断言切到 `app.tools.surface`

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `backend/app/tools/surface.py` 不存在

Green + 回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `164 passed`

静态检查：

```bash
ruff check backend/app/tools/surface.py \
  backend/app/services/agent_tools.py \
  backend/app/services/agent_tool_domains/channel_delivery.py \
  backend/app/tools/handlers/communication.py \
  backend/app/services/agent_tool_domains/feishu_docs.py \
  backend/app/services/agent_tool_domains/feishu_calendar.py \
  backend/app/services/wechat_personal_stream.py \
  backend/app/api/feishu.py \
  backend/app/api/slack.py \
  backend/app/api/teams.py \
  backend/app/api/telegram.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/api/test_telegram_channel.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的主干已经进一步清晰：

1. tool metadata 真源：`ToolRegistry`
2. tool surface 组装真源：`app.tools.surface`
3. tool execution 真源：`ToolRuntimeService`
4. channel delivery 真源：`agent_tool_domains/channel_delivery`
5. `agent_tools.py` 已不再同时承担 runtime + selection + channel bridge 三种职责

### 下一步

下一刀应转向 `agent_tools.py` 底部的大量 legacy re-export：

1. 盘点哪些 re-export 仍有真实生产 import 面
2. 先补架构测试，明确哪些可以立即下沉/删除
3. 逐步把 `handler -> agent_tools -> domain` 的残余兼容面压到最小

---

## 11. 第五轮继续收口（2026-04-14）

### 已完成的 legacy re-export 清退

1. `backend/app/services/agent_tools.py`
   已删除整段：
   - `# ─── Domain module re-exports ───`
2. 该文件不再 re-export：
   - workspace domain functions
   - messaging domain functions
   - Feishu office domain functions
   - web/mcp domain functions
3. `_get_tool_runtime_service()` 内对 `_execute_mcp_tool(...)` 的调用
   已改为函数内直接从：
   - `agent_tool_domains.web_mcp`
   取实现，不再依赖模块底部 re-export。

### 测试与调用面同步

以下测试已切到真实 domain，而不是继续命中 `agent_tools` 兼容面：

1. `backend/tests/services/test_agent_message_runtime.py`
2. `backend/tests/services/test_skill_loading.py`
3. `backend/tests/services/test_agent_tool_domains.py`
4. `backend/tests/services/test_tool_error_envelopes.py`
5. `backend/tests/services/test_agent_tools.py`

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在进一步固定：
   - `agent_tools.py` 不允许再出现 `# Domain module re-exports`
   - `agent_tools.py` 不允许再有：
     - `from app.services.agent_tool_domains.workspace import (...)`
     - `from app.services.agent_tool_domains.messaging import (...)`
     - `from app.services.agent_tool_domains.web_mcp import (...)`

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `agent_tools.py` 仍保留 `# Domain module re-exports` 段

Green + 回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `185 passed`

静态检查：

```bash
ruff check backend/app/services/agent_tools.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的主干已经又干净了一层：

1. `agent_tools.py` 不再同时承担 runtime facade 与 domain compatibility surface
2. production 侧允许保留的 underscore import 已极少：
   - `_execute_tool_direct`
   - `_agent_has_feishu`
   - `_agent_has_feishu_office_access`
   - `_agent_has_feishu_cli_access`
3. 旧的 `handler/api -> agent_tools -> domain` 多跳兼容心智已基本被拿掉

### 下一步

下一刀应开始判断 `agent_tools.py` 上剩余 facade 的最终归宿：

1. `approval_service` 依赖的 `_execute_tool_direct` 是否需要更清晰的 runtime facade 边界
2. `api/tools.py` 依赖的 Feishu availability facade 是否应迁到 `app.tools.surface` 或新的 capability query facade
3. 在不打断当前 API 契约的前提下，继续缩小 `agent_tools.py` 的对外责任面

---

## 12. 第六轮继续收口（2026-04-14）

### 已完成的 availability facade 收口

1. `backend/app/api/tools.py`
   已不再从：
   - `app.services.agent_tools`
   获取：
   - `_agent_has_feishu`
   - `_agent_has_feishu_office_access`
   - `_agent_has_feishu_cli_access`
2. 当前已直接改为从：
   - `app.tools.surface`
   读取这组三个能力判定函数。

### 已完成的 agent_tools 再收缩

1. `backend/app/services/agent_tools.py`
   已删除：
   - `_filter_feishu_tools_for_access`
   - `_agent_has_feishu`
   - `_agent_has_feishu_office_access`
   - `_agent_has_feishu_cli_access`
2. 到这里，这个文件已经不再承担 availability query facade。

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在进一步固定：
   - `agent_tools.py` 不允许再保留上述 Feishu availability/filter facade
   - `api/tools.py` 不允许再从 `agent_tools` 导入这组三个函数
   - `api/tools.py` 必须直接从 `app.tools.surface` 导入它们

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `agent_tools.py` 仍保留 Feishu availability/filter facade

Green + 回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `185 passed`

静态检查：

```bash
ruff check backend/app/services/agent_tools.py \
  backend/app/api/tools.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/api/test_tools_api_surface.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的主干边界已经更薄更清楚：

1. metadata 真源：`ToolRegistry`
2. tool surface 真源：`app.tools.surface`
3. execution 真源：`ToolRuntimeService`
4. channel delivery 真源：`agent_tool_domains/channel_delivery`
5. `agent_tools.py` 现在主要只剩 runtime entry / execution facade，而不是各种工具查询与兼容桥的聚合地

### 下一步

下一刀应转向最后一块仍挂在 `agent_tools.py` 上的高价值入口：

1. `approval_service -> _execute_tool_direct`
2. 判断这是否应进入更明确的 runtime facade（例如 `app.tools.execution_entry` 一类）
3. 在不破坏审批后执行链的前提下，把 `agent_tools.py` 再向“薄 facade”收一层

---

## 13. 第七轮继续收口（2026-04-14）

### 已完成的 runtime entry 主干迁移

1. 新增：
   - `backend/app/tools/execution_entry.py`
2. 该模块现承接：
   - `execute_tool_direct(...)`
   - `execute_tool(...)`
   - `execute_tool_inner(...)`
   - execution registry 初始化
   - runtime service 装配
   - MCP fallback 兜底

### agent_tools 再次收缩

1. `backend/app/services/agent_tools.py`
   已不再持有：
   - `_TOOL_EXECUTION_REGISTRY`
   - `_TOOL_RUNTIME_SERVICE`
   - `_ensure_tool_execution_registry()`
   - `_get_tool_runtime_service()`
2. 当前只保留：
   - tool surface facade
   - runtime entry facade

### 生产调用面调整

1. `backend/app/services/approval_service.py`
   已不再从：
   - `app.services.agent_tools`
   导入：
   - `_execute_tool_direct`
2. 当前已直接改为从：
   - `app.tools.execution_entry`
   调用：
   - `execute_tool_direct(...)`

### 测试同步

以下测试已切到真实 runtime entry：

1. `backend/tests/services/test_agent_tools.py`
2. `backend/tests/services/test_agent_tools_executor_dispatch.py`

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在进一步固定：
   - `app.tools.execution_entry` 必须承载 runtime entry 与 execution registry
   - `agent_tools.py` 不允许再持有 execution registry / runtime service 全局状态
   - `agent_tools.py` 上的 `execute_tool*` 只能是 facade

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `app.tools.execution_entry` 不存在

Green + 回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_tools_executor_dispatch.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `187 passed`

静态检查：

```bash
ruff check backend/app/tools/execution_entry.py \
  backend/app/services/agent_tools.py \
  backend/app/services/approval_service.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_tools_executor_dispatch.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的主干已经非常接近最终形态：

1. metadata：`ToolRegistry`
2. tool surface：`app.tools.surface`
3. execution entry：`app.tools.execution_entry`
4. runtime execution：`ToolRuntimeService`
5. channel delivery：`agent_tool_domains/channel_delivery`
6. `agent_tools.py` 已基本退化为兼容 facade，而不是承载真实主逻辑的聚合中心

### 下一步

下一刀要回答一个很具体的问题：

1. `runtime/invoker.py / heartbeat.py / messaging.py` 是否还应继续通过 `agent_tools.execute_tool`
2. 还是应该逐步切到 `app.tools.execution_entry.execute_tool`
3. 在这一步之前，先补架构测试，避免把 facade 调用点切散成第二轮漂移

---

## 14. 第八轮继续收口（2026-04-14）

### 已完成的 execution facade 调用面收口

1. 以下生产调用点已不再通过：
   - `app.services.agent_tools.execute_tool`
2. 当前已直接改为从：
   - `app.tools.execution_entry`
   导入：
   - `execute_tool(...)`
3. 已完成迁移的调用点：
   - `backend/app/runtime/invoker.py`
   - `backend/app/services/heartbeat.py`
   - `backend/app/services/agent_tool_domains/messaging.py`

### 本轮暴露并修复的测试漂移

1. `backend/tests/services/test_agent_message_runtime.py`
   初次回归失败，原因不是生产链断，而是测试仍在 monkeypatch：
   - `app.services.agent_tools.execute_tool`
2. 真实调用点在 `messaging.py` 内已经变成模块级绑定：
   - `from app.tools.execution_entry import execute_tool`
3. 因此测试已改为 patch：
   - `app.services.agent_tool_domains.messaging.execute_tool`
4. 这次失败本身很有价值，它证明：
   - production trunk 已切换
   - tests 仍可能把旧 facade 当成真源
   - 后续每次迁移都必须同步追踪 monkeypatch / import binding 的真实落点

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在进一步固定：
   - `runtime/invoker.py` 必须从 `app.tools.execution_entry` 导入 `execute_tool`
   - `heartbeat.py` 必须从 `app.tools.execution_entry` 导入 `execute_tool`
   - `agent_tool_domains/messaging.py` 必须从 `app.tools.execution_entry` 导入 `execute_tool`
   - 上述三个调用点不允许继续回退到 `app.services.agent_tools.execute_tool`

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_heartbeat.py \
  backend/tests/services/test_agent_message_runtime.py -q
```

首次失败点：

- `backend/tests/services/test_agent_message_runtime.py`
- 失败原因：测试仍 patch 旧 facade，未命中新 trunk import binding

Green + 局部回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_heartbeat.py \
  backend/tests/services/test_agent_message_runtime.py -q
```

结果：

- `50 passed`

广义回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_tools_executor_dispatch.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `187 passed`

静态检查：

```bash
ruff check backend/app/runtime/invoker.py \
  backend/app/services/heartbeat.py \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_message_runtime.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的 execution 面已经进一步清楚：

1. `app.tools.execution_entry` 是显式 execution entry 真源
2. `agent_tools.py` 上的 `execute_tool*` 现只剩兼容 facade 价值，不再是生产主调路径
3. 当前剩余的主要 facade 债务，已经从 execution 面转移到 surface 面

### 下一步

下一刀应继续削薄 `agent_tools.py` 的 surface facade：

1. 盘点谁还在通过 `app.services.agent_tools` 读取：
   - `CORE_TOOL_NAMES`
   - `get_combined_openai_tools()`
   - `get_agent_tools_for_llm()`
2. 优先把高价值生产调用点切到：
   - `app.tools.surface`
3. 目标是让 `agent_tools.py` 最终只剩极薄兼容层，甚至只为过渡期测试与极少数 legacy 调用保留

---

## 15. 第九轮继续收口（2026-04-14）

### 已完成的 surface facade 生产调用面清退

1. 以下生产模块已不再从：
   - `app.services.agent_tools`
   读取 tool surface 元数据
2. 当前已直接切到：
   - `app.tools.surface`
3. 已完成迁移的调用点：
   - `backend/app/runtime/invoker.py`
   - `backend/app/services/pack_service.py`
   - `backend/app/runtime/prompt_eval.py`
   - `backend/app/runtime/task_eval.py`

### 当前结构变化

1. `backend/app` 生产代码内，`agent_tools.py` 已不再被其他生产模块 import
2. `agent_tools.py` 当前实际定位已进一步收缩为：
   - 兼容 facade
   - 测试/过渡期入口
3. 这意味着 Phase 5 的 production trunk 已进一步清楚为：
   - metadata / surface：`app.tools.surface`
   - execution entry：`app.tools.execution_entry`
   - execution runtime：`ToolRuntimeService`
   - delivery bridge：`agent_tool_domains/channel_delivery`

### 新增架构护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现在进一步固定：
   - `runtime/invoker.py` 必须从 `app.tools.surface` 读取 `CORE_TOOL_NAMES / get_agent_tools_for_llm / get_combined_openai_tools`
   - `pack_service.py` 必须从 `app.tools.surface` 读取 `CORE_TOOL_NAMES / get_combined_openai_tools`
   - `prompt_eval.py / task_eval.py` 必须从 `app.tools.surface` 读取 `CORE_TOOL_NAMES`
   - `backend/app` 内除 `services/agent_tools.py` 本体外，不允许再出现对 `app.services.agent_tools` 的生产 import

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `runtime/invoker.py` 仍从 `app.services.agent_tools` 导入 tool surface

Green + 局部回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/services/test_pack_service.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py -q
```

结果：

- `54 passed`

扩展回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_tools_executor_dispatch.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/services/test_pack_service.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `200 passed`

静态检查：

```bash
ruff check backend/app/runtime/invoker.py \
  backend/app/services/pack_service.py \
  backend/app/runtime/prompt_eval.py \
  backend/app/runtime/task_eval.py \
  backend/tests/architecture/test_tool_runtime_trunk.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 已经非常接近“生产主干完成、兼容层留待 Phase 6 清退”的状态：

1. 生产代码对 `agent_tools.py` 的依赖已经清零
2. `agent_tools.py` 不再承担任何 production 真源职责
3. 后续若要继续推进，重点不再是迁移消费者，而是决定：
   - 是否在 Phase 6 直接删除该 facade
   - 还是保留到更后面的兼容清退窗口

### 下一步

下一刀建议转入 Phase 5 的退出判定与 Phase 6 的接缝准备：

1. 盘点 `agent_tools.py` 当前只剩哪些测试/兼容消费者
2. 判断是否需要单独建立“compat facade deletion checklist”
3. 在进入删除前，先确保不存在外部隐式依赖（如脚本、文档、管理命令）

---

## 16. 第十轮继续收口（2026-04-14）

### 已完成的 compat facade 删除

1. 已删除：
   - `backend/app/services/agent_tools.py`
2. 删除前最后两步已完成：
   - `backend/tests/tools/test_bridge_equivalence.py` 改为直接依赖 `app.tools.surface`
   - `backend/tests/services/test_agent_tools.py` 中仅服务于 compat facade 的 delegation tests 已移除

### 新增护栏

1. 新增：
   - `backend/tests/architecture/test_legacy_agent_tools_allowlist.py`
2. 该测试现在固定：
   - `backend/` 内不允许再出现对 `app.services.agent_tools` 的 import
3. `backend/tests/architecture/test_tool_runtime_trunk.py`
   现已进一步固定：
   - `backend/app/services/agent_tools.py` 不再存在
   - `execute_tool` 主链只允许：
     - `kernel/engine.py`
     - `runtime/invoker.py`
     - `services/heartbeat.py`
     - `services/agent_tool_domains/messaging.py`
     - `tools/execution_entry.py`

### 本轮验证

Red：

```bash
pytest backend/tests/architecture/test_legacy_agent_tools_allowlist.py \
  backend/tests/architecture/test_tool_runtime_trunk.py -q
```

首次失败点：

- `backend/tests/services/test_agent_tools.py` 仍 import `app.services.agent_tools`
- `backend/app/services/agent_tools.py` 仍存在并出现在 execution path 扫描结果中

Green + 局部回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/architecture/test_legacy_agent_tools_allowlist.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/tools/test_bridge_equivalence.py -q
```

结果：

- `15 passed`

扩展回归：

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/architecture/test_legacy_agent_tools_allowlist.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/services/test_agent_tools_executor_dispatch.py \
  backend/tests/services/test_agent_message_runtime.py \
  backend/tests/services/test_skill_loading.py \
  backend/tests/services/test_agent_tool_domains.py \
  backend/tests/services/test_tool_error_envelopes.py \
  backend/tests/services/test_tool_registry.py \
  backend/tests/tools/test_bridge_equivalence.py \
  backend/tests/tools/test_hr_handler.py \
  backend/tests/services/test_tool_seeder.py \
  backend/tests/services/test_prompt_contracts.py \
  backend/tests/services/test_system_skill_templates.py \
  backend/tests/services/test_pack_service.py \
  backend/tests/api/test_tools_api_surface.py \
  backend/tests/runtime/test_invoker.py \
  backend/tests/runtime/test_coordinator.py \
  backend/tests/api/test_telegram_channel.py \
  backend/tests/api/test_feishu_channel_runtime.py \
  backend/tests/api/test_wecom_channel_runtime.py \
  backend/tests/services/test_channel_delivery_service.py \
  backend/tests/services/test_feishu_calendar_runtime.py \
  backend/tests/services/test_feishu_handler_runtime.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/services/test_wechat_personal_runtime.py \
  backend/tests/services/test_wecom_stream_runtime.py -q
```

结果：

- `208 passed`

静态检查：

```bash
ruff check backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/architecture/test_legacy_agent_tools_allowlist.py \
  backend/tests/services/test_agent_tools.py \
  backend/tests/tools/test_bridge_equivalence.py
```

结果：

- `All checks passed!`

### 当前判断

到这里，Phase 5 的真实主干已经完成了最后一层收口：

1. surface 真源：`app.tools.surface`
2. execution entry 真源：`app.tools.execution_entry`
3. runtime 真源：`ToolRuntimeService`
4. delivery 真源：`agent_tool_domains/channel_delivery`
5. `services/agent_tools.py` 已不再存在，不会再形成“旧入口悄悄复活”的回退面

### 下一步

下一刀应转向 Phase 6 的文档与历史描述收尾：

1. 清点哪些治理文档仍把 `agent_tools.py` 写成“当前存在的兼容 facade”
2. 区分“历史记录”与“当前状态”，避免文档层再次制造理解断层
3. 继续扩到其它 legacy wrapper / compat alias 的删除清单

---

## 17. Phase 6 接缝补刀（2026-04-14）

### 已完成的 governance compat 删除

1. 已删除：
   - `backend/app/tools/governance.py::_request_approval_compat`
2. 当前审批主链现已固定为：
   - `run_tool_governance()` / `_run_governance_inner()` 直接调用 `deps.request_approval(...)`
   - `backend/app/tools/governance_resolver.py` 提供 canonical `request_approval(agent_id, user_id, tool_name, arguments, capability, reason=None)`
   - resolver 内部再映射到 `approval_service.request_approval(...)`

### 新增护栏

1. `backend/tests/architecture/test_tool_runtime_trunk.py`
   已新增并固定：
   - `governance.py` 中不允许重新出现 `_request_approval_compat`
   - 审批分支必须直接 `await deps.request_approval(...)`
   - `governance_resolver.py` 必须保留 canonical `reason: str | None = None` 参数位
2. `backend/tests/tools/test_governance.py`
   已把 approval mock 契约对齐到 canonical 参数面，不再容忍 compat bridge 语义漂移

### 本轮验证

```bash
pytest backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/tools/test_governance.py \
  backend/tests/tools/test_governance_resolver.py -q
```

结果：

- `8 passed`

```bash
ruff check backend/app/tools/governance.py \
  backend/app/tools/governance_resolver.py \
  backend/tests/architecture/test_tool_runtime_trunk.py \
  backend/tests/tools/test_governance.py \
  backend/tests/tools/test_governance_resolver.py
```

结果：

- `All checks passed`

### 当前判断

到这里，Phase 5 留给 Phase 6 的 governance 尾巴已经被切掉：

1. runtime governance 不再多一层 compat adapter
2. approval 参数面只剩一套 canonical 形状
3. Phase 5 的剩余风险继续收缩到文档历史叙述与其它 legacy persistence / session 迁移桥

---

## 18. 交叉主干复核（2026-04-15）

### 本轮复核目标

这轮不再新增 Phase 5 结构改造，而是验证它在和 Phase 3 / Phase 4 接缝联调时是否还会回流旧系统：

1. `gateway transcript / participant contract` 修复之后，tool runtime 主干不能出现旧 `agent_tools` facade 回流
2. `prompt-memory / collaboration / gateway` 交叉回归时，`execute_tool -> execution_entry -> ToolRuntimeService` 这条主链必须稳定
3. Phase 5 的退出判断必须建立在交叉主干证据上，而不是单独自证

### 本轮复核结果

1. 已确认：
   - `backend/app/services/agent_tools.py` 仍不存在
   - `backend/tests/architecture/test_tool_runtime_trunk.py`
   - `backend/tests/architecture/test_legacy_agent_tools_allowlist.py`
   - `backend/tests/tools/test_bridge_equivalence.py`
   这三层护栏在交叉回归里继续成立
2. 已跑交叉回归：

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

### 当前判断

到这里，Phase 5 在生产主干层面的可信度已提升到可以按 `95%` 计：

1. metadata 真源仍只有 `ToolRegistry / collector`
2. execution 真源仍只有 `execution_entry / ToolRuntimeService`
3. governance 真源仍只有 canonical approval path
4. 与 Phase 3 / Phase 4 联调时，没有再出现旧 facade、第二执行器或旁路回流
