# Phase 6: 契约收紧与 Legacy 删除

## 1. 本阶段目标

在主干都已收口之后，做最后一轮“删尾巴”：

- 删除旧 wrapper
- 删除旧 API 壳
- 删除过时 metadata
- 删除无主 legacy 兼容代码

---

## 2. 进入本阶段的前提

必须同时满足：

1. T5/T4/T6/T3/T2 已完成
2. 各自主干局部回归都通过
3. 全量主干回归已通过

如果以上任一未满足，不得进入 Phase 6。

---

## 3. 清理对象

重点清理：

- 标记为 deprecated 但仍残留的 wrapper
- backward compat alias
- 已被新主干替代的旧 API
- 旧 metadata 字段
- 无调用方但仍保留的旧后台代码

---

## 4. 执行步骤

### W1 建 `no_legacy_paths` 测试

测试目标：

- 被宣告删除的旧路径搜索结果为 0

### W2 建 legacy 清单

来源：

```bash
rg -n "deprecated|legacy|backward compat|compat" backend/app
```

每一项都要标记：

- 保留理由
- 删除时机
- 删除前置条件

### W3 成批删除

删除原则：

- 每次只删同一主干内已失效的一组 legacy
- 删完立刻跑对应回归

### W4 收紧 contract

重点：

- `AgentInvocationRequest`
- `SessionContext.metadata`
- delegation / agent_message metadata
- source/channel 枚举语义

---

## 5. 退出条件

1. 主干相关 legacy 清单清零或只剩明确延期项
2. 旧 wrapper 不再被新代码依赖
3. 架构测试与全量主干回归继续通过

---

## 6. 当前 compat facade 基线（2026-04-14）

### `agent_tools.py` 当前状态

1. `backend/app` 生产代码内已无模块继续 import `app.services.agent_tools`
2. 兼容 facade `backend/app/services/agent_tools.py` 已删除
3. canonical 责任面现已明确为：
   - tool surface：`app.tools.surface`
   - execution entry：`app.tools.execution_entry`

### 当前仍保留的真实消费者

截至本轮盘点，仓库里已不存在显式 import `app.services.agent_tools` 的真实消费者。

### 已迁出的测试消费者

以下测试已从 `app.services.agent_tools` 改为直接依赖 `app.tools.surface`：

1. `backend/tests/services/test_pack_service.py`
2. `backend/tests/services/test_system_skill_templates.py`
3. `backend/tests/services/test_prompt_contracts.py`
4. `backend/tests/services/test_tool_registry.py`
5. `backend/tests/services/test_tool_seeder.py`
6. `backend/tests/tools/test_hr_handler.py`
7. `backend/tests/runtime/test_coordinator.py`
8. `backend/tests/tools/test_bridge_equivalence.py`

### 已删除的 facade 专项残留

1. `backend/tests/services/test_agent_tools.py`
   原本仍保留两条 facade delegation tests；
   本轮已删除这两条仅服务于 compat facade 的专项测试，保留的其余内容已全部直接验证：
   - `app.tools.surface`
   - `app.tools.execution_entry`

### 新增护栏

1. `backend/tests/architecture/test_legacy_agent_tools_allowlist.py`
   当前固定：
   - `backend/` 内不允许再出现对 `app.services.agent_tools` 的 import
2. `backend/tests/architecture/test_tool_runtime_trunk.py`
   当前固定：
   - `backend/app/services/agent_tools.py` 不再存在
   - execution / surface 真源不能回退到旧 facade

### 删除前置条件

`agent_tools.py` 删除前置条件已在本轮满足并完成。接下来应关注的是：

1. 是否还有文档把它描述成当前主干入口
2. Phase 5 历史记录是否需要补一条“compat facade 已完成删除”
3. 后续若新增工具入口，不允许再以兼容 facade 形式回插 `services/`

### 下一批高价值 legacy 候选（基于本轮盘点）

1. `backend/app/services/session_service.py` / `backend/app/services/feishu_identity_maintenance.py`
   - 仍保留 legacy conversation id 归并逻辑
   - 风险：会牵动 Phase 2 session-message 主干与渠道历史连续性
2. `backend/app/runtime/context_budget.py`
   - 仍保留旧 `schedule` session source 枚举
   - 风险：cheap-route gating 与当前 `trigger` 主干语义脱节，形成静默质量漂移

当前优先级建议：

1. 先处理 `context_budget.py` 的旧枚举漂移，因为这是 active runtime contract，而且修复面更清晰
2. 再回到 `session_service / Feishu identity maintenance` 内剩余的 legacy conversation 归并桥

### 该候选项已完成（2026-04-15）

1. `backend/app/runtime/context_budget.py`
   已完成旧枚举修复：
   - `schedule` 已从 `_NO_CHEAP_ROUTE_SESSION_SOURCES` 移除
   - `trigger` 已补入 canonical internal session source 集合
2. 已新增双层护栏：
   - 行为护栏：`backend/tests/runtime/test_context_budget.py`
   - 架构护栏：`backend/tests/architecture/test_prompt_memory_trunk.py`
3. 这一项的意义不是“改命名”，而是：
   - 防止 trigger runtime 被错误降级到 cheap fallback model
   - 把 Phase 1 自主触发主干与 Phase 4 runtime routing contract 真正对齐
4. 因此下一优先级重新回到：
   - `session_service.py`
   - `feishu_identity_maintenance.py`
   里剩余的 runtime compat 归并桥

### 当前进行中的下一刀（2026-04-15）

1. 继续处理 `session_service.py` 的 gateway runtime compat bridge
2. 这轮排查重点不是“能不能删”，而是先确认它有没有造成部分归一化：
   - `ChatMessage.conversation_id` 已改成 canonical
   - 但 `GatewayMessage.conversation_id` 仍停在 legacy
3. 如果这个断层存在，就会导致：
   - transcript 主链已经收口
   - gateway 队列侧还挂旧 id
   - 同一对 agent 的历史与回执链出现“半 canonical、半 legacy”的隐形债务

### 该候选项本轮已推进（2026-04-15）

1. `session_service.py`
   的 gateway runtime compat bridge 已补完整：
   - 运行期归一化不再只改 `ChatMessage`
   - `GatewayMessage.conversation_id` 现在也会一起改到 canonical session UUID
2. 这意味着即使在还没跑维护脚本的历史环境里，
   runtime compat 也不再制造“消息历史已 canonical、回执队列还 legacy”的半收口状态
3. `feishu_identity_maintenance.py`
   这轮也补了一刀：
   - legacy alias session 合并进 canonical session 时
   - 会额外保住 `participant_id` 与 `delivery_target_json`
4. `db_legacy_feishu_session_migration.py`
   这轮也已与 runtime merge 对齐：
   - DB-level alias merge 现在不再丢 `participant_id / delivery_target_json`
   - canonical session owner 与迁移消息的 `user_id` 也会一起收敛
5. 当前下一优先级继续保留为：
   - 进一步判断 `session_service.py` / `feishu_identity_maintenance.py`
     里哪些 bridge 仍是动态收敛所必需，哪些已经可以继续下沉到 maintenance path
6. 本轮继续完成的收尾点：
   - `session_service.py` 的 runtime gateway bridge
     现在也已对齐 `created_at / last_message_at`
   - runtime merge 不再只改 conversation id，而是会一起回填 canonical session 的时间边界
7. 本轮继续完成的另一处对齐：
   - `db_legacy_gateway_conversation_migration.py`
     在复用已有 canonical session 时，也会同步回拉 `created_at`
   - gateway 的 runtime bridge 与 DB helper 现在已在时间边界语义上重新一致

### 本轮新增进度（2026-04-14）

1. `schedule_surface.py` 已建立为 canonical schedule surface
2. runtime compat 已收口，schedule legacy 已移交 DB migration 层
3. `scheduler.py` 已物理删除
4. `supervision_reminder.py` 已物理删除
5. `main.py` 已接管启动期全量 legacy schedule migration，外部迁移脚本入口已收回
6. `backend/app/scripts/migrate_schedules_to_triggers.py` 已删除
7. `backend/app/main.py / backend/entrypoint.sh / backend/seed.py` 已不再把 `app.models.schedule` 注入 bootstrap `create_all`
8. `backend/app/models/schedule.py` 已删除
9. `backend/app/services/schedule_compat.py` 已删除
10. `backend/app/services/legacy_schedule_migration.py` 已删除
11. 新增 `backend/app/db_legacy_schedule_migration.py`，供 bootstrap / Alembic 共用
12. 新增 `backend/alembic/versions/drop_legacy_agent_schedules_0414.py`
13. `backend/alembic/env.py` 已不再 import `AgentSchedule`
14. `api/schedules.py` 已完全脱离 compat migration，`main.py` 也已不再承担 legacy schedule migration
15. Phase 1 在代码层已闭环；剩余仅为运行层应用 Alembic 头 `drop_legacy_agent_schedules_0414`
16. `memory_service.on_conversation_end()` 已删除，memory wrapper 尾巴继续收窄
17. `tools/governance.py::_request_approval_compat` 已删除，治理审批链现只允许：
    - `governance.py` 直接调用 `deps.request_approval(...)`
    - `governance_resolver.py` 负责把 canonical 参数映射到 `approval_service.request_approval(...)`
18. `backend/tests/architecture/test_tool_runtime_trunk.py` 已新增护栏：
    - 不允许 `_request_approval_compat` 回流
    - `governance.py` 必须直接 await `deps.request_approval(...)`
19. `memory/store.py` 已删除 `memory.json` 双写路径，legacy json 只保留一次性导入职责
20. `api/memory.py` 已改为经 `PersistentMemoryStore.load_semantic_facts()` 读取，不再直读 `memory.json`
21. `backend/tests/architecture/test_prompt_memory_trunk.py` 已新增护栏：
    - `api/memory.py` 不允许再直接依赖 `memory.json`
    - `memory/store.py` 不允许重新出现 `_write_legacy_json`
22. `api/gateway.py::_find_or_create_gateway_agent_pair_session()` 已删除 `migrate_legacy_transcripts` compat 开关
23. `gateway/report_result()` 已不再在入口层判断 legacy `gw_agent_*` 形状，统一回到 pair-session helper 做 canonical 化
24. `services/channel_session.py` 已删除通用参数 `legacy_external_conv_ids`
25. Feishu session alias 归并已收回 `feishu_identity_maintenance.py`：
    - `build_feishu_session_lookup_ids()`
    - `find_or_create_feishu_chat_session()`
26. `api/feishu.py` 与 `agent_tool_domains/messaging.py` 已不再在调用点显式传 `legacy_external_conv_ids`
27. `services/memory_service.py` 已删除对 `FileBackedMemoryStore` 的生产 fallback 依赖
28. retrieval pipeline 异常时，memory fallback 现直接走 canonical：
    - `_load_session_summary()`
    - `_load_previous_session_summary()`
    - `_load_agent_memory()`
29. `backend/tests/architecture/test_prompt_memory_trunk.py` 已新增护栏：
    - `memory_service.py` 不允许重新出现 `FileBackedMemoryStore`
    - retrieval fallback 不允许回到 legacy memory store
30. `api/gateway.py` 本地 `_find_or_create_gateway_agent_pair_session()` helper 已删除
31. `session_service.find_or_create_agent_pair_session()` 现统一承担 legacy `gw_agent_*` transcript 归一化
32. `backend/tests/architecture/test_session_message_trunk.py` 与 `backend/tests/api/test_gateway_conversation_contract.py`
    已新增护栏：
    - `gateway.py` 不允许重新出现 `gw_agent_*`
    - `gateway.py` 不允许重新长出本地 pair-session compat helper
33. `api/feishu.py` 已不再自己调用 `build_feishu_session_lookup_ids()` 做 alias 预读
34. Feishu 文本消息入口现统一：
    - 先 `find_or_create_feishu_chat_session()`
    - 再按 canonical session UUID 读取 history
35. `backend/tests/services/test_feishu_outbound_identity_source.py` 已新增护栏：
    - `api/feishu.py` 不允许重新出现 `build_feishu_session_lookup_ids`
    - `api/feishu.py` 不允许重新出现 `pre_session_conv_ids`
36. 已新增 `backend/app/db_legacy_gateway_conversation_migration.py`
    与 `backend/app/scripts/cleanup_legacy_gateway_conversations.py`
    作为 legacy `gw_agent_*` conversation 的一次性清尾路径
37. `backend/tests/test_db_legacy_gateway_conversation_migration.py`
    与 `backend/tests/test_alembic_bootstrap.py`
    已新增维护路径测试：
    - legacy id 解析
    - pair 去重
    - 缺失 agent 跳过
    - bootstrap 自动执行 gateway legacy 归并
38. `backend/tests/scripts/test_maintenance_scripts_source.py` 已新增脚本层护栏：
    - 维护脚本不允许继续 import 已删除的 `schedule` 模型
    - gateway 清尾脚本必须直接复用 DB-level migration helper
39. `backend/app/services/session_maintenance.py` 已删除；
    `gw_agent_*` 维护路径不再保留中间 async wrapper
40. 已新增 `backend/app/db_legacy_feishu_session_migration.py`
    作为 `feishu_p2p_<open_id> -> feishu_p2p_<user_id>` 的 DB-level 一次性清尾路径
41. `backend/app/db_bootstrap.py` 现会在启动期自动执行 `promote_legacy_feishu_sessions()`
    也就是 Feishu legacy session 归并已从运行时 wrapper 进一步下沉到 bootstrap / maintenance 主链
42. `backend/app/scripts/cleanup_duplicate_feishu_users.py` 已改为直接复用：
    - `merge_duplicate_feishu_users()`
    - `promote_legacy_feishu_sessions()`
    不再继续 import `reconcile_feishu_identity_state()`
43. `backend/app/services/feishu_identity_maintenance.py` 已删除：
    - `normalize_feishu_chat_sessions()`
    - `reconcile_feishu_identity_state()`
    当前 Feishu runtime 边界只保留 canonical lookup / create helper
44. `backend/tests/architecture/test_legacy_session_compat_allowlist.py`
    与 `backend/tests/scripts/test_maintenance_scripts_source.py`
    已新增护栏：
    - 已删除的 Feishu wrapper 不允许回流
    - duplicate-user 维护脚本必须直接走 DB-level session normalization helper
45. 已新增 `backend/app/session_identifiers.py`
    作为 gateway / Feishu session identifier 的公共 contract 模块
46. 以下模块现已改为直接复用这份 contract，而不是各自维护一套字符串规则：
    - `services/session_service.py`
    - `db_legacy_gateway_conversation_migration.py`
    - `services/feishu_identity_maintenance.py`
    - `db_legacy_feishu_session_migration.py`
    - `services/pending_reply_service.py`
    - `services/channel_user_service.py`
47. `backend/tests/test_session_identifier_contracts.py`
    与 `backend/tests/architecture/test_session_identifier_contract.py`
    已新增护栏：
    - gateway / Feishu identifier 的 build + parse 必须集中在一个模块
    - `pending_reply_service.py` / `channel_user_service.py` 不允许继续手写 `feishu_p2p_` 解析
48. `api/activity.py` 的 Feishu conversation label 逻辑也已切到公共 parser：
    - 新增 `_feishu_conversation_partner_name()`
    - 不允许再在 activity read path 内手写 `startswith("feishu_p2p_")`
49. `services/feishu_identity_maintenance.py` 内剩余 runtime compat 已继续显式分层：
    - `_apply_feishu_session_runtime_updates()`
    - `_merge_legacy_feishu_session_into_canonical()`
    - `_promote_legacy_feishu_alias_session()`
    当前目的不是保留新 facade，而是把“canonical 主干入口”与“临时 compat 逻辑”分层钉死，方便后续继续下沉/删除。
50. `session_identifiers.py` 已新增 `canonicalize_agent_pair_ids()`

51. `api/activity.py` 的 session-backed channel 历史读取已切回 canonical session UUID：
    - `list_conversations()` 已不再按 `web_% / feishu_% / slack_% / discord_%` 扫描 `ChatMessage`
    - `web / feishu / slack / discord` 会话现统一从 `ChatSession` 出发，再按 `conversation_id == str(session.id)` 读取消息
52. `get_conversation_messages()` 已明确分成两条读链：
    - `source_channel == "agent"` 继续保留 participant sender-name 逻辑
    - 其余 session-backed channels 走 canonical UUID 读取，并统一剥离 `[发送者: ...]` 前缀
53. legacy `web_ / feishu_ / slack_ / discord_` 已压缩为只读 fallback；
    主路径不再依赖旧前缀 shape 做会话列表聚合。
54. 已新增 `backend/tests/api/test_activity_chat_history_sessions.py`
    与 `backend/tests/architecture/test_session_message_trunk.py` 护栏：
    - `activity.py` 不允许重新出现 `conversation_id.like("web_%")`
    - `activity.py` 不允许重新出现 `conversation_id.like("feishu_%")`
    - session-backed channel conversation list 必须继续走 `ChatSession` 主干
55. `pending_reply_service.py` 已修复 Slack sender identity contract 漂移：
    - `slack_<channel_id>_<sender_id>` 不再被解析成 `slack:<channel_id>_<sender_id>`
    - `slack_dm_<sender_id>` 不再被解析成 `slack:dm_<sender_id>`
    - 当前统一回到 `slack:<sender_id>`，与待回复上下文 capture 侧保持一致
56. `trigger_daemon.py` 与 `api/feishu.py` 的 pending-reply sender identity 解析
    已统一改为 `sender_identity_from_session(session_obj)`：
    - 不再在入口层先按 `external_conv_id` 手动猜一遍
    - `delivery_target_json` 若才是最新 canonical identity，会被优先消费
57. `activity.py` 已不再只覆盖 `web/feishu/slack/discord`；
    当前统一历史视图已纳入：
    - `telegram`
    - `wecom`
    - `dingtalk`
    - `wechat_personal`
    - `microsoft_teams`
58. `api/dingtalk.py` 与 `api/teams.py` 现已在 session 创建时写入 canonical `delivery_target`，
    不再让这些渠道继续裸奔只靠 `external_conv_id`
59. `ChannelDeliveryService.identity_from_delivery_target()` 已补齐
    `dingtalk / microsoft_teams`，`pending_reply_service` 也已补齐 `dingtalk_p2p_*` fallback parser。
60. `api/chat_sessions.py` 已完成 source_channel 命名收口：
    - 不再使用旧的 `teams`
    - 统一改用 canonical `microsoft_teams`
61. `api/chat_sessions.py` 已把内部 session 从聊天管理视图显式排除：
    - `trigger`
    - `task`
    - `heartbeat`
62. 前端 `AgentChatSection.tsx` 也已补上 `microsoft_teams` label 映射，
    避免 source_channel contract 已统一但展示层仍旧掉队。
63. `memory/t2_store.py` 的 source naming contract 也已补齐：
    - `microsoft_teams` 已进入 `_HUMAN_SOURCES`
    - 避免 Teams 人类对话在 T2 memory 权重层被误判成非 human source
64. `services/session_recall.py` 已把 `task` 纳入 `_EXCLUDED_CHANNELS`，
    避免内部 task session 再次从 memory recall 边界漏回用户/渠道会话主干。
    gateway pair session 的 canonical 排序规则现已从：
    - `services/session_service.py`
    - `db_legacy_gateway_conversation_migration.py`
    收口为单一来源。
51. 已新增 `channel_message_contracts.py`
    统一承接 `[发送者: ...]` sender prefix 的提取与剥离；
    `api/activity.py` 与 `runtime/hooks_setup.py` 不允许继续各自手写 sender prefix 解析。
52. `channel_message_contracts.py` 已继续接管 sender prefix 的写侧：
    - 新增 `prefix_message_with_sender_label()`
    - `api/feishu.py` 不允许继续自己拼 `[发送者: {sender_name}...]`
53. websocket history read path 已从 legacy `web_{user_id}` 直读切回 canonical session 主干：
    - 新增 `session_service.find_web_chat_session()`
    - `api/websocket.py::get_chat_history()` 必须先找 canonical web session，再按 session UUID 读消息
54. `web_session_contract.py` 已新增 `parse_web_external_conv_id()`
    web external conv id 的 build / parse 开始配对收口；
    `pending_reply_service.py` 不允许继续自己手写 `startswith("web_")`

### 继续收尾：Feishu alias merge 的时间边界也已对齐（2026-04-15）

1. 在把：
   - `participant_id`
   - `delivery_target_json`
   - `user_id`
   这些 identity 字段对齐之后，继续复核 Feishu alias merge，又发现一处和 gateway 曾经同类的残差：
   - `backend/app/services/feishu_identity_maintenance.py`
     在 runtime merge legacy alias session 进 canonical session 时，
     之前不会把最早 `created_at` 一起回拉
   - `backend/app/db_legacy_feishu_session_migration.py`
     在复用已有 canonical session 时，
     之前同样不会把 legacy alias session 的更早 `created_at` 回拉回来
2. 风险：
   - Feishu 会话看起来已经 canonical 化
   - 但 runtime merge 与 bootstrap / maintenance helper 仍会留下“起点时间偏晚”的假时间线
   - session 排序与历史时间边界语义因此继续漂移
3. 本轮已完成修复：
   - `backend/app/services/feishu_identity_maintenance.py`
     现已在 legacy alias session 并入 canonical session 时，
     把 canonical session 的 `created_at` 回拉到两者中的最早时间
   - `backend/app/db_legacy_feishu_session_migration.py`
     在复用已有 canonical session 时，
     也按同一规则回拉：
     - `created_at`
4. 结果：
   - Feishu runtime merge
   - Feishu bootstrap migration
   - Feishu maintenance helper
   现在和 gateway 一样，在时间边界语义上重新一致：
   - `created_at`
   - `last_message_at`
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_maintenance.py`
     新增断言：legacy alias merge 进 canonical session 时，必须保住最早 `created_at`
   - `backend/tests/test_db_legacy_feishu_session_migration.py`
     新增断言：DB helper 复用已有 canonical session 时，也必须把 `created_at` 回拉到 legacy 更早时间
   - 红阶段已确认旧实现真实失败
6. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_feishu_identity_maintenance.py \
  backend/tests/test_db_legacy_feishu_session_migration.py \
  backend/tests/test_alembic_bootstrap.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_legacy_session_compat_allowlist.py \
  -q
```

结果：

- `24 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/feishu_identity_maintenance.py \
  backend/app/db_legacy_feishu_session_migration.py \
  backend/tests/services/test_feishu_identity_maintenance.py \
  backend/tests/test_db_legacy_feishu_session_migration.py
```

结果：

- `All checks passed`

### 继续收尾：org_sync_service 也已统一收口到 provider-backed identity 主干（2026-04-15）

1. 在把发送、搜索、只读面收口之后，继续往后台同步链看，又确认 `org_sync_service.py` 还残留一套独立的旧匹配逻辑：
   - 既有 `OrgMember` 主要按：
     - `feishu_user_id`
     - `feishu_open_id`
     查找
   - 平台 `User` 则继续手写：
     - `feishu_user_id`
     - `feishu_open_id`
     - `email`
     的旧匹配 / 创建流程
2. 风险：
   - 这会让 org sync 在后台维护时，
     再次绕开已经存在的：
     - `external_identities`
     - provider-backed `external_id/open_id`
     主干
   - 最终结果是：
     - 运行时已经 canonical
     - 但后台同步仍可能产生重复用户或错绑对象
3. 本轮已完成修复：
   - `backend/app/services/org_sync_service.py`
   - 既有 `OrgMember` 现在会按：
     - `OrgMember.external_id or OrgMember.feishu_user_id`
     - `OrgMember.open_id or OrgMember.feishu_open_id`
     统一匹配
   - 平台用户解析现在改为优先复用：
     1. `feishu_auth_provider._find_user_by_external_identity(...)`
     2. `feishu_auth_provider._find_user_by_legacy_fields(...)`
     3. `feishu_auth_provider._create_user(...)`
   - 同步逻辑不再自己维护一套独立的旧用户匹配 / 创建分支
4. 结果：
   - `org_sync_service -> org member reconcile -> platform user reconcile -> external identity upsert`
     现在也开始共用同一条 canonical provider-backed 主干
5. 已补红绿测试：
   - `backend/tests/services/test_org_sync_source.py`
     新增 contract：
     - org sync 必须匹配 `external_id/open_id`
     - 平台用户解析必须复用 auth provider 的 identity resolution 主干
   - 红阶段已确认旧实现真实失败：
     - 缺少 `OrgMember.external_id == user_id`
     - 缺少 `feishu_auth_provider._find_user_by_external_identity`

### 继续收尾：cleanup_duplicate_feishu_users.py 现在也会同步 provider-backed write-through（2026-04-15）

1. 继续检查维护脚本时，又发现：
   - `cleanup_duplicate_feishu_users.py`
     在 user/member backfill 时，
     主要还停在：
     - 只补 `feishu_user_id`
     - `OrgMember` 只补一半 canonical 字段
2. 风险：
   - 维护脚本本身虽然能把 `open_id` session 迁到 `user_id`
   - 但如果字段写回不完整，
     还是会留下：
     - `legacy field` 已更新一半
     - `provider-backed field` 没对齐
     的半状态
3. 本轮已完成修复：
   - `backend/app/scripts/cleanup_duplicate_feishu_users.py`
   - 用户 backfill 现改为复用：
     - `feishu_auth_provider._write_through_user_fields(...)`
     - `feishu_auth_provider._hydrate_user_profile(...)`
   - org member backfill 现在也会同步刷新：
     - `open_id`
     - `feishu_open_id`
     - `unionid`
     - `feishu_user_id`
4. 结果：
   - 维护脚本不再只补 `feishu_user_id`
   - provider-backed / legacy write-through 现在开始同步对齐
5. 已补红绿测试：
   - `backend/tests/scripts/test_maintenance_scripts_source.py`
     新增断言：
     - 脚本必须显式调用 `feishu_auth_provider._write_through_user_fields`
     - 必须同步刷新 `member.feishu_open_id`
   - 红阶段已确认旧实现真实失败

### 本轮补充验证结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py \
  backend/tests/services/test_relationships_file.py \
  backend/tests/services/test_org_sync_source.py \
  backend/tests/api/test_users_api.py \
  backend/tests/scripts/test_maintenance_scripts_source.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `67 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/channel_user_service.py \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/app/services/agent_tool_domains/feishu_users.py \
  backend/app/services/relationships_file.py \
  backend/app/services/org_sync_service.py \
  backend/app/scripts/cleanup_duplicate_feishu_users.py \
  backend/app/api/users.py \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py \
  backend/tests/services/test_relationships_file.py \
  backend/tests/services/test_org_sync_source.py \
  backend/tests/api/test_users_api.py \
  backend/tests/scripts/test_maintenance_scripts_source.py
```

结果：

- `All checks passed`

### 继续收尾：feishu_identity_maintenance / db_legacy_feishu_session_migration 的 provider-backed 边角已收平（2026-04-15）

1. 在把 org sync 与 cleanup script 收回 provider-backed 主干之后，继续往最后两块 maintenance / migration helper 看，又确认还留着一组“主流程已 canonical、维护面还半步落后”的尾巴：
   - `feishu_identity_maintenance.py`
     在 duplicate user merge 时，
     虽然已经会把 `ExternalIdentity.user_id` 迁回主用户，
     但主用户自己的：
     - `feishu_user_id`
     - `feishu_open_id`
     - `feishu_union_id`
     仍可能停留为空
   - `db_legacy_feishu_session_migration.py`
     在启动期做 `feishu_p2p_<open_id> -> feishu_p2p_<user_id>` 迁移时，
     仍主要依赖：
     - `users.feishu_user_id`
     - `users.feishu_open_id`
     不会读取 provider-backed `external_identities`
2. 风险：
   - duplicate user merge 后，
     会出现：
     - canonical identity mapping 已经归并
     - primary user legacy field 还没补齐
     的半状态
   - 启动期 / 维护脚本迁移 Feishu session alias 时，
     也会出现：
     - runtime merge 能认 provider-backed identity
     - DB helper 却因为旧列为空而直接漏迁
     的 runtime / bootstrap 不一致
3. 本轮已完成修复：
   - `backend/app/services/feishu_identity_maintenance.py`
     新增：
     - `_hydrate_primary_user_from_external_identities(...)`
   - duplicate user merge 在迁移 `ExternalIdentity.user_id` 后，
     会继续回灌主用户缺失的：
     - `feishu_user_id`
     - `feishu_open_id`
     - `feishu_union_id`
   - `backend/app/db_legacy_feishu_session_migration.py`
     现已支持：
     - 先读取 `external_identities`
     - 若存在 `identity_providers`，则只吸收 `provider_type == "feishu"` 的 identity
     - 当 `users.feishu_*` 为空时，回退使用 provider-backed `provider_user_id / provider_open_id`
4. 结果：
   - `duplicate user merge -> external identity move -> primary user write-through`
     现在开始回到单一主干
   - `runtime Feishu session merge`
     与
     `bootstrap / maintenance Feishu session migration`
     现在在 identity source 上也开始共享同一套 canonical provider-backed 语义
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_maintenance.py`
     新增断言：
     - duplicate merge 之后，primary user 必须能从 `ExternalIdentity` 补齐缺失的 `open_id / union_id`
   - `backend/tests/test_db_legacy_feishu_session_migration.py`
     新增断言：
     - 当 `users.feishu_*` 为空、但 `external_identities` 里仍有 provider-backed identity 时，session 迁移仍必须完成
   - 红阶段已确认旧实现真实失败：
     - primary user 的 legacy Feishu 字段未被回灌
     - DB helper 直接返回 `migrated == 0`

### 本轮补充验证结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_feishu_identity_maintenance.py \
  backend/tests/test_db_legacy_feishu_session_migration.py \
  -q
```

结果：

- `14 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/feishu_identity_maintenance.py \
  backend/app/db_legacy_feishu_session_migration.py \
  backend/tests/services/test_feishu_identity_maintenance.py \
  backend/tests/test_db_legacy_feishu_session_migration.py
```

结果：

- `All checks passed`

### 扩展回归结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py \
  backend/tests/services/test_relationships_file.py \
  backend/tests/services/test_org_sync_source.py \
  backend/tests/api/test_users_api.py \
  backend/tests/scripts/test_maintenance_scripts_source.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  backend/tests/services/test_feishu_identity_maintenance.py \
  backend/tests/test_db_legacy_feishu_session_migration.py \
  -q
```

结果：

- `81 passed, 10 warnings`

### 继续收尾：relationship member 只有 open_id 时，发送主链也会先回拉 canonical user_id（2026-04-15）

1. 在把 direct input、org member fallback、search 入口逐层收口之后，继续往发送主链深挖，又发现一处更贴近 runtime 的残留：
   - relationship member 如果只有：
     - `open_id`
   - 旧实现会直接把发送流程锁在：
     - Step3 `open_id`
     - Step4 `org-sync open_id fallback`
   - 不会在真正发送前先尝试把它回拉成：
     - canonical `user_id`
2. 风险：
   - 同一个 relationship 联系人
   - 名称解析层已经越来越 canonical
   - 但真正发送层仍可能继续停在 transport identity
   - 这会让：
     - 普通发送成功路径
     - `org-sync` cross-app fallback
     - tool args / session capture
     继续出现隐形双轨
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
   - 新增发送前 canonicalization helper：
     - 当 relationship target 只有 `open_id`
       会先尝试：
       1. `resolve_feishu_user(..., provider_open_id=...)`
       2. `get_feishu_delivery_target(...)`
   - 若能回拉到 canonical `user_id`，
     则 Step1 会先尝试：
     - `user_id`
   - `email/phone -> resolve_open_id` 成功后，
     也会继续把 `open_id` 反拉成 canonical `user_id`
   - `org-sync` fallback 里如果 `user_id` 已可用，
     也会先走：
     - `org-sync app + user_id`
4. 结果：
   - `relationship member open_id -> canonical user_id -> send / org-sync / session capture`
     这条链现在也被并回同一条主干
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_resolution.py`
     新增断言：
     - relationship 成员只有 `open_id` 时，也必须先尝试 canonical `user_id`
     - `org-sync` cross-app fallback 对这类成员也必须优先尝试 canonical `user_id`
   - 红阶段已确认旧实现真实失败：
     - `KeyError: 'user_id'`

### 继续收尾：relationships.md 现在也会展示 provider-backed Feishu 身份（2026-04-15）

1. 在把发送/搜索/管理面逐步收口之后，继续检查只读文档面时，又发现：
   - `relationships_file.render_relationships_markdown()`
     原先只展示：
     - `member.feishu_open_id`
2. 风险：
   - 某个关系成员即使已经迁到：
     - `external_id`
     - `open_id`
   - 关系网络文档里仍然完全看不见 canonical `user_id`
   - 这会形成“runtime 主干已经 canonical，但人工查看文档时仍只看到旧字段”的展示断点
3. 本轮已完成修复：
   - `backend/app/services/relationships_file.py`
   - 关系文档现在会同时展示：
     - `飞书 user_id`
     - `飞书 open_id`
   - 并优先使用：
     - `external_id / open_id`
     - 其次才是旧 `feishu_*`
4. 结果：
   - 关系文档不再把 canonical Feishu 身份隐藏掉
   - 人工查看面与真实发送主干开始对齐
5. 已补红绿测试：
   - `backend/tests/services/test_relationships_file.py`
     新增断言：
     - provider-backed `user_id/open_id` 必须出现在渲染结果里
   - 红阶段已确认旧实现真实失败：
     - 文档里完全看不到 `u_provider_123`

### 继续收尾：/api/users 现在也会按 canonical delivery target 标识 Feishu 用户来源（2026-04-15）

1. 继续排查只读管理面时，又发现：
   - `backend/app/api/users.py::list_users()`
     原先只凭：
     - `user.feishu_open_id`
     判断：
     - `source = "feishu" | "registered"`
2. 风险：
   - 如果用户已经迁到：
     - canonical `user_id`
     - external identity
   - 但本地 `feishu_open_id` 为空
   - 管理面板会把它误判成：
     - `registered`
   - 这会让运营侧继续看到“旧真相”
3. 本轮已完成修复：
   - `backend/app/api/users.py`
   - `list_users()` 现在会调用：
     - `channel_user_service.get_feishu_delivery_target(...)`
   - 若 delivery target 存在，
     就把用户来源标为：
     - `feishu`
   - 若 canonical target 本身是 `open_id`，
     也会把它写进返回的：
     - `feishu_open_id`
4. 结果：
   - `/api/users`
     现在也开始和 canonical delivery target 主干共享同一套身份判断
   - 管理面板不再把 provider-backed Feishu 用户误判成普通注册用户
5. 已补红绿测试：
   - `backend/tests/api/test_users_api.py`
     新增断言：
     - provider-backed `user_id` 也必须把 `source` 判成 `feishu`
     - provider-backed `open_id` 也必须映射到返回的 `feishu_open_id`
   - 红阶段已确认旧实现真实失败：
     - `source == "registered"`

### 本轮补充验证结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py \
  backend/tests/services/test_relationships_file.py \
  backend/tests/api/test_users_api.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `62 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/channel_user_service.py \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/app/services/agent_tool_domains/feishu_users.py \
  backend/app/services/relationships_file.py \
  backend/app/api/users.py \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py \
  backend/tests/services/test_relationships_file.py \
  backend/tests/api/test_users_api.py
```

结果：

- `All checks passed`

### 继续收尾：Org member fallback 也已从 open_id 回拉到 canonical user_id（2026-04-15）

1. 继续往 `channel_user_service.resolve_feishu_delivery_target_by_name()` 深挖时，又发现 relationship member / tenant org member 这两条分支还残留一层旧逻辑：
   - 如果 `OrgMember` 上只有：
     - `open_id`
   - 旧实现会直接返回：
     - `open_id`
   - 不会继续尝试：
     - `resolve_feishu_user(...)`
     - `get_feishu_delivery_target(...)`
2. 风险：
   - 这会让：
     - `name -> org member fallback`
     仍然可能停在 transport identity
   - 即使平台用户侧已经拥有 canonical `user_id`
   - 也无法在名称解析时被重新拉回主干
3. 本轮已完成修复：
   - `backend/app/services/channel_user_service.py`
   - 新增 org member delivery target 收口逻辑：
     - 优先使用 `external_id / feishu_user_id`
     - 若只有 `open_id / feishu_open_id`
       则继续尝试：
       1. `resolve_feishu_user(..., provider_open_id=...)`
       2. `get_feishu_delivery_target(...)`
   - 如果能反查到 canonical `user_id`，就返回：
     - `user_id`
   - 只有找不到 canonical user target 时，才保留：
     - `open_id`
4. 结果：
   - `name -> relationship member / tenant org member -> canonical delivery target`
     现在也统一并回同一条 Feishu canonical identity 主干
5. 已补红绿测试：
   - `backend/tests/services/test_channel_user_service.py`
     新增断言：
     - relationship member 只有 `open_id` 时，也必须能回拉到 canonical `user_id`
     - tenant org member 只有 `open_id` 时，也必须能回拉到 canonical `user_id`
   - 红阶段已确认旧实现真实失败：
     - 旧实现返回 `('ou_provider_only', 'open_id')`

### 继续收尾：Direct open_id 输入现在也会走 provider-backed 校验并补回 canonical user_id（2026-04-15）

1. 继续检查 `send_feishu_message()` 的 direct input 支路时，又确认一处更底层的 contract 漂移：
   - direct `user_id/open_id` 输入校验原先只查：
     - `OrgMember.feishu_user_id`
     - `OrgMember.feishu_open_id`
   - 没有同步接受：
     - `OrgMember.external_id`
     - `OrgMember.open_id`
2. 风险：
   - 如果组织成员已经迁到 provider-backed canonical 字段
   - 但 direct send 入口还只认旧 `feishu_*`
   - 调用方即使给了正确的 `open_id`
     也可能在入口校验、identity backfill、session capture 上再次分叉
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
   - direct `user_id` 校验现同时接受：
     - `OrgMember.external_id`
     - `OrgMember.feishu_user_id`
   - direct `open_id` 校验现同时接受：
     - `OrgMember.open_id`
     - `OrgMember.feishu_open_id`
   - 同时 direct `open_id` 支路现在若尚未拿到 stable `user_id`，
     也会继续尝试：
     - `resolve_feishu_user(..., provider_open_id=...)`
     - `get_feishu_delivery_target(...)`
   - 若可回拉 canonical `user_id`，
     则会补回：
     - tool args
     - outgoing session capture
4. 结果：
   - `direct open_id -> provider-backed validation -> canonical backfill -> session capture`
     这条支路现已重新对齐主干
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_resolution.py`
     新增断言：
     - direct `open_id` 输入命中 provider-backed `OrgMember.open_id` 后，
       必须回填 canonical `user_id`
   - 红阶段已确认旧实现真实失败：
     - `KeyError: 'user_id'`

### 继续收尾：feishu_user_search 现在也开始输出 provider-backed / canonical identity（2026-04-15）

1. 在把发送主链大部分收齐之后，继续回看 `feishu_user_search`，又发现搜索入口本身也残留旧字段消费：
   - `OrgMember` 分支原先只输出：
     - `feishu_user_id`
     - `feishu_open_id`
   - `User` 分支原先也只输出：
     - `user.feishu_user_id`
     - `user.feishu_open_id`
2. 风险：
   - 即使发送主链、session 主链、pending-reply 主链都已经开始收口到 canonical identity
   - 搜索入口仍可能把用户重新展示成：
     - 旧 `feishu_*`
     - 或干脆丢失 `user_id`
   - 这会让上游工具补偿线再一次拿到降级后的 identity
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/feishu_users.py`
   - `OrgMember` 分支现在优先输出：
     - `external_id`
     - `open_id`
   - `User` 分支现在优先调用：
     - `channel_user_service.get_feishu_delivery_target(...)`
   - 如果拿到 canonical `user_id`，就直接把它输出给搜索结果
   - 只有取不到 canonical target 时，才退回用户行上的旧 `feishu_*`
4. 结果：
   - `feishu_user_search -> messaging fallback`
     现在也开始共享同一条 canonical identity 主干
   - 搜索入口不再把已经修好的发送主链重新压回 legacy identity
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_user_search.py`
     新增断言：
     - provider-backed `OrgMember.external_id/open_id` 必须原样体现在搜索结果里
     - `User` 分支若 canonical delivery target 已能给出 `user_id`，搜索结果必须优先输出该 `user_id`
   - 红阶段已确认旧实现真实失败：
     - directory 分支丢失 `user_id`
     - users 分支只输出 legacy `open_id`

### 本轮补充验证结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `56 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/channel_user_service.py \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/app/services/agent_tool_domains/feishu_users.py \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/services/test_feishu_user_search.py
```

结果：

- `All checks passed`

### 继续收尾：Feishu owner/creator fallback 也已回到 canonical delivery target（2026-04-15）

1. 在把：
   - relationship member fallback
   - session user fallback
   - `feishu_user_search` fallback
   这些 Feishu 出站补偿入口逐步收口后，继续往下扫 `send_feishu_message()`，又发现 owner/creator 分支还残留一条旧链：
   - 命中 agent owner / creator 时，
     直接读取：
     - `User.feishu_user_id`
     - `User.feishu_open_id`
   - 没有先经过：
     - `channel_user_service.get_feishu_delivery_target(...)`
2. 风险：
   - 如果 owner 用户行上还保留旧 `open_id`
   - 但平台侧已经通过 `external_identities` 或 canonical provider mapping 知道稳定 `user_id`
   - 旧实现仍会把 owner fallback 锁在：
     - `open_id`
   - 结果就是：
     - 发信成功可能还停在 transport identity
     - tool args 回填继续写 `open_id`
     - session capture / pending-reply 也会被拖回非 canonical 身份
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
   - owner/creator fallback 现先尝试：
     - `channel_user_service.get_feishu_delivery_target(db, user=_owner_user)`
   - 如果该 canonical target 已能给出：
     - `user_id`
     就优先走 `user_id`
   - 只有拿不到 canonical target 时，才退回：
     - `user.feishu_user_id`
     - `user.feishu_open_id`
   - 同时 owner email 反查 `OrgMember` 时，也改为走：
     - `_stable_feishu_user_id(...)`
     - `_stable_feishu_open_id(...)`
   - owner 临时 member 现会同时带上：
     - `external_id`
     - `open_id`
     - `tenant_id`
     避免后续发送与 org-sync fallback 再次丢字段语义
4. 结果：
   - `owner/creator fallback -> send_feishu_message -> tool args backfill -> session capture`
     这条分支现已重新并回 canonical identity 主干
   - owner 命中后不再因为用户行里残留旧 `open_id` 而把后续链路拉回 transport identity
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_resolution.py`
     新增断言：
     - owner fallback 命中时，
       即使 owner 用户行只留旧 `open_id`，
       只要 `get_feishu_delivery_target(...)` 能给出 `user_id`，
       最终发送和 tool args 都必须走 `user_id`
   - 红阶段已确认旧实现真实失败：
     - `KeyError: 'user_id'`

### 继续收尾：Gateway relationship context 也已识别 provider-backed Feishu channel（2026-04-15）

1. 继续沿着“同一 canonical 联系人是否在所有消费面都被看见”这条线扫 `api/gateway.py`，又发现一处更前置的只读断点：
   - gateway 在组装 relationship context 时，
     原先只用：
     - `member.feishu_user_id`
     - `member.feishu_open_id`
     判断联系人是否具备 Feishu 通道
   - 但发送主干已经开始接受：
     - `OrgMember.external_id`
     - `OrgMember.open_id`
2. 风险：
   - 某个 OrgMember 如果已经迁到 provider-backed canonical 字段
   - 但旧 `feishu_*` 字段为空
   - gateway 关系上下文会把它误判成：
     - “没有 Feishu 通道”
   - 这会让：
     - 上游关系视图
     - prompt/context 注入
     - 后续工具分支选择
     在只读入口再次出现旧/新双轨
3. 本轮已完成修复：
   - `backend/app/api/gateway.py`
   - gateway 关系上下文现改为统一接受：
     - `member.external_id or member.feishu_user_id`
     - `member.open_id or member.feishu_open_id`
   - 只使用 provider-backed canonical 字段的 Feishu 联系人，
     现在也会正确暴露：
     - `channels=["feishu"]`
4. 结果：
   - canonical Feishu org member 不会再在 gateway 入口被“看不见”
   - 关系视图与真实发送主干在 Feishu 通道可达性判断上开始对齐
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_outbound_identity_source.py`
     新增 source contract：
     - gateway relationship context 必须同时接受 `external_id/open_id`
   - 红阶段已确认旧实现真实失败：
     - 断言缺失 provider-backed 字段判断

### 本轮补充验证结果（2026-04-15）

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `51 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/app/api/gateway.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_feishu_outbound_identity_source.py
```

结果：

- `All checks passed`

### 继续收尾：Feishu name-based delivery target 解析也已优先回到 canonical user_id（2026-04-15）

1. 在把：
   - `send_feishu_message` 的 open-id success path
   - `pending_reply` 的 outbound identity 回写
   这些出站写侧 contract 收齐之后，继续往更前面的名字解析入口看，又发现 `backend/app/services/channel_user_service.py` 里还有一处旧优先级残差：
   - `resolve_feishu_delivery_target_by_name()`
     之前会优先拿：
     - 旧 Feishu session 的 `external_conv_id`
   - 如果这条历史 session 还是：
     - `feishu_p2p_ou_xxx`
     就会直接返回：
     - `open_id`
   - 它不会先检查这个 session 绑定的 `User` 是否已经有：
     - provider-backed `user_id`
2. 风险：
   - session 主干和用户主干其实已经知道“这个人是谁”
   - 但 name-based resolve 还是可能被历史 `open_id` 压回旧 transport identity
   - 后果是：
     - 出站 tool args 继续偏向 `open_id`
     - pending-reply / sender match / delivery target contract 又重新被拖回双轨
3. 本轮已完成修复：
   - `backend/app/services/channel_user_service.py`
     新增 `_session_row_user(...)`
   - `resolve_feishu_delivery_target_by_name()` 现会在解析出 session 的 `external_conv_id` 后，
     先检查该 session 绑定的 `User`
   - 如果该 `User` 的 canonical delivery target 已能给出：
     - `user_id`
     则优先返回 canonical `user_id`
   - 同时，名字解析最终落到 tenant `User` 时，
     也不再只看：
     - `user.feishu_user_id`
     - `user.feishu_open_id`
     而是会先走：
     - `get_feishu_delivery_target(...)`
     以便复用 provider-backed `user_id`
   - 只有在 canonical target 不存在、或者仍只有 `open_id` 时，
     才继续回退到 session-derived identifier
4. 结果：
   - `name -> session user binding / tenant user fallback -> canonical delivery target`
     这条链重新回到单一主干
   - 历史 session 中遗留的 app-scoped `open_id`
     不再压过已经存在的稳定 `user_id`
5. 已补红绿测试：
   - `backend/tests/services/test_channel_user_service.py`
     新增断言：
     - 若 session 绑定用户已有 canonical `user_id`，则必须优先返回 `user_id`
     - 若确实没有 `user_id`，才允许继续回退 `open_id`
     - tenant user fallback 若已有 provider-backed `user_id`，也必须优先返回 `user_id`
   - 红阶段已确认旧实现真实失败：
     - 旧实现直接返回 `('ou_app_scoped', 'open_id')`
6. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `48 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/channel_user_service.py \
  backend/tests/services/test_channel_user_service.py \
  backend/app/services/agent_tool_domains/messaging.py
```

结果：

- `All checks passed`

### 继续收尾：Feishu user search fallback 也不再因 regex 过窄退回 open_id（2026-04-15）

1. 在把：
   - `name -> session user binding`
   - `name -> tenant user fallback`
   这两层 canonical delivery target 优先级收齐之后，继续往 `send_feishu_message` 的搜索补偿分支看，又发现一处更细的解析残差：
   - `backend/app/services/agent_tool_domains/messaging.py`
     在 `resolve_feishu_delivery_target_by_name()` 失败后，
     会回退到：
     - `_feishu_user_search(...)`
   - 但它解析搜索结果时使用的 regex 之前是：
     - ``user_id: `([A-Za-z0-9]+)` ``
   - 这会漏掉带下划线的 `user_id`，例如：
     - `u_staff_123`
   - 一旦 `user_id` 抓取失败，而搜索结果里又同时有 `open_id`，
     当前逻辑就会错误退回：
     - `open_id`
2. 风险：
   - 搜索补偿分支本来是为了把名字解析拉回 canonical 身份
   - 但 regex 过窄会让同一个搜索结果里已经存在的 `user_id`
     再次被忽略
   - 结果是：
     - 第二跳出站继续走 `open_id`
     - `tool_args`
     - pending-reply identity
     - session merge
     这些面又会被错误拉回旧 transport identity
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
     中对 `_feishu_user_search(...)` 结果的提取规则已改为：
     - ``user_id: `([^`]+)` ``
     - ``open_id: `([^`]+)` ``
   - 解析逻辑仍保持：
     1. 优先 `user_id`
     2. 再回退 `open_id`
   - 但不再假定 Feishu 标识只包含纯字母数字
4. 结果：
   - `feishu_user_search -> send_feishu_message fallback`
     这条补偿链现在也开始和前两轮一样优先回到 canonical `user_id`
   - 即使 `user_id` 带 `_`，
     也不会再被 regex 静默漏掉
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_resolution.py`
     新增断言：
     - 当搜索结果同时包含：
       - `user_id: u_staff_123`
       - `open_id: ou_realappscoped123`
       时，第二跳必须优先发送 `user_id`
   - 红阶段已确认旧实现真实失败：
     - `KeyError: 'user_id'`
6. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `49 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/app/services/channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_channel_user_service.py
```

结果：

- `All checks passed`

### 继续收尾：Feishu user search 只给 open_id 时也会继续回拉 canonical user_id（2026-04-15）

1. 在把：
   - `feishu_user_search` 里带下划线的 `user_id`
     正确解析出来之后，
   继续往同一条补偿链深一层看，又发现另一个 residual：
   - 搜索结果有时只会给：
     - `open_id`
   - 但平台用户侧其实已经可以通过：
     - `resolve_feishu_user(provider_open_id=...)`
     找回对应 `User`
   - 再通过：
     - `get_feishu_delivery_target(...)`
     取到 canonical `user_id`
   - 旧实现到这里会直接停在 `open_id`
2. 风险：
   - 名字搜索补偿分支虽然已经知道“这是哪个人”
   - 但如果只看搜索结果表层字段，
     仍然会把第二跳发送、tool args、pending-reply identity 继续锁在：
     - `open_id`
   - 这会让已经存在的 canonical `user_id` 无法在补偿链里被复用
3. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
     在 `_feishu_user_search(...)` 只解析出 `open_id` 时，
     现会继续尝试：
     1. `channel_user_service.resolve_feishu_user(..., provider_open_id=...)`
     2. `channel_user_service.get_feishu_delivery_target(...)`
   - 如果反查出的 canonical target 是：
     - `user_id`
     就会把搜索补偿链从 `open_id` 升级回 canonical `user_id`
4. 结果：
   - `feishu_user_search -> resolve_feishu_user -> get_feishu_delivery_target`
     现在也被接进同一条 canonical identity 主干
   - 搜索分支不再只停在 transport identity
5. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_resolution.py`
     新增断言：
     - 当搜索结果只给 `open_id`，但平台可以通过 `open_id` 反查出 canonical `user_id` 时，
       第二跳必须改用 `user_id`
   - 红阶段已确认旧实现真实失败：
     - `KeyError: 'user_id'`
6. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `50 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/app/services/channel_user_service.py \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_channel_user_service.py
```

结果：

- `All checks passed`

### 继续收尾：Feishu outbound pending-reply identity 也已回到 canonical user_id（2026-04-15）

1. 在把 Feishu session 的 merge / activity read path 收齐之后，继续往 outbound side 看，又发现一处更隐蔽的断点：
   - `backend/app/services/agent_tool_domains/messaging.py::send_feishu_message`
     在部分成功路径下虽然已经把消息发给了正确的 Feishu 收件人，
     但写回 tool args 的仍然可能只有：
     - `open_id`
   - 与此同时，runtime session / `delivery_target_json` / pending-reply 注入主干已经越来越统一到：
     - `user_id`
2. 风险：
   - 同一个 Feishu 人类对象
   - outbound capture 可能记成：
     - `feishu:ou_xxx`
   - inbound session / pending-reply match 则可能认成：
     - `feishu:u_xxx`
   - 结果不是“消息没发出去”，而是“消息发出去了，但后续 delayed reply context 永远匹配不上”
   - 这是典型的 identity contract 双轨残差
3. 这次确认会断的入口有两条：
   - relationship member 已知 stable `user_id`，但 `user_id` 发送失败后，fallback 到 `open_id` 成功
   - 直接 `open_id` 发送成功，但组织成员记录里其实已经有 canonical `user_id`
4. 本轮已完成修复：
   - `backend/app/services/agent_tool_domains/messaging.py`
     新增本地 helper：
     - `_stable_feishu_user_id(...)`
     - `_stable_feishu_open_id(...)`
     - `_backfill_successful_identity(...)`
   - `send_feishu_message` 现在在以下成功路径都会回写 canonical identity：
     1. relationship member 的 `open_id` success
     2. email/phone resolve 出 `open_id` 后 success
     3. direct `open_id` success
     4. org-sync fallback 的 `open_id` success
   - 当调用点已知 stable `user_id` 时，
     即使真正发消息走的是 `open_id`，
     tool args 也会同步补齐：
     - `user_id`
5. 结果：
   - `send_feishu_message -> pending_reply capture -> sender_identity match`
     这条链在“已知 stable Feishu 联系人”的主路径上重新回到单一 canonical identity：
     - `feishu:<user_id>`
   - `open_id` 仍可继续承担 delivery transport 的职责，
     但不再单独决定 pending-reply 记账身份
6. 已补红绿测试：
   - `backend/tests/services/test_feishu_identity_resolution.py`
     新增断言：
     - relationship member 走 `open_id` fallback 成功后，仍必须回写 canonical `user_id`
     - direct `open_id` 成功发送时，若组织成员里已有 stable `user_id`，也必须补回 `user_id`
   - 红阶段已确认旧实现真实失败：
     - `KeyError: 'user_id'`
7. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/services/test_feishu_identity_resolution.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/services/test_feishu_outbound_identity_source.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  -q
```

结果：

- `41 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/services/agent_tool_domains/messaging.py \
  backend/tests/services/test_feishu_identity_resolution.py
```

结果：

- `All checks passed`

### 继续收尾：Feishu activity label 读取也已改用 canonical delivery target（2026-04-15）

1. 在把 Feishu session 的：
   - `conversation_id`
   - `participant_id`
   - `delivery_target_json`
   - `created_at / last_message_at`
   这些写侧与 merge 侧 contract 收齐之后，继续往只读视图排查，又发现 `api/activity.py` 还残留一处“主干已经有标准 identity，读取面却还在猜”的断点：
   - Feishu partner label 之前优先依赖：
     - `first_user_message` 里的 sender prefix
   - 当正文里没有 `[发送者: ...]` 前缀时，
     即使 session 已经带有 canonical `delivery_target_json.user_label`
     也会退回：
     - `📱 飞书用户`
2. 风险：
   - runtime 写入面已经有 canonical sender label
   - 但 activity list 仍可能展示泛化标签
   - 这会形成“session 主干已对，读取视图还在用旧推断”的只读漂移
3. 本轮已完成修复：
   - `backend/app/api/activity.py`
     中的 `_feishu_conversation_partner_name(...)`
     现已增加：
     - `delivery_target_label`
   - Feishu 会话 partner label 现在按以下顺序解析：
     1. sender prefix
     2. canonical `delivery_target_json.user_label`
     3. 泛化 fallback `📱 飞书用户`
4. 结果：
   - Feishu 的 runtime 写入面
   - session-backed activity 读取面
   现在开始共享同一份 canonical sender label contract
5. 已补红绿测试：
   - `backend/tests/api/test_activity_conversation_labels.py`
     新增断言：没有 sender prefix 时，必须回退到 `delivery_target_json.user_label`
   - `backend/tests/api/test_activity_chat_history_sessions.py`
     新增断言：`list_conversations()` 对 Feishu session 必须输出 canonical delivery target label
   - 红阶段已确认旧实现真实失败
6. 本轮补充验证结果：

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/tests/api/test_activity_conversation_labels.py \
  backend/tests/api/test_activity_chat_history_sessions.py \
  backend/tests/services/test_pending_reply_service.py \
  backend/tests/runtime/test_pending_reply_hooks.py \
  backend/tests/architecture/test_session_message_trunk.py \
  backend/tests/architecture/test_session_identifier_contract.py \
  backend/tests/architecture/test_channel_message_contract.py \
  -q
```

结果：

- `45 passed`

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  backend/app/api/activity.py \
  backend/tests/api/test_activity_conversation_labels.py \
  backend/tests/api/test_activity_chat_history_sessions.py
```

结果：

- `All checks passed`
