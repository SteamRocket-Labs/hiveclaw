# 后端主干治理执行总览

> 这不是讨论稿，而是一组可以直接执行的治理文档。
> 目标：把后端从“多代架构叠层并存”收口为“单一主干系统 + 明确分支接入”的结构。

## 1. 本目录怎么用

阅读顺序固定如下：

1. [01-trunk-catalog.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/01-trunk-catalog.md)
2. [02-dependency-and-break-risk-map.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/02-dependency-and-break-risk-map.md)
3. [03-detection-and-evidence-playbook.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/03-detection-and-evidence-playbook.md)
4. Phase 文档，按编号顺序执行
5. [20-master-regression-plan.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/20-master-regression-plan.md)
6. [21-branch-repair-order.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/21-branch-repair-order.md)

## 2. 执行纪律

整个治理过程必须满足：

1. 先修主干，后修分支
2. 先写架构测试，再删旧链路
3. 每条主干修完先做局部回归
4. 所有主干修完后先做全量主干回归，再进入分支修复
5. 不允许新增永久兼容层

## 3. 当前主干顺序

```text
T0 基础设施
  -> T1 统一执行
    -> T2 工具运行时
    -> T3 Prompt / Context / Memory
    -> T4 会话与消息
      -> T5 自主触发
      -> T6 协作与委派
        -> B1/B2/B3 分支层
```

这条顺序不是随意安排的，而是按“下游是否建立在上游之上”来定。

## 4. 当前建议的执行阶段

## 4.1 当前执行状态（2026-04-14）

### 已完成

- Phase 1 第一轮收口已落地：
  - `api/schedules.py` 已切到 `AgentTrigger` 主干
  - legacy schedule 数据迁移已下沉到 DB migration 层，旧 `AgentSchedule` 数据会被迁入 trigger surface
  - schedule manual run 改为 `manual pending -> trigger_daemon`
  - 兼容 `schedule_run` 历史日志，前端历史查询不断
- Phase 1 回归已通过：
  - 自主触发主干架构测试
  - `trigger_daemon / scheduler / supervision_reminder / heartbeat / prompt_eval / memory_integration` 相关回归

### 当前未完成

- Phase 1 在代码层已闭环；运行层仍需应用 Alembic 头 `drop_legacy_agent_schedules_0414`

代码仓内已无 Phase 1 legacy 尾巴，剩余是数据库升级执行动作。

### 当前进行中

- Phase 2 会话与消息主干
  - 统一 `session_service` 已落地
  - 第一轮迁移已覆盖：`websocket`、A2A、`trigger/heartbeat/task`、`channel_session`、`channel_delivery_service`
  - 第二轮已完成：`gateway`、`api/chat_sessions.py`
  - 扩展清理已完成：`supervision_reminder.py`
  - 新写入主路径已不再由这两个入口直接 `ChatSession(...)`
  - `gateway` 新的 agent-to-agent conversation 已收口到 canonical session UUID
  - `gateway/report_result` 对旧 `gw_agent_*` 的处理已变成“先归一化再写 canonical UUID”，不再直接写 legacy conversation id
  - `gateway` 本地 pair-session compat helper 已删除，legacy transcript canonical 化现统一下沉到 `session_service.find_or_create_agent_pair_session()`
  - `gateway/report_result`、native-agent 背景链路与 send-message 入口都只再调用 canonical `session_service`
  - `channel_session.py` 已删除通用参数 `legacy_external_conv_ids`
  - Feishu alias 归并已收回 `feishu_identity_maintenance.py`，`api/feishu.py` 与 `messaging.py` 不再在调用点显式传 legacy alias 参数
  - `api/feishu.py` 已不再自己做 alias 预读；文本消息入口现先取 canonical session，再按 session UUID 读取 history
  - 已补 `db_legacy_gateway_conversation_migration.py` + `cleanup_legacy_gateway_conversations.py`，`gw_agent_*` 现已有 DB 级一次性清尾路径，不再只能依赖运行时 compat
  - `session_maintenance.py` 已删除，gateway legacy 维护链不再保留中间 wrapper
  - 已补 `db_legacy_feishu_session_migration.py`，启动期会自动把 `feishu_p2p_<open_id>` 归并到 canonical `feishu_p2p_<user_id>`
  - `cleanup_duplicate_feishu_users.py` 已直接复用 DB-level Feishu session normalization helper，`normalize_feishu_chat_sessions()` / `reconcile_feishu_identity_state()` 已删除
  - 已补 `session_identifiers.py` 作为 gateway / Feishu session identifier 公共 contract，runtime、maintenance、pending-reply、channel user resolution 开始复用同一套 build/parse 规则
  - `api/activity.py` 的聊天历史读取已切回 `ChatSession -> ChatMessage(conversation_id=session.id)` 主干，旧前缀 `web_/feishu_/slack_/discord_` 仅保留只读 fallback
  - `pending_reply_service.py` 的 Slack sender identity 也已对齐 canonical contract，`slack_<channel_id>_<sender_id>` / `slack_dm_<sender_id>` 都会归一成 `slack:<sender_id>`
  - `trigger_daemon.py` 与 `api/feishu.py` 的 pending-reply 注入链也已统一走 `sender_identity_from_session()`，不再各自手写 `external_conv_id -> identity` 优先级
  - `activity.py` 已把 `telegram / wecom / dingtalk / wechat_personal / microsoft_teams` 也纳入 canonical session 历史视图；`api/dingtalk.py` 与 `api/teams.py` 现会写入标准 `delivery_target_json`
  - `api/chat_sessions.py` 也已完成 `microsoft_teams` 命名收口，并显式排除 `trigger / task / heartbeat` 内部 session；前端 `AgentChatSection.tsx` 已补齐 Teams 渠道标签映射
  - `memory/t2_store.py` 也已把 `microsoft_teams` 归入 human source bucket，避免同一条 Teams 对话在记忆权重层继续沿用旧命名分叉
  - `services/session_recall.py` 现已把 `task` 纳入 internal-channel 排除集合，内部执行 session 不会再混进 cross-session recall
  - 当前全仓业务层 `ChatSession(...)` 直建点已清零，仅保留 `session_service` 本体
  - 当前进入收尾观察阶段，后续只继续删除临时兼容读口
  - 2026-04-15 已完成：`runtime/context_budget.py` 的 cheap-route 禁用集合已从旧 `schedule` 改为真实的 `trigger` 主干语义；内部执行 session 的模型路由约束现重新对齐 `trigger / task / heartbeat / agent`
  - 2026-04-15 已完成：`trigger_daemon.py` 调 `call_llm(...)` 时显式透传 `session_source="trigger"` 与 `session_channel="trigger"`；trigger runtime 不再以默认 `web/web` 身份进入统一 invoker
  - 2026-04-15 已完成：`session_service.py` 的 gateway runtime compat bridge 现会同时归一化 `ChatMessage.conversation_id` 与 `GatewayMessage.conversation_id`；同一对 agent 的 transcript 与队列消息不再出现“半 canonical、半 legacy”
  - 2026-04-15 已完成：`feishu_identity_maintenance.py` 在 legacy alias session 合并进 canonical session 时，现会保住 `participant_id` 与可复用的 `delivery_target_json`，避免会话身份字段静默丢失
  - 2026-04-15 已完成：`db_legacy_feishu_session_migration.py` 现已与 runtime merge 对齐；启动期/维护脚本在合并 legacy Feishu alias session 时，也会保住 `participant_id / delivery_target_json` 并把 canonical session 的 `user_id`、迁移消息的 `user_id` 一起对齐
  - 2026-04-15 已完成：`session_service.py` 的 gateway runtime compat bridge 现已和 DB-level helper 对齐；legacy `gw_agent_*` transcript 在运行期归一化时，也会回填 canonical session 的 `created_at / last_message_at`
  - 2026-04-15 已完成：`db_legacy_gateway_conversation_migration.py` 在复用已有 canonical session 时，也会把 `created_at` 回拉到 legacy transcript 的最早时间；gateway 的 runtime bridge 与 DB migration helper 现已在 `conversation_id / created_at / last_message_at` 三个关键面保持一致
  - 2026-04-15 已完成：`feishu_identity_maintenance.py` 与 `db_legacy_feishu_session_migration.py` 现也已把 legacy alias session 的最早 `created_at` 回拉到 canonical session；Feishu 的 runtime merge / bootstrap migration / maintenance helper 在时间边界语义上开始和 gateway 一样保持一致
  - 2026-04-15 已完成：`api/activity.py` 的 Feishu partner label 读取现在也开始优先吃 canonical `delivery_target_json.user_label`；当消息正文里没有 sender prefix 时，session-backed 历史视图不再退回泛化“飞书用户”，读取面与 runtime 写入的 canonical delivery target 已重新对齐
  - 2026-04-15 已完成：`send_feishu_message` 在 `open_id` 成功路径下，现也会把 canonical `user_id` 回写进 tool args；`pending_reply` 不再因为 `open_id / user_id` 混用而把同一个 Feishu 收件人记成两套 identity
  - 2026-04-15 已完成：`channel_user_service.resolve_feishu_delivery_target_by_name()` 现会先检查 session 绑定用户、tenant user fallback 的 canonical delivery target；当历史 session 或用户行只留下 app-scoped `open_id`、但用户侧已存在 stable `user_id` / provider-backed target 时，名称解析不再被旧 `open_id` 压回非 canonical 身份
  - 2026-04-15 已完成：`send_feishu_message` 的 `feishu_user_search` fallback 现已能正确抽取带下划线的 `user_id`；搜索结果里若同时存在 `user_id` 与 `open_id`，出站补偿链不再因为 regex 过窄而误退回 `open_id`
  - 2026-04-15 已完成：`send_feishu_message` 在 `feishu_user_search` 只返回 `open_id` 时，现也会继续尝试 `resolve_feishu_user -> get_feishu_delivery_target` 把它 canonicalize 回 `user_id`；搜索补偿分支不再停在 transport identity
  - 2026-04-15 已完成：`send_feishu_message` 的 owner/creator fallback 现也已改走 `get_feishu_delivery_target(...)`；即使 owner 用户行上还残留旧 `open_id`，只要平台已能解析出 canonical `user_id`，出站发送、tool args 回填与 session capture 都会优先收口到同一条 `user_id` 主干
  - 2026-04-15 已完成：`api/gateway.py` 的关系上下文构建现也开始识别 `OrgMember.external_id/open_id`；只使用 provider-backed canonical 字段的 Feishu 联系人，不会再在 gateway 视图里被误判成“没有 Feishu 通道”
  - 2026-04-15 已完成：`channel_user_service.resolve_feishu_delivery_target_by_name()` 的 relationship / tenant org member fallback 现也会在只有 `open_id` 时继续尝试 `resolve_feishu_user -> get_feishu_delivery_target`；org member 分支不再提前停在 transport identity
  - 2026-04-15 已完成：`send_feishu_message` 的 direct `user_id/open_id` 输入校验现在同时接受 `OrgMember.external_id/open_id`；当调用方直接给 `open_id`、但平台已可回拉 canonical `user_id` 时，出站回填与 session capture 也会重新并回 `user_id` 主干
  - 2026-04-15 已完成：`feishu_user_search` 在 `OrgMember` 与 `User` 两个分支上，现分别开始输出 provider-backed `external_id/open_id` 与 canonical `get_feishu_delivery_target(...)`；搜索入口不再把已经修好的 Feishu canonical identity 主干重新降级回旧 `feishu_*` 字段
  - 2026-04-15 已完成：relationship member 只有 `open_id` 时，`send_feishu_message` 现也会在真正发送前先做一次 `open_id -> canonical user_id` 回拉；普通发送成功路径与 `org-sync` cross-app fallback 都会优先尝试 `user_id`，不再把这类关系成员锁死在 transport identity
  - 2026-04-15 已完成：`relationships_file.render_relationships_markdown()` 现会同时展示 provider-backed `user_id/open_id`；关系网络文档不再只暴露旧 `feishu_open_id` 而把 canonical Feishu 身份隐藏掉
  - 2026-04-15 已完成：`api/users.py::list_users()` 现也会基于 `get_feishu_delivery_target(...)` 计算用户来源与展示用 `feishu_open_id`；已经迁到 canonical identity 的 Feishu 用户，不会再在管理面板里被误判成普通 `registered`
  - 2026-04-15 已完成：`org_sync_service.py` 现在会用 provider-backed `external_id/open_id` 匹配既有 `OrgMember`，并把平台用户解析统一收口到 `feishu_auth_provider._find_user_by_external_identity / _find_user_by_legacy_fields / _create_user`；组织同步不再自己维护一套容易分叉的旧用户匹配链
  - 2026-04-15 已完成：`cleanup_duplicate_feishu_users.py` 在用户/成员回填时，现也会同步刷新 `open_id / union_id / legacy field write-through`；维护脚本不再只补 `feishu_user_id` 而把 provider-backed 身份对齐留半截
  - 2026-04-15 已完成：`feishu_identity_maintenance.py` 在 duplicate user merge 结束后，现会再从已归并的 `ExternalIdentity` 回灌缺失的 `feishu_user_id / feishu_open_id / feishu_union_id`；主用户不再停留在“identity 已迁移、legacy 字段还半空”的半状态
  - 2026-04-15 已完成：`db_legacy_feishu_session_migration.py` 现在即使遇到 `users.feishu_*` 已清空，也会读取 provider-backed `external_identities` 来构造 canonical `feishu_p2p_<user_id>`；启动期 / 维护脚本不再只认旧列而漏迁 session alias
- Phase 3 协作与委派主干
  - 真实盘点已完成
  - 第一轮已收口：`advanced API -> collaboration_service -> runtime A2A/delegation`
  - `collaboration_service.send_message_between_agents()` 已不再走 event bus / file inbox 旧桥
  - `send_message_to_agent` 已支持 `target_agent_id` 精准路由
  - `send_message_to_agent` 的 `task_delegate` 语义已下线，委派统一收口到 `delegate_to_agent`
  - 第二轮首段已完成：`collaboration_service` 的 audit 语义已统一为 `delegation / agent_message`
  - `collaboration:delegation` 与 `collaboration:agent_message` 已对齐 runtime `interaction_type`
  - hook 盘点已确认：`orchestrator + hooks_setup` 对 `delegation` / `agent_message` 的边界已是清晰的，不需要反向兼容旧 message bridge
  - 前端盘点已确认：当前 `frontend/src` 还没有直接消费 `/collaborate/*`，因此 `advanced API` 暂时不是活跃分叉面
  - 第二轮继续中：接下来转向更深层的 runtime / task / prompt / memory 连接处
  - 2026-04-15 已完成：`agents/orchestrator.py` 已把 `agent_message` 与 `delegation` 的 runtime prompt 分流；A2A 不再复用 `Delegated Task Brief / Delegated Worker Mode`，只保留自己的消息 brief，而 delegation 继续保留 worker prompt / memory rule 语义
  - 2026-04-15 已完成：`RuntimeTask.metadata_json` 与 `resume_persisted_async_delegations()` 已开始显式持久化/恢复 `interaction_type`；重启恢复链不再把已分流的 A2A 语义默认回落成 delegation prompt
  - 2026-04-15 已完成：`runtime/invoker.py` 现会把 `session_context.metadata` 继续透传到 `SESSION_START / SESSION_CLOSE` hook；`hooks_setup.py`、`t0_logger.py` 与 `session_recall.py` 也已开始把 `agent_message` 作为独立 T0 行为类型处理，A2A 不再在日志/回忆层被吞回普通 `chat`
  - 2026-04-15 已完成：`hooks_setup.py` 现会让 `delegation` 子会话跳过 `SESSION_CLOSE / SESSION_IDLE` 的普通 chat T0 落盘，只保留 `DELEGATION_END` 这条 canonical delegation 观察面；委派执行不再在 T0 层双写成 `chat + delegation`
  - 2026-04-15 已完成：`session_memory.py` 与 `hooks_setup.py` 现会优先用 `interaction_type` 作为 memory / extraction source；A2A 的 `agent_message` 不再在 session memory、extract 调度和后续记忆抽取入口处再次回落成通用 `agent`
  - 2026-04-15 已完成：`memory/t2_store.py` 已把 `agent_message`（以及 generic `agent`）纳入 autonomous source bucket；协作链产生的 T2 权重不再错误落入 `system` 桶，`agent_message -> extract -> T2 weight` 这条链路已与 delegation 对齐
  - 2026-04-15 已完成：`skill_distiller.py` 已把 `source_channel=\"agent\"` 纳入内部 session source；下游 internal workflow distillation 不再漏掉正式协作主干产生的 agent 会话，协作链从 runtime 到记忆再到 skill distillation 的消费面进一步统一
  - 2026-04-15 已完成：`t0_logger.py` 现会把 A2A T0 frontmatter 的 `source` 明确写成 `agent_message`；`session_recall.py` 也会把历史 `type=agent_message + source=agent` 旧日志归一化成 `agent_message`，避免 recall 消费面继续把 A2A 糊回 generic `agent`
  - 2026-04-15 已完成：`api/chat_sessions.py` 在列出 agent 协作会话时，`peer_agent_id / peer_agent_name` 现会相对于当前请求的 agent 正确返回“对端”而不是错误返回自己；协作会话的 session consumer 契约进一步与 canonical agent-pair session 语义对齐
  - 2026-04-15 已完成：`api/activity.py` 在列出 agent 协作会话时，消息统计与最后一条消息现会基于 canonical `session.agent_id` 读取；peer 侧查看协作历史时不再因错误使用请求方 `agent_id` 而读空数据
  - 2026-04-15 已完成：`session_recall.py` 的 DB fallback 现会把 `peer_agent_id` 纳入 session 过滤，并移除对 `ChatMessage.agent_id == 当前请求 agent_id` 的错误绑定；当 peer 侧没有 T0 命中时，DB recall 也能正确读取 canonical agent-pair session 的历史内容
  - 2026-04-15 已完成：`session_recall.py` 的 DB fallback 不再把 `source_channel=\"agent\"` 误排除，并会把 DB 回退命中的协作 source 统一归一化为 `agent_message`；同一类 A2A 会话现在无论走 T0 recall 还是 DB fallback，都不会再对外返回两套 `agent / agent_message` 语义
  - 2026-04-15 已完成：`t0_logger.py::backfill_recent_chat_logs()` 已不再停留在旧的 `chat-only` 假设；dream 阶段的历史补录现在也会识别 peer 侧 agent session、移除对 `ChatMessage.agent_id` 的错误绑定，并按 canonical 类型补成 `agent_message-*.md`，同时会把已有 `agent_message` T0 视作已补录，避免重复回填
  - 2026-04-15 已完成：`api/gateway.py::_send_to_agent_background()` 现已为 native-agent 补偿路径显式构造 `SessionContext(metadata={interaction_type=\"agent_message\", ...})`；`api/websocket.py::call_llm()` 的 `auto_close_session` 也会继承 `session_context.metadata` 到 `SESSION_CLOSE` hook。gateway / OpenClaw 补偿线不再在关会话时丢掉 `agent_message` 语义
  - 2026-04-15 已完成：`api/gateway.py` 的 OpenClaw-to-OpenClaw 直连分支现已把出站请求与回包都落回 canonical `ChatMessage` transcript；协作旁路不再只写 `GatewayMessage` 队列而让会话主干在历史读取面留下空洞
  - 2026-04-15 已完成：`api/gateway.py` 现已把 OpenClaw-to-OpenClaw 与 OpenClaw-to-native 两条 transcript 写入链都补齐 `participant_id`，`/gateway/poll` 的 history sender 也会优先按 `Participant.display_name` 解析；gateway 协作 transcript 与 `chat_sessions/messages` 的 sender contract 已重新统一
  - 2026-04-15 已完成：`agent_tool_domains/messaging.py::_send_message_to_agent()` 对 OpenClaw target 的分支现在也会先建 canonical `agent_pair_session`、写入 outbound `ChatMessage(user)`、并把 `GatewayMessage.conversation_id` 绑到同一条 session；tool domain 与 `api/gateway.py` 两条 OpenClaw A2A 发信入口现已共享同一条 transcript 主干
  - Phase 4 Prompt / Context / Memory 主干
  - 第一轮真实入口盘点已开始
  - 当前生产态主路径已确认：`runtime/invoker.py -> kernel/engine.py -> prompt_builder frozen/dynamic split`
  - `build_agent_context`、`build_agent_runtime_context`、`build_memory_snapshot` 当前生产调用面都已收口到 `invoker`
  - `memory_service.on_conversation_start()` 已删除，legacy memory wrapper 假入口减少一个
  - `memory_service.on_conversation_end()` 已删除，runtime memory compat wrapper 再减少一个
  - 第二轮已推进：`request.memory_context` 已从 `websocket -> AgentInvocationRequest -> InvocationRequest -> invoker` 生产主链路移除
  - canonical memory 现只由 `build_memory_snapshot()` 进入 system prompt，不再允许调用侧额外塞入 manual memory block
  - retrieval 区块已进一步拆清：`runtime context / relevant memory recall / knowledge` 现在有显式 section 边界，不再作为一整块裸文本落进 `Knowledge`
  - `runtime/prompt_builder.py::build_runtime_prompt()` 已从生产模块移除，tests 已直接对 frozen/dynamic/assemble 真实主干做验证
  - `build_agent_context()` 上的 `include_memory_file / include_focus` deprecated 参数已删除，`invoker` 不再显式传永远为 `False` 的兼容字段
  - `build_frozen_prompt_prefix(memory_snapshot=...)` 假参数已删除，`prompt_builder` 内部的 `_compute_system_prompt_budget / _render_active_packs` compat wrapper 也已清退
  - `memory/store.py` 已删除 `memory.json` 双写路径，legacy json 只保留只读导入职责
  - `api/memory.py` 已切到 `PersistentMemoryStore.load_semantic_facts()`，不再直读 `memory.json`
  - `memory_service.py` 已删除对 `FileBackedMemoryStore` 的生产 fallback 依赖，retrieval 失败时只回到 canonical summary + agent memory fallback
 - 当前阶段判断：Phase 4 已基本满足退出条件，后续只保留局部回归与跨主干联调观察，不再继续新增 prompt/memory 兼容口
  - 2026-04-15 已完成：`context_budget` 对内部 session source 的旧 `schedule` 枚举残留已清掉；相关 runtime + architecture 回归已补齐并通过
  - 2026-04-15 已完成：`call_llm` 的显式 session contract 已补测试，`gateway / feishu / trigger_daemon` 这类非 web 入口现在都被架构测试固定为必须传入自己的 source/channel
- Phase 5 工具运行时主干
  - 真实盘点已启动
  - 当前已确认：
    - `get_combined_openai_tools()` 现已统一经 `ToolRegistry` 输出标准化 tool surface，不再直接暴露 raw collector schema
    - `get_agent_tools_for_llm()` 已统一经 `ToolRegistry.from_openai_tools(...).to_openai_tools()` 输出 schema 与只读/并行安全元数据
    - `execute_tool()` / `_execute_tool_direct()` 已统一委托给 `ToolRuntimeService.execute()` / `execute_direct()`
    - `runtime/invoker.py`、`heartbeat.py`、`agent_tool_domains/messaging.py` 这几个注入点虽然存在自定义 `tool_executor`，但底层仍收口回 `services.agent_tools.execute_tool()`
  - 第一轮已落地：
    - 新增 `backend/tests/architecture/test_tool_runtime_trunk.py`
    - `_get_tool_runtime_service()` 内的 `direct_fallback_executor` 已削薄为仅兜未知工具 / MCP passthrough，不再手写第一类工具执行分发
    - `CORE_TOOL_NAMES` 契约测试已和生产现实重新对齐，`send_channel_message` 不再处于“代码在主干里、测试却假装它不在”的漂移状态
    - `list_mcp_resources / read_mcp_resource` 的 `read_only` 属性已回到 decorator metadata，collector 现可直接表达这两个工具的只读属性
    - `registry.py` 的静态 `read_only / parallel_safe` 集合已删除，当前两类元数据只由 collector 延迟解析
    - `ToolRegistry.category` 已改为优先吃 collector / DB category overrides，不再主要靠 `infer_category(name)` 猜测
    - `tools/handlers` 中 `communication / filesystem / plaza / triggers / email / feishu` 已直接回连各自 domain 模块，不再经过 `agent_tools` 下划线 re-export
    - `send_channel_message / send_channel_file` 已下沉到 `agent_tool_domains/channel_delivery.py`
    - `channel_file_sender / channel_web_agent_id / channel_feishu_sender_open_id` 三个 channel context var 已从 `agent_tools.py` 迁出
    - `Telegram / Feishu / Slack / Teams / WeChat Personal` 的 channel file sender import 已统一切到新 domain，相关回归已补齐
    - `app.tools.surface` 已接管 canonical tool surface 组装与 agent 级工具裁剪；`agent_tools.py` 现只保留 facade + runtime entry
    - `get_combined_openai_tools()` 与 `get_agent_tools_for_llm()` 的真实实现已移出 `agent_tools.py`
    - `agent_tools.py` 底部 legacy domain re-export 已整段删除，测试已切到真实 domain 模块
    - `api/tools.py` 的 Feishu runtime availability 查询已直接切到 `app.tools.surface`
    - `app.tools.execution_entry` 已接管 canonical runtime entry；`approval_service` 不再经 `agent_tools` 走 direct execution
    - `runtime/invoker.py`、`heartbeat.py`、`agent_tool_domains/messaging.py` 已直接从 `app.tools.execution_entry` 导入 `execute_tool`
    - `backend/tests/services/test_agent_message_runtime.py` 的 monkeypatch 已对齐真实 import binding，不再钉旧 facade
    - `runtime/invoker.py`、`pack_service.py`、`runtime/prompt_eval.py`、`runtime/task_eval.py` 已直接从 `app.tools.surface` 读取 tool surface / core tool metadata
    - 当前 `backend/app` 生产代码内，已无模块继续 import `app.services.agent_tools`
    - `backend/app/services/agent_tools.py` compat facade 已删除
    - `backend/tests/tools/test_bridge_equivalence.py` 已切到 `app.tools.surface`
    - 新增 `backend/tests/architecture/test_legacy_agent_tools_allowlist.py`，固定 `backend/` 内不允许再 import `app.services.agent_tools`
    - `backend/app/tools/governance.py::_request_approval_compat` 已删除，approval path 现只保留 canonical `deps.request_approval(...)`
    - `backend/tests/architecture/test_tool_runtime_trunk.py` 已新增治理护栏，禁止 governance compat bridge 回流
    - 2026-04-15 已复核：Phase 3/4/5 交叉回归已覆盖 `tool runtime / gateway transcript / prompt-memory / collaboration` 主链，`128 passed`；当前生产代码未见 `app.services.agent_tools` 回流，也未见第二套工具执行入口复活
 - Phase 1 自主触发主干
   - `backend/app/services/schedule_surface.py` 已承接 canonical schedule surface
   - `api/schedules.py` 已完全脱离 compat migration
   - `trigger_daemon.py` 已不再 import compat migration
   - `main.py` 已不再承担 legacy schedule migration
   - `backend/app/services/scheduler.py` 已物理删除
   - `backend/app/services/supervision_reminder.py` 已物理删除
   - `backend/app/scripts/migrate_schedules_to_triggers.py` 已物理删除
   - `backend/app/models/schedule.py` 已物理删除
   - `backend/app/services/schedule_compat.py` 已物理删除
   - `backend/app/services/legacy_schedule_migration.py` 已物理删除
   - `backend/app/main.py / backend/entrypoint.sh / backend/seed.py` 已不再把 `app.models.schedule` 注入 bootstrap `create_all`
   - 新增 `backend/app/db_legacy_schedule_migration.py` 作为 DB migration helper
   - `db_bootstrap.py` 与 Alembic 头 `drop_legacy_agent_schedules_0414` 共同承担 legacy schedule 数据迁移
   - 当前仓库内已不存在 `from app.models.schedule import AgentSchedule`
   - 当前 Phase 1 剩余动作只是在历史环境应用 Alembic 头 `drop_legacy_agent_schedules_0414`
  - 当前主要风险：
    - 当前风险已从 compat facade 本身转为“历史文档叙述是否还把它当成当前主干”
    - Phase 5 与 Phase 6 的接缝重点已从迁移/删除转为“收尾校验与防回退护栏”
    - 下一批高价值尾巴已收敛到：
      - `session_service.py` / `feishu_identity_maintenance.py` 的 legacy conversation 归并桥

### 当前总体进度判断（2026-04-15）

- Phase 3 协作与委派主干：`97.5%`
- Phase 4 Prompt / Context / Memory 主干：`96%`
- Phase 5 工具运行时主干：`98%`
- Phase 6 契约收口：`98%`
- P3-P5 综合判断：`97.6%+`
- 当前剩余重点：已从“主干双轨并存”转为“Phase 3/4 深水区收口 + 少量非 Feishu maintenance/migration helper 的最终清尾”
- 当前进行中（2026-04-15）：
  - `pending_reply -> Feishu outbound identity -> session delivery_target` 这条链的第四轮收口已完成
  - `owner/creator fallback -> canonical delivery target` 已完成收口
  - `gateway relationship context -> provider-backed Feishu channel visibility` 已完成收口
  - `org member fallback -> canonical delivery target` 已完成收口
  - `direct user_id/open_id input -> provider-backed validation + canonical backfill` 已完成收口
  - `feishu_user_search -> provider-backed/canonical search result` 已完成收口
  - `relationship member open_id -> canonical user_id before send / org-sync fallback` 已完成收口
  - `relationships.md display -> provider-backed Feishu identity` 已完成收口
  - `/api/users -> canonical Feishu source detection` 已完成收口
  - `org_sync_service -> provider-backed org/user reconciliation` 已完成收口
  - `cleanup_duplicate_feishu_users.py -> provider-backed write-through` 已完成收口
  - `feishu_identity_maintenance.py -> primary user legacy field hydration from ExternalIdentity` 已完成收口
  - `db_legacy_feishu_session_migration.py -> provider-backed identity fallback` 已完成收口
  - 本批 Feishu maintenance / migration helper 边角已完成清尾
  - `messaging.py::_send_message_to_agent -> OpenClaw target canonical transcript` 已完成收口
  - `messaging.py::_send_message_to_agent -> OpenClaw target GatewayMessage sender/content contract` 已与 `api/gateway.py` 主干重新对齐
  - 本轮补扫 `backend/app` 其余 `GatewayMessage` agent-to-agent 写入点，暂未再发现第二处同类 `sender_user_id / [From ...]` 回流
  - `.gitignore` 现已精准放行 `docs/backend-trunk-governance/*.md`；治理进度文档不再停留在“工作树已更新、但 Git 不可见”的隐形断层状态
  - 当前目标已提升为：把各 Phase 的残余断点继续压缩到 `98%+`，并尽量消除“主干已 canonical、补偿分支仍在旧 fallback”这类隐形双轨
  - 下一批继续扫描重点：
    - 其它非 Feishu maintenance / migration helper 是否仍存在“只认 legacy 字段、不认 canonical 主干”的读写漂移
    - Phase 3 / Phase 4 内仍可能把 canonical session / identity 拖回旧 fallback 的深层辅助入口

### Phase 0

先落架构测试骨架和证据采集流程。

### Phase 1

修 T5 自主触发主干：

- `AgentTrigger`
- `trigger_daemon`
- 清退 `AgentSchedule / scheduler / supervision_reminder`

### Phase 2

修 T4 会话与消息主干：

- `ChatSession`
- `ChatMessage`
- 统一 session factory / service

### Phase 3

修 T6 协作与委派主干：

- `send_message_to_agent`
- `delegate_to_agent`
- `RuntimeTask`
- `orchestrator`

### Phase 4

修 T3 Prompt / Context / Memory 主干。

### Phase 5

修 T2 工具运行时主干。

### Phase 6

修 T1/T0 剩余契约收紧与 legacy 删除。

## 5. 本轮不做什么

在主干未修完之前，不做这些：

- 不先大改渠道实现
- 不先大改前端页面
- 不先大改 Desktop
- 不先调 feature flags / MCP 产品层表达
- 不先做“广义整洁化重构”

## 6. 文档集

- [01-trunk-catalog.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/01-trunk-catalog.md)
- [02-dependency-and-break-risk-map.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/02-dependency-and-break-risk-map.md)
- [03-detection-and-evidence-playbook.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/03-detection-and-evidence-playbook.md)
- [10-phase-1-autonomy-trigger-trunk.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/10-phase-1-autonomy-trigger-trunk.md)
- [11-phase-2-session-message-trunk.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/11-phase-2-session-message-trunk.md)
- [12-phase-3-collaboration-delegation-trunk.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/12-phase-3-collaboration-delegation-trunk.md)
- [13-phase-4-prompt-memory-trunk.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/13-phase-4-prompt-memory-trunk.md)
- [14-phase-5-tool-runtime-trunk.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/14-phase-5-tool-runtime-trunk.md)
- [15-phase-6-contract-hardening-and-legacy-deletion.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/15-phase-6-contract-hardening-and-legacy-deletion.md)
- [20-master-regression-plan.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/20-master-regression-plan.md)
- [21-branch-repair-order.md](/Users/rocky243/vc-saas/hiveclaw/docs/backend-trunk-governance/21-branch-repair-order.md)
