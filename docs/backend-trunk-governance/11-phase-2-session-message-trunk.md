# Phase 2: 会话与消息主干收口

## 1. 本阶段目标

建立一条唯一的 session/message 主线：

- 所有 `ChatSession` 由统一 service/factory 创建
- 所有 `conversation_id` 由统一 service 决定
- 所有入口只消费 session，不直接造 session

---

## 2. 当前真实问题

当前 `ChatSession` 创建点分散在：

- websocket
- agent-to-agent messaging
- trigger_daemon
- heartbeat
- supervision_reminder
- 各渠道流

这意味着：

1. session 归一化逻辑分散
2. legacy merge 逻辑分散
3. source/channel 语义可能漂移
4. recall / t0 / memory 读取结果可能不稳定

---

## 3. 唯一保留路径

本阶段要确立：

- 一个统一 `session_service` 或 `session_factory`

它负责：

1. find-or-create session
2. 归并 legacy session
3. 生成 canonical `conversation_id`
4. 规范 `source_channel`
5. 规范 `agent_id / peer_agent_id / user_id / participant_id`

---

## 4. 本阶段不做什么

- 不改 prompt 主干
- 不改 delegation 语义
- 不重做 trigger 触发策略

---

## 5. 执行步骤

### W1 建架构测试

新增：

- `backend/tests/architecture/test_session_trunk.py`

必须先写断言：

1. 除统一 session service 外，其他模块不得直接 `ChatSession(...)`
2. `conversation_id` 只能由统一 service 生成/归一化

### W2 盘点写点

执行：

```bash
rg -n "ChatSession\\(|conversation_id=|source_channel=" backend/app
```

把结果分四类：

1. web chat
2. channel inbound/outbound
3. trigger/task/heartbeat/internal
4. A2A / agent internal

### W3 设计统一 session 契约

统一输入参数建议：

- `agent_id`
- `source_channel`
- `external_conv_id`
- `peer_agent_id`
- `user_id`
- `participant_id`
- `title`
- `delivery_target`

统一输出：

- `ChatSession`
- `conversation_id`

### W4 迁移最核心入口

优先顺序：

1. websocket
2. A2A messaging
3. trigger_daemon
4. 渠道 session helper
5. heartbeat/task

### W5 清退直接创建

删掉：

- 入口模块里的直接 `ChatSession(...)`
- 分散的 legacy merge 逻辑

### W6 局部回归

建议至少覆盖：

- websocket 历史会话续接
- A2A session 复用
- 渠道会话 merge
- trigger internal session 持久化

---

## 6. 风险与下游影响

### 对 T3 的影响

- memory recall 可能因为 conversation_id 变化而漂移

控制：

- 做 session transcript 回归
- 做 summary / recall 回归

### 对 T6 的影响

- A2A 和 delegation 的 session 关联会改变

控制：

- Phase 3 前先锁定 agent-internal source/channel 约定

---

## 7. 退出条件

1. 全仓 `ChatSession(...)` 创建点只剩统一 service
2. 各入口不再自己拼 `conversation_id`
3. 渠道、web、A2A、trigger 的 session 都经过统一路径
4. 局部回归通过

---

## 8. 第一轮执行范围（2026-04-14）

本阶段不做“大爆破”，先做第一轮高风险入口收口。

### 第一轮必须覆盖

1. `backend/app/api/websocket.py`
2. `backend/app/services/agent_tool_domains/messaging.py` 的 A2A session
3. `backend/app/services/trigger_daemon.py`
4. `backend/app/services/heartbeat.py`
5. `backend/app/services/task_executor.py`

### 第一轮可暂缓

1. `backend/app/api/gateway.py`
2. `backend/app/services/channel_delivery_service.py`
3. `backend/app/api/chat_sessions.py`
4. 仍处于 legacy 清退链上的 `supervision_reminder.py`

### 第一轮的完成标准

1. 以上五个核心入口不再直接 `ChatSession(...)`
2. 新增统一 session service / factory
3. A2A pair session 的 agent 排序、owner 归属、`conversation_id` 归一规则被封装
4. websocket 默认 session 创建与 session 复用逻辑被封装

---

## 9. 本轮实际进度（2026-04-14）

### 已完成

1. 已新增 `backend/app/services/session_service.py`
2. 已新增第二条主干架构测试：
   - `backend/tests/architecture/test_session_message_trunk.py`
3. 已新增 session service 行为测试：
   - `backend/tests/services/test_session_service.py`
4. 第一轮已完成迁移的入口：
   - `backend/app/api/websocket.py`
   - `backend/app/services/agent_tool_domains/messaging.py`
   - `backend/app/services/trigger_daemon.py`
   - `backend/app/services/heartbeat.py`
   - `backend/app/services/task_executor.py`
   - `backend/app/services/channel_session.py`
   - `backend/app/services/channel_delivery_service.py`
5. 已统一的能力：
   - web session 复用与默认创建
   - A2A pair session 的 canonical 查找/创建
   - internal reflection session 创建
   - `conversation_id` 统一取 session UUID

### 本轮明确未完成

1. `backend/app/api/gateway.py`
2. `backend/app/api/chat_sessions.py`
3. 其他仍在 legacy 清退链上的入口

### 当前判定

Phase 2 当前状态应视为：

- `session factory 第一轮已建立`
- `核心高风险入口已迁移`
- `全仓唯一 service 目标尚未完全完成`

---

## 10. 第二轮执行范围（2026-04-14）

第二轮只收两个剩余核心入口，不扩大战线：

1. `backend/app/api/gateway.py`
2. `backend/app/api/chat_sessions.py`

### 第二轮目标

1. 这两个入口不再直接 `ChatSession(...)`
2. `gateway` 的 agent-to-agent session 归并改走 `session_service`
3. `gateway` 的 legacy `gw_agent_*` conversation 迁移，收口到 canonical session UUID
4. `api/chat_sessions.py` 的手动建会话入口改走 `create_chat_session`

### 第二轮红线

1. 不在入口层继续扩散 session UUID 生成逻辑
2. 不新增新的永久兼容数据结构
3. 不因为 session 收口破坏现有 transcript / history 查询面

### 第二轮完成标准

1. 架构测试把 `gateway` 与 `api/chat_sessions.py` 纳入约束
2. 行为测试覆盖手动创建 session 走统一 factory
3. 第二轮改动通过局部回归与主干联合回归

### 第二轮实际进度（2026-04-14）

#### 已完成

1. `backend/app/api/gateway.py` 已切到 `find_or_create_agent_pair_session`
2. `backend/app/api/chat_sessions.py` 已切到 `create_chat_session`
3. `gateway` 新的 A2A conversation 写入已统一为 canonical session UUID

### 第二轮补口完成：`activity` 会话历史读取归一（2026-04-14）

1. `backend/app/api/activity.py` 已完成主路径切换：
   - `list_conversations()` 先读 `ChatSession`
   - `web / feishu / slack / discord` 统一视为 session-backed channel conversations
   - session-backed 会话统计与最后一条消息统一按 `ChatMessage.conversation_id == str(session.id)` 读取
2. `get_conversation_messages()` 已完成分流：
   - 先把 `conv_id` 解析为 UUID 并查询 `ChatSession`
   - `source_channel == "agent"` 继续走 agent participant sender-name 逻辑
   - 其余 session-backed channels 统一走 canonical session UUID 读取，并去掉 `[发送者: ...]` 前缀
3. legacy 边界已收紧为只读 fallback：
   - `web_ / feishu_ / slack_ / discord_` 仅在直接传入旧前缀 `conv_id` 时兜底读取
   - 会话列表主路径不再按这些 legacy 前缀扫描 `ChatMessage`
4. 已新增/通过的测试：
   - `backend/tests/api/test_activity_chat_history_sessions.py`
   - `backend/tests/api/test_activity_conversation_labels.py`
   - `backend/tests/architecture/test_session_message_trunk.py`
   - `backend/tests/services/test_pending_reply_service.py`
   - `backend/tests/runtime/test_pending_reply_hooks.py`
5. 当前阶段结论：
   - session-backed channel 的“写 canonical UUID、读 legacy prefix”断层已补上
   - `activity` 读面现已回到 `ChatSession -> ChatMessage(conversation_id=session.id)` 这条唯一主干

### 第二轮补口继续推进：Slack pending-reply identity 对齐（2026-04-14）

1. 已确认并修复 `backend/app/services/pending_reply_service.py` 中的 Slack identity 断层：
   - 出站待回复上下文记录的是 `slack:<sender_id>`
   - 入站 session 的 `external_conv_id` 真实形状是：
     - `slack_<channel_id>_<sender_id>`
     - `slack_dm_<sender_id>`
   - 旧逻辑错误地把整段 payload 当成 identity，导致待回复上下文无法命中
2. 当前已统一为：
   - `slack_<channel_id>_<sender_id> -> slack:<sender_id>`
   - `slack_dm_<sender_id> -> slack:<sender_id>`
3. 这次修复的意义：
   - pending-reply 的 capture / lookup 再次对齐同一条 canonical identity contract
   - 避免“session 已 canonical，但跨会话回复关联仍失效”的隐形断层

### 第二轮补口继续推进：pending-reply 注入链统一走 session identity（2026-04-14）

1. `backend/app/services/trigger_daemon.py` 与 `backend/app/api/feishu.py` 已不再优先按
   `external_conv_id` 手动推 sender identity
2. 当前统一改为调用：
   - `sender_identity_from_session(session_obj)`
3. 这次调整的意义：
   - `delivery_target_json` 若才是最新 canonical identity，会优先被消费
   - trigger 命中链与 Feishu pending-reply 注入链不再各自维护一套“先猜 external_conv_id，再兜 delivery_target”的次序
   - session identity contract 现在从 websocket / hooks / trigger / feishu 四个入口继续收口为同一条主线

### 第二轮补口继续推进：补齐多渠道 session identity contract（2026-04-14）

1. `activity.py` 统一历史视图已纳入更多真实已 canonical 的渠道：
   - `telegram`
   - `wecom`
   - `dingtalk`
   - `wechat_personal`
   - `microsoft_teams`
2. 其中 `telegram / wecom / dingtalk / wechat_personal / microsoft_teams` 的展示名策略已明确：
   - 优先 `delivery_target_json`
   - 缺失时再回退 `User.display_name`
3. `api/dingtalk.py` 与 `api/teams.py` 现已在 `find_or_create_channel_session(...)` 时写入 canonical `delivery_target`
4. `ChannelDeliveryService.identity_from_delivery_target()` 已补齐：
   - `dingtalk -> dingtalk:<user_id>`
   - `microsoft_teams -> microsoft_teams:<sender_id>`
5. `pending_reply_service.sender_identity_from_external_conv_id()` 已补齐：
   - `dingtalk_p2p_<staff_id> -> dingtalk:<staff_id>`
6. 这次修复的意义：
   - canonical session 已经覆盖的渠道，不会再在“历史视图”和“pending-reply identity”两侧掉队
   - `delivery_target_json` 进一步成为跨渠道统一 sender identity contract 的主载体

### 第二轮补口继续推进：session 管理视图渠道命名收口（2026-04-14）

1. `api/chat_sessions.py` 已删除旧的 `teams` 假命名，统一改为 canonical `microsoft_teams`
2. `api/chat_sessions.py` 已把内部非聊天 session 从列表接口显式排除：
   - `trigger`
   - `task`
   - `heartbeat`
3. 这意味着：
   - agent 管理面里的 session 列表，不会再因为旧渠道名漂移漏掉 Teams 会话
   - 也不会再把内部执行 session 混进人工聊天视图
4. 前端 `AgentChatSection.tsx` 也已补上 `microsoft_teams -> common.channels.teams` 的 label 映射，
   避免后端主干已经统一、UI 还显示成“无渠道标签”的半断状态
4. `gateway` 后台 native-agent 链路会把旧 `gw_agent_*` transcript 迁移到 canonical conversation
5. `backend/app/services/supervision_reminder.py` 也已切到统一 session 主干
6. 当前全仓业务层 `ChatSession(...)` 直建点已清零，只剩 `session_service` 本体负责创建
7. `gateway/report_result` 已不再直接回写 legacy `gw_agent_*` conversation id，而是先归一化到 canonical session UUID
8. 第二轮红测试已转绿：
   - `backend/tests/architecture/test_session_message_trunk.py`
   - `backend/tests/api/test_chat_sessions_permissions.py`
   - `backend/tests/services/test_supervision_reminder.py`
   - `backend/tests/api/test_gateway_conversation_contract.py`
9. 第二轮联合回归已通过（含 Phase 1/2 主干相关集）

#### 当前残留

1. `gateway` 主入口已不再保留本地 `gw_agent_*` helper
2. 旧 transcript 归一化现在只剩 `session_service.find_or_create_agent_pair_session()` 一处承担
3. 其余非核心 legacy 入口仍需继续清点，但第二条主干的核心直建点已清掉

#### 后续继续收口（2026-04-14）

1. `backend/app/api/gateway.py`
   本地 `_find_or_create_gateway_agent_pair_session()` helper 已删除。
2. `backend/app/services/session_service.py`
   当前统一承担：
   - canonical pair session 查找/创建
   - legacy `gw_agent_*` transcript 向 canonical session UUID 的归一化
3. `report_result()`、gateway native-agent 背景链路、`send-message` 入口
   现在都只调用 `find_or_create_agent_pair_session()` + `session_conversation_id()`，
   不再在 gateway 入口层保留第二套归一化逻辑。
4. 这意味着 Phase 2 在 gateway 入口层又少了一层“canonical 主干 + compat helper”的双心智。
5. 当前该主干剩余的真实尾巴已进一步缩小为：
   - Feishu session alias 归并层
   而不再是 gateway 主入口或通用 `channel_session.py` 本身。

#### 渠道会话层继续收口（2026-04-14）

1. `backend/app/services/channel_session.py`
   已删除通用参数：
   - `legacy_external_conv_ids`
2. 这意味着 generic channel session helper 现在只承担：
   - 按 canonical `external_conv_id` 查找
   - 若不存在则统一经 `create_chat_session()` 创建
   - 已存在 session 的 user / delivery_target 归位
3. Feishu 的 alias 归并逻辑已收回：
   - `backend/app/services/feishu_identity_maintenance.py::build_feishu_session_lookup_ids()`
   - `backend/app/services/feishu_identity_maintenance.py::find_or_create_feishu_chat_session()`
4. `backend/app/api/feishu.py` 与 `backend/app/services/agent_tool_domains/messaging.py`
   已改为统一调用 `find_or_create_feishu_chat_session()`，不再在调用点显式拼 `legacy_external_conv_ids`
5. `backend/app/api/feishu.py`
   已不再调用 `build_feishu_session_lookup_ids()` 做预读 alias lookup；
   文本消息入口现在会先拿 canonical session，再按 session UUID 读取 history。
6. 当前阶段判断：
   - Phase 2 的 generic 会话主干已经更干净
   - 剩余兼容逻辑已被限制在 `feishu_identity_maintenance.py` 专属边界，而不是污染全渠道 session 主接口

#### 维护路径补齐（2026-04-14）

1. 已新增：
   - `backend/app/db_legacy_gateway_conversation_migration.py`
   - `backend/app/scripts/cleanup_legacy_gateway_conversations.py`
2. 这条维护路径负责把历史 `gw_agent_<source>_<target>` conversation id
   批量回放到 canonical `find_or_create_agent_pair_session()` 主干上。
3. 当前意义是：
   - `session_service.py` 里的 runtime compat 仍保留，避免历史环境立刻断链
   - 但仓库里已经有明确的一次性清尾路径，不再只能依赖运行时长期兜底
4. 已新增测试：
   - `backend/tests/test_db_legacy_gateway_conversation_migration.py`
   - `backend/tests/test_alembic_bootstrap.py`
   - 其中固定了：
     - legacy gateway conversation id 的解析规则
     - pair 去重规则
     - 缺失 agent 时必须跳过，不能乱造 session
     - bootstrap 路径也必须自动执行这条 gateway legacy 数据归并
5. 同时已补一个脚本层护栏：
   - `backend/tests/scripts/test_maintenance_scripts_source.py`
   - 防止维护脚本继续 import 已删除的 `schedule` 模型
   - 并固定 gateway 清尾脚本必须直接复用 DB-level migration helper
6. 随后又继续收口一层：
   - `backend/app/services/session_maintenance.py` 已删除
   - `gw_agent_*` 维护路径现只剩：
     - `db_legacy_gateway_conversation_migration.py`
     - `cleanup_legacy_gateway_conversations.py`
   不再保留中间 async wrapper

#### Feishu 维护路径继续收口（2026-04-14）

1. 已新增：
   - `backend/app/db_legacy_feishu_session_migration.py`
2. 这条维护路径负责把历史 `feishu_p2p_<open_id>` session
   归并到 canonical `feishu_p2p_<user_id>` 上，并在必要时：
   - 复用已存在的 canonical session
   - 回收 legacy session
   - 同步 `chat_messages.conversation_id`
   - 修正 message / session 上的 `user_id`
3. `backend/app/db_bootstrap.py`
   现会在 bootstrap 阶段自动执行：
   - `promote_legacy_gateway_conversations()`
   - `promote_legacy_feishu_sessions()`
   也就是 gateway / Feishu 两条 legacy session 清尾都已有启动期一次性维护路径。
4. `backend/app/scripts/cleanup_duplicate_feishu_users.py`
   已改为直接复用：
   - `merge_duplicate_feishu_users()`
   - `promote_legacy_feishu_sessions()`
   不再保留 `reconcile_feishu_identity_state()` 这类中间 wrapper。
5. `backend/app/services/feishu_identity_maintenance.py`
   已删除：
   - `normalize_feishu_chat_sessions()`
   - `reconcile_feishu_identity_state()`
   现在运行时只保留：
   - `build_feishu_session_lookup_ids()`
   - `find_or_create_feishu_chat_session()`
   用于“未完成清尾前的读取/写入连续性保护”。
6. 已补测试与护栏：
   - `backend/tests/test_db_legacy_feishu_session_migration.py`
   - `backend/tests/test_alembic_bootstrap.py`
   - `backend/tests/scripts/test_maintenance_scripts_source.py`
   - `backend/tests/architecture/test_legacy_session_compat_allowlist.py`
   当前固定：
   - Feishu DB helper 必须可独立完成 legacy session 归并
   - bootstrap 必须自动执行这条 Feishu 清尾路径
   - duplicate-user 维护脚本必须直接走 DB-level helper
   - 已删除的 Feishu wrapper 不允许回流
7. 当前阶段判断：
   - Phase 2 的 Feishu 兼容面已经从“调用点显式拼 alias + 运行时 wrapper + 人工清理脚本”
     收口为“runtime 单点 alias helper + DB-level 一次性清尾路径”
   - 后续真正要继续删的，只剩 runtime 中这层 alias lookup 是否还能继续下沉/删除，而不是再保留第二套维护系统

#### 会话标识契约继续单源化（2026-04-14）

1. 已新增：
   - `backend/app/session_identifiers.py`
2. 当前这一个模块统一承接：
   - legacy gateway `gw_agent_*` conversation id 的构造
   - legacy gateway conversation id 的解析
   - Feishu `feishu_p2p_*` canonical / alias lookup id 的构造
   - Feishu `feishu_p2p_*` external conversation id 的解析
3. 已切到该公共 contract 的运行时 / 维护路径包括：
   - `backend/app/services/session_service.py`
   - `backend/app/db_legacy_gateway_conversation_migration.py`
   - `backend/app/services/feishu_identity_maintenance.py`
   - `backend/app/db_legacy_feishu_session_migration.py`
   - `backend/app/services/pending_reply_service.py`
   - `backend/app/services/channel_user_service.py`
   - `backend/app/api/activity.py`
4. 这意味着：
   - gateway 与 Feishu 两条主干，不再由 runtime helper 和 DB helper 各自保存一份 identifier 规则
   - outbound / inbound / maintenance 三类路径开始共用同一份 session identifier contract
5. 已新增测试：
   - `backend/tests/test_session_identifier_contracts.py`
   - `backend/tests/architecture/test_session_identifier_contract.py`
   当前固定：
   - identifier builder / parser 必须有单一来源
   - `pending_reply_service.py` 与 `channel_user_service.py` 不允许重新手写 `feishu_p2p_` 前缀解析
6. 本轮联合验证结果：
   - `pytest` 主干联合回归：`82 passed`
   - `ruff check`：通过
   说明当前 Phase 2 的 session / Feishu identifier 收口没有把 gateway、bootstrap、pending-reply、channel user resolution 几条相邻链路带偏。
7. 读侧继续收口：
   - `backend/app/api/activity.py` 已新增 `_feishu_conversation_partner_name()`
   - Feishu 会话展示名判断现统一复用 `parse_feishu_p2p_conv_id()`
   - `activity.py` 不允许重新手写 `startswith("feishu_p2p_")`
8. 该读侧收口的针对性验证结果：
   - `pytest`：`58 passed`
   - `ruff check`：通过
   当前说明不仅写路径，连 activity/chat-history 这类读路径也开始共用同一份 session identifier contract。

#### Feishu runtime compat 继续显式化（2026-04-14）

1. `backend/app/services/feishu_identity_maintenance.py`
   内部已把剩余 runtime compat 逻辑拆成显式 helper：
   - `_apply_feishu_session_runtime_updates()`
   - `_merge_legacy_feishu_session_into_canonical()`
   - `_promote_legacy_feishu_alias_session()`
2. 现在 `find_or_create_feishu_chat_session()` 的主干形状已经更清楚：
   - 先找 canonical session
   - 若存在则吞并 legacy alias session
   - 若不存在则尝试提正 legacy alias session
   - 最后才回退到 generic `find_or_create_channel_session()`
3. 这一步的意义不是新增一层 wrapper，
   而是把“主干入口”和“剩余 compat 处理”在同一个文件里明确分层，
   为后续继续把 runtime compat 下沉到 maintenance path 做准备。
4. 已补行为覆盖：
   - canonical 已存在时，legacy alias session 必须被并入 canonical session
   - `chat_messages.conversation_id` 必须改写到 canonical session id
   - canonical session 的 `user_id / title / last_message_at / delivery_target_json` 必须同步更新
5. 针对性验证结果：
   - `pytest`：`25 passed`
   - `ruff check`：通过

#### Gateway pair contract 继续单源化（2026-04-14）

1. `backend/app/session_identifiers.py`
   已新增：
   - `canonicalize_agent_pair_ids()`
2. 这意味着 agent pair session 的 canonical 排序规则，
   不再由以下两条路径各自维护一份：
   - `backend/app/services/session_service.py`
   - `backend/app/db_legacy_gateway_conversation_migration.py`
3. 当前 gateway pair 相关 contract 已统一收口为：
   - pair 排序：`canonicalize_agent_pair_ids()`
   - legacy conversation id build：`build_legacy_gateway_conversation_ids()`
   - legacy conversation id parse：`parse_legacy_gateway_conversation_id()`
4. 已补测试：
   - `backend/tests/test_session_identifier_contracts.py`
   - `backend/tests/architecture/test_session_identifier_contract.py`
   当前固定：
   - `session_service.py` 不允许继续手写 `min/max(..., key=str)`
   - `db_legacy_gateway_conversation_migration.py` 不允许继续手写 `tuple(sorted(..., key=str))`
5. 针对性验证结果：
   - `pytest`：`16 passed`
   - `ruff check`：通过

#### Feishu sender 前缀协议继续单源化（2026-04-14）

1. 已新增：
   - `backend/app/channel_message_contracts.py`
2. 当前这一个模块统一承接：
   - `[发送者: XXX]` / `[发送者：XXX]` 前缀的人名提取
   - 该前缀的显示剥离
3. 已切到公共 sender-prefix contract 的路径包括：
   - `backend/app/api/activity.py`
   - `backend/app/runtime/hooks_setup.py`
4. 这意味着：
   - Feishu 文本消息写入时形成的 sender prefix 约定
   - activity read path 的展示解析
   - pending-reply hook 的 originator 提取
   开始共用一份协议解释，不再各自手写字符串切片 / regex。
5. 已补测试：
   - `backend/tests/test_channel_message_contracts.py`
   - `backend/tests/architecture/test_channel_message_contract.py`
   - `backend/tests/api/test_activity_conversation_labels.py`
   - `backend/tests/runtime/test_pending_reply_hooks.py`
6. 针对性验证结果：
   - `pytest`：`9 passed`
   - `ruff check`：通过
7. sender prefix 的写侧也已并入同一份 contract：
   - `backend/app/channel_message_contracts.py` 已新增 `prefix_message_with_sender_label()`
   - `backend/app/api/feishu.py` 不再自己拼接 `[发送者: ...]`
8. 这意味着 Feishu sender prefix 现在已经形成完整单源协议：
   - 写入：`prefix_message_with_sender_label()`
   - 读取标签：`extract_sender_label_from_message()`
   - UI 展示剥离：`strip_sender_label_prefix()`
9. 该补充收口的针对性验证结果：
   - `pytest`：`7 passed`
   - `ruff check`：通过

#### Web 历史读取已接回 canonical session 主干（2026-04-14）

1. `backend/app/services/session_service.py`
   已新增只读 helper：
   - `find_web_chat_session()`
2. 这条 helper 负责：
   - 按 `requested_session_id` 查找指定 web session
   - 未指定时回到当前 user/agent 的最新 web session
   - 不创建新 session
3. `backend/app/api/websocket.py::get_chat_history()`
   已不再按 legacy `web_{current_user.id}` conversation id 直读消息；
   现在会：
   - 先 `find_web_chat_session()`
   - 再按 canonical session UUID 读取 `ChatMessage.conversation_id`
4. 这一步修掉的是一个真实断层：
   - websocket 主写链已经切到 canonical session UUID
   - 但历史读链之前还停在 legacy `web_{user_id}` conversation id
   - 现在读写两边已重新回到同一条主干
5. 已补测试与护栏：
   - `backend/tests/api/test_chat_history_permissions.py`
   - `backend/tests/services/test_session_service.py`
   - `backend/tests/architecture/test_session_message_trunk.py`
   当前固定：
   - `get_chat_history()` 必须按 canonical session UUID 读取消息
   - `websocket.py` 不允许重新出现 `f"web_{current_user.id}"`
6. 针对性验证结果：
   - `pytest`：`10 passed`
   - `ruff check`：通过

#### Web external conversation id 也开始单源化（2026-04-14）

1. `backend/app/services/web_session_contract.py`
   已新增：
   - `parse_web_external_conv_id()`
2. 当前 web external conversation id 的 contract 已明确分成：
   - build：`canonical_web_external_conv_id()`
   - parse：`parse_web_external_conv_id()`
3. 已切到该 parse contract 的路径包括：
   - `backend/app/services/web_session_contract.py`
   - `backend/app/services/pending_reply_service.py`
4. 这意味着：
   - web session 的 external conv id 回绑判断
   - pending-reply 里的 sender identity 解析
   开始共用一份 `web_` 前缀解释，不再各自 `startswith("web_")`
5. 已补测试与护栏：
   - `backend/tests/services/test_web_session_contract.py`
   - `backend/tests/services/test_pending_reply_service.py`
   - `backend/tests/architecture/test_channel_message_contract.py`
6. 针对性验证结果：
   - `pytest`：`30 passed`
   - `ruff check`：通过

#### Trigger runtime session identity 透传（2026-04-15）

1. 新发现的断点不是 `ChatSession` 直建，而是 runtime 入口语义丢失：
   - `trigger_daemon.py` 虽然已经按主干创建 `source_channel="trigger"` 的 Reflection Session
   - 但它调用 `call_llm(...)` 时之前没有透传 `session_source / session_channel`
   - `call_llm()` 的默认值因此会把它补成 `web / web`
2. 风险：
   - unified invoker 看见的是一个“伪装成 web”的 trigger session
   - 后续基于 `session_context.source` 的 routing / hooks / contract 判断会继续错位
3. 本轮已完成修复：
   - `backend/app/services/trigger_daemon.py`
     已显式传入：
     - `session_source="trigger"`
     - `session_channel="trigger"`
4. 已新增护栏：
   - `backend/tests/api/test_websocket_call_llm.py`
     固定 `call_llm()` 在显式传入 source/channel 时，必须把它们写进 runtime request
   - `backend/tests/architecture/test_session_message_trunk.py`
     固定 `gateway / feishu / trigger_daemon` 这类非 web 调用方必须显式透传自己的 session contract
5. 结果：
   - trigger 会话从 DB session 层到 runtime session 层，现在终于是同一套身份语义
6. 针对性验证结果：
   - `pytest`：`6 passed`
   - 扩展回归：`101 passed`
   - `ruff check`：通过

#### Gateway runtime compat bridge 不再只改一半（2026-04-15）

1. 继续追 `session_service.py` 时发现一处隐形断层：
   - `_normalize_legacy_agent_pair_transcripts()`
     之前只会把 legacy `gw_agent_*` transcript 里的 `ChatMessage.conversation_id`
     改写到 canonical session UUID
   - 但同链路上的 `GatewayMessage.conversation_id`
     之前不会一起改
2. 风险：
   - 同一对 agent 的 transcript 已进入 canonical session
   - gateway 队列消息却还挂 legacy id
   - 结果会形成“历史查询走 canonical、回执队列还停 legacy”的半收口状态
3. 本轮已完成修复：
   - `backend/app/services/session_service.py`
     现会同时归一化：
     - `ChatMessage.conversation_id`
     - `GatewayMessage.conversation_id`
4. 已补测试：
   - `backend/tests/services/test_session_service.py`
     现在明确要求 runtime compat bridge 两张表都必须一起改写
5. 针对性验证结果：
   - `pytest`：`23 passed`
   - `ruff check`：通过

#### Gateway runtime timestamp 语义已与 DB helper 对齐（2026-04-15）

1. 在补完 conversation id 归一化后继续盘点，又发现一个更隐蔽的断层：
   - `session_service.find_or_create_agent_pair_session()`
     运行期虽然会把 legacy `gw_agent_*` transcript 迁到 canonical session
   - 但之前不会像 DB helper 那样一起回填：
     - `created_at`
     - `last_message_at`
2. 风险：
   - 同样一批 legacy transcript
   - 走 DB-level migration helper 时，session 排序时间是旧会话真实边界
   - 走 runtime compat bridge 时，session 排序时间却可能是“当前创建时间 + 空 last_message_at”
3. 本轮已完成修复：
   - `backend/app/services/session_service.py`
     已新增 legacy pair timestamp 边界读取
   - 当前运行期归一化会把 canonical session 的：
     - `created_at`
     - `last_message_at`
     对齐到 legacy transcript + gateway queue rows 的真实时间边界
4. 这意味着：
   - gateway runtime bridge
   - `db_legacy_gateway_conversation_migration.py`
   现在开始对同一批 legacy transcript 输出一致的 session 排序语义
5. 已补测试：
   - `backend/tests/services/test_session_service.py`
6. 针对性验证结果：
   - `pytest`：`8 passed`
   - 扩展回归：待后续继续并入总集

#### Gateway DB helper 也已补齐 existing-session 的 created_at 回拉（2026-04-15）

1. 在把 runtime bridge 的时间边界补齐后继续对照，发现 DB helper 仍有一个残差：
   - `db_legacy_gateway_conversation_migration.py`
     在“已有 canonical session”场景下
     之前只会更新：
     - `last_message_at`
   - 不会把 `created_at`
     回拉到 legacy transcript 的最早时间
2. 风险：
   - runtime bridge 会把 session 起点时间回拉
   - DB migration helper 却不会
   - 两条收敛路径对同一批历史消息会产出不同的 session 排序语义
3. 本轮已完成修复：
   - `backend/app/db_legacy_gateway_conversation_migration.py`
     现在在复用已有 canonical session 时，
     也会把 `created_at` 对齐到 legacy 最早时间
4. 结果：
   - gateway runtime bridge
   - gateway DB migration helper
   现在已在以下三个关键面重新一致：
   - `conversation_id`
   - `created_at`
   - `last_message_at`
5. 已补测试：
   - `backend/tests/test_db_legacy_gateway_conversation_migration.py`
6. 针对性验证结果：
   - `pytest`：`36 passed`
   - `ruff check`：通过

#### Feishu alias merge 开始保住 session identity 字段（2026-04-15）

1. 继续排查 `feishu_identity_maintenance.py` 时又发现一处“删 alias 但没完整搬家”的尾巴：
   - legacy alias session 合并进 canonical session 时
   - 之前只保：
     - `user_id`
     - `title`
     - `last_message_at`
   - 但不会保：
     - `participant_id`
     - `delivery_target_json`
2. 风险：
   - 会话虽然被 canonical 化了
   - 但 sender identity / 渠道路由所需字段可能静默丢失
   - 这种问题不会立刻报错，只会在展示和后续 reply routing 上慢慢漂移
3. 本轮已完成修复：
   - `backend/app/services/feishu_identity_maintenance.py`
     合并 legacy alias session 时，现会把以下字段在缺失时补回 canonical session：
     - `participant_id`
     - `delivery_target_json`
4. 已补测试：
   - `backend/tests/services/test_feishu_identity_maintenance.py`
5. 针对性验证结果：
   - `pytest`：`27 passed`
   - `ruff check`：通过

#### Feishu DB-level migration 与 runtime merge 已开始对齐（2026-04-15）

1. 在补完 runtime merge 后继续盘点，发现 DB-level 维护路径仍存在同类断层：
   - `backend/app/db_legacy_feishu_session_migration.py`
     之前在把 legacy alias session 合并进 canonical session 时，
     还不会补：
     - `participant_id`
     - `delivery_target_json`
   - 同时也不会把 canonical session 的 `user_id`
     以及被迁移 `chat_messages.user_id`
     一起对齐到 legacy alias session 对应的真实 user
2. 风险：
   - 运行期 merge 和启动期/维护脚本 merge 会产出两套不同结果
   - 看起来“都完成 canonical 化了”，但 owner / participant / delivery target 语义其实继续分叉
3. 本轮已完成修复：
   - `backend/app/db_legacy_feishu_session_migration.py`
     现在在列存在时会同步补齐：
     - `participant_id`
     - `delivery_target_json`
     - `chat_sessions.user_id`
     - `chat_messages.user_id`
4. 这意味着：
   - runtime merge
   - bootstrap migration
   - maintenance script
   三条 Feishu alias 收敛路径，开始真正输出同一套 canonical session 结果
5. 已补测试：
   - `backend/tests/test_db_legacy_feishu_session_migration.py`
   - `backend/tests/test_alembic_bootstrap.py`
6. 针对性验证结果：
   - `pytest`：`24 passed`
   - `ruff check`：通过
