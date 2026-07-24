# Kimi 独立原子化审查报告 — CCPlus / Agent-Native（2026-07-24）

原始审查者：Kimi Code（独立审查，非实现参与方）
当前版本：Codex current-source 复核与逐项修复台账（2026-07-24）
审查依据：`docs/ccplus-agent-native-independent-review-prompt.md`（可复用正文全文执行）
本报告原始版本由 Kimi 新建；Codex 先完成 current-source 复核，随后按用户授权逐项修复。修订遵循“保留原编号、正文同步改正、撤销项留痕、每个修复部分附机械证据并独立提交”的审计规则。

---

## 0. 修复执行台账

| 部分 | 状态 | 变更与机械证据 | 验收边界 |
|---|---|---|---|
| Hook 产品边界文档校正（SA-04/UI-09） | 已完成 | commit `b4712dcc`；撤销“员工自定义 Hook 缺失”，冻结员工/Owner、Operator/Auditor、Platform Developer 三层边界 | 产品边界已由后续 UI-09 代码修复执行 |
| GV-01/GV-02 确认车道 | **代码闭环；本地真 PG 验收待补** | `request_action_preflight_approval` 把 typed ASK/ESCALATE 接入 durable Approval ticket；移除 ToolRuntimeService 的 InProcess gateway 默认回填与孤立 checkpoint 写入；immutable approval 只消解 ASK/ESCALATE，不覆盖 REFUSE/PREPARE_ONLY；approved boundary block 统一把票据记为 `failed`。测试命令：`backend/.venv/bin/pytest -q backend/tests/tools/test_service.py backend/tests/tools/test_tool_runtime_preflight.py backend/tests/services/test_action_preflight.py backend/tests/services/test_approval_execution_runtime.py backend/tests/tools/test_governance.py backend/tests/tools/test_governance_resolver.py` → `103 passed, 4 skipped` | 4 项均因本机 Docker/Testcontainers 不可用而 skip；实际 ToolRuntimeService registry 执行 seam 未 monkeypatch 的 ASK→批准→效果发生与 REFUSE→failed 组合测试已通过，真 PG 事务/worker 组合仍保留在 §11 |
| GV-03 typed Owner action policy | **代码闭环；真 PG 并发验收待补** | 新增唯一机器合同 `hive.owner_action_policy.v1`，以三个 exact action id 映射 `full_authority/confirm_first/never_do`；`ConfigRevision` 保存不可变版本、actor、hash、history/rollback，legacy agent 在模型输入、工具执行或管理 API 首次消费时幂等回填默认版本；真实 prompt assembly、ToolContextResolver、ToolRuntimeService preflight、Approval ticket 与员工业务设置卡已贯通，Owner/Manager 可两步确认恢复上一版。后端聚合测试 `168 passed`，Ruff 通过；前端定向测试 `115 passed`，生产 build 与 bundle budget 通过 | 未新增 schema/Alembic 迁移，复用现有 `config_revisions`；本机无 Docker，尚未实测两个 PG worker 同时首次回填与事务 rollback。员工设置只显示业务动作政策和恢复动作，不显示 action id/revision ID/Hook；原 raw Hook 卡已由 UI-09 删除 |
| SA-05 Workflow confirmed-plan handoff | **代码闭环；真 PG lease 验收待补** | `start_workflow` 加入 canonical `ACTION_KINDS/_ACTION_INTENT` 并映射 `in_session_execution`；工具注册为 `bridge:self`，继续由 durable preview/显式 start 自行确认，不触发通用 Plan Mode 硬门。新增 REST 真实路径测试，覆盖 preview→claim→真实 `PlanModeGate.check`→lease→launch；13 个相关 test 文件聚合 `239 passed`，Ruff 通过 | 无 schema、数据或配置迁移；10 个 PlanAuthorizationLease 真 PG 用例因本机 Docker/Testcontainers 不可用全部 skip，不能外推为生产事务验收 |
| SA-01 turn token budget | **代码闭环；真 PG permission-resume 验收待补** | `turn_orchestrator` 现消费 `RuntimeConfig.turn_token_budget`：按 `extract_usage_tokens` 的 cache-miss 口径累计；达到预算且模型提出下一批工具时，先提交 provider result seal、记账、发 `turn_token_budget_exhausted` typed event、持久化恢复状态，再以 `TerminalReason.TOOL_BUDGET` 退出，未执行的新工具为零。完成态模型答案即使超过预算也保持 byte-faithful；零 cache-miss 不回落为整段 prompt 估算。权限恢复从同一 run/turn 已提交的 logical-root `SessionModelResult` seal 重建累计用量，排除 output continuation 双计；终态 receipt 使用 turn 累计值，quota 只记本次 resume 新增量。扩大后的 14 文件邻接聚合 `374 passed, 7 skipped`，Ruff/format/compileall 全绿 | 无 schema/config/数据迁移；复用既有 invoker 派生值、ModelResult seal、recovery manifest、token tracker、Web terminal persistence。7 个 skip 均为 Docker/Testcontainers unavailable 的 Session permission 真 PG 文件；预算是显式资源边界，不检查自然语言、不删授权输入、不改写已完成模型答案 |
| GV-04 platform_admin 跨租户化身审计 | **代码闭环** | `get_current_user` 在目标租户 active 校验和 RLS pin 后、业务 route 执行前，把真实跨租户选择写入独立提交的 operator-only `platform_security.tenant_impersonation`；receipt 含 actor/home tenant/target tenant/method/path/IP/trace request id，并在 request state 留 event id。审计写失败返回 typed 503；同租户选择不误记。6 个相关 test 文件聚合 `49 passed`，Ruff 通过 | 无 schema/config 迁移；历史化身从未产生机械证据，无法可信回填。公共身份 dependency 与 strict operator sink 已有代码证据；统一 operator 查询/验链由 GV-06 完成，生产 DB/Railway 多实例仍纳入 §11 |
| GV-05 Desktop LLM proxy quota/metering/rate limit | **代码闭环；真 PG/Redis 验收待补** | live `/api[/v1]/llm/v1/chat/completions` 在 upstream 前执行 tenant/user token quota 与 tenant+user Redis 60/min 限流；非流式响应返回前、SSE `[DONE]` 前严格提交 `TokenUsageEvent(source=desktop_llm_proxy)` 并更新 tenant/user counters。stream 强制请求 provider usage；缺失时仅在计量故障路径使用可观察的 CJK-aware estimate；断流执行恢复记账，metering 失败不伪造完成。扩大聚合 `76 passed`，Ruff/format 通过 | 复用既有 usage 表与 counters，无迁移/回填。3 个真 PG quota/counter 用例因 Docker unavailable skip；生产 Redis sliding-window 与 PG row-lock 并发仍须验收 |
| GV-06 operator 安全审计链与证据信任分层 | **代码闭环；真 PG/多实例验收待补** | `platform_security.*` 现只允许 strict operator sink 写入 `hive.platform_security_audit.v2`：PG transaction advisory lock 串行 sequence/prev_hash，event hash 覆盖 row id/action/完整 envelope；首次 `chain_cutover` 锚定不可变 v1 集合，后续事件刷新 legacy anchor 以恢复滚动部署期间的晚到 v1，损坏链头拒绝继续追加。新增 platform_admin-only 查询/全链验签 API；Desktop 上报固定标成 `client_asserted`，客户端字段嵌套隔离，claimed Agent 必须属于服务端已固定 tenant。扩大聚合 `106 passed, 3 skipped`，Ruff/format 通过 | 复用 `audit_logs` 既有 UPDATE/DELETE/TRUNCATE 数据库禁写，不新增表或伪造历史回填；通用 fail-soft operational sink 明确拒绝 `platform_security.*`。3 个不可变迁移真 PG 用例，以及包含 operator 写入→查询→验链路径的集成文件 6 个用例，均因 Docker unavailable skip；生产 DB、滚动多实例与平台管理员实际查询仍须验收 |
| GV-07 quota admission 故障状态与消费 | **代码闭环；生产故障注入待补** | 保留 token/cost 资源权威不可验证时的 fail-closed，不再误导为“基础设施故障可降级放行”；`QuotaExceeded` 与 quota authority failure 分别产生 `quota_denied` / `quota_unavailable` 终态，以及 durable `quota_exceeded` 的 `denied` / `unavailable+retryable` 事件。web chat transcript、运行任务终态和员工页可读状态均消费同一 typed receipt，底层异常文本不回显。RED 为后端 `3 failed`、前端 `1 failed`；最终后端相邻聚合 `68 passed`、前端 `176 passed`，Ruff/format 与生产 build/bundle budget 通过 | 无 schema、配置或历史数据迁移。尚未在生产注入 quota DB/authority 故障并观察真实 WebSocket→transcript→reload，但本地 live-path 函数与投影契约已覆盖 |
| GV-09 DecisionTrace 单一权威与员工消费 | **代码闭环；真 PG/浏览器与遗留数据 apply 待验收** | 删除 live JSONL store，SQL 成为唯一运行时权威；Decision 与 feedback 不再内部 commit，员工反馈与会话事件共用 request transaction。新增 tenant+agent+session 精确授权的 typed decisions API，并接入员工会话右栏的 “Action decisions”；中英文 UI 只显示已知业务动作、可理解结果/原因和 Helpful/Misleading，未知工具统一显示“数字员工操作”，不显示 decision ID 或 raw reason code。后端相邻聚合 `152 passed, 1 skipped`，前端 `139 passed`，Ruff/format 与生产 build/bundle budget 通过 | 既有 SQL schema 无迁移。一次性 JSONL 工具强制 dry-run hash 绑定、零坏行/孤儿、缺 tenant 显式赋权、幂等导入和可逆归档；真实本地遗留文件 dry-run 为 2 decisions/2 feedback/0 skipped/0 orphan，但两条 decision 无 tenant，因此保持原文件不动，须由 operator 明确 tenant 并在验收环境执行 `--apply` |
| GV-08 exact-secret / Model Agency 边界 | **代码闭环；真 PG、生产凭据库存/backfill 与真实 provider 验收待补** | `PrivacyLayer` 的 credential regex 只保留 count-only 候选，不再产生 PL4/REFUSE；ToolRuntime 在 hook、Plan Mode、runtime 参数注入和执行前只按 tenant-scoped LLM/channel/tool/MCP credential authority 的 exact bytes 阻断，模型输入/流式输出/thinking/tool result/error/event/T0/Web/Session/Channel durable ingress 均只遮蔽 exact bytes，并保留 value-free receipt。typed reply target 与 Channel inbox transport token 由既有 JSON/JSONB 物理列透明加密；文件执行使用 immutable snapshot 和 SHA-bound download。最终核心/存储回归 `330 passed, 10 skipped`，入口相邻回归 `407 passed, 57 skipped`；Ruff、compileall、`git diff --check` 全绿 | 无 DDL；`migrate_channel_secrets` 升级为 v3 count-only dry-run，并在显式 `--apply --confirm` 下轮转 channel config、delivery target、ingress transport token，再对 legacy ingress 做当前 exact credential backfill。生产必须配置真实 `SECRETS_MASTER_KEY`，先审 dry-run receipt，再由 operator apply；本地 skip 不替代真 PG、真实 keyring、stream/provider、断线重试与 offboarding 验收 |
| HC-01 本地 A2A 结果回传与恢复查询 | **代码闭环；真 PG/真实设备唤醒验收待补** | Local Agent 消息入队先校验 server-issued `ExecutionPrincipal` 与 tenant/source Agent/requester/parent session/target Agent/target owner 的 exact binding；目标 channel conversation 在 PG advisory lock 下复用，channel session 与首条 message 原子提交；本地 result 与 `a2a_delegation` completion outbox 也在同一事务提交，由既有 parent-continuation worker 自动唤醒来源 Agent。重复 result replay 以已持久化原结果幂等补齐 outbox，不接受重复 payload 覆盖；`check_async_task` 现以 `task_id` 或 `message_id` 二选一查询，并返回 Local Message、artifact、receipt 与实际 outbox 状态。相关后端邻接聚合 `111 passed`，Ruff/compileall/diff check 全绿 | 无 DDL/历史回填；复用既有 Runtime Result/Notification Outbox。新增真实 PG message→result→outbox 测试，但本机 Docker/Testcontainers connection refused 而 skip；真实 Hive Connect result→outbox worker→来源会话续跑、断线重试与 reload 仍须在 acceptance 环境验收 |
| HC-03 bridge 文件策略执行点 | **代码闭环；真 PG/真实设备文件验收待补** | 新增唯一 live policy resolver，Agent override 优先 tenant default，缺策略或关闭均 403；bridge upload 在任何 workspace/ChatArtifact 写入前检查 `files:upload` scope 与 `local_agent.file_upload`，channel download 在路径解析/读文件前检查 `local_agent:receive` 与 `local_agent.file_download`。未绑定 Agent 的 legacy connection 明确 409，不再落入无策略用户空间旁路。TDD RED `8 failed, 20 passed`；最终 Local Agent 邻接 `58 passed, 8 skipped`，Ruff/format/compileall/diff check 全绿 | 无 DDL、配置或历史回填；既有未绑定连接本来已无法建立受治理 channel，恢复路径是重新 pairing。8 skip 是 Docker/Testcontainers unavailable 的真实 PostgreSQL 协议文件，其中新增 live policy flip 查询合同待 Docker-on 补绿；canonical 真实设备 upload/download 仍属 §11 |
| A2A-03 协作组管理与 Owner 确认 | **代码闭环；真 PG/浏览器验收待补** | 修正原报告“后端已闭环、仅缺前端”的误判：runtime callable read model 正确隐藏 pending/rejected/revoked，但此前没有独立 human management projection，目标 Owner 因而看不到邀请，也拿不到真实 membership key。新增 manager-only、tenant-scoped management/candidate API；create/invite/approve/reject/revoke 全部进入 `AgentA2ASection`，普通 use-only 员工不加载管理查询。目标 Owner 或带必填治理理由的 org/platform admin 才能确认；path Agent 必须就是目标 member Agent，状态转换 fail-closed，过期群组禁止邀请/批准，群主 membership 禁止撤销，重新邀请清除旧 approval/reject/revoke 状态，五类变更与 canonical tenant audit 同事务提交。后端定向 `17 passed, 1 skipped`，前端 `143 passed`，Ruff/format、生产 build/bundle budget 通过 | 无 DDL、迁移或历史回填；复用既有 group/member 表和 SecurityAuditEvent。管理 API 会传输动作所需 member/agent key，但 UI 不渲染 raw id/owner id；runtime prompt/read model 未接入管理数据。唯一 skip 是 Docker/Testcontainers unavailable 的真 PG “pending 仅管理可见、runtime 不可调用”用例；真实浏览器仍须跑 create→search→invite→另一 Owner approve→callable→revoke→消失 |
| DOC-01 / HN-01/02 文档事实源 | **闭环；缺失能力保持如实登记** | `AGENTS.md` 删除手填代码/迁移/测试计数与过时 `4223 passed` 基线，改为 current-checkout inventory 命令；清除不存在的 scheduler/extract/knowledge/viking 服务、Objective、legacy relationship 与 ONLYOFFICE 员工面宣称，改为 `AgentSessionGoal`、Collaboration Group、OfficeCLI preview 等真实路径。`proactive_employee_loop` / `policy_replay` 明确标成没有 live runtime，不再伪装完成；Sentinel 只注明不可作为 authority。硬编码旧仓库路径的 Feishu 测试 RED `1 failed, 5 passed`，改为 `__file__` 相对定位后 `6 passed`，Ruff/format 通过 | 文档与测试基础设施修复，无 schema/config/数据迁移。逐项文件存在性检查确认 16 个新声明路径存在、8 个退役/缺失路径仍不存在；规模数字以后必须现场生成，不能重新复制进 handbook |
| UI-10 前端组件极简性 | **闭环** | 不提高既有行数阈值；把 Action decisions 的标签/理由/反馈组件从 `SessionRuntimePanel` 提取为 163 行的 `SessionDecisionHistory` domain owner，并把测试/相邻组件从 `AgentChatSection` 代理 re-export 改为直接依赖真实 owner。`AgentChatSection` 2405→2379 行，`SessionRuntimePanel` 1225→1069 行；Architecture 合同 RED `2 failed, 5 passed` → GREEN `7 passed`，12 文件 chat/runtime 邻接 `285 passed`，TypeScript/Vite build 与 bundle budget 通过 | 纯前端结构重构，无 API/schema/i18n/data 迁移；行为与现有 decision feedback、runtime panel、artifact/lineage/tool result 合同保持不变 |
| UI-02 治理产品消费面 | **代码闭环；真 PG/Redis/浏览器验收待补** | 修正“三个治理 router 均无前端”的误判：`config_history` 是只接受 `ai_asset` 的退役兼容 adapter，canonical `/enterprise/ai-assets/*` 的 revision/detail/rollback/reconcile 已由 `WorkspaceAIAssetsSection` 消费；新增 Company Control Plane 的 Action Guardrails 和 Platform Admin 的 Feature Rollout。GuardPolicy 强制 expected version、完整 known-subset 校验、row/advisory lock 与同事务 tenant audit；global FeatureFlag 收紧为 `platform_admin`，加入 typed targeting/expiry、空更新拒绝、row lock + `updated_at` 冲突、效果前 strict platform audit 与 commit 后 cache invalidation。RED backend `8 failed, 5 passed`，另有 audit/首写锁/stale/empty-update 四轮各 `1 failed`；frontend 5 个文件全红且 build 暴露类型/route/icon 合同。仅暂存树最终 backend `88 passed, 30 skipped`、frontend `10 files / 34 passed`，Ruff/format、locale JSON、TypeScript/Vite build 与四项 bundle budget 全绿 | 无 DDL、迁移或回填；复用既有 `feature_flags.expires_at`、GuardPolicy/SystemSetting 与 canonical audit。30 skip 均为 Docker/Testcontainers unavailable 的 Workflow/Trigger 真 PG 邻接。员工/Owner 页面不渲染 raw policy JSON、tool id 或 platform flag；真实 PG first-write/concurrency、Redis after-commit、strict audit 链和三角色浏览器旅程仍须验收 |
| UI-03 i18n 唯一库存与双语闭环 | **代码闭环；真实双语浏览器验收待补** | 入库 AST-aware `i18n-audit`、35 条 exact 动态 key 规则（覆盖 42 个调用点）与 1 条受控 translation-wrapper 规则，CI 同时阻断静态缺 key、单边 locale、重复 JSON path、中文 literal/defaultValue fallback 和未解释动态 key。以纯 staged tree 重建的最终库存为 215 个 source files、2601 个静态调用（2122 unique）、116 个动态调用、en/zh 各 3461 key；八项 gate 全为 0，四个 inventory SHA-256 已记录在 §8/§14。合并会被 `JSON.parse` 静默覆盖的两组 `agent.extensions`，补齐双语 catalog，移除 5 个生产文件的 143 个中文默认值；测试 mock 改读真实中文 catalog。Node extractor `9 passed`，Frontend 全量 `128 files / 774 passed`，生产 build/bundle budget 全绿 | locale-only/前端/CI 变更，无 API/schema/data migration；动态规则必须以精确 source+expression+reason 更新，wrapper 必须以 exact source+callee+reason 登记，runtime fallback 不能豁免。AST 与 catalog 闭环不替代真实中英文浏览器旅程，后者仍在 §12 |
| HN-05 Personal KB grant 审计 | **代码闭环；真 PG 验收待补** | `PersonalKnowledgeService` 在 grant create/update/reactivate 与 revoke 的同一 request transaction 中追加 canonical `AuditLog`；记录 tenant/actor、resource、grantee、permission、requester/session/purpose/delegation、sensitivity、expiry 与不可逆 binding hash，不复制任意 metadata。非 Owner、校验失败或不存在 grant 均不产生伪事件；audit flush 失败会阻止业务 commit。TDD RED `2 failed, 1 passed`，最终 service/API/真 PG 定义集合 `61 passed, 11 skipped`，Ruff/format 全绿 | 复用既有 append-only `audit_logs`，无 DDL 或配置迁移；过去未记录的授权变化没有可信事件事实，明确不伪造回填。11 skip 均为 Docker/Testcontainers unavailable；其中真实 create→commit→revoke→两条审计同租户链需在 Docker-on 补绿 |
| UI-09 Hook 产品与权限边界 | **代码闭环；生产清理/浏览器验收待补** | 员工设置删除 `HookRuntimeControlCard` 及浏览器 raw adapter；概览改读 `/agents/{id}/runtime-health`，只显示健康/受保护中止/重试动作。raw registry/receipt 移到 `/admin/agents/{id}/runtime-hooks`，仅 `platform_admin` 可读写，并按目标 Agent/tenant matcher 过滤；PATCH 仅允许当前已注册 plugin Hook，所有内置 Hook 按员工维度不可变，变更先写 strict platform-security audit。启动时只恢复已注册 plugin 覆盖，内置或失效覆盖移入带 SHA-256/reason 的 `retired_hook_runtime_overrides` 可恢复区并写强审计；per-agent JSONB 首写以 advisory transaction lock 串行化，既有行与启动清理均使用 row lock，避免并发覆盖。后端相邻聚合 `112 passed`；前端 `152 passed`；Ruff/format/compileall、生产 build 与 bundle budget 全绿 | 无 DDL/Alembic 迁移；清理在现有 `system_settings` JSONB 内可逆完成，配置字节不删除。本地覆盖 pure/startup fake-DB 路径，尚未读取或改变生产行；需在部署验收确认实际 retirement receipt、平台审计链和员工浏览器页面不再出现 raw Hook |

## 1. 执行摘要与上线判断

原始审查从当前源码重新建立结论，不继承既有完成声明，覆盖 14 个审计面（CC/FreeCode 基线、kernel 与模型循环、会话生命周期、工具治理、session 中段五能力、记忆系统、自进化、A2A、知识平面、企业治理、Hive Connect、前端消费、Codex/hermes 对照、验收面）。Codex current-source 复核确认了六个原始 P0 所指向的代码事实，同时发现一项漏判的 live Model Agency 违规、两项 Hive Connect source-boundary 误报、一项前端产品状态误分类、一项员工设置的运行时实现与权限边界泄漏，以及若干不能由现有证据支持的上线表述。

**总体判断：Hive 的单 Agent 机制主干在当前源码上有较强接线证据，多处实现也体现了 CCPlus/Hive-native 增量；但“Goal 1 智能质量至少达到 hermes”尚无行为级对比证据。经 current-source 确认的 P0 已全部完成代码修复，HC-01 已接入来源会话的 durable completion outbox 与 `message_id` 恢复查询，HC-03 已把 bridge 文件读写接入 live scope+policy，UI-09 的员工 Hook 泄漏、SA-01 的 turn token budget 空转、A2A-03 的跨 Owner 协作组管理断点、DOC-01/HN-01/02 的假完成文档、UI-10 的前端组件预算回归、UI-02 的治理产品消费面、UI-03 的 i18n 唯一库存/双语门禁以及 HN-05 的 Personal KB grant 审计也已完成代码闭环。当前裁决仍为 NO-GO；其余 P1/P2/P3 断点尚未全部修复，且本地代码闭环只恢复进入真环境验收的资格，不自动构成上线判断。**

核心结论：

- **单 Agent 机制主干有较强源码证据，但行为质量未证实。** kernel 模型循环唯一接线、compaction 带 coverage ledger（强于 CC 单摘要与 Codex 单 turn 摘要）、LoopGuard 只告警且终态解释仍由模型撰写、记忆系统 T0→T2→T3→soul 以 LLM-primary 为主、Plan Mode/Subagent/Work Ledger/Skill 均有真实接线。不过不能据此推导“智能质量至少达到 hermes”；该主张仍需同模型、同任务、同证据条件下的行为级对比。
- **治理自伤 GV-01/GV-02 已完成代码修复**：Preflight ASK/ESCALATE 现复用 durable Approval ticket 的单次消费、hash 绑定与精确重放；ToolRuntimeService 不再创建孤立内存 checkpoint；approved replay 只把 ASK/ESCALATE 转为可执行，不绕过 REFUSE/PREPARE_ONLY；任何 typed boundary block 都不再误记 `succeeded`。本机 Docker 不可用，因此真 PG worker 事务验收仍待补。
- **P0 代码修复已全部完成，但不等于 GO**：HC-01 现把本地 result 与 `a2a_delegation` outbox 同事务提交，既有 completion worker 自动续跑来源 Agent 会话；`check_async_task(message_id=...)` 提供严格 session-bound 的恢复查询。GV-08 也已移除 pattern→REFUSE 违规。HC-01 的真 PG/真实设备唤醒，以及 GV-01/02/04/05/08、SA-05 的 PG、Redis、keyring、provider 和生产边界仍在 §11 保留。
- **DOC-01/HN-01/02 假完成文档已收正**：AGENTS.md 的 turn-level token budget 保留精确 cache-miss gate 合同；不存在的 `proactive_employee_loop`、`policy_replay` 和其余幽灵服务已从 live inventory 移除或明确标成已知缺失，Objective 改为 `AgentSessionGoal`，Office 改为 OfficeCLI preview 事实，手填规模/测试数字全部改为现场生成。Feishu 测试的旧机器绝对路径也已改为 checkout-relative，避免健康仓库仍因文档测试基础设施失败。
- **Hook 产品边界已由 UI-09 代码修复收正**：员工设置与浏览器 adapter 已完全移除 Hook registry、handler/event/failure receipt 和开关；员工概览只消费可行动的运行保护健康投影。raw 诊断已迁到仅 `platform_admin` 可达且按目标 Agent/tenant 过滤的独立路径，内置 Hook 无 per-agent mutation 路径；只有当前已注册 plugin Hook 可由平台开发者修改并写 strict audit。生产遗留覆盖 retirement 与真实浏览器仍待部署验收。
- **A2A-03 已按正确产品边界闭环**：普通员工可调用名单继续只返回 same-owner/public/active group；Manager 的独立管理面才显示 pending/rejected/revoked。跨 Owner 邀请必须由目标 Owner 或填写治理理由的管理员确认，前端已接 create/search/invite/approve/reject/revoke/reinvite，并且 raw membership/owner ID 不进入页面。真 PG 与双 Owner 浏览器旅程仍在 §11。
- **Codex/hermes 对照**：Hive 已吸收大部分工程增量（sandbox provider、approval 路由、resume/fork、compaction）；可吸收但未做的：unified exec（PTY/持久 shell 会话）、execpolicy 命令级声明策略、hermes 的 session_search（跨会话原文检索）与 verify-on-stop。无"错误改变 CC 语义"的吸收。
- **上线判断**：**NO-GO / Acceptance incomplete**。仍须完成本报告其余 P1/P2/P3 修复，并重新核生产接线与 §11 中 Goal 1 行为对标、真 PG、Redis、多进程、自治批准回放、GV-08 真实 keyring/backfill/provider、HC-01 来源会话唤醒和真实 Hive Connect 安装态验收；不得从“P0 代码修复完成”直接跳到 GO。

---

## 2. 审查范围、当前环境与未覆盖面

### 2.1 环境记录

- 仓库根：`/Users/rocky243/vc-saas/hiveclaw-main`；证据时间点：2026-07-24 01:34（+0800）。
- **git 元数据已恢复且未覆盖工作树**：原 `.git` 指向不存在的 worktree gitdir；修复阶段从远端 `main@211dbdadd2735280f76d39e88905423917d5f159` 重建 metadata/index，并在 `codex/kimi-review-remediation-20260724` 上逐项提交。恢复前的 broken pointer 保存在 `/tmp/hiveclaw-git-recovery.WeuVmt/original-git-pointer.txt`；原有其他 session 脏文件保持未暂存。
- 基线源码均可访问：FreeCode（`/Users/rocky243/vc-saas/free-code-main`）、claude-code-org、claw-code（Py/Rust）、codex-rs、hermes-agent。原报告未读取 Skill 实际安装的 canonical `/Users/rocky243/vc-saas/hive-connect`，导致 HC-02/HC-04 source-boundary 误报；本修订已补读对应 `0.1.9` 源码。
- 只读验证已执行：`alembic heads` → 单头 `completion_outbox_index_0721`；178 个迁移文件；923 个后端测试文件 / 7278 个测试函数 / 7687 个收集后用例；全量后端 pytest 已复跑；canonical Hive Connect 的 Hive adapter、daemon 与 CLI 包测试已执行（结果见 §14）。
- 子审查员实跑测试证据：kernel+invoker 195 项全绿；knowledge 平面 134 项全绿（13 项 Docker-off skip）。

### 2.2 覆盖面

| 面 | 覆盖 |
|---|---|
| CC/FreeCode 基线 | 31 条生命周期能力账本（含 hooks 28 事件、权限五模式、compaction、resume/fork/rewind、MCP、后台任务） |
| 单 Agent | kernel 全 7 文件、invoker、llm_client、web_chat_runtime、websocket、RuntimeTask claim/cancel/recovery、session 中段五能力 |
| Hive Native | memory/ 全部 31 模块、自进化（skill distillation/dream/soul）、A2A/delegation、知识平面、canonical Hive Connect 与仓内 legacy local_bridge 边界 |
| 企业治理 | RLS、capability gate、GuardPolicy、approval、quota、secrets、租户/运营双审计链与证据信任分层、AI 资产、admin/desktop 面 |
| UI/UX | 前端 16 页、57 个 api/domains、chat 传输全链、三受众分层；员工设置 Hook 暴露有用户生产截图与修复前后 current-source/红绿证据；i18n 已有仓内 AST inventory、动态规则表、双语 catalog 与 CI gate，真实浏览器仍待验收 |
| 验收 | CI 三门禁、178 迁移、部署契约、测试断言抽查（write_gate/preflight/approval/sandbox/migrations） |

### 2.3 未覆盖面（诚实声明）

- 真实浏览器端到端行为（未跑 Playwright）；UI-09 另有用户提供的生产页面截图，但本次未独立操作浏览器复现。
- 生产数据库事实（feature flag 行、tenant 策略行的真实配置）。
- 多进程/多实例部署行为（Redis cancel bus、WS fanout、进程内 dict 一致性）。
- Railway 生产运行证据与最近一次 CI run 状态（无网络核实）。
- FreeCode 未实际运行，基线为静态源码证据。
- `~/.hive/data/agents` 磁盘实物（T0/T2 文件）未抽样。
- canonical `@hiveclaw243/hive-connect` 已确认存在 ping/daemon 源码与包级测试，但未在真实登录设备执行 install→restart→presence 的端到端验收。

---

## 3. 双北极星与 Model Agency 裁决

### 3.1 北极星 Goal 1（最强可控数字员工）：**机制主干有较强证据，行为质量未证实**

成立面（源码与接线证据）：

- 模型循环唯一接线：`invoker.py:1617 → invocation_orchestrator.py:56 → engine.py:3811 → turn_orchestrator.py:326`，全库无第二工具循环；kernel 零 DB import 不变量成立。
- compaction 是当前三个基线中最强实现：0.7 窗口输入比、map-reduce 完整覆盖 + sha256 coverage manifest（`conversation_summarizer.py:269-407`）、20K 输出预算对齐 CC COMPACT parity、无机械语义 fallback（失败走诚实降级标记）。强于 FreeCode 单摘要（`services/compact/compact.ts:541-653`）与 codex 单 turn 摘要（`core/src/compact.rs:123`）。
- 工具治理七道有序门禁（zone→tenant→guard→mcp→capability→dangerous→hooks，`tools/governance.py:1054-1075`），deny/ask/unavailable 全 typed 且只冻结目标工具，带教学信息。
- 记忆系统全链路保留模型判断权：T2 包三个 LLM（summary/labels/独立 review），无模型配置时 **held 不降级**（`t2/segment_package.py:259-287` 明示 "no mechanical summary fallback"）；retriever 用 LLM 语义选择器，模型失败走 ref-only 不机械代选（`retriever.py:294-306`）。
- 对 hermes benchmark：动态激活（`memory/activation.py` + retriever）优于 hermes 的会话开始冻结快照注入（`hermes agent/tools/memory_tool.py`）；压缩用主模型+coverage ledger 强于 hermes aux 模型方案。

扣分面（实证）：

- **GV-01/GV-02 的原扣分事实已由本轮代码修复移除**：自治 Agent 的外部可见动作 ASK 进入 durable Approval ticket；批准后沿同一 ToolRuntimeService kernel 精确重放并执行，硬拒绝仍保持拒绝。真 PG worker 事务验收因 Docker 不可用仍是 Acceptance 缺口，不能把本地代码闭环外推为生产闭环。
- **GV-08 的 live Model Agency 违规已由本轮代码修复移除**：secret-shaped 文档、fixture 和模板不再被 regex 提升为凭据事实；只有 tenant-scoped credential authority 返回的 exact active bytes 才能阻断原始工具参数或在入站/出站 seam 被精确遮蔽。除禁止字节外，模型输入、工具结果与最终表达保持 byte-faithful；regex 只留下 count-only audit candidate。
- **SA-01 已完成代码修复**：turn 级 cache-miss token 预算现从 invoker 派生值进入 kernel live loop；只在还有新工具动作时阻止继续放大，并形成 exact usage、typed event、provider result receipt、恢复持久化与 Web 终态。permission resume 从 durable logical-root ModelResult 恢复同 turn 累计值，同时只对新增量计费；已完成答案不因预算文本或阈值被平台重写。
- HN-01/HN-02：原先宣称的主动管理循环（proactive_employee_loop、policy_replay）在源码中不存在；当前 heartbeat 已收窄为纯记忆固化（无工具执行器）。DOC-01 已把两项改为明确的已知缺失，不再假装存在；能力本身未因此被实现。
- **尚未证明的北极星主张**：没有在相同模型、相同授权证据与相同任务集上运行 Hive 与 hermes 的行为级对比，因此本报告不得把结构优势外推为“智能质量至少一样好”。

### 3.2 北极星 Goal 2（公司级控制中台）：**主体成立，证据面有缺口**

- 成立：RLS 60+ 迁移 ENABLE+FORCE、strict 启动拒 superuser/BYPASSRLS（`rls_runtime_guard.py:89`）、唯一旁路强制 reason；secrets Fernet+HKDF 密钥环、无 master key 拒启动；AI 资产（Agent/Skill/Workflow/外部能力）revision/usage/rollback 接在真实变更点；租户安全事件哈希链 + immutable/no-truncate 触发器 + entrypoint 启动 gate。
- 已修：platform_admin 真实跨租户身份帧强制生成 operator-only 审计 receipt，审计不可用即 fail-closed（GV-04）；Desktop LLM proxy 消费同一 quota/counter/append-only usage 权威并受 Redis 路由限流，SSE 成功终止与 durable metering 绑定（GV-05）；operator security 现有独立不可变 v2 哈希链、legacy 锚点、platform_admin-only 查询/验链面，Desktop 自报证据也与服务端事实明确分层（GV-06）。
- 已修：UI-02 的真实消费断点是 GuardPolicy 与 global FeatureFlag，而不是三个等价的治理 router。Company Control Plane 现消费业务化 Action Guardrails，Platform Admin 现消费 typed Feature Rollout；`config_history` 只是退役 compatibility adapter，canonical AI asset history/rollback 早已由 `/enterprise/ai-assets/*` 与 `WorkspaceAIAssetsSection` 消费。A2A-03 与 UI-09 也已代码闭环；这些路径的真 PG/Redis/生产数据/浏览器验收，以及 GV-06 真 PG、滚动多实例和生产查询仍在 §11，不把代码闭环外推为生产闭环。

### 3.3 Model Agency Boundary 裁决：**已消除已知 live 违规，保留一处观察项**

多数模块未发现关键词/正则/计数器替代模型语义判断、饿输出或静默裁剪。工具 preflight 的 PL4 模式扫描违规已完成代码闭环，但真环境 credential inventory、历史 backfill 与 provider stream 仍须验收：

- 正面证据：LoopGuard 启发式只 warn 不裁决，硬终止需"工具自报重试耗尽+无副作用+进度 token 未推进"三重机械证据，终态解释仍由模型撰写（`loop_guard.py:194-212`、`turn_orchestrator.py:2700-2733`）；`infer_task_profile` 故意返回中性值、永不降级主模型（`context_budget.py:118-126`）；work ledger 明示不做关键词分类（`agent_work_ledger.py:982-997`）；A2A collaborator 注入显式"永不机械裁剪"（`a2a_collaborators.py:21`）；测试钉住"low_confidence 不机械变 abstention"（`test_write_gate.py:17`）、"平台不得伪造模型回答"（`test_engine.py:108`）。
- **GV-08（代码闭环；真环境验收待补）exact authority 取代 pattern authority**：`_build_tool_preflight_input` 只消费 `ToolExecutionContext.exact_secret_boundary` 对模型原始参数的 exact match；`PrivacyLayer._CREDENTIAL_PATTERNS` 只产生 `credential_candidate_count`，不再改变 sensitivity、sanitized bytes 或执行结果。权威 inventory 在 tenant-pinned transaction 中汇合启用的 LLM key、Agent/tenant channel secret、Tool password/credential config 与 MCP OAuth binding；加载失败返回 typed retryable unavailable，不能静默降级为空 inventory。ToolRuntime 在 hook、Plan Mode、runtime-owned 参数注入、governance 与 executor 之前阻断原始参数中的 exact active secret，而受信 runtime 注入的 credential 不会被误拦。
- **输入、输出、证据与恢复 seam 已贯通**：模型初始/中途输入、hook context、流式正文/thinking、tool event/result/error、model response commit、T0、Session V2、Web chat、Channel runtime 与 durable Channel inbox 均在各自最早可用 authority seam 只遮蔽 exact bytes，并生成不含 secret value 的 source-ref/count receipt。typed channel reply target 和 inbox transport token 在既有 JSON/JSONB 列通过 keyring 透明加密；provider raw body 先按 exact tenant inventory 遮蔽，原 body SHA-256 仍作为 collision authority。文件路径在扫描后绑定 immutable snapshot，fallback download 绑定 SHA-256，消除 scan→execute 的 mutable-file TOCTOU。
- **迁移与回归**：`migrate_channel_secrets` v3 默认输出 count-only dry-run；显式 `--apply --confirm` 才轮转 channel config、reply target、ingress transport token，并对 legacy ingress 当前 exact credential 做 backfill。benign `api_key=sk-example...` 写入、嵌套 active secret、stream chunk split、模型原始输出保真、错误文本、T0/Web/Session/Channel 入站、typed transport encryption/offboarding 与 legacy dry-run/apply 均有回归。最终两个分别可运行的聚合为 `330 passed, 10 skipped` 与 `407 passed, 57 skipped`；skip 仍要求真 PG/keyring/provider 验收，不外推为生产闭环。
- **观察项**：heartbeat 不进全工具循环是显式设计收窄，当前源码下不是 Model Agency 违规；但它使 HN-01 的"heartbeat 准备低危工作"成为文档虚构。

---

## 4. CCPlus 基线账本与源码对照

基线从 FreeCode 当前源码直读建立（`free-code-main/src`，31 条），Hive 映射与判定来自各模块生产路径追踪。差异类别：缺失 / 语义退化 / 可接受实现差异 / 工程增强 / 主动超越 / 排除。

| # | 能力（生命周期节点） | FreeCode 语义要点（证据） | Hive 当前映射 | 差异类别 | 七原子状态 |
|---|---|---|---|---|---|
| 1 | 系统提示组装 | 静态段+动态 registry 段，`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 保 cache（`constants/prompts.ts:444,114`） | prompt_sections + 动态 suffix（skill catalog `agent_context.py:230`、`invoker.py:656`）+ canonical cache anchors（`prompt_cache.py:96`） | 工程增强 | 闭环 |
| 2 | CLAUDE.md / 项目指令 | 嵌套目录、条件规则、`@` include（`context.ts:155`、`claudemd.ts:618`） | soul.md + governed identity/charter 段（`prompt_sections/identity.py:34-53`、`agent_manager.py:122-184`） | 可接受差异+治理增强 | 闭环 |
| 3 | 上下文组装顺序 | system 块（git status 截断 2000 字符）+ user 块（`context.ts:116,22`） | 分层 context 组装 + ActivationContext fail-closed（`memory_service.py:199-218`） | 可接受差异 | 闭环 |
| 4 | 工具清单与发现 | 40+ 工具、deny 规则过滤（`tools.ts:193,262`） | 100+ 注册工具 + L2 发现链（`tool_search`→deferred schema 扩展 `kernel/engine.py:495`，resume 恢复 `:875-900`） | 工程增强 | 闭环 |
| 5 | 权限模式 | default/acceptEdits/plan/bypassPermissions/dontAsk（`types/permissions.ts:16`） | capability gate 五段判定 + session permission + Plan Mode（`capability_gate.py:327-523`、`session_permission_runtime.py:520-635`） | 可接受差异 | 闭环 |
| 6 | 权限规则引擎 | allow/deny/ask 三类、deny 优先、分层规则源（`permissions.ts:473,1071`） | GuardPolicy 精确工具名+argument_equals 机器契约（`tools/guard_policy.py:32-85`）；无命令级 DSL | 可接受差异（execpolicy 可吸收，见 §10） | 闭环 |
| 7 | Hooks 事件面 | 28 事件（`entrypoints/sdk/coreTypes.ts:25`） | 30+ 事件（`runtime/hooks.py:27-121`），PRE/POST_TOOL_USE 支持 block/modified_args（`kernel/engine.py:1895,2272`），receipts 落 invocation_spans | 主动超越 | 闭环 |
| 8 | Hook 扩展与产品边界 | 本地开发者 settings 可配置任意 command 钩子（`types/hooks.ts:238`） | 员工面已移除内部 Hook；平台健康投影与 raw Developer diagnostics 分离；内置 Hook per-agent 不可变，当前只允许已注册 plugin Hook 经 platform-admin+strict audit 修改。通用 Developer/Extension 产品面仍无已接受契约 | **员工边界已收正；通用扩展面不伪装为员工功能** | 代码闭环 / 产品契约待定 |
| 9 | Plan Mode | 只读探索→plan 文件→ExitPlanMode 批准；AskUserQuestion 澄清（`tools/EnterPlanModeTool`、`ExitPlanModeTool`） | 确认边界机械强制（`tools/service.py:1153,1339` 只读 block）、hash/版本绑定确认（`plan_mode_service.py:571`）、澄清卡（`handlers/plan_mode.py:482-601`） | 工程增强 | 闭环 |
| 10 | TodoWrite | 模型自维护清单，纯认知记账（`tools/TodoWriteTool`） | Work Ledger（track_todo/record_finding/read_ledger），明示不做关键词分类（`agent_work_ledger.py:982-997`），跨 compaction 恢复注入 | 语义等价 | 闭环 |
| 11 | Skills 渐进披露 | 清单 1% 上下文预算、描述截 250 字符、调用时载入全文（`SkillTool/prompt.ts:22,27`） | `load_skill` 不限字符（`workspace.py:304-406`）、catalog 预算 4000+tool_search 兜底、可执行组件走受治理运行时（`skill_runtime.py:81`） | 工程增强 | 闭环 |
| 12 | Subagent（Agent 工具） | 独立上下文、按定义过滤工具、concise report 返回（`AgentTool.tsx:196`） | spawn_subagent：standalone prompt 不继承宿主身份、结果未截断（`agents/subagent.py:1127,1262,1306`）、前后台双模式 | 工程增强 | 闭环 |
| 13 | Subagent fork/resume | sidechain transcript 可 resume（`forkSubagent.ts`、`resumeAgent.ts`） | replay-safe resume（`subagent_run_service.py:81,1106,1478`）、RuntimeTask worker 认领、完成唤醒 drain | 工程增强 | 闭环 |
| 14 | Compaction 触发 | 阈值=有效窗口−13K buffer（`autoCompact.ts:62-90`） | 75% proactive + 60% microcompact + PTL reactive 三道（`ccplus_contracts.py:124-125`、`turn_orchestrator.py:1530-1816`） | 主动超越 | 闭环 |
| 15 | Compaction 质量 | LLM 单摘要 + 重挂 plan/skill/附件（`compact.ts:541-653`） | map-reduce 完整覆盖 + sha256 coverage manifest + 20K 输出（`conversation_summarizer.py:269-407`），失败 hold 不机械兜底 | **主动超越** | 闭环 |
| 16 | Transcript 持久化 | 每会话 JSONL、sidechain 子目录、50MB 读限（`sessionStorage.ts:202,229`） | ChatTranscriptEvent（云端事务事实）+ T0 events.jsonl/source.md 双投影 hash 链（`memory/t0/ledger.py:632,655`） | 工程增强 | 闭环 |
| 17 | resume/continue/fork | `-c/-r/--fork-session/--resume-session-at`（`main.tsx:988-991`） | durable RuntimeTask 重启恢复（`resume_persisted_web_chat_runs` `web_chat_runtime.py:2807`、SKIP LOCKED reclaim `runtime_task_claim_service.py:175-210`）+ fork API（`chat_sessions.py:446,489,498`） | 工程增强 | 闭环 |
| 18 | checkpoint/rewind | 编辑快照 + rewind 恢复文件态（`fileHistory.ts:86,347`） | workspace 快照（每 user event）+ projection rewind + branch rewind 9 种 mode（`conversation_branch_service.py:326`） | 可接受差异（双 rewind 语义并存，SA-02） | 局部闭环 |
| 19 | Slash commands | 内建+`.claude/commands/*.md`（`commands.ts:476`） | session commands（rewind 等 `session_command_runtime.py`）；无用户自定义命令面 | 可接受差异 | 局部闭环 |
| 20 | MCP 客户端 | stdio/SSE/HTTP、OAuth、elicitation（`services/mcp/client.ts`） | MCP import/call + authz（拒 token passthrough/URL userinfo/access_token，`mcp_authz.py:62-104`） | 工程增强（治理） | 闭环 |
| 21 | 后台任务 | run_in_background + 持久任务态（`tasks/LocalShellTask`） | RuntimeTask + worker claim + typed delivery receipt | 工程增强 | 闭环 |
| 22 | 取消/Esc | abortController 贯穿流式与工具（`useCancelRequest.ts:63`） | durable ControlInput + 幂等键 `cancel-run:{run_id}` + Redis cancel bus + fence CAS 结算 | 工程增强 | 闭环 |
| 23 | 成本/token 追踪 | 会话成本累计、resume 恢复（`cost-tracker.ts:278`） | token_tracker 三层配额 fail-closed（`invoker.py:1507-1537`）；turn cache-miss budget 在新工具动作前 typed hard-stop，permission resume 从 committed ModelResult 恢复累计量且只计费新增量（SA-01） | 工程增强 | 闭环 |
| 24 | 多模型路由 | 主循环覆盖 + 429/529 fallback（`model.ts:95`、`withRetry.ts:163`） | 429-only 重试×10+Retry-After、overload→fallback_model、账户类错误不 fallback（`llm_client.py:426-487`、`turn_orchestrator.py:1816-1907`） | 语义等价 | 闭环 |
| 25 | Team/多 agent | 进程内 teammate + 消息（`TeamCreateTool`、`SendMessageTool`） | A2A：send_message_to_agent 同步咨询 + delegate_to_agent 异步委派 + Lease/Signal 原语 + 权限收缩（DelegationToken）；本地委派结果已回来源 session；协作组人类管理面已接通 | 主动超越（但见 A2A-02） | 局部闭环 |
| 26 | 定时/触发 | cron 调度工具（`ScheduleCronTool`） | trigger_daemon 15s tick + fire lease + RuntimeTask | 工程增强 | 局部闭环（SA-03） |
| 27 | Workflow | FreeCode 侧为 stub（`tools/WorkflowTool/` 仅 constants）——**非 CC parity 债务** | Hive-native workflow：RuntimeTask+PG step/leaf journal+quota+gate+trigger+admin ops | 主动超越 | 局部闭环（SA-06） |
| 28 | 输出样式/statusline | output style 注入系统提示（`outputStyles/`） | 前端表达层承担（userFacingRuntimeStatus 人性化） | 可接受差异 | 闭环 |
| 29 | 会话恢复中断检测 | TurnInterruptionState（`sessionRestore.ts:409`） | terminal ghost 对账 + 尝试上限隔离（`web_chat_runtime.py:4880,4911`） | 工程增强 | 闭环 |
| 30 | 远程会话 teleport/CCR | 依赖 Anthropic 托管远程执行（`main.tsx:735-764`） | — | **排除**（供应商私有远程能力，依据充分） | 排除 |
| 31 | 文件历史跨会话复制 | resume 复制 file history（`fileHistory.ts:922`） | workspace 快照上限 50/1000 文件，跨会话文件版本 UI 缺失（UI-05） | 可接受差异 | 局部闭环 |

**基线账本结论**：31 条中闭环 23、局部闭环 7、缺失 0、排除 1。SA-01 的 turn token budget 已从局部闭环提升为闭环。原报告把 FreeCode 的本地开发者 Hook settings 直接等同于 Hive 员工产品面，因此误报了一条“用户自定义 hook 缺失”。当前真实问题不是员工少了一个 Hook 配置入口，而是平台内部 Hook 被错误暴露且可由员工管理权限禁用；该泄漏也已由 UI-09 修复。是否建设受治理的 Developer/Extension Hook 面，必须先有独立产品与权限契约；在该契约成立前，不得把员工面“不支持自定义 Hook”记为缺失。

---

## 5. 单 Agent 审查

### 5.1 闭环能力（择要，证据见 §4 账本）

模型循环、工具治理、compaction 三道、恢复、取消幂等、Plan Mode、Subagent、Work Ledger、Skill 渐进披露均为闭环。其中超出基线、必须保持的实现：

- **durable web chat run**：先 commit `RuntimeTask(pending)` 再唤醒 worker，执行与 socket 解耦；启动恢复扫描 + SKIP LOCKED reclaim + ghost 对账（`web_chat_runtime.py:2059,2429,2807,4880`）。
- **active-run 唯一性**：会话 advisory 锁 + DB 部分唯一索引（`models/runtime_task.py:90-107`），并发消息降级为队列注入。
- **取消幂等**：三入口统一进 durable ControlInput，幂等键+确定性 control_uuid+fence CAS 终态结算（`session_live_input.py:401-455`、`session_control_input.py:991`）。
- **工具结果驱逐**：50KB/result、200KB/round，sha256+read_file 指针可精确找回，写失败保完整证据（`turn_orchestrator.py:2305-2320`、`engine.py:3523-3548`）。

### 5.2 断点与局部闭环

**SA-01（代码闭环，原 P1）turn_token_budget 已进入 live kernel。**
- 原断裂原子：Authority→Execution。根因是 Session context controller 重构删除了 `engine.py` 的 gate 与读取，却保留 invoker 派生、recovery manifest 字段、消息函数和两条反向测试，形成假接线。
- 当前执行：`turn_orchestrator.py` 每次 provider round 后使用 `extract_usage_tokens` 的 cache-miss 口径累计；`0` 是可信零使用量，只有 `None` 才进入可观察估算。达到正预算且 response 仍提出工具时，在任何新工具执行前记录 exact tokens、发 `turn_token_budget_exhausted`、持久化恢复状态并以 `TerminalReason.TOOL_BUDGET` 退出。
- 证据与恢复：provider `model_response_commit` 已先产生 durable result receipt，budget result 继续携带该 receipt；permission-resume 读取同 tenant/session/run/turn 的 `round_committed` ModelResult，只汇总恢复点之前的 logical root，排除 `output-continuation` 行，provider usage 缺失时才按已封存 wire request/response 做可观察估算。恢复后的预算与 `turn_tokens_used` 使用累计值，`record_token_usage` 只提交本次进程新增 delta，避免预算重置和重复计费。事件同时进入 callback/transcript 与 result parts，Web finalizer 将 typed reason/turn_tokens_used 落入 failed terminal metadata。
- Model Agency 边界：预算来自明确资源配置，不读取自然语言；不裁剪授权上下文、不移除工具资格、不改写已经完成的模型答案。仅当模型还要扩大工作时拒绝下一批未执行工具；普通文本即使包含 `[Runtime Limit]` 仍按 `TURN_STOP` 原字节完成。
- 回归：原真空用例反转后先红 `1 failed / 2 passed`；零 cache-miss 被错误回落为整段 prompt 估算再独立红 `1 failed / 99 deselected`；resume 累计/新增计费合同先红 `1 failed / 114 deselected`，durable logical-root 汇总合同先红 `2 failed / 118 deselected`，Web→invoker 传递先红 `1 failed / 13 deselected`。最终 14 文件聚合 `374 passed, 7 skipped`；7 个 skip 均为本机 Docker/Testcontainers 不可用的 Session permission 真 PG 文件；Ruff/format/compileall 全绿。

**SA-02（局部闭环）双 rewind 语义并存**：projection rewind（同会话 `session_command_runtime.py:1239-1290`）与 branch rewind（新会话 `conversation_branch_service.py` mode="rewind"）语义不同、均有前端消费。非双事实源，但命令面需统一文档。

**SA-03（局部闭环）trigger/heartbeat 进程内执行、恢复需人工和解**：`_tick` 建 RuntimeTask(running) 后 `asyncio.create_task`（`trigger_daemon.py:2542`、`heartbeat.py:1502`），不经 claim worker；重启后 session-bound run 置 `needs_reconciliation` 不盲重放（副作用安全的设计裁决）。代价：重启后执行中的 trigger 醒不过来，需管理员介入，且该队列无 UI 曝光（未证实）。方向：纳入 LEASE_RECLAIMABLE + worker 分发（web_chat 已证明可行），或显式记录裁决并补运营面。

**SA-04（撤销“员工用户自定义 hook 缺失”，重分类为产品边界）**：FreeCode 的 Hook 是本地开发者 harness 扩展面，不能直接映射为企业员工设置。Hive 员工与其 Owner/Manager 应配置业务可理解的权限、审批、自主性和故障结果，不应知道或选择 `turn_stop`、`post_compaction`、handler key、required/advisory 等运行时实现。若 CCPlus 后续确认需要 Hook 扩展能力，应单独定义 Platform Developer/Extension 面：只承载显式安装的扩展，声明目的、事件、权限、数据可见性、副作用、审计、版本与回滚；平台内置 Hook 与扩展 Hook 必须分 namespace、分权限、分消费面，且不得开放任意本机命令。该通用扩展面尚无已接受产品契约，因此本报告不把它登记为当前员工产品缺失；已证实的 UI-09 泄漏现已代码修复。

**SA-05（代码闭环；真 PG lease 验收待补）REST confirmed-plan handoff 不再 500。**
- 原断裂原子：Execution↔Acceptance。修复前 `api/workflows.py` 传入 `action_kind="start_workflow"`，`intent_type_for_action` 因 canonical vocabulary 缺项抛 `ValueError`；原 API 测试用 fake gate 遮蔽了生产分支。
- 当前接线：`plan_mode_core.ACTION_KINDS/_ACTION_INTENT` 将 `start_workflow` 精确映射为 `in_session_execution`；`ToolMeta.plan_gate_action_kind="bridge:self"` 与 registry 明确记录它是 plan-governed 自有确认面。ToolRuntimeService 仍不对 start_workflow 施加通用硬门，durable `preview_workflow`→显式 `start_workflow` 语义和“不自动进入 Plan Mode”合同未改变。
- 路径证据：`test_confirmed_plan_reaches_real_gate_and_starts_exact_workflow_preview` 不再 monkeypatch `_plan_gate_check`，完整走 REST preview、durable claim、生产 `_plan_gate_check`、真实 `PlanModeGate.check/intent_type_for_action`、authorization lease port、`start_ephemeral_workflow_for_agent`，断言 exact preview hashes/target binding 后返回 200。旧 fake 测试保留为 API 参数投影单测，不再承担 wiring proof。
- 命令证据：7 个直接相关 test 文件聚合 → `146 passed, 1 warning`；再加入 `test_exit_plan_mode_tool.py`、`test_plan_authorization_lease.py`、`test_plan_mode_service.py`、`test_plan_mode_delegation_handoff.py`、`test_plan_gate_helper.py`、`test_plan_mode_rest_gate.py` 的 13 文件合并命令 → `239 passed, 1 warning`；相关 Ruff → `All checks passed!`。
- 数据/恢复：无 schema、存量数据或配置变更；preview claim、lease evidence 与 workflow launch 仍沿既有 durable authority，修复只补 canonical action vocabulary 和真实消费测试。
- Acceptance 边界：`test_plan_authorization_lease_postgres.py -rs` 的 10 项真 PG 用例均因 Docker connection refused skip；本地已证明 500 根因与真实 gate wiring 消除，不宣称 PG 事务验收闭环。

**SA-06（局部闭环）tool 路径 start_workflow 确认强度弱**：确认证据是 agent 自己所在 turn 的 `turn_id`（`handlers/workflow.py:165-180`），`claim_workflow_preview_record` 只校验非空。"preview 后须用户同意"靠工具描述约束，无机械强制；高风险动作另有 preflight/capability gate 兜底，故定性为确认强度弱于文档表述，非治理绕过。

**SA-07（局部闭环）ChatSession.summary 与 T2 summary 双摘要源**：`memory_service.py:1171-1231` LLM 写 DB summary 供 episodic 检索；T3 consolidator 只读 T2 包。同一 session 两份摘要无语义对账。方向：episodic 检索以 T2 读模型（`t2/read_model.py`）为权威，DB summary 降为 UI 投影。

**SA-08（局部闭环）T2→chat prompt 固化延迟**：新知识要等 heartbeat T3 core 或 dream 才进入 resident/wiki（设计意图），"刚说过的事下次对话记不住"的感知风险存在；`save_memory` 显式覆盖层是即时补救通道。建议 UI 曝光"已记住/待巩固"状态。

**SA-09（死代码，验收瑕疵）**：`handle_web_chat_disconnect`（web_chat_runtime.py:1742）、`start_heartbeat`（heartbeat.py:1612）、`ConnectionManager`（websocket.py:78-150）、`_claim_pending_reply_suffix_for_session`（websocket.py:206-232）、`GET /chat/{agent_id}/history` + 前端 `getChatHistory`（遗留 `web_{user_id}` 方案，潜在双事实源）、`llm_utils.py` 纯 re-export shim、`engine.py:3094/3135` 惰性 shim、`agent_work_ledgers` 死表（真实账本在 AGENT_DATA_DIR 文件）。

---

## 6. Hive Connect 与 Hive Native 审查

### 6.1 Hive Native 主体：记忆与自进化（闭环，Hive 最强差异化面）

T0→T2→T3→soul 全链路经生产路径验证（证据见各条目），且**全链路 LLM-primary、失败一律 hold**，是 Model Agency 的教科书实现：

- **T0**：`append_t0_session_event`（`memory/t0/ledger.py:98`）JSONL+MD 双投影 hash 链；web/trigger/delegation/subagent 经 control bus bridge（幂等+顺序闸门+sweep）；listener 常驻 `main.py:686-688`。
- **T2**：live 调用者=TURN_STOP/IDLE/TRIGGER_END/DELEGATION_END hook → `run_t2_segment_package_job`；三个 LLM（summary/labels/独立 review）；无模型配置时 held 不降级；崩溃恢复 job manifest + 启动 sweep + heartbeat sweep。
- **T3**：两入口（heartbeat direct core 120K 输入带 coverage / agent 工具）；Platform Gate 强制 `t2://`/`explicit://` provenance + review rubric + 原子事务提交；two-plane 目标文件（`memory/self|profiles|knowledge|milestones`），docs 的 episodes/user/worker/capabilities 已退役为 LEGACY_T3_FILES。
- **soul**：dream RuntimeTask → LLM IdentityPromoter + Soul Memory Gate review + Platform Soul Gate 物理检查（frozen charter/schema/source_refs/base drift）→ owner 审批门 → rollback 快照 + 原子写；kernel 消费（frozen prefix + compaction 后恢复）。
- **召回**：ActivationContext fail-closed（principal 解析失败即 blocked_authority）→ resident profile plane（超预算告警不裁剪）→ retriever（explicit overlay+wiki+DB summary 候选→敏感性剥离→LLM 语义选择 ≤5 条 + coverage receipt；模型失败 ref-only 不机械代选）。
- **skill 自进化**：候选（T3 capability marker）→ LLM 起草 → 硬验证（sandbox artifact gate + `run_evolution_verification`）→ 独立 LLM referee → 事务化 commit → provisional trial 写 rollback baseline → 模型评审 promote/rollback。`skill_candidate_loop_v1` 默认开。
- **写旁路证伪**：唯一写面 `explicit_overlay.py`（LLM gate）；工具描述明示 "Direct file edits under memory/ are refused"；未发现绕过 Platform Gate 的 T2/T3 直写。

### 6.2 Hive Native 断点与缺失

**HN-01（文档闭环；能力已知缺失）proactive_employee_loop 不存在**（主审 `ls` 复核确认）。当前 heartbeat 已收窄为无工具执行器的纯 T3 固化（`heartbeat_t3_core.py:49-61`）；`AGENTS.md` 现按真实 `heartbeat`/`auto_dream`/`evolution_daemon` 路径说明，并明确没有 live `proactive_employee_loop`，不再把缺失能力宣称为已实现。若未来重建，必须另立 charter+Checkpoint 合同和验收，不由本次文档修复暗示存在。

**HN-02（文档闭环；能力已知缺失）memory/policy_replay 不存在**（主审复核确认）。`AGENTS.md` 已删除“policy tuning 必经 replay guard”的假完成合同并明确没有 live `memory/policy_replay`；未来建设仍需独立实现和证据。

**HN-03（缺失/已退役）Objective 系统**：`objective_service.py`/`api/objectives.py`/`Objective` model 均不存在；继任者 `AgentSessionGoal` + `api/session_goals.py` + memory goal_terms + 前端 SessionGoalPanel 已闭环。退役成立，AGENTS.md 实体清单失真。

**HN-04（已知缺失）Enterprise Knowledge 未实现**：无 `search_company_kb/read_company_kb` handler；HR 工具明示 "Company knowledge is not implemented yet"（`handlers/hr.py:73`）；退役测试钉死无 enterprise_kb 路由；施工规格 `docs/company-knowledge-base-spec-2026-07-07.md` 未落地。公司知识需求降级为 unresolved knowledge debt（`hr.py:227-237`）。按北极星这是 Goal 2 的实质性缺口，但不是回归——标已知缺失。

**HN-05（代码闭环；真 PG 验收待补，原 P2）Knowledge grant 变更已进入同事务审计**：

- **Authority/Execution**：审计放在 `PersonalKnowledgeService.create_personal_grant/delete_personal_grant`，覆盖 API 和其他直接 service caller，而不是只补某个 HTTP handler。只有 Owner 通过完整 resource/grantee/binding 校验后才追加事件；非 Owner、非法绑定、tenant grantee 不存在或 grant 不存在均无事件。
- **Evidence**：create/update/reactivate 统一写 `personal_kb.grant.upserted` 并用 `operation` 区分，revoke 写 `personal_kb.grant.revoked`。两者记录 tenant/actor、grant/resource/grantee、permission、requester/session/purpose/delegation、sensitivity、expiry/revoked time 与 SHA-256 binding key；不复制调用者任意 metadata。Agent grant 同时绑定 `AuditLog.agent_id`，user grant 保持为空。业务对象与 AuditLog 在同一 session flush、由外围 request transaction 一次 commit；audit constraint/flush 失败不会留下已提交授权变化。
- **Recovery/历史**：复用既有 append-only `audit_logs`，无 DDL/config/data migration。过去没有 event/actor/时序事实的 grant 变化不能从当前行可信还原，因此不伪造历史事件；当前 `KnowledgeGrant` 行仍是现状 authority，部署后的每次 authority mutation 均有机械事件。
- **回归**：新增合同先得到 `2 failed, 1 passed`（create/revoke 均无 AuditLog，非 Owner 已正确无事件）。最终 service/API/真实 PG 定义三文件聚合 `61 passed, 11 skipped`；11 项全因 Docker/Testcontainers connection refused，其中 create→commit→revoke→两条 tenant audit 的真 PG 断言已定义。3 个 Python 文件 Ruff/format 与 diff check 全绿。

**HN-06（已退役，文档残留）**：`viking_client`、`knowledge_inject`、`extract_queue`、`extract_agent` 均已删除（主审 `ls` 复核确认），退役测试钉死（`test_company_knowledge_retirement.py:20-22`）。Personal KB tool-only 边界反而因此更干净：全仓无任何 prompt 组装模块 import knowledge model；replay 时内容替换为 pointer-only 投影（`web_chat_runtime.py:1314-1380`）。

**HN-07（局部闭环）charter 校准提案链孤儿**：`propose_charter_calibrations_from_feedback`（auto_dream.py:1991）及下游 `decision_trace.calibration_candidates` 外部消费零调用者。

**HN-08（增强空间）KB 检索纯词法**：PG tsvector+ilike（`personal_knowledge_index_search.py:89-96`），无向量/语义召回；召回质量依赖模型构造查询。非违规，是能力增强空间。

**HN-09/HN-10（可吸收缺失）**：hermes 的 session_search（FTS5 跨会话原文零成本回忆）与 verify-on-stop（编辑后无新证据时的有界追问，policy-only 不阻断）Hive 均无（grep 反向证伪）。均不违反 Model Agency，建议吸收。

### 6.3 A2A / Delegation

闭环面：send_message_to_agent（pair session+transcript 双写走唯一写入器）、delegate_to_agent（RuntimeTask 落库+重启恢复+结果投影父 session+唤醒通知）、委派权限收缩（tool_profile→allowed/excluded + DelegationToken 签发→governance 校验，子 agent 用自身 capability gate 非继承父权）、Lease（PG ON CONFLICT 原子，重放幂等，终态释放）、Agent Card/interoperability profile 诚实标注 `not_exposed`。

**A2A-01（断点，与 GV-01 同根）**：见 §7 GV-01。
**A2A-02（局部闭环）Signal 双后端读写不对称**：写端经 gateway 默认落 PG；`consume_subagent_signals`（subagent.py:1399）直读内存且无生产调用方（死 API）；`COORDINATION_BACKEND=memory` 时唤醒静默丢失。当前默认 postgres 下闭环成立；需删死 API+加 backend 切换契约测试。
**A2A-03（代码闭环；真 PG/浏览器验收待补）A2A 协作组管理面已接通**：
- 原报告“后端全端点闭环、只缺前端”不准确。`build_a2a_collaboration_read_model` 有意只返回可调用的 same-owner/public/active group；pending/rejected/revoked 不应进入模型 prompt，但此前也没有独立 human management projection。因此目标 Owner 看不到 pending 邀请、前端拿不到真实 membership key，即使直接挂五个按钮也无法工作。
- Authority/Execution：新增 `GET /agents/{agent_id}/a2a/management` 与 group-scoped bounded candidate search，只对该 Agent 的 `manage` authority 开放并固定 tenant，System HR 仍排除。目标 Owner 只在自己的目标 Agent 路径上 approve/reject/revoke；org/platform admin 代审必须填写治理理由。approve/reject 只接受 pending，revoke 只接受 active，非法重放返回 409；invite role 是 exact enum，reinvite 会清除旧批准/拒绝/撤销状态。create/invite/approve/reject/revoke 均在业务 commit 前写 canonical tenant audit。
- Consumption：`AgentDetail` 把真实 `canManage` 传给 `AgentA2ASection`。普通 use-only 用户不加载 management query；Manager 可创建组、按名称/角色搜索同租户 active non-HR 数字员工、邀请/再次邀请，并在管理卡中处理 pending/rejected/revoked。UI 使用业务状态和 Owner 显示名，不渲染 raw membership/agent/owner ID；reject/revoke 有二次确认。普通 callable 区仍只显示已批准成员，管理数据从未接入 runtime prompt。
- Evidence/Acceptance：首轮 RED 后端为 `10 failed, 2 passed`，前端为 `3 failed, 27 passed`；状态机加固 RED 为 `2 failed, 15 passed`。最终后端定向 `17 passed, 1 skipped`，前端 adapter/section/AgentDetail 聚合 `143 passed`，Ruff/format、JSON 解析、TypeScript/Vite build 与 bundle budget 全绿。唯一 skip 是 Docker/Testcontainers unavailable 的真 PG disclosure-boundary 用例；双 Owner 真实浏览器 create→search→invite→approve→callable→revoke 仍交 §11。
**A2A-04（缺失/死代码）**：Sentinel 全家零生产调用；`AgentAgentRelationship` 孤儿表（A2A 权威已迁 AgentCollaborationGroup）。DOC-01 已停止把二者写成 live authority；源码删除与表迁移仍属于后续 P3 清理。

### 6.4 Hive Connect（canonical 外部消费者与仓内 legacy bridge）

当前产品 Skill 安装的是 npm `@hiveclaw243/hive-connect`，不是仓内 `@hiveclaw243/hive-bridge`。两套客户端必须分开裁决，legacy 实现的缺陷不能外推为 canonical Hive Connect 缺失。闭环面：设备配对（user_code/device_code 哈希+15min+一次性）、bridge token（sha256+user/tenant 绑定+scope allowlist+可 revoke）、云→本地持久队列+WS 投递+幂等键、断线重连（delivery lease+stale 重排+5 次后 needs_reconciliation）、canonical 客户端本地 durable execution receipt、浏览器聊天 UI、文件上传。

**HC-01（代码闭环；真 PG/真实设备唤醒待验收，原 P0）A2A 本地结果已回到来源 Agent session。**
- 原断裂原子：Consumption（孤立结果）。修复后状态：代码闭环。
- 权威与输入：`enqueue_channel_message` 只在 `source=a2a + execution_target=local_agent` 时接受完整 server-issued `ExecutionPrincipal`，并把 tenant、source Agent、requester、parent `ChatSession`、target Agent 与 target owner 逐项 exact 校验；任一漂移返回 409，目标 channel session 与首条 message 一并 rollback，不产生孤立会话。相同 A2A conversation 由 transaction advisory lock 串行并复用 active channel，幂等重试不再积累无消息孤儿 session。后续 turn 允许 `root_runtime_task_id` 变化，但 tenant/source/requester/parent session 不可变化。
- 执行与恢复：`record_channel_result` 在提交 Local Message 终态、result event、目标侧 transcript 与 span 的同一事务中调用既有 `enqueue_completion_notification`，写入 `source_kind=a2a_delegation`、`task_type=a2a_local_delegation`、`delivery_mode=parent_continuation`；既有 Runtime Result/Notification Outbox worker 因此自动续跑来源会话。重复 result 请求只重用已持久化的原 output/artifacts 幂等补齐 outbox，不允许重复 payload 改写事实。
- 消费：本地委派仍返回 stable `message_id`，并明确“结果自动回来源 session”；`check_async_task` schema/runtime 强制 `task_id`/`message_id` 二选一，Local Message 查询绑定同一 tenant/source/requester/parent session，返回 terminal result、artifacts、execution receipt，以及 outbox 的 pending/processing/delivered/dead-letter 真实状态。
- TDD/命令证据：原实现稳定出现 4 个失败（无 outbox、replay 不补偿、schema 只收 task_id、message_id 无法查询）；跨 turn root task、并发 result 行锁、幂等 channel 复用再分别先红，共记录 8 个失败。最终 Local Channel/A2A/agent-message/API 聚合 `57 passed`，Tool/prompt/registry 邻接聚合 `54 passed`，共 `111 passed`；相关 Ruff、compileall、`git diff --check` 全绿。
- 验收边界：不新增 schema，不伪造历史回填。新增真实 PG `message→result→outbox` 用例，但本机 Docker/Testcontainers connection refused，`1 skipped`；真实设备 result 上报、outbox worker 唤醒来源 Agent、断线重试与 reload 仍列 §11。

**HC-02（撤销，不是当前 Hive Connect 断点）presence 假离线**：原报告只读取仓内 legacy mjs/py runner，遗漏 Skill 实际安装的 canonical `@hiveclaw243/hive-connect`。对应 `0.1.9` 源码在 `platform/hive/hive.go:31,387-392,489-500` 每 25 秒启动并发送应用层 `{"type":"ping"}`，所以“在线 runner 90 秒后必假离线”不能成立。真实设备长连与 UI presence 仍需 E2E receipt，但状态应是**未验收**，不是**缺失/断点**。

**HC-03（代码闭环；真 PG/真实设备文件验收待补）file_download/file_upload 已接入唯一 live policy authority。**
- 原断裂原子：Authority→Execution。策略种子存在，但 bridge 文件 I/O 没有消费，管理员关闭能力不生效。
- 权威与执行：`require_local_agent_capability_policy` 统一解析 tenant default 与 Agent override，Agent 规则优先；未绑定 Agent 返回 409，missing/deny 返回 403。`POST /local-bridge/upload` 在 `save_upload_for_agent`、ChatMessage/ChatArtifact 之前同时检查 token 的 `files:upload` scope 与 live `local_agent.file_upload`；`GET /local-bridge/channel/workspace/download` 在安全路径解析和 `FileResponse` 前检查 `local_agent:receive` 与 live `local_agent.file_download`。浏览器本人/Manager 的 workspace 路由继续由登录态与 Agent/session authority 管理，不错误套用 bridge token policy。
- 恢复/迁移：无 schema、配置或数据迁移。历史未绑定 connection 没有可归属的 per-Agent policy，明确 fail-closed；这类 connection 原本也无法完成 signed channel capability snapshot，重新 pairing 是既有恢复路径。
- 证据：RED `8 failed, 20 passed`，分别证明上传未调用策略、拒绝后仍写盘、下载缺 scope/live policy、resolver 不存在；修复后目标集合 `28 passed`，扩大 Local Agent API/service/protocol/architecture 集合 `58 passed, 8 skipped`。8 skip 全来自 Docker/Testcontainers unavailable 的真实 PG 文件，新增用例已定义“同一 Agent file policy 由 allow 改 deny 后下一次查询立即拒绝”。8 个相关文件 Ruff/format、compileall 与 diff check 全绿。
- 残余验收：当前只声明代码闭环。Docker-on PG 的真实 policy 行优先级、canonical Hive Connect 实机 upload/download 以及策略变更后的在线设备行为仍交 §11。

**HC-04（撤销，不是缺失）常驻服务实现**：`.agents/skills/hive-connect/SKILL.md:21,59` 安装并调用的是 external `hive-connect`。对应 `0.1.9` 源码 `cmd/cc-connect/daemon.go:16-105` 已实现 install/uninstall/start/stop/restart/status/logs，macOS `daemon/launchd.go:42-78` 已实现 plist 写入、`launchctl bootstrap` 与 `kickstart`，且相关 Go 包测试通过。`bin/hive-bridge.mjs` 的提示只描述 legacy `hive-bridge`，不能证明 canonical daemon 缺失。真实机器 install→restart→presence 仍属于验收缺口。

**HC-05（断点，死代码）legacy gateway poll 通道**：`client.py:66,81,93` 与 `client.mjs:63-87` 调 `/api/v1/gateway/poll|send-message|report`，**后端无 gateway router**（main.py 无挂载，全仓 grep 无该路径）；Python CLI 默认 transport=poll → 必然 404 死循环。建议退役 Python 平行实现。

**HC-06（局部闭环）**：span 在 `needs_reconciliation` 时永不关闭（停留 running，审计面出现永不结束的 span）；`desktop_*` 四 router 有真实逻辑但消费者在仓外 Desktop 客户端（未证实）；多实例部署时进程内 fanout 可能丢实时投递（未证实，需部署证据）。

---

## 7. 企业治理、安全与 AI 资产审查

### 7.1 闭环面

RLS 真实强制（60+ 迁移 ENABLE+FORCE、`database.py:491-496` pin + after_begin 重钉、strict 启动拒 superuser/BYPASSRLS、唯一旁路强制 reason）、`check_agent_access` 逐层判定 fail-closed、工具执行治理唯一入口（kernel 全部经 `execute_tool`→governance runner，无绕过旁路）、CapabilityPolicy 门（STRICT 映射 fail-closed + 启动 drift 审计）、GuardPolicy 精确机器契约 shrink-only、secrets Fernet+HKDF（DEBUG=false 无 master key 拒启动、API 仅回掩码尾 4 位）、AI 资产 revision/usage/rollback 接在真实变更点、审批票证一次性消费+hash 绑定+启动 reconcile、admin 面/调试面分离（`/admin/*` 全部 platform_admin，调试面不暴露给企业管理员）。

### 7.2 断点

**GV-01（代码闭环，原 P0）Preflight ASK 已并入 durable Approval ticket。**
- 原断裂原子：Evidence→Recovery→Consumption。
- 当前接线：`request_action_preflight_approval`（`tools/governance.py:754-794`）复用既有 ApprovalService request port，并固定 `approval_origin_type="action_preflight"`；`execution_pipeline.py:589-625` 只消费 typed `REQUIRE_APPROVAL`，返回带 `approval_id` 的可恢复票据结果。`ToolRuntimeService` 已删除 `coordination_runtime`/`coordination_gateway` 字段与 InProcess 默认回填（`tools/service.py:700-730`），preflight 决策不再写 `checkpoint_id`（`service.py:1608-1628`）。
- 数据处理：旧 checkpoint 只存在进程内存，无 PG 存量可回填；源代码删除后无第二事实源。DecisionTrace 继续保留 ASK 机械证据，但不再持有悬垂 checkpoint 引用。
- 回归证据：`test_tool_runtime_service_preflight_asks_before_external_visible_tool` 验证 ASK 返回 durable `approval_id`、绑定 execution envelope/decision id、registry 未执行、DecisionTrace 无 `checkpoint_id`；`test_tool_runtime_service_fails_typed_when_preflight_approval_ticket_cannot_be_created` 验证票据依赖失败返回 typed `UNAVAILABLE`，不伪装审批成功。
- Acceptance：本地代码路径闭环；真 PG request→approve→worker 事务测试因 Docker 不可用 skip，仍在 §11 保留，不宣称生产闭环。

**GV-02（代码闭环，原 P0）Immutable approval 可消解 ASK/ESCALATE，硬拒绝不被覆盖，票据终态不再误记。**
- 原断裂原子：Evidence→Recovery。
- 当前接线：`service.py:1569-1602` 仅当 exact immutable `approval_decision` 存在且 preflight 为 ASK/ESCALATE 时把 effective decision 记为 DO，并在 runtime evidence 写 `approval_satisfied`/`approval_id`；REFUSE/PREPARE_ONLY 保持原裁决。`service.py:1093-1121` 对任何 typed `ToolBoundaryBlock` 把 Approval ticket 完成为 `failed`，并写 `boundary_outcome`/`boundary_reason_code`，不再依赖展示文本或 `<tool_error>` 才识别失败。
- 回归证据：`test_execute_approved_satisfies_preflight_ask_and_executes_exact_external_effect` 使用真实 `ToolRuntimeService.execute_approved → pipeline → registry` seam（未 monkeypatch `execute`/registry），验证 agent-bot external-visible 请求批准后效果返回 `SENT`、registry 恰执行一次、票据 `succeeded`；`test_execute_approved_does_not_override_preflight_refuse_and_marks_ticket_failed` 验证 REFUSE 不执行且票据 `failed`。
- 命令证据：`backend/.venv/bin/pytest -q backend/tests/tools/test_service.py backend/tests/tools/test_tool_runtime_preflight.py backend/tests/services/test_action_preflight.py backend/tests/services/test_approval_execution_runtime.py backend/tests/tools/test_governance.py backend/tests/tools/test_governance_resolver.py` → `103 passed, 4 skipped`；`-rs` 复核 4 项均为 Docker/Testcontainers connection refused。

**GV-03（代码闭环；真 PG 并发验收待补）Owner action policy 已进入模型输入与唯一工具执行治理面。**
- 原断裂原子：Authority→Execution；原 `AgentAccountabilityContext.action_posture` 仍是自然语言 charter 指导，不再被误当机器授权。
- Input/Authority：`owner_action_policy.py:23-33,94-133` 定义唯一 schema `hive.owner_action_policy.v1`、三个 exact action id（external effect/local read/local write）和 exact zone 校验；没有关键词、正则或自然语言模糊分类。默认合同保持既有行为：外部效果 `confirm_first`，内部读写 `full_authority`（`:158-172`）。
- Execution：`ToolContextResolver.resolve` 在 tenant-scoped session 加载/回填 policy（`tools/resolver.py:113-166`），`_build_tool_preflight_input` 只按工具 registry 的 `external_visible`/`is_read_only_tool` 机械映射 exact action id 并查 zone（`tools/service.py:1688-1755`）。`full_authority` 可直接通过 advisory 风险轴，`confirm_first` 进入 GV-01 的同一 Approval ticket，`never_do` 和损坏/不可用合同对效果型动作在普通 approval 前保持 REFUSE；内部只读明确标成 non-effectful，policy 依赖故障不会连带禁用。PL4、runtime permission、company boundary 等更窄硬不变量仍优先。
- Evidence/Recovery：policy 的 schema/action/zone/version/revision/hash/source/valid/error 写入 runtime preflight trace（`tools/service.py:1570-1572`）。`ConfigRevision` 是唯一 durable authority；`load_owner_action_policy` 用 savepoint+唯一键处理首次回填竞争（`owner_action_policy.py:328-370`），更新用 `FOR UPDATE`+`expected_version`，history/rollback 恢复为新不可变 revision（`:373-460`）；manage-only API 同事务写 `AuditLog`（`api/autonomy.py:235-361`）。无新表和 Alembic 迁移，也无第二政策事实源。
- Consumption：普通 agent 的真实 prompt assembly 装配完整 typed policy（`runtime/invoker.py:648-666`）；依赖不可用时以 typed unavailable 暴露并仅阻断效果型工具，不删除推理/只读能力。员工设置的 `AgentActionPolicyCard` 只展示“外部动作/内部只读/内部变更”和“直接做/先询问/永不做”，不展示 action id、revision ID、Hook 名或 event；Owner/Manager 可保存，并通过两步确认恢复上一版，其他访问者只读。原 raw Hook 卡已由 UI-09 删除。
- 回归证据：`test_tool_runtime_service_executes_external_effect_under_owner_full_authority` 走真实 registry seam；`test_execute_approved_does_not_override_owner_never_do_policy` 证明普通 approval 不能越权；`test_tool_runtime_resolver_degrades_policy_dependency_without_blocking_read_only_tools` 与 `test_unavailable_owner_policy_keeps_read_only_tool_non_effectful` 证明 policy 依赖故障只关闭效果型动作；service 测试直接验证 stale expected-version 冲突与 rollback 复制历史内容为新 revision。另有 exact schema、legacy backfill、损坏合同 fail-closed、prompt 完整输入、tenant/access/history/rollback/audit 与前端 raw-id 隐藏测试。
- 命令证据：后端聚合命令覆盖 8 个相关 test 文件 → `168 passed, 1 warning`；对应 16 个实现/测试文件 Ruff → `All checks passed!`。前端三个相关 test 文件 → `115 passed`；`npm run build` → Vite `7366 modules transformed`，AgentDetail `342090/380000 bytes`、gzip `94459/115000`，shared vendor 两项 budget 均通过。
- Acceptance 边界：本机 Docker 不可用，尚未用真 PG 双 worker 验证首次 legacy 回填唯一键竞争和事务 rollback；因此登记为代码闭环而非生产验收闭环。

**GV-04（代码闭环，原 P0）platform_admin 跨租户化身已有强制 operator 审计与失败边界。**

- 原断裂原子：Authority→Evidence。原 `TenantMiddleware` 只从已签 JWT 提前 pin 所选 tenant，权威身份与 active target 校验仍在公共 `get_current_user` dependency，因此不能在 middleware 的未验证 JWT 阶段伪造安全事件。
- 当前接线：`get_current_user` 验证数据库中的 canonical `platform_admin` 角色与目标 tenant active 状态，完成 target RLS pin 后、返回用户给业务 route 前调用 `_audit_platform_admin_tenant_impersonation`。只有 home tenant 与 target 不同才产生 `platform_security.tenant_impersonation`；同租户显式选择不误记。该 dependency 当前被 API 声明广泛复用，修复不是孤立 helper。
- Evidence/Recovery：专用 `write_platform_security_audit_event` 使用独立 session + 显式 operator RLS bypass + commit，写入 `audit_logs.tenant_id=NULL`；envelope 保存 actor id/role、home tenant、target tenant、request method/path、IP、TraceIdMiddleware 的 request correlation id，返回 event id 并挂入 request state。该专用 sink 不使用通用 `write_audit_log` 的吞错路径；任何写入异常转为 `503 Security audit unavailable`，业务 route 不会执行。
- 回归证据：先新增事件完整 envelope 与 fail-closed 测试，修复前分别以“零事件”和“未抛异常”稳定失败；修复后覆盖跨租户恰写一次、同租户零事件、无效 target 零事件、operator insert 不吞异常及 selected-tenant 既有行为。聚合命令覆盖 6 个相关 test 文件 → `49 passed, 1 warning`；4 个变更文件 Ruff → `All checks passed!`。
- 数据边界：无 schema/config 迁移。历史请求没有 event/trace 事实，无法从现存数据安全推断，明确不做伪造回填。operator 审计的统一哈希链与产品查询面已由 GV-06 独立完成并单列证据，不把它倒算为 GV-04 当时已经具备的事实。

**GV-05（代码闭环；真 PG/Redis 验收待补，原 P0）Desktop LLM proxy 已接入统一配额、计量和分布式限流。**

- 原断裂原子：Authority→Acceptance。stable HEAD 已证明 `llm_proxy_router` 接入主应用，但原 handler 只做 JWT/tenant model 解析后裸转发，未消费任何 quota、usage ledger 或 rate authority。
- Authority/Execution：`proxy_chat_completions` 解析机器请求后、查询 provider key 和打开 upstream 前，调用 `check_user_token_quota(user_id, tenant_id=selected_tenant)`；quota exhausted 返回 typed 429，quota 依赖故障 typed 503。随后调用既有 Redis sliding-window `rate_limit_or_429`，key 同时绑定 tenant+user，硬上限 60/min；Redis 不可验证时 fail-closed 503。无 tenant context 直接拒绝。
- Evidence：非流式 response 返回前，及流式 `[DONE]` 转发前，统一调用 `record_autonomous_llm_token_usage(source="desktop_llm_proxy", raise_on_error=True)`，写既有 append-only `token_usage_events` 并更新 tenant/user daily/monthly/total counters。直接用户计量原先只增 tenant、不增 user 的旁路已同步修复；tenant/user 聚合读取使用 `FOR UPDATE`，避免并发 lost update。
- Streaming/Recovery：upstream body 强制 `stream_options.include_usage=true`，最终 provider usage 是首选机械事实。兼容 provider 不返回 usage 或客户端中途断开时，才使用既有 CJK-aware token estimator；event metadata 明示 `usage_source=estimated_missing_provider_usage` 和 input/output estimate，不伪装 provider receipt。正常流必须先完成严格计量再发 `[DONE]`；计量失败时非流式 withholding 503，流式发 typed `usage_metering_unavailable` 且不发 `[DONE]`；断流 `finally` 仍尝试恢复记账。
- Live wiring：主应用当前把 router 同时挂载为 `/api/llm/v1/*` 与 `/api/v1/llm/v1/*`；运行期 route inventory 复现四个 model/completion path。修复直接位于唯一 `proxy_chat_completions` handler，不是旁路 service。
- 回归证据：TDD 红态为 `6 failed, 7 passed`，分别复现未查 quota/rate、stream 未请求 usage、零记账、user counter 漏计与吞错；修复后定向 `20 passed`，加入 runtime invoker/workflow quota 相邻回归后 `76 passed`。覆盖 provider usage、缺失 usage fallback、disconnect cleanup、meter-before-DONE、metering failure、rate authority failure和 typed quota denial；Ruff 与 format check 全绿。
- 迁移/验收边界：复用现有 `token_usage_events` 和 User/Tenant counters，无 schema/数据回填。3 个真 PG quota/counter 用例因本机 Docker/Testcontainers 不可用而 skip；Redis 多实例 sliding window、PG row-lock 竞争与真实 provider SSE usage 仍须在验收环境补绿。

**GV-06（代码闭环；真 PG/滚动多实例验收待补，原局部闭环）operator security 具有独立不可变链、专用消费面与明确证据信任边界。**

- 原断裂原子：Evidence→Consumption→Acceptance。租户 `security_audit_events` 已有哈希链与查询/验链，而 operator security 原先只是 `audit_logs` 内一类无链行；通用 `write_audit_log` 吞错，Desktop 自报字段还能覆盖 `source/rule/blocked`，所以三类证据会在同一表面被误读成同等可信。
- Authority/Execution：`write_platform_security_audit_event` 是 `platform_security.*` 的唯一合法写入器；通用 fail-soft operational sink 对该 namespace 直接拒绝。专用 writer 在独立 operator RLS bypass transaction 内取得固定 PG advisory lock，读取链头并写单调 `sequence_num`、`prev_hash`、`recorded_at` 与 `event_hash`；hash 覆盖 AuditLog row id、row action 和除 hash 自身外的完整 envelope，合法并发不会 fork。写入/commit 异常保持向上传播，GV-04 的身份切换继续转为 typed 503。
- 历史与恢复：没有改写不可变 v1 行。首次 v2 写先提交 `platform_security.chain_cutover`，其 hash 内保存 legacy event count、首尾 ID 和全量 canonical digest；每一条后续 v2 事件再封入当时完整 `legacy_anchor`。因此滚动发布期间旧实例晚到的 v1 会令验链明确失败，下一条新 writer 事件可重新锚定并恢复，而不是留下永久断链或静默忽略。
- Evidence/Consumption：`GET /enterprise/platform-security-audit` 与 `/verify` 只允许 canonical `platform_admin`。列表与验链在同一 operator lock 下验证完整链；有效时行标为 `chained/legacy_anchored`，链未初始化或损坏时明确标为 `chain_invalid/legacy_unverified`，不会把未锚定 v1 冒充可信。验链逐项检查 schema、sequence、prev hash、row action、recorded_at、event hash、cutover 与最新 legacy anchor，任一异常返回 first invalid event/reason。两个旁路 callsite 均加入静态、到期的 RLS allowlist，不借 tenant 管理员权限读取 operator 平面。
- Desktop 边界：`desktop_audit.py` 只接受当前服务端 tenant 下存在的 claimed Agent，否则 403；顶层 `schema_version/evidence_trust=client_asserted/source/authenticated user+tenant/request id` 由服务端写入。客户端 timestamp/details 嵌套为 claimed 字段，Guard 的结构化 rule/blocked 不能被 details 覆盖；这些行不冒充 server-enforced security receipt。
- 数据边界：复用 `audit_logs` 已存在的数据库级 UPDATE/DELETE 与 TRUNCATE 禁止触发器，不新增第二张 operator 表或 Alembic 迁移。普通生命周期/遥测仍可使用 fail-soft operational sink，但它不属于 canonical platform-security evidence。
- 回归证据：修复前四个定向文件为 `9 failed, 4 passed`，稳定复现缺少链函数、只写 v1、API 不存在、Desktop 证据信任未标记/字段可伪造/跨租户 Agent 可接受。最终扩大到 16 个审计、RLS、不可变迁移与身份相关 test 文件 → `106 passed, 3 skipped, 1 warning`；3 个 skip 均为 Docker/Testcontainers unavailable。对应 11 个实现/测试文件 Ruff 与 format check 全绿；`test_startup_auth_rls_bootstrap.py` 的 6 个真 PG 用例同因 Docker unavailable skip，其中新增的真实 operator 写入→过滤查询→全链验签验收未在本机执行。

**GV-07（代码闭环；生产故障注入待补，原局部闭环）quota admission 继续 fail-closed，但失败原因、证据和消费现已 typed。**

- 裁决修正：token/cost quota 属于 Model Agency hard-constraint allowlist 中的显式资源不变量。权威不可验证时继续发起 LLM 调用会绕过成本上限，因此原建议“只对基础设施不可用提供降级放行”不成立；正确恢复动作是可重试地阻断当前调用，而不是消耗未获授权的 token。
- Execution：`_enforce_invocation_quota` 对真实耗尽返回 `TerminalReason.QUOTA_DENIED`，对 quota authority 异常返回新增的 `TerminalReason.QUOTA_UNAVAILABLE`；两者都在 kernel 调用前停止。不可用分支只返回稳定用户消息和异常 class，不把数据库凭据、连接文本等底层 exception message 回显。
- Evidence/Recovery：两个分支统一发出 `type=quota_exceeded`。耗尽 receipt 为 `status=denied/code=token_quota_exceeded/retryable=false`；权威故障为 `status=unavailable/code=token_quota_unavailable/retryable=true`。这让调用方能区分“调整额度”与“稍后重试”，同时保持同一个不可绕过的 admission gate。
- Consumption：`quota_exceeded` 已加入 `SESSION_NATIVE_EVENT_TYPES`，其 `code/quota_type/error_type/retryable` 写入结构化 message part；既有 web chat terminal transcript 路径会把它判为 failed，`_terminal_reason_value_for_web_run` 原样持久化 `quota_unavailable`。前端既有 realtime reducer 对 `quota_exceeded` 收敛为 failed，本轮再把 `quota_denied/quota_unavailable` 映射为 `Needs attention`，不再显示为 `Working`。
- 回归证据：修复前定向后端为 `3 failed`（旧 `type=quota`、不可用误记 `provider_error`、事件不 durable），前端为 `1 failed`（quota 终态回落 `Working`）。修复后后端 invoker/message-part/web terminal/WebSocket 相邻聚合为 `68 passed`，前端 AgentDetail + chatRuntime 为 `176 passed`；6 个 Python 文件 Ruff/format 全绿，`npm run build` 成功且 AgentDetail/shared vendor 四项 bundle budget 通过。无 schema/config/历史数据迁移；生产 quota authority 故障注入与真实 reload 仍交给 acceptance 环境。

**GV-08（代码闭环；真 PG、生产 keyring/backfill/provider 验收待补，Codex current-source 复核新增）凭据治理已从模式权威改为 exact credential authority**：见 §3.3。Authority→Execution→Evidence→Recovery 的本地代码链现由 tenant-scoped inventory、最早 ingress/pre-effect gate、value-free receipt、typed encrypted transport store 与显式 v3 dry-run/apply migration 贯通。原先钉住 benign pattern 误伤的测试已反转；真实 active secret、嵌套输入、流式 chunk、tool/model 出站、T0/Web/Session/Channel persistence、immutable file snapshot 与 legacy backfill 均有回归。Acceptance 仍需真实 PG、生产 keyring inventory、迁移 receipt、provider stream/重试和 offboarding 验证。

### 7.3 已修复的局部闭环

**GV-09（代码闭环；真 PG/浏览器与遗留数据 apply 待验收，原 P2）DecisionTrace 已收敛为 SQL 单一权威，并进入员工可理解的会话消费面。**

- 原裁决低估了严重度：ToolRuntimeService 默认把 preflight 决策写入 SQL，但 `record_session_feedback` 默认另开 JSONL store；它不仅是“兼容回退”，而是无法找到 SQL decision 的分裂权威。`SqlDecisionTraceStore.record_decision/record_feedback/import_*` 又各自内部 commit，使决策反馈先于同请求的 Memory overlay、`SessionFeedbackEvent` 与 AuditLog 独立落盘，失败时无法作为一个事务回滚。
- Authority/Execution：live `DecisionTraceStore` JSONL 实现和 JSONL backfill helper 已删除；生产写入只剩 `SqlDecisionTraceStore` / `TenantScopedSqlDecisionTraceStore`。SQL store 只 `flush`，由 ToolRuntime 的 tenant-scoped session 或 session feedback API 的 request transaction 统一 commit。decision id 收紧到既有 128 字符机器合同；decision-linked feedback 必须同时精确匹配 tenant、agent、session，缺字段、越权和不存在统一在 API 边界返回不泄露存在性的 404。无 live caller 的 `propose_charter_calibrations_from_feedback` 与其 fake-only 测试同时删除，避免把孤立 helper 当成 Dream 消费闭环；真实 feedback 仍进入既有 explicit Memory overlay。
- Consumption：新增 `GET /agents/{agent_id}/sessions/{session_id}/decisions` typed contract，先复用 session authority，再以 tenant+agent+session 三键查询并聚合 feedback count。`AgentChatSection` 的真实 query key 接线到 `SessionRuntimePanel`；员工右栏显示 “Action decisions”、业务动作、人类可读 outcome/reason 和 Helpful/Misleading。中英文文案均有 locale 合同，已知业务工具显式映射，未知值统一为“数字员工操作”，不把任意内部 `tool_name` 仅改大小写后继续暴露。decision ID 只作为反馈机器键，不渲染；raw reason code 不进入页面。普通 message feedback 只在真实 UUID message 上写 `message_id`，不再把 streaming/synthetic message id 伪造成 `decision_id`，也不伪造用户未填写的 rationale。
- Recovery/迁移：新增显式一次性命令 `app.scripts.migrate_decision_trace_jsonl`。默认只读 dry-run，receipt 包含 source SHA-256、完整行计数、坏行、孤儿和缺失 tenant；apply 必须绑定已审 dry-run hash，零坏行/孤儿，且对旧无 tenant 行提供 operator 显式 `--unscoped-tenant-id`，随后按原 public id 幂等导入并把源文件移动到 hash 命名的可逆 archive。重复 apply 若发现同 hash archive 会安全复用，若内容不同则拒绝覆盖；不会从内容或路径猜测 tenant。
- 真实遗留数据证据：`/Users/rocky243/.hive/data/agents/_control_plane/decision_traces.jsonl` dry-run 读到 `2 decisions / 2 feedback / 0 skipped / 0 orphan`，SHA-256 为 `ae55ed97e15f7b0410a6e504981e43e519d361d1c121f1df38e5d2870db303bf`；两个 decision 均缺 tenant/agent/session，故 `can_apply=false`。文件仍为 1720 bytes、mtime `2026-06-12T18:46:54+0800`，本轮没有 `--apply`、移动或改写；必须由有权 operator 明确 tenant 后再执行。
- 回归证据：RED 覆盖内部 commit、JSONL 默认权威、缺 API/adapter/UI、synthetic message 伪关联、缺 response schema、无 session scope、不安全迁移与缺失中文产品文案。最终后端六个相邻文件聚合 `152 passed, 1 skipped`；唯一 skip 是 Docker/Testcontainers 不可用导致的真实 PG tenant/RLS 用例。前端 adapter + AgentDetail 聚合 `139 passed`，Frontend Surface Hygiene `3 passed`；13 个 Python 文件 Ruff/format 全绿；`npm run build` 成功，7366 modules transformed，AgentDetail `347276/380000` bytes、gzip `95831/115000`，shared vendor 两项 budget 通过。

### 7.4 AI 资产

Agent/Skill/Workflow/外部能力资产管理闭环（revision/usage 投影接在真实变更点、rollback/reconcile 仅 admin、租户作用域）；Knowledge grant 的 create/update/reactivate/revoke 已由 HN-05 接入同事务 tenant audit；A2A 协作组已补独立管理面与完整前端操作流。UI-02 复核进一步确认：legacy `config_history` 不是另一个待建设产品面，canonical AI asset revision/history/rollback 已有前端真实消费者；GuardPolicy 与 global FeatureFlag 的产品消费面也已补齐。

---

## 8. 用户功能与 UI/UX 审查

前端消费面对账（源码级，未跑浏览器）：后端能力的前端消费总体**已有较强接线**——chat 传输（streaming/keepalive/断线重连/resume 同一事实/active run 状态）、Plan Mode 确认卡全状态机、Approval 卡、Session 权限卡、Workflow gate（approve/reject/repair/cancel/promote）、Subagent 状态与模型主导恢复、Work Ledger Dock、Session feedback、Workspace 文件 CRUD/上传/下载均有真实接线。多数高频路径做到了分层：operator_view 强制审计理由、operator drawer 门控、tool raw payload 默认折叠、状态人性化、UUID 标签抑制、token 图表仅 admin；UI-09 曾反证“三受众分层整体合规”，本轮已按该反证修复员工 Hook 消费面。**前端侧 Model Agency 零违规**（subagent 恢复走"请 agent 检查并重试"的模型主导路径，是正确范式），不等于其余产品抽象与权限消费面零缺陷。

**UI-01（撤销为产品断点，DOC-01 已闭环）Office 专用编辑面已显式退役**：`AgentDetailSections.test.tsx:2253-2265` 明确断言移除 Office tag、dedicated tab、`OfficeWorkbenchSection` 与 `activeTab === 'office'`，`ArchitectureSimplicityContract.test.ts:28-53` 再次锁定该退役边界。`AGENTS.md` 现只声明真实 OfficeCLI tool/preview 与 `ArtifactSurface` 消费，并明确 ONLYOFFICE WYSIWYG/专用 Workbench 已退役；除非新的产品 authority 明确要求恢复，否则不得重新登记为实现断点。

**UI-02（代码闭环；真 PG/Redis/浏览器验收待补，P1）治理产品消费面已按真实 authority 分层**：

1. **原 finding 校正**：`config_history` 明示为 generic history 的退役 compatibility adapter，除 `entity_type=ai_asset` 外统一 `410 Gone`；它不是应再暴露一个页面的 canonical 产品面。真实 `/enterprise/ai-assets/*` catalog/detail/revisions/rollback/reconcile 已由 `aiAssetsApi` 与 `WorkspaceAIAssetsSection` 消费，并以 adapter/component 测试锁定 revision、diff、rollback 和 reconcile。
2. **Company Action Guardrails**：Control Plane 新增 `/enterprise/action-guardrails`，只把 GuardPolicy 映射成“所有员工操作/离开公司的操作”两项业务控制及 normal/approval/block，不向 Owner/Manager 渲染 `tool_rules`、raw tool name、zone id 或政策 JSON；保存时保留 extension-owned rule。服务端要求 `expected_version`，对完整 known subset 做机器合同校验；已有行 `FOR UPDATE`，首写用 tenant+agent advisory transaction lock 串行化；冲突 typed 409，变更与只含版本、changed lanes、before/after SHA-256 的 tenant audit 共用 request transaction。
3. **Platform Feature Rollout**：global FeatureFlag 明确只供 `platform_admin`，org_admin 不再可列举或修改全局 rollout。管理面提供 boolean/percentage/tenant gate/allowlist、expiry 与结构化 tenant/user overrides，不使用 raw JSON editor。API 强制 key/type/percentage/UUID/boolean override 机器合同，空 PATCH 在查库前 422；更新/删除以 `FOR UPDATE + expected_updated_at` 拒绝 stale state。create/update/delete 在效果前写 strict platform-security audit，audit unavailable 返回 typed 503 且不发生 mutation；Redis invalidation 只在外围 request transaction commit 后调度。
4. **回归证据**：Backend 首轮 `guard_policies + feature_flags` 为 `8 failed, 5 passed`；随后 audit dependency、GuardPolicy first-write lock、FeatureFlag stale update、empty update 四项各自先红 `1 failed`。Frontend governance adapter、两组件、Control Plane/Admin Platform 五个目标文件首轮全部失败；首轮 build 还分别暴露 override draft 类型、percentage undefined 与 guard-policy icon/route 缺口。仅暂存树最终 backend 扩大邻接 `88 passed, 30 skipped`（30 项均为 Docker/Testcontainers unavailable 的 Workflow/Trigger 真 PG 用例），Frontend 10 文件 `34 passed`；Ruff/format、locale JSON、TypeScript/Vite build 与 AgentDetail/shared-vendor 四项 bundle budget 全绿。

**UI-03（代码闭环；真实双语浏览器验收待补，原 P1）i18n 已建立唯一可复现库存并清零机械欠账**：

1. **原数字校正**：原审查的 349/306 与“5 文件 124 处”均无可复现提取器，不能继续作为事实。新 AST inventory 在本部分修复前的纯 staged baseline 机械得到 `missingBoth=285`、`catalogOnlyChinese=21`、`duplicateCatalogKeys=2`、`chineseDefaults=143`；其中 143 个中文默认值实际分布于 `Login`、`PersonalKnowledge`、`AuthShell`、`AgentEvolutionSection`、`AgentKnowledgeSection` 五个生产文件。
2. **唯一 inventory 与动态边界**：`frontend/scripts/i18n-audit.mjs` 只统计真实导入 `react-i18next` 的 `t()`，并允许以 exact source+callee+reason 登记实际接收 `t` 的受控 wrapper；识别 literal/no-substitution template，展开有限枚举和 catalog pattern，其余动态表达式必须在 `i18n-audit.config.mjs` 以 exact source+expression+reason 登记。当前 35 条显式动态规则覆盖 42 个调用点，另有 69 个 catalog pattern、5 个有限枚举，共覆盖 116 个动态调用；`featureFlagAudienceSummary` 的 `translate` wrapper 也进入静态/动态库存。仅有 runtime fallback 不能绕过规则。Node fixtures 覆盖重复 JSON path、误识别排除、受控 wrapper、中文 defaultValue、动态展开与 fallback 逃逸。
3. **Catalog 与源码清理**：合并 en/zh 中重复的 `agent.extensions` 对象，避免 `JSON.parse` 静默丢弃前一组 key；补齐双语及 21 个英文 parity key，移除五个生产文件的 143 个中文 fallback。AI asset rollback 的 `{{version}}` 改由结构化 interpolation options 传值。相关测试不再靠源码 fallback 造中文，而是通过 `translateFromCatalog` 读取真实 `zh.json` 并覆盖插值。
4. **最终 staged-tree inventory**：215 个 source files；en/zh 各 3461 key；2601 个静态调用（2122 unique）、116 个动态调用；`missingBoth/missingEnglish/missingChinese/catalogOnlyEnglish/catalogOnlyChinese/duplicateCatalogKeys/chineseDefaults/unresolvedDynamic` 八项均为 0。库存 SHA-256：static `38d255c7143919b0d22f1de0ba5cd081fa94f8f1ef2deaba47753ff6d27dceb6`，dynamic `7c9ff32f2eec1012992f91f58770f282d85c0cd6511ca5bae4b81c8d9341fe44`，English catalog `acd5525ad3102725f386b196415c874c34673dd35783decf24b0cb83beb8252c`，Chinese catalog `2b4b9cb5693e1938ceb19161ddeff39e7b649af65118ba1fe023c8a9da33ada2`。
5. **回归与 CI**：`npm run i18n:check` 的 Node fixtures `9 passed` 且 inventory gate 通过；第一次全量 Vitest 发现 Node test 文件名被 Vitest 收集，得到 `1 failed / 128 passed / 774 tests passed`，改成非 Vitest glob 的 `i18n-audit.node.mjs` 后，纯 staged tree Frontend 全量为 `128 files / 774 passed`。同一 staged tree `npm run build` 成功，7370 modules transformed，AgentDetail `348097/380000` bytes、gzip `96071/115000`，shared vendor `591449/620000`、gzip `186474/200000`；CI 已在 Frontend tests 前强制 `npm run i18n:check`。无 API/schema/data migration；真实中英文浏览器旅程仍是 Acceptance 缺口。

**UI-04（局部闭环）soul.md 全文无直接阅读入口**：workspace 浏览器 rootPath="workspace" 看不到 soul 正文；owner 只能审批候选，看不到当前 soul 全文。
**UI-05（局部闭环）文件版本历史 UI 缺失**：files API 无 version 端点，无回滚入口。
**UI-06（瑕疵）**：owner 审批卡直接 `JSON.stringify(details)`（`AgentApprovalsSection.tsx:64`）——Owner/Manager 受众不应见裸 JSON。
**UI-07（风险）**：`/design-gallery` 公开路由无鉴权无 env 门控（`App.tsx:126`），生产构建应移除或加守卫。
**UI-08（死代码）**：`LocalAgentLinkCard.tsx` 零消费者、`enterpriseApi.templates` 无页面消费者。
**UI-09（代码闭环；生产清理/浏览器验收待补，原 P1）员工 Hook 实现与权威泄漏已收口**：

1. **产品消费面**：`AgentSettingsSection` 已移除整张 `HookRuntimeControlCard`，对应组件、错误契约单测、`ccParityApi.listHooks/updateHookRuntimeConfig` 与 `/agents/{id}/hooks` 浏览器调用全部删除；员工/Owner/Manager 不再取得或渲染 handler、event、failure mode、raw receipt/error 或 Enable/Disable。`HookProductBoundary.test.ts` 以源文件和 adapter 不变量锁定该边界。
2. **员工健康投影**：新增 `GET /agents/{agent_id}/runtime-health`，仍复用 Agent access authority，但只返回 `healthy/degraded/needs_attention`、受保护中止数、后台异常数、是否可重试和最后异常时间；不返回 Hook、handler、event、key、receipt id、session id 或底层 error。`AgentStatusSection` 在“概览”的 Runtime Protection 卡消费该投影，只显示“请求被平台保护中止/重试原请求/联系支持”等业务语言，中英文文案均已入库。
3. **Developer 诊断与 mutation authority**：raw registry/receipt 改到 `/admin/agents/{agent_id}/runtime-hooks`，PATCH 改到同 namespace，旧 GET/PATCH route 已不存在。两条路径在读取 Agent 前先要求 `platform_admin`（当前 Platform Developer/Operator 认证角色）；raw registrations 再按所选 Agent 的 tenant/agent matcher 过滤，修复旧实现把全局、含其他 tenant plugin 的 registry 一并返回的问题。PATCH 只接受当前注册且 `plugin:` namespace/profile 匹配的扩展；内置 required/advisory Hook 均按员工维度不可变。每次允许的 extension mutation 在效果前写 strict `platform_security.extension_hook_config`，audit 失败不修改配置。
4. **历史配置清理与恢复**：启动加载器只应用当前注册 plugin Hook 的 per-agent 配置；内置覆盖和已失效 extension 覆盖从 active `hooks` 移入同一 `SystemSetting` 的 `retired_hook_runtime_overrides`。每个原 config 保留 schema、reason、SHA-256 与完整 bytes，重复启动幂等；聚合 retirement 在提交前进入 strict platform-security chain。没有 DDL/Alembic，也没有删除恢复证据；生产行尚未在本轮读取或修改。
5. **回归证据**：新增后端合同初始为 `10 failed / 1 passed`；提交前并发复核再以 `2 failed / 5 deselected` 复现首写无锁与启动清理未锁行；新增前端三项合同分别红于缺 runtime-health adapter、状态消费和 Hook 卡仍存在（同一首次命令另暴露两项既有组件行数预算失败，未混入 UI-09 计数）。Green 后端 Hook/API/startup/runtime 相邻集 `112 passed`；前端 boundary/adapter/AgentDetail/CCParity `152 passed`。5 个 Python 文件 Ruff/format、compileall、`git diff --check` 全绿；staged-tree `npm run build` 成功，7364 modules transformed，AgentDetail `348085/380000` bytes、gzip `96087/115000`，shared vendor 两项 budget 通过。

**UI-10（闭环；修复阶段新增）组件行数预算已恢复**：DOC-01 的相邻 Office 合同曾确认 `ArchitectureSimplicityContract.test.ts` 两项失败：`AgentChatSection.tsx` 测试计数 2406 行（物理 2405），超过 2400；`SessionRuntimePanel.tsx` 测试计数 1226 行（物理 1225），超过 1200。修复没有放宽阈值：Action decisions 的语义标签、理由映射、反馈 UI 提取为独立 `SessionDecisionHistory.tsx`（物理 163 行，新增 220 行预算）；SessionRuntime 直接消费该 owner，测试和 AgentAware/SessionExperiencePolicy 也改为直接依赖各自真实 lineage/artifact/runtime/tool-result owner，删除 AgentChat 的代理 re-export。最终 AgentChat 为 2379 行、SessionRuntime 为 1069 行；Architecture `7 passed`，12 文件 chat/runtime 邻接 `285 passed`，生产 build/bundle budget 全绿。

**Frontend Experience Handoff 判断**：确定性待实现产品缺口现集中于 UI-04/UI-05；UI-02、UI-03 与 UI-09 从实现 handoff 降为真实浏览器/部署验收项。i18n 精确库存和 CI gate 已代码闭环，但实际中英文渲染、三受众信息层级、通知/恢复端到端体验仍无法纯源码定论——**建议输出有限范围 Frontend Experience Handoff**（见 §12），不需要全面重审。

---

## 9. 七原子矩阵与断点清单

### 9.1 七原子矩阵（能力域 × 原子，●=闭环 ◐=局部 ○=断点/缺失）

| 能力域 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 |
|---|---|---|---|---|---|---|---|
| 模型循环/kernel | ● | ● | ● | ● | ● | ● | ● |
| Web chat 会话生命周期 | ● | ● | ● | ● | ● | ● | ● |
| 工具治理平面 | ● | ● | ● | ● | ◐(GV-01/03、GV-08 生产 backfill 待补) | ● | ◐(GV-01/02/03/08/09 真 PG/生产验收待补) |
| Plan/Subagent/Ledger/Skill | ● | ● | ● | ● | ● | ● | ● |
| Workflow | ● | ◐(SA-06) | ● | ● | ● | ● | ◐(SA-06 用户确认强度) |
| Hooks | ● | ● | ● | ● | ● | ● | ◐(UI-09 生产 retirement/浏览器待验收) |
| Memory T0/T2/T3/soul | ● | ● | ● | ● | ● | ◐(SA-07/08) | ● |
| 自进化（skill/dream） | ● | ● | ● | ● | ● | ● | ● |
| 主动管理循环 | ○(HN-01/02 已知缺失；文档已闭环) | — | — | — | — | — | — |
| A2A/委派 | ● | ● | ● | ◐(A2A-02) | ● | ● | ◐(A2A-03 真 PG/浏览器；HC-01 真 PG/设备唤醒待验收) |
| Personal KB | ● | ● | ● | ● | ● | ● | ◐(HN-05 真 PG audit 与既有 authority 测试 Docker-off) |
| Enterprise Knowledge | ○(HN-04 已知缺失) | — | — | — | — | — | — |
| 企业治理（RLS/配额/审计） | ● | ● | ● | ● | ● | ● | ◐(GV-05/06 真 PG/Redis/多实例；UI-02 浏览器待补) |
| Hive Connect | ● | ● | ◐(HC-05 legacy) | ◐(HC-06) | ● | ● | ◐(HC-01/03 真 PG + canonical device E2E 未验收) |
| 前端消费面 | ● | ● | ● | ● | ● | ○(UI-04/05) | ◐(UI-02/03/09 浏览器验收) |
| 文档事实源 | — | — | — | ●(DOC-01) | — | — | ◐(DOC-02) |

### 9.2 断点登记册（按严重级排序；均含反证记录与最小闭环方向，详见各模块章节）

| 编号 | 模块 | 状态 | 严重级 | 断裂原子 | 一句话 |
|---|---|---|---|---|---|
| GV-01 | 工具治理 | **代码闭环；PG 验收待补** | 原 P0 | Evidence→Recovery→Consumption | ASK 已进入 durable Approval ticket；孤立 checkpoint 已删除 |
| GV-02 | 工具治理 | **代码闭环；PG 验收待补** | 原 P0 | Evidence→Recovery | exact approval 消解 ASK/ESCALATE；硬拒绝保持且票据失败 |
| SA-05 | Workflow | **代码闭环；真 PG lease 验收待补** | 原 P0 | Execution↔Acceptance | canonical action 已注册；REST 走真实 PlanModeGate 后 200，fake 不再充当 wiring proof |
| GV-04 | 治理 | **代码闭环** | 原 P0 | Authority→Evidence | canonical 身份边界写 operator receipt；失败 503，同租户不误记 |
| GV-05 | 治理 | **代码闭环；真 PG/Redis 验收待补** | 原 P0 | Authority→Acceptance | proxy 已接统一 quota、append-only usage/counters 与 distributed rate limit |
| GV-06 | 治理 | **代码闭环；真 PG/多实例验收待补** | 原 P2 | Evidence→Consumption→Acceptance | operator v2 链、legacy 锚点、platform_admin 查询/验链与 Desktop client-asserted 分层已贯通 |
| GV-07 | 治理 | **代码闭环；生产故障注入待补** | 原 P2 | Execution→Evidence→Recovery→Consumption | fail-closed 不变；denied 与 unavailable/retryable 已形成 durable typed receipt 并进入 UI |
| GV-09 | 工具治理 / 前端 | **代码闭环；真 PG/浏览器与遗留 apply 待验收** | 原 P2 | Evidence→Recovery→Consumption→Acceptance | SQL 单一权威与同事务反馈、精确会话 API、员工可读决策历史及 hash-bound 可逆迁移已贯通 |
| HC-01 | Connect | **代码闭环；真 PG/真实设备唤醒待验收** | 原 P0 | Consumption→Recovery→Acceptance | result 与来源 session outbox 同事务；自动 continuation + `message_id` 恢复查询已贯通 |
| GV-08 | 工具治理 / Model Agency | **代码闭环；真 PG/keyring/backfill/provider 验收待补** | 原 P0 | Acceptance | pattern 仅作 audit candidate；exact inventory、最早入站/效果边界、输出保真、加密 transport store 与迁移已贯通 |
| SA-01 | kernel | **代码闭环；真 PG resume 验收待补** | 原 P1 | Authority→Execution→Evidence→Recovery | cache-miss 预算已接 live loop；permission resume 恢复累计值且只计费新增量；新工具前 typed hard-stop，已完成答案保持原字节 |
| A2A-03 | A2A / 前端 | **代码闭环；真 PG/浏览器验收待补** | 原 P1 | Authority→Evidence→Consumption→Acceptance | callable/management 分面、目标 Owner 确认、状态机/同事务审计与六操作 UI 已贯通 |
| GV-03 | 治理 | **代码闭环；真 PG 并发验收待补** | 原 P1 | Authority→Execution→Acceptance | typed policy 已贯通 prompt、tenant-scoped resolver、preflight、Approval、revision/history/rollback/audit 与业务 UI |
| UI-09 | Hooks / 前端 | **代码闭环；生产 retirement/浏览器验收待补** | 原 P1 | Acceptance | 员工 raw 卡/adapter/旧 route 已删；健康投影、platform-only tenant-filtered diagnostics、内置不可变和可恢复清理已贯通 |
| UI-10 | 前端极简性 | **闭环** | P2（修复阶段新增） | Acceptance | 独立 Decision History owner 与直接依赖已恢复原阈值，Architecture 7/7 通过 |
| UI-02 | 前端 / 治理 | **代码闭环；真 PG/Redis/浏览器验收待补** | P1 | Consumption→Acceptance | canonical AI asset history 已有消费者；Action Guardrails 与 platform-only Feature Rollout 已贯通 typed authority、concurrency、audit 与业务 UI |
| UI-03 | 前端 | **代码闭环；浏览器验收待补** | 原 P1 | Acceptance | AST inventory、动态规则、双语 catalog、中文 fallback 清理与 CI gate 已闭环，八项欠账均为 0 |
| DOC-01 | 文档 | **闭环** | P1 | Evidence | live inventory 改为现场生成；幽灵服务、旧实体、旧 Office 面与历史测试数字均已收正 |
| HN-01/02 | Native | **文档闭环；能力已知缺失** | P1（文档） | — | handbook 明确 proactive_employee_loop / policy_replay 无 live runtime，不再宣称完成 |
| HN-04 | Native | 已知缺失 | P2 | — | Enterprise Knowledge 未实现（规格存在） |
| HC-03 | Connect | **代码闭环；真 PG/真实设备文件验收待补** | 原 P2 | Authority→Execution→Acceptance | bridge 文件读写已在 I/O 前消费 scope + live policy；未绑定 connection fail-closed |
| HC-05 | Connect | 断点（死代码） | P2 | Execution | Python 客户端默认 poll 已删除的 gateway → 404 循环 |
| HN-05 | KB | **代码闭环；真 PG 验收待补** | 原 P2 | Acceptance | grant upsert/revoke 与 tenant AuditLog 同事务；历史无事实不伪造回填 |
| SA-02/03/06/07/08、A2A-02、HC-06、UI-04/05/06、DOC-02 | 各 | 局部闭环 | P2 | 各异 | 见各模块章节 |
| A2A-04、HN-07、SA-09、UI-08 | 各 | 死代码/孤儿 | P3 | — | 无生产消费者，可删除（见 §10） |

### 9.3 撤销或重分类项

| 编号 | 原始结论 | current-source 复核裁决 |
|---|---|---|
| HC-02 | canonical runner 不 ping、90 秒后必假离线 | **撤销**。`@hiveclaw243/hive-connect@0.1.9` 每 25 秒发送应用层 ping；仅真实设备 presence E2E 未验收 |
| HC-04 | `hive-connect daemon install` 未实现 | **撤销**。canonical CLI、launchd/systemd/Windows service manager 均有实现；仅真实机器安装与重启 E2E 未验收 |
| UI-01 | Office 专用浏览器编辑面缺失是产品断点 | **重分类为 DOC-01 且已闭环**。当前前端测试合同明确要求退役该面；AGENTS.md 已同步到 OfficeCLI preview 与 ArtifactSurface |
| SA-04 | 员工用户自定义 hook 无注册面是 P2 缺失 | **撤销为员工产品缺失；UI-09 已代码修复**。本地开发者 Hook 不能直接映射到员工设置；内部 Hook 与禁用权已移除，受治理通用扩展面仍需另立产品契约 |

---

## 10. 代码极简性与目标架构建议

### 10.1 可删除/合并清单（保留能力不变，每项均给出迁移与回归证明方式）

1. **InProcessCoordinationGateway 作为 ToolRuntimeService 默认回填（已删除）**——GV-01 根因已移除；其他 coordination 消费者仍按自身持久化边界使用 gateway，不受本修复影响。
2. **preflight CoordinationCheckpoint 写入（已删除）**——ASK 已收敛到 durable Approval ticket；DecisionTrace 只保留决策证据，不再保存悬垂 checkpoint id。
3. **Sentinel 全家**（`agents/coordination.py` 约 60 行+单测）——零生产消费者。AGENTS.md 已停止把它写成 live authority；源码与单测仍应删除。
4. **`AgentAgentRelationship` 孤儿表**（`models/org.py:80`）——已被 AgentCollaborationGroup 取代。迁移删除（alembic drop + db_bootstrap 清理）。
5. **`agent_work_ledgers` 死表**（`models/work_ledger.py`）——真实账本在 AGENT_DATA_DIR 文件。退役或接线，二选一，消除双事实源隐患。
6. **`consume_subagent_signals` 内存读取 API**（`subagent.py:1399`）——与 wake consumer 双事实源，保留 PG 版。
7. **DecisionTrace 文件 JSONL store（已删除）**——GV-09 已删除 live JSONL 权威和分裂 backfill helper；生产只保留 SQL store，测试 fake 位于 `backend/tests`，旧文件只能通过 hash-bound 一次性迁移命令进入 SQL。
8. **`services/t0_logger.py`（1240 行 legacy 层）**——与 `memory/t0/ledger.py` 并存；收敛为 ledger + 一次性迁移脚本。
9. **local_bridge Python 平行实现 + legacy gateway 客户端方法 + poller**——npm 是 skill 安装路径，Python 包无发布消费者且默认通道 404。退役。
10. **`hive_bridge_auto_adapter.py`**——关键词决定任务行为的 demo 脚本（典型 Model Agency 违规），不在生产路径，删除。
11. **前端死代码**：`LocalAgentLinkCard`、`getChatHistory`+`GET /chat/{agent_id}/history`（遗留双事实源）、`officeApi.createDocument`（在现行 Office 专用面退役合同下先核实无其他消费者，再退役）、`enterpriseApi.templates`（若无页面则删）、`/design-gallery` 公开路由（加守卫或移除）。
12. **后端死代码**：`handle_web_chat_disconnect`、`start_heartbeat`、`ConnectionManager`、`_claim_pending_reply_suffix_for_session`、`llm_utils` re-export、`engine.py:3094/3135` 惰性 shim、`direct_fallback_executor` 死字段、`agent_tools._execute_tool_inner`、heartbeat/auto_dream 内已退役循环残骸（`_build_evolution_context` 等 6 个）、charter 校准孤儿链（HN-07）、ELICITATION 无生产者分支、retriever 恒空 hook（`_retrieve_semantic_backend`/`_retrieve_external`）、`memory_service.on_conversation_start/end` 无调用方 wrapper。
13. **裸 subprocess 旁路**（非 Agent 控制代码执行但绕过 env 政策）：`agent_tool_domains/feishu_cli.py:31`、`external_capabilities/materializer.py:616`（git clone 继承全量 os.environ 含密钥）——至少过 `sanitize_agent_execution_env`。
14. **双目录 pack**：`packs/personal_knowledge_pack/pack.yaml` 与 `backend/packs/...` 逐字节相同，保留单一来源；`skill-package/` 与 `skills/` 两份 hive-bridge SKILL.md 同理。
15. **api/config_history.py**——已 410 退役的纯转发兼容适配器，可删。

### 10.2 文档事实源修复（DOC-01，已闭环）

`AGENTS.md` 已按 current source 完成以下收正：删除迁移、router/model/service、前端 test file 和历史 pass count 等手填数字，改为 `git ls-files` / `rg --files` / `alembic heads` 现场生成；从 live catalog 删除 `scheduler`、`extract_queue`、`extract_agent`、`knowledge_inject`、`viking_client` 等不存在项；将 Objective 改为 `AgentSessionGoal`，A2A 关系改为 `AgentCollaborationGroup`/member；`proactive_employee_loop` / `policy_replay` 明确为无 live runtime；Sentinel 只保留“不可作为运行权威”的事实说明，不再宣称消费；Office 改为 backend OfficeCLI tool/preview + frontend `ArtifactSurface`，并明确专用 `OfficeWorkbenchSection` 与 ONLYOFFICE WYSIWYG/env 已退役。另将 `test_feishu_streaming_cards.py` 的旧机器绝对路径改为 `__file__` 相对定位。

Evidence：Feishu 定向 RED `1 failed, 5 passed`，修复后 `6 passed`；Ruff/format 通过。结构检查确认 handbook 中 16 个 live path 均存在、8 个 retired/missing path 均不存在；stale active tokens（`4223 passed`、`Migrations | 79`、`Vitest 4 (39...)`、旧服务/实体/ONLYOFFICE env）零命中。Office retirement/Artifact preview 三文件 `116 passed`，Architecture 中对应 lazy-boundary 契约 `1 passed, 6 skipped`。未过滤的四文件组合还暴露 2 个无关行数预算失败，已独立登记 UI-10，不把它伪装成 DOC-01 失败或静默吞掉。该闭环不把 HN-01/HN-02 的未建设能力伪装成实现。

### 10.3 目标架构判断

未发现需要架构重做的点。现有分层（kernel 无 DB / ToolRuntimeService 唯一执行面 / RuntimeTask 唯一后台执行记录 / ChatTranscriptEvent+T0 双投影 / Memory Gate+Platform Gate 双门）是健康的能力保持型结构。三个结构性收敛建议：

- **确认车道统一**：preflight ASK 已并入 enterprise Approval ticket 的单次消费+hash 绑定+精确重放底座，GV-01/GV-02 代码断点已消除；typed Owner action policy 也已通过 exact action id 接到同一 preflight 装配，GV-03 代码断点消除。session permission 与 workflow gate 保留各自语义，但不得再产生无恢复消费者的新确认对象。
- **Secret egress 回到精确事实边界**：PrivacyLayer 的模式识别只能提供候选/audit 信号；硬拒绝必须来自当前 principal 无权披露的真实 credential bytes 或可信 secret reference。对最终表达只遮蔽精确禁止字节，不得因自然语言像 secret 而重写或拒绝整个工具调用。
- **execpolicy 与 unified exec 吸收**（Codex 增量，不冲突 CC 语义）：命令级声明式策略 DSL（`codex-rs execpolicy/src/decision.rs`）补 GuardPolicy 与 capability 之间的粒度空档；PTY/持久 shell 会话（`core/src/unified_exec/`）补长交互式命令场景（当前 run_command 一次性）。turn diff tracker 为次要 UI 增量。

---

## 11. Eval Handoff 与待证明能力

以下事项仅靠本次源码与运行审查无法充分证明，必须交接独立 Eval/Acceptance 阶段；这些不是“修完 P0 后可跳过”的附加项：

1. **单 Agent 智能对标 hermes 的端到端质量主张**（北极星 Goal 1 核心）。已有证据：记忆/压缩/技能机制源码闭环且多处更强。缺口：无行为级对比 trace。应比基线：hermes-agent 当前 checkout。必须机械验证的硬不变量：记忆召回命中后续 turn、skill 晋升后真实被加载使用；开放判断：回答质量。环境：双 checkout + 相同模型。不验证的风险：Goal 1"至少一样好"停留在架构宣称。
2. **真 PG 故障注入**：reclaim exactly-once（`test_runtime_task_claim_fencing_postgres.py` 本次 Docker-off skip）、RLS 跨 owner 拒绝、A2A pending management-only/runtime-hidden disclosure boundary、迁移回填（97 个真 PG 测试文件本地静默 skip）。已有证据：CI ubuntu runner 有 Docker；缺口：本 checkout 无运行证据。不验证的风险：多 worker 并发、RLS 与跨 Owner 披露边界缺陷被 skip 掩盖。
3. **多进程部署行为**：Redis cancel bus cross_process 分支、WS fanout 进程内实现多实例丢失、`_summary_breaker` 进程内 dict。需双进程验收证据。
4. **GV-01/GV-02 真 PG 自治车道端到端**：本轮已有不 monkeypatch ToolRuntimeService execution/registry seam 的组合测试，覆盖 ASK→批准→效果发生→票据 succeeded 与 REFUSE→票据 failed；但 request→approve→RuntimeTask worker 的真 PG 事务测试在本机因 Docker/Testcontainers 不可用全部 skip。生产闭环前必须在 Docker-on/CI 环境跑过该链及 continuation exactly-once。
5. **GV-08 真环境 exact-secret 验收**：本地已覆盖 benign 文档/fixture、真实活跃 secret、嵌套参数、工具/模型出站、Web/Session/Channel durable ingress、typed transport 加密、legacy dry-run/apply 与非禁止字节 byte-faithful。Acceptance 环境仍须用真实 `SECRETS_MASTER_KEY` 审核 v3 count-only inventory，执行经 operator 确认的 backfill/rotation，并在真 PG、实际 provider stream/断线重试、Channel replay 与 tenant offboarding 中证明 plaintext 为零、exact active secret 不外泄且 benign bytes 不变。
6. **KB 检索召回质量**：词法检索在真实 owner 语料上的命中率；L2 发现链（tool_search→schema 扩展）生产命中率无 trace。
7. **Hive Connect 真实设备端到端**：canonical `@hiveclaw243/hive-connect@0.1.9` 的 ping 与 daemon source 已验证，HC-01 的 source-session outbox/查询与 HC-03 的文件 scope/live-policy I/O gate 也有本地单元与邻接回归；仍需在 Docker-on PG 与真实登录设备验证 install→restart→presence、云端 A2A 委派→本地 result→outbox worker→来源 Agent 自动续跑、断线重试/reload、策略 allow→deny 后的 upload/download、消息/文件/回执，以及 Desktop 对 `desktop_*` router 的真实消费。
8. **触发器/heartbeat needs_reconciliation 队列的运营闭环**：恢复后人工和解是否真实可操作（无 UI 入口证据）。
9. **A2A-03 双 Owner 浏览器旅程与审计 receipt**：在两个真实 Owner、两个私有 Agent 下执行 source Owner create→search→invite，确认 target Owner 的目标 Agent 管理面出现 pending 且普通 callable 面仍为空；target Owner approve 后双方 callable，revoke 后立即消失，reinvite 后回到 pending。同步查询 tenant security audit，核对 create/invite/approve/revoke 与 actor/target/group 事实；org_admin 代审无理由必须失败、有理由才成功。
10. **UI-02 治理面真环境旅程**：在真 PG/Redis 下并发执行同一 Agent GuardPolicy 首写与 stale update，确认唯一版本、typed 409、tenant audit 与业务页面恢复；模拟 strict platform audit 不可用，确认 global FeatureFlag mutation 为零；成功 commit 后确认 Redis key 才失效。真实浏览器分别以 Owner/Manager、org_admin、platform_admin 验证 Action Guardrails 的可保存/恢复与 raw policy 零渲染、global Feature Rollout 的角色隔离、typed targeting/expiry/override CRUD 和 stale 冲突恢复。

## 12. Frontend Experience Handoff

**需要，但限定范围**。确定性待实现产品缺口集中在 UI-04（soul 可见性）与 UI-05（文件版本）；Office 专用面的 DOC-01 文档漂移已经收正，不是默认恢复项。A2A-03、UI-02、UI-03 与 UI-09 的代码边界已经执行：A2A 普通区只显示可调用员工，pending/revoked 只进管理区；Company Control Plane 只呈现业务化 Action Guardrails，global Feature Rollout 只进入 Platform Admin；普通用户与 Owner/Manager 只消费业务政策、可理解的健康结果和恢复动作；raw handler/event/receipt 仅在 Platform Developer 诊断 API 按需披露，所有内置平台 Hook 均不可按员工禁用；AST i18n inventory 的八项 gate 已归零并进入 CI。真实浏览器阶段还要验证：①A2A 双 Owner create/invite/approve/revoke 旅程中 pending 不进入 callable 列表、页面不渲染 raw id；②员工设置不再出现 Hook 名、event、failure mode、raw receipt 或 Enable/Disable，概览健康投影在 healthy/degraded/needs_attention 下均可理解；③Owner/Manager 能保存并恢复 Action Guardrails 且从不看到 `tool_rules`/tool id/raw JSON，org_admin 看不到 global flags，platform_admin 能完成 typed rollout CRUD/expiry/override/stale 冲突恢复；④在真实中英文浏览器逐页检查缺失 key、插值、切换语言和 reload，不重复实现另一套 inventory；⑤三受众在 live、reconnect、reload、history、resume 下的信息层级与恢复操作体验。交接 `ccplus-frontend-product-review-prompt.md` 时不得预设恢复已退役的 Office tab，也不得把“自定义 Hook”设计回员工设置。

## 13. 完整落地方向与验收矩阵

| 项 | 最小完整闭环方向 | 迁移/回填/清理 | 验收要求 |
|---|---|---|---|
| GV-01+GV-02（确认车道） | **已实现**：preflight ASK/ESCALATE 并入 Approval ticket；exact approval audit-evidence 后放行；REFUSE/PREPARE_ONLY 不可由 approval 覆盖；typed block 票据失败 | 内存 checkpoint 无持久化存量；InProcess 默认回填与孤立 checkpoint 创建已删除 | 本地真实 execution seam 组合测试已通过；真 PG worker 4 项因 Docker 不可用 skip，CI/验收环境必须补绿 |
| GV-03（typed Owner action policy） | **已实现**：prompt 与 preflight 消费同一 exact schema/action/zone；full_authority 不被 advisory 风险轴反向收紧，confirm_first 走同一 Approval ticket，never_do/损坏合同对效果型动作在普通 approval 前 fail-closed；依赖故障保留无关只读能力 | 复用 `ConfigRevision`，无 schema migration；legacy agent 首次 live 消费幂等回填；Owner manage API 强制 expected-version、支持 history/rollback 并写 audit；员工设置消费保存与两步确认恢复，隐藏机器 ID | 后端相关聚合 `168 passed`、Ruff 绿；前端 `115 passed`、build/bundle budget 绿；真 PG 双 worker 首次回填竞争与 rollback 仍待验收环境补绿 |
| SA-05 | **已实现**：`start_workflow` 注册 ACTION_KINDS/_ACTION_INTENT，并以 `bridge:self` 保持 Workflow 自有 durable preview/显式 start 确认，不引入自动 Plan Mode | 无 schema/数据/config 迁移 | 真实 REST→PlanModeGate→lease→launch 测试返回 200；13 文件相关聚合 `239 passed`、Ruff 绿；10 项真 PG lease 测试因 Docker 不可用 skip |
| GV-04 | **已实现**：canonical platform_admin + active target 校验和 RLS pin 后、业务 route 前写独立提交的 `platform_security.tenant_impersonation`；只记录真实跨租户，审计失败 typed 503 | 无 schema/config 迁移；历史化身无事实证据，明确不可回填 | operator row/envelope 含 actor/home/target/method/path/IP/request id 与 event receipt；相关聚合 `49 passed`、Ruff 绿；统一 operator 查询/验链已由 GV-06 接通 |
| GV-05 | **已实现**：upstream 前查 tenant/user quota + tenant/user Redis 60/min；provider usage 优先，缺失 usage/断流走显式 estimated fallback；严格计量成功后才发非流式结果或 SSE `[DONE]` | 复用既有 usage 表/counters，无 schema 或历史回填；direct-user counter 漏计已修，聚合更新加 row lock | typed 429/503、`desktop_llm_proxy` usage event、user+tenant counter、meter-before-DONE、断流恢复均有回归；扩大聚合 `76 passed`、Ruff/format 绿；3 个真 PG 用例与真实 Redis/provider SSE 待验收 |
| GV-06 | **已实现**：`platform_security.*` 由 strict v2 writer 在 PG advisory lock 下写 sequence/prev/event hash；chain_cutover 和每事件 legacy anchor 覆盖不可变 v1；损坏 head 拒绝追加；platform_admin 有过滤查询与全链验签；未锚定/损坏行不标可信；Desktop 行明确 `client_asserted` 且 claimed Agent 受 tenant 校验 | 复用 `audit_logs` 既有 UPDATE/DELETE/TRUNCATE 禁写，无 schema migration；历史 v1 只做 digest anchor、不伪造回填；通用 fail-soft sink 拒绝 platform-security namespace | 红态 `9 failed, 4 passed`；最终审计/RLS/immutability 聚合 `106 passed, 3 skipped`，Ruff/format 绿；3 个迁移真 PG 用例和包含 operator 写入→查询→验链路径的集成文件 6 个用例因 Docker unavailable skip，生产多实例仍待验收 |
| GV-07 | **已实现**：quota exhausted 与 quota authority unavailable 分别返回 `quota_denied` / `quota_unavailable`，统一产生 durable `quota_exceeded` typed event；资源权威不可验证仍 fail-closed，且不回显底层 exception message | 无 schema/config/历史数据迁移；事件复用现有 ChatTranscriptEvent/message-part 投影 | RED 后端 `3 failed`、前端 `1 failed`；最终后端相邻聚合 `68 passed`、前端 `176 passed`，Ruff/format、TypeScript/Vite build 与 bundle budget 绿；生产 authority outage→WebSocket→reload 故障注入待补 |
| GV-09 | **已实现**：DecisionTrace 生产只写 SQL；decision feedback、SessionFeedbackEvent 与 AuditLog 共用 request transaction，既有 explicit Memory overlay 保持独立可恢复 receipt；typed session decisions API 复用 session authority 并精确绑定 tenant+agent+session；员工右栏只消费中英文人类可读动作、结果、原因与反馈，未知 tool 不暴露标识 | 既有 SQL 表无需迁移；live JSONL store 已删。旧 JSONL 只能经 dry-run SHA-256 绑定、显式补齐缺 tenant、零坏行/孤儿、幂等导入后移动到可逆 archive；本地 4 行遗留文件因无 tenant 保持不动 | 后端相邻聚合 `152 passed, 1 skipped`，唯一 skip 为 Docker-off 真 PG；前端 `139 passed`；Ruff/format、TypeScript/Vite build 和 bundle budget 绿。仍需 Docker-on PG、真实浏览器和有权 operator 确认后的遗留 apply |
| GV-08 | **已实现**：模式扫描只生成 count-only audit candidate；tenant-scoped LLM/channel/tool/MCP inventory 提供 exact authority；ToolRuntime 在最早 pre-effect seam 拒绝原始 secret bytes，模型/工具/Web/Session/Channel/T0 ingress/egress 只遮蔽 exact bytes；typed reply target/inbox token 透明加密；扫描文件以 immutable snapshot 和 SHA receipt 绑定执行 | 无 DDL；`migrate_channel_secrets` v3 默认 count-only dry-run，`--apply --confirm` 轮转 channel config、delivery target、ingress transport token，并 backfill legacy ingress exact active secrets；offboarding 清除所有 typed target/ingress token | RED 先后为 `5 failed, 11 passed, 9 skipped`、`2 failed, 25 passed`，stream 日志和 legacy backfill 合同也先红；最终核心/存储 `330 passed, 10 skipped`、入口相邻 `407 passed, 57 skipped`，Ruff/compileall/diff check 绿；真 PG/keyring/provider/backfill acceptance 仍待补 |
| HC-01 | **已实现**：入队 exact 绑定来源 authority，advisory lock 复用同一 active channel；result 与 `a2a_delegation` completion outbox 同事务；既有 parent continuation 自动唤醒；`check_async_task(message_id)` 返回本地执行与 outbox 状态 | 复用既有 Runtime Result/Notification Outbox，无 DDL/历史回填；terminal replay 以原始持久化 result 幂等修复缺 outbox 行 | 本地邻接 `111 passed`、Ruff/compileall/diff check 绿；真实 PG 用例因 Docker unavailable skip，Docker-on + 真实 Hive Connect 设备需补 result→source continuation/retry/reload receipt |
| SA-01 | **已实现**：invoker 派生预算由 live kernel 消费；cache-miss 累计达到预算且模型还提出工具时，在任何新工具执行前发 typed event、持久化并以 `TOOL_BUDGET` 退出；完成答案 byte-faithful；permission resume 从 committed logical-root ModelResult 恢复累计量，排除 continuation 双计，quota 只提交新增 delta | 无 schema/config/数据迁移；复用 recovery manifest、provider result seal、token usage 与 Web terminal metadata | RED 为原真空合同 `1 failed`、零 cache-miss 边界 `1 failed`、resume 累计合同 `1 failed`、durable 汇总合同 `2 failed`、Web 传递合同 `1 failed`；最终 14 文件聚合 `374 passed, 7 skipped`，Ruff/format/compileall 绿；7 skip 为 Docker-off 真 PG permission runtime |
| A2A-03 | **已实现**：callable read model 保持 active-only；另建 manager-only human projection 与 tenant-bounded candidate search；目标 Owner/带理由 admin 状态机和 canonical audit 在服务端强制；过期群组不能邀请/批准且 group owner membership 不可撤销；AgentA2ASection 接 create/search/invite/reinvite/approve/reject/revoke | 无 DDL/回填；复用既有 group/member 与 SecurityAuditEvent，旧 pending/rejected/revoked 行会直接出现在正确 Owner 的管理面 | 首轮 RED backend `10 failed`、frontend `3 failed`，状态机加固 RED `2 failed`；最终 backend `17 passed, 1 skipped`、frontend `143 passed`、Ruff/format/build/bundle 绿；Docker-on PG 与双 Owner 浏览器/审计 receipt 待补 |
| HC-03 | **已实现**：上传在任何 workspace/ChatArtifact 写前检查 `files:upload` + live `local_agent.file_upload`；下载在路径解析/读文件前检查 `local_agent:receive` + live `local_agent.file_download`；Agent override 优先 tenant，missing/deny/unbound 全部 fail-closed | 无 DDL/回填；未绑定 legacy connection 通过重新 pairing 恢复 | RED `8 failed`；最终 Local Agent 邻接 `58 passed, 8 skipped`，Ruff/format/compileall/diff check 绿；Docker-on PG 与 canonical 真实设备文件旅程待补 |
| HC-05 + canonical E2E | 退役 Python/legacy gateway 实现；不重做已存在的 ping/daemon | 删除无消费者 Python package、legacy mjs gateway 方法/poller 与关键词 auto adapter，保留 canonical channel runner 所需 npm 文件 | 真实设备 install→restart→presence；legacy 404 poll 不再可达 |
| HN-05 | **已实现**：PersonalKnowledgeService 的 grant create/update/reactivate/revoke 与 canonical tenant AuditLog 同 session flush、同 request transaction；完整记录 authority binding，拒绝路径零事件 | 无 DDL/config/data migration；过去无 event/actor/时序事实，明确不伪造历史审计 | RED `2 failed, 1 passed`；最终 service/API/PG 定义集合 `61 passed, 11 skipped`，Ruff/format/diff check 绿；11 skip 均为 Docker-off，真实两事件事务链待补 |
| UI-09 + SA-04 | **已实现**：员工设置/card/browser adapter/旧 route 全部移除；员工概览只消费 `/runtime-health`；raw catalog/receipt 与 mutation 位于 `/admin/.../runtime-hooks`，先验 `platform_admin` 并按 Agent/tenant matcher 过滤；所有内置 Hook per-agent 不可变，仅当前注册 plugin Hook 可修改且先写 strict audit | 无 DDL/Alembic；启动时 active 只保留已注册 plugin 覆盖，内置或失效覆盖移入 `retired_hook_runtime_overrides`，逐 config 保留 reason/SHA-256/原 bytes，重复运行幂等并写 retirement strong audit；advisory transaction lock + row lock 防止并发 first-write/cleanup 覆盖 | RED backend 首轮 `10 failed`、并发补充 `2 failed`，frontend 新合同 3 项；Green backend `112 passed`、frontend `152 passed`，Ruff/format/compileall/build/bundle 绿。仍需生产读取 retirement receipt/audit chain，并在真实浏览器验证设置页零 raw Hook 与概览三状态 |
| UI-02 | **已实现并校正产品边界**：legacy `config_history` 不另建 UI，canonical AI assets 已消费；Control Plane 提供业务化 Action Guardrails，Platform Admin 提供 typed Feature Rollout；服务端收紧权限、机器合同、乐观并发、审计与 commit 后 cache invalidation | 无 DDL/回填；复用既有 GuardPolicy/SystemSetting、FeatureFlag `expires_at`、AI asset revision 与 canonical audit | RED backend 首轮 `8 failed, 5 passed`，另有 audit/first-write/stale/empty 四项各 `1 failed`；frontend 五目标文件全红并有 build 合同 RED；最终 staged backend `88 passed, 30 skipped`、frontend `34 passed`、Ruff/format/locales/build/bundle 绿；真 PG/Redis/audit/browser 待补 |
| UI-04/05 | soul 只读视图；文件版本入口 | — | 各面 UI 可达、权限正确并有浏览器验收 |
| UI-03 | **已实现**：AST-aware inventory + exact dynamic/wrapper rule table；双语 catalog 对齐、重复 path 清理、五文件中文默认值删除；测试从真实 catalog 取文案 | 无 API/schema/data migration；CI 在 frontend tests 前执行 `i18n:check` | Node `9 passed`；最终库存 215 files、3461/3461 keys、八项 gate=0；Frontend 全量 `128 files / 774 passed`，production build/bundle budget 绿；真实双语浏览器待补 |
| UI-10 | **已实现**：提取独立 `SessionDecisionHistory`，移除 AgentChat 代理 re-export，消费者直连 lineage/artifact/runtime/tool-result owner；不提高原阈值 | 无 API/schema/i18n/data 迁移 | RED `2 failed, 5 passed` → Architecture `7 passed`；12 文件邻接 `285 passed`，production build/bundle budget 绿 |
| DOC-01 | **已实现**：AGENTS.md live inventory 现场生成；幽灵服务/实体/Office 面改成真实路径或已知缺失；Feishu source contract 使用 checkout-relative 路径 | 无 schema/config/数据迁移 | RED `1 failed, 5 passed` → GREEN `6 passed`；Ruff/format、16 个 live path 与 8 个 absent path 结构检查、stale token 零命中均通过 |
| SA-03 | trigger/heartbeat 纳入 worker 分发或曝光 needs_reconciliation 运营面 | — | 重启后 trigger run 自动恢复或运营队列可操作 |

## 14. 实测证据、未证实项与发布边界

### 14.1 本次实测验收证据（原审记录 + Codex 复核）

- `alembic heads` → **单头** `completion_outbox_index_0721`；文件系统计数为 **178 个迁移文件**。原报告的“140 个含回填”未在本次复核中建立统一机械口径，不能与已复现事实混写。
- Codex 复跑 `.venv/bin/pytest tests -q --tb=short`：**7089 passed / 594 skipped / 4 failed（60.51s）**，与原报告数量一致。4 个失败逐一定性：①`test_feishu_streaming_cards` 硬编码已失效绝对路径；②③`test_agent_native_repair_ledger` 与 ④`test_schema_startup_gate` 因当前 `.git` worktree 指针损坏而在 `git ls-files` 失败。594 skipped 含 Docker-off 真 PG 测试，不能视为 acceptance green。
- 当前规模复现为 **923 个 test 文件 / 7278 个 test function 定义 / 7687 个 pytest collected cases**。该数字只保留为本报告时点证据；AGENTS.md 已删除 "4223 passed" 与所有手填规模基线，改为 current-checkout 现场生成。
- 修复前直接调用 `intent_type_for_action("start_workflow")` 可复现 `ValueError`，而 `test_confirmed_plan_is_consumed_for_the_exact_workflow_preview` 因 fake gate 仍绿；SA-05 已用真实 gate REST 回归替换这项 wiring proof。GV-08 原 `test_tool_runtime_service_preflight_refuses_credential_arguments` 已反转为 benign secret-shaped fixture 必须执行；另以可信 binding 注入的 exact active secret 单独验证在 hook/Plan Mode 前 fail-closed。
- `npm view @hiveclaw243/hive-connect version bin` 返回 **0.1.9** / `hive-connect: run.js`；对应源码 `npm/package.json` 同版本。`go test ./platform/hive ./daemon ./cmd/cc-connect` 三包全部通过，反证 HC-02/HC-04 的“未实现”结论。
- 子审实测：kernel+invoker 195 项全绿；knowledge 平面 134 项全绿。
- GV-03 后端聚合实测：`backend/.venv/bin/pytest -q backend/tests/services/test_owner_action_policy.py backend/tests/services/test_action_preflight.py backend/tests/services/test_agent_context.py backend/tests/runtime/test_invoker.py backend/tests/tools/test_tool_runtime_preflight.py backend/tests/tools/test_resolver.py backend/tests/tools/test_service.py backend/tests/api/test_agent_autonomy_api.py` → **168 passed / 0 failed / 1 Starlette deprecation warning**；对应 Ruff 命令 → **All checks passed**。
- GV-03 前端实测：`npm test -- --run src/api/domains/autonomy.test.ts src/pages/agent-detail/AgentActionPolicyCard.test.tsx src/pages/agent-detail/AgentDetailSections.test.tsx` → **3 files / 115 tests passed**；`npm run build` → **成功**，AgentDetail 与 shared vendor raw/gzip 四项 budget 全部通过。
- SA-05 实测：13 个 Plan Mode/Workflow 相关 test 文件合并运行 → **239 passed / 0 failed / 1 Starlette deprecation warning**，Ruff → **All checks passed**；其中新增 REST 用例使用真实 `PlanModeGate`。`backend/tests/integration/test_plan_authorization_lease_postgres.py -rs` → **10 skipped**，原因均为 Docker/Testcontainers connection refused。
- GV-04 实测：修复前定向回归 → **2 failed / 12 passed**（跨租户零审计、operator sink 故障未 fail-closed）；修复后 `uv run pytest tests/core/test_security.py tests/core/test_tenant_middleware_public_paths.py tests/core/test_policy_audit.py tests/services/test_audit_logger_rls_scope.py tests/api/test_selected_tenant_scope_api.py tests/api/test_tenants_api.py -q` → **49 passed / 0 failed / 1 Starlette deprecation warning**，对应 Ruff → **All checks passed**。`rg` 复核公共 `get_current_user` 在 API 中有 378 处 dependency 声明/引用，事件写入位于该 live 身份边界而非孤立模块。
- GV-05 实测：修复前 `uv run pytest tests/api/test_llm_proxy.py tests/services/test_token_tracker.py -q` → **6 failed / 7 passed**，机械复现 quota/rate/usage/user-counter/strict-failure 六个断点；修复后同组扩展到 **17 passed**，加入既有 autonomous metering 后为 **20 passed**。最终相邻聚合 `uv run pytest tests/runtime/test_invoker.py tests/runtime/test_workflow_quota.py tests/api/test_llm_proxy.py tests/services/test_token_tracker.py tests/services/test_llm_usage_metering.py -q` → **76 passed / 0 failed**；4 个变更 Python 文件 Ruff/format check 通过。`tests/services/test_quota_guard.py -rs` → **3 skipped**，均为 Docker/Testcontainers connection refused。运行期 router inventory 复现 `/api` 与 `/api/v1` 下 models/completions 四条 live path。
- GV-06 实测：首轮四个新/改 test 文件 → **9 failed / 4 passed**，分别复现 operator chain/sealer/verifier 缺失、仍只写 v1、platform-admin 查询/验链 API 不存在、Desktop 未声明低信任/顶层事实可伪造/跨 tenant Agent claim 可接受；补充回归还证明未初始化的 legacy 行不会被误标成 anchored、损坏的 v2 head 不能继续追加、无 tenant 身份不能写入 Desktop client evidence、Desktop action 在入库前受列宽机器合同约束。最终 16 个审计、RLS、不可变迁移、身份相关 test 文件聚合 → **106 passed / 3 skipped / 1 Starlette deprecation warning**；3 个 skip 均为 Docker/Testcontainers unavailable。11 个实现/测试文件 Ruff → **All checks passed**，format check → **11 files already formatted**。`tests/integration/test_startup_auth_rls_bootstrap.py -q -rs` → **6 skipped**（同一 Docker 原因），其中真实 PG 下的 v2 写入→platform_admin 过滤查询→全链验签尚未在本机执行。
- GV-07 实测：RED 后端三项 → **3 failed**（旧 `type=quota`、authority failure 误标 provider、`quota_exceeded` 未进入 durable native event），前端 status 用例 → **1 failed**（quota reason 回落为 Working）。修复后 `tests/runtime/test_invoker.py`、`tests/services/test_chat_message_parts.py`、web terminal reason 与 WebSocket 邻接用例聚合 → **68 passed**；前端 `AgentDetailSections.test.tsx + chatRuntime.test.ts` → **176 passed**。6 个 Python 文件 Ruff → **All checks passed**、format → **6 files already formatted**；`npm run build` → TypeScript/Vite 成功、7366 modules transformed，AgentDetail `342157/380000` bytes、gzip `94465/115000`，shared vendor 两项 budget 通过。
- GV-09 实测：修复前回归机械复现 SQL store 内部 commit、session feedback 默认 JSONL、会话 decisions API 404、前端 adapter/UI 缺失；追加回归又分别先红于 unscoped decision 可跨 session 反馈、迁移缺少 dry-run hash/tenant assignment gate、API 无 typed response、synthetic message feedback 无纯函数合同与中文员工文案缺失。最终 `backend/.venv/bin/pytest -q backend/tests/api/test_chat_session_feedback.py backend/tests/services/test_decision_trace.py backend/tests/services/test_session_feedback.py backend/tests/services/test_auto_dream.py backend/tests/scripts/test_migrate_decision_trace_jsonl.py backend/tests/tools/test_service.py` → **152 passed / 1 skipped**；`test_decision_trace.py -rs` 确认唯一 skip 是 Docker/Testcontainers connection refused 的真实 PG tenant/RLS 用例。13 个 Python 文件 Ruff → **All checks passed**、format → **13 files already formatted**。`npm test -- --run src/api/adapter-cleanup.test.ts src/pages/agent-detail/AgentDetailSections.test.tsx` → **2 files / 139 tests passed**；`FrontendSurfaceHygiene.test.ts` → **3 passed**；`npm run build` → TypeScript/Vite 成功、7366 modules transformed，AgentDetail `347276/380000` bytes、gzip `95831/115000`，shared vendor budget 通过。真实遗留 JSONL dry-run receipt 为 **2 decisions / 2 feedback / 0 skipped / 0 orphan / 2 unresolved tenant / can_apply=false**；`stat` 仍为 1720 bytes 与原 mtime，证明未 apply/移动。
- GV-08 实测：第一轮 ingress TDD 为 **5 failed / 11 passed / 9 skipped**，实现最早 Session V2/Web/Channel exact-redaction 后转绿；补 direct Web/Channel durable persistence 时先为 **2 failed / 25 passed**，再转绿；raw stream message logging 与 legacy ingress exact backfill 均以失败合同先行。最终先用 `git write-tree` + `git archive` 构造只含 staged index 的隔离快照，再运行核心/存储集合（25 个 GV-08 runtime/service/API/memory/migration/tool 文件）→ **330 passed / 10 skipped / 1 warning**，Session/Web/Channel 相邻集合（29 个文件）→ **407 passed / 57 skipped / 1 warning**；因此本提交不依赖另一个 session 未暂存的 dashboard/completion-outbox 改动。两集合部分重叠，故不相加伪造总数。涉及实现与测试文件的 Ruff → **All checks passed**；`python -m compileall` 和 `git diff --check` 均 exit 0。扩展回归暴露的 Feishu 绝对路径已由 DOC-01 单独以 RED→GREEN 修复，未混入 GV-08 计数。
- DOC-01 实测：`tests/api/test_feishu_streaming_cards.py` 修复前 **1 failed / 5 passed**，失败点精确为旧 `/Users/rocky243/vc-saas/hiveclaw/...` 路径；改为由 `Path(__file__).resolve().parents[2]` 定位 backend 后 **6 passed**，Ruff/format 绿。AGENTS.md 的 live/absent path shell contract 全绿，stale active token 查询零命中，`git diff --check` 通过。前端 Office retirement/Artifact preview 三文件 **116 passed**，过滤后的 Architecture lazy-boundary 契约 **1 passed / 6 skipped**；未过滤组合的 **2 failed / 121 passed** 属于 UI-10 行数预算，已单独登记并保留原失败证据。
- SA-01 实测：原 `test_turn_token_budget_does_not_preempt_tool_followup` 反转为真实资源合同后，首轮为 **1 failed / 2 passed / 96 deselected**，证明 live loop 仍执行了工具和第二次 provider call；新增零 cache-miss 用例又以 **1 failed / 99 deselected** 证明可信 `0` 被错误回落为整段 prompt 估算。恢复审计随后以 **1 failed / 114 deselected** 证明 kernel 不接受 durable prior usage，以 **2 failed / 118 deselected** 证明 committed logical-root 汇总不存在，并以 **1 failed / 13 deselected** 证明 Web→invoker 未传递恢复用量。修复后预算合同覆盖新工具前 hard-stop、cache-read 排除、可信零、完成答案原字节、resume 累计预算、output-continuation 去重、缺 provider usage 的 seal-backed estimate、累计终态与新增量计费；Web finalizer 另覆盖 `TOOL_BUDGET`→failed terminal metadata 且 content 不改写。`cd backend && .venv/bin/pytest -q` 的 14 文件邻接集合 → **374 passed / 7 skipped**；7 个 skip 均来自 `test_session_permission_runtime.py`，原因为 Docker/Testcontainers connection refused。11 个变更 Python 文件 Ruff → **All checks passed**，format/compileall 通过。无迁移或历史数据处理；真 PG permission approval→resume→budget receipt 仍保留在验收环境。
- A2A-03 实测：current-source 先证明原报告误把 callable read model 当管理 read model；pending target Owner 既拿不到 group，也拿不到 membership key。首轮 RED `tests/services/test_a2a_group_management.py + tests/api/test_a2a_api.py` → **10 failed / 2 passed**，机械覆盖模块/API 缺失、管理越权、零审计、admin 无理由代审、错误 path Agent 与非法状态重放；Frontend section+adapter → **3 failed / 27 passed**，覆盖 management query 不存在、use-only gating 缺失和六类 API 消费不完整。状态机加固再得到 RED **2 failed / 15 passed**，钉死 group owner 不可撤销；修复后 backend service/API/真 PG 定义集合 → **17 passed / 1 skipped**；唯一 skip 是 Docker/Testcontainers connection refused 的 pending-management-only/runtime-hidden PostgreSQL 用例。Frontend adapter+section+AgentDetail → **3 files / 143 passed**；5 个 Python 文件 Ruff/format 绿，locale JSON 可解析；仅暂存树 `npm run build` 成功、7364 modules transformed，AgentDetail `348097/380000 bytes`、gzip `96085/115000 bytes`，shared vendor raw/gzip budget 全绿。source trace 只发现 HTTP management consumer，没有 runtime/prompt consumer；页面断言同时钉死 raw membership/agent id 不渲染。
- UI-09 实测：Backend 新合同初始 `10 failed / 1 passed`，分别复现缺 employee-safe health projection、org_admin 仍可 raw 读取、全局 registry 跨 tenant 泄漏、内置 Hook 可修改、extension mutation 无 strong audit、旧 route 仍存在、startup 无 allowlist/retirement/recovery；提交前并发复核再以 **2 failed / 5 deselected** 机械复现 first-write 无串行锁与 startup cleanup 未锁行。Frontend 三项新增合同分别红于缺 runtime-health adapter、概览不消费健康投影和 Hook 卡/browser adapter 仍存在。首次前端命令还暴露 `AgentChatSection` 与 `SessionRuntimePanel` 两项既有行数预算失败，未混入本 finding 计数。最终用 `git write-tree` + `git archive` 构造只含 staged index 的隔离快照验证：Backend Hook API/service/startup/runtime/wire/governed runner → **112 passed**；Ruff → **All checks passed**，format → **5 files already formatted**，compileall exit 0。Frontend boundary/adapter/AgentDetail/CCParity → **4 files / 152 passed**；`npm run build` → TypeScript/Vite 成功、7364 modules transformed，AgentDetail `348085/380000` bytes、gzip `96087/115000`，shared vendor 两项 budget 通过。`rg` 只在反向不变量测试中命中已删除组件/API 名；旧 `/agents/{id}/hooks` GET/PATCH route inventory 为零。
- UI-10 实测：既有 `ArchitectureSimplicityContract.test.ts` 在修复前为 **2 failed / 5 passed**，分别记录 `AgentChatSection.tsx` 测试计数 2406 行（物理 2405）超过 2400、`SessionRuntimePanel.tsx` 测试计数 1226 行（物理 1225）超过 1200。修复没有提高阈值：最终物理行数分别为 2379 与 1069，新 `SessionDecisionHistory.tsx` 为 163/220；Architecture 合同 **7 passed**。仅暂存树运行 12 文件 chat/runtime 邻接集合 → **285 passed**；`npm run build` → TypeScript/Vite 成功、7365 modules transformed，AgentDetail `348097/380000` bytes、gzip `96079/115000`，shared vendor `591449/620000` bytes、gzip `186474/200000`，四项 budget 全绿。`git diff --cached --check` 通过；没有 API/schema/i18n/data 迁移。
- UI-02 实测：current-source 先反证原“三 router 零消费”结论——`config_history.py` 是只代理 `ai_asset` 的 legacy compatibility surface，canonical `aiAssetsApi`/`WorkspaceAIAssetsSection` 已消费 catalog/detail/revisions/rollback/reconcile。真实缺口的 Backend 首轮为 **8 failed / 5 passed**，机械复现 stale GuardPolicy 500、malformed policy 未拒绝、tenant audit 缺失、org_admin 可操作 global flags 以及 key/percentage/override/expiry 合同缺口；后续 strict audit failure、首写 advisory lock、stale flag update 与 empty PATCH 又分别得到 **1 failed** 的 RED。Frontend governance adapter、Action Guardrails、Feature Rollout、Control Plane 与 Admin Platform 五个目标文件首轮全部失败，build/type/route/icon 合同也逐项先红。最终仅暂存树 Backend 八文件邻接集合 → **88 passed / 30 skipped / 1 warning**；`-rs` 证明 30 skip 全部是 Workflow/Trigger 文件因 Docker/Testcontainers connection refused。Frontend 10 文件 → **10 files / 34 passed**；Ruff → **All checks passed**，format → **5 files already formatted**，locales JSON 可解析；`npm run build` → TypeScript/Vite 成功、7370 modules transformed，AgentDetail `348097/380000` bytes、gzip `96079/115000`，shared vendor `591449/620000` bytes、gzip `186474/200000`，四项 budget 全绿。无 DDL、迁移或回填。
- UI-03 实测：原 349/306 缺失数先降级为未证实；仓内 AST inventory 在修复前 staged baseline 得到 **285 missingBoth / 21 catalogOnlyChinese / 2 duplicateCatalogKeys / 143 chineseDefaults**。Node TDD 依次以 module-not-found、duplicate-key export 缺失和 runtime fallback 误豁免动态规则形成 RED；AI asset interpolation 合同也先证明字符串 fallback 未传 `version`。移除生产 fallback 后，旧测试 mock 首轮为 **9 failed / 118 passed**，暴露测试只消费源码默认值；新增真实 catalog helper 自身先以 module-not-found RED，再把相关 7 文件修到 **129 passed**。差异复核发现 `featureFlagAudienceSummary` 通过 `translate` 参数使用 `t`，新增 wrapper 合同先为 **1 failed / 8 passed**，随后用 exact source+callee+reason 纳入同一库存。最终纯 staged tree `npm run i18n:check` → **9 Node tests passed**；inventory 为 **215 files / en=3461 / zh=3461 / 2601 static calls (2122 unique) / 116 dynamic calls**，八项 gate 全 0，四个 SHA-256 见 §8。第一次全量 Vitest 因 Node test 文件名落入收集 glob 得到 **1 failed / 128 files passed / 774 tests passed**；更名后同一 staged-tree 全量 → **128 files / 774 passed**。`npm run build` → 7370 modules transformed，AgentDetail `348097/380000` bytes、gzip `96071/115000`，shared vendor `591449/620000`、gzip `186474/200000`；`git diff --cached --check` 通过。无 API/schema/data migration。
- HN-05 实测：service 层 create/revoke AuditLog 合同首轮为 **2 failed / 1 passed**；失败稳定为授权对象已改变但 session 中零 AuditLog，非 Owner 零事件原合同保持。修复后 `backend/.venv/bin/pytest -q backend/tests/services/test_personal_knowledge_service.py backend/tests/api/test_agent_personal_knowledge_api.py backend/tests/integration/test_personal_knowledge_cross_owner.py -rs` → **61 passed / 11 skipped / 1 warning**；11 skip 全来自 Docker/Testcontainers connection refused，新增真 PG 查询已要求同一 grant 精确出现 `upserted`、`revoked` 两条 tenant/actor 事件。3 文件 Ruff → **All checks passed**，format → **3 files already formatted**，`git diff --check` 通过。无 DDL/config/data migration，历史变化不伪造回填。
- HC-03 实测：目标 API/service 合同首轮为 **8 failed / 20 passed**，稳定复现 upload 不读取 policy、deny 后仍写盘、download 不验 receive scope/live policy、统一 resolver 缺失。修复后同一集合 → **28 passed**；扩大到 Local Agent pairing/channel/A2A/protocol/trust-boundary 集合 → **58 passed / 8 skipped / 1 warning**。8 skip 全来自 Docker/Testcontainers unavailable 的 `test_local_agent_channel_protocol.py`，其中新增真实查询要求 Agent file policy 从 allow 改 deny 后立即拒绝。8 个相关 Python 文件 Ruff → **All checks passed**，format → **8 files already formatted**，compileall 与 `git diff --check` exit 0。无 DDL/config/history backfill；未绑定旧 connection 通过重新 pairing 恢复。

### 14.2 未证实项汇总

T0 磁盘实物抽样与 T0_STARTUP_BACKFILL 生产执行；Redis 传输不可用时 bridge 仅靠 sweep 兜底的行为；后台 subagent 完成唤醒 payload 上限；`skill_candidate_loop_v1` 生产 FeatureFlag 行真实状态；GV-01/GV-02 的真 PG request→approve→worker→continuation exactly-once；GV-03 的真 PG 双 worker 首次 legacy policy 回填竞争与 transaction rollback；SA-05 confirmed workflow 的真 PG lease consumption/rollback；SA-01 的真 PG permission approval→同 run resume→committed ModelResult 累计预算/新增量计费；A2A-03 的真 PG pending disclosure boundary、双 Owner 浏览器 create/invite/approve/revoke 与 canonical tenant audit receipt；HN-05 的真 PG grant upsert/revoke 与两条 tenant audit 同事务链；UI-02 的真 PG GuardPolicy first-write/row-lock 冲突与 tenant audit 原子性、FeatureFlag strict platform audit→mutation→request commit→Redis invalidation 次序、org_admin/platform_admin 浏览器角色边界及 raw policy 零渲染；GV-05 的真 PG counter row-lock/额度边界竞争、真实 Redis sliding window 和真实 provider SSE usage；GV-06 的真 PG operator chain writer/query/verifier、滚动多实例 v1→v2 cutover 与生产 platform-admin 查询；GV-07 的真实 quota authority outage→WebSocket→durable transcript→reload；GV-08 的生产 `SECRETS_MASTER_KEY` inventory、channel/delivery/ingress v3 dry-run 与 operator-confirmed apply、真 PG plaintext-zero 查询、provider stream/断线重试、Channel replay 和 tenant offboarding；GV-09 的真 PG tenant/RLS 读写、真实浏览器 Action decisions 操作，以及由有权 operator 为两条无归属遗留 decision 明确 tenant 后执行的 hash-bound apply；HC-01 的真 PG Local Message/result/outbox 事务与真实设备 result→来源 session continuation/retry/reload；HC-03 的真 PG file policy precedence 与 canonical 设备 allow→deny→upload/download；UI-09 的生产 `system_settings` legacy Hook override retirement receipt、strict audit chain 与真实浏览器员工设置/概览验收；MCP `row.config["api_key"]` 明文 vs 加密；enterprise.py 32 端点逐一租户作用域；审批无人值守恢复全程；desktop_* 仓外真实消费者；canonical Hive Connect 的真实机器安装/自启/presence；FreeCode 运行期行为；最近一次 CI run 状态；多实例 Railway 部署拓扑。

### 14.3 残余风险

git metadata 已从远端 main 精确恢复且现可逐项 diff/commit；恢复前无法建立的历史变更归属仍不能倒推。原全量测试的 4 个已知失败中 3 个 git-metadata 条件已经消失，Feishu 硬编码路径也已定向转绿；最终仍必须重跑全量以证明没有新的失败。594 个 Docker-off skip 可能藏着只有真 PG 才暴露的缺陷。前端浏览器行为、生产 DB/keyring/backfill、Railway、多进程与真实 Hive Connect 设备态均未经实测。上述缺口意味着本报告可以给出 NO-GO 与修复清单，但不能给出 GO。

### 14.4 证据边界声明

HC-03 已有 bridge token scope→单一 live policy resolver→workspace I/O 的 current-source、红绿回归和 Docker-on policy flip 合同；真 PG 与 canonical 真实设备文件旅程仍未执行，因此只标代码闭环，不标生产闭环。

不再使用“约 90%”这类不可机械复算的单值置信度。当前证据分层如下：原始六个 P0 的代码事实与新增 GV-08 有 current-source 证据，且本轮均已形成代码闭环；其中 GV-01/GV-02 已有 source diff、typed receipts 与不替换执行 seam 的组合回归，但真 PG worker 验收仍缺。GV-03 已有 prompt→resolver→preflight→Approval/registry、revision/history/rollback/audit 与业务 UI 的 current-source 和本地可执行证据，真 PG 并发验收仍缺；SA-05 已有真实 REST→PlanModeGate→lease port→launch 的 current-source/可执行证据，真 PG lease 验收仍缺；GV-04 已有公共身份 dependency→active target/RLS pin→独立 operator commit 的 current-source 与红绿回归，历史事件不可回填；GV-05 已有 live router→quota/rate→provider→strict meter/counters 的 current-source、红绿回归与断流恢复证据，真 PG/Redis/provider acceptance 仍缺；GV-06 已有 strict writer→v2 chain/legacy anchor→platform-admin query/verify 与 Desktop client-asserted 分层的 current-source、红绿回归及既有不可变迁移合同，真 PG/滚动多实例 acceptance 仍缺；GV-07 已有 invoker admission→typed terminal/event→durable message part/web terminal→前端状态的 current-source 与红绿回归，生产 quota authority outage/reload 验收仍缺；GV-08 已有 tenant credential inventory→Tool/Model/Web/Session/Channel/T0 exact ingress/egress→typed encrypted transport store→v3 dry-run/apply 的 current-source、红绿回归和 value-free receipt，但真 PG、生产 keyring/inventory/backfill/provider acceptance 仍缺；GV-09 已有 ToolRuntime SQL write→request-scoped feedback transaction→typed session API→AgentChat query→员工可读 UI 的 current-source、红绿回归与真实遗留 dry-run receipt，真 PG、浏览器及经 operator 确认的 apply 仍缺；A2A-03 已有 callable/management source separation、tenant/manage authority、目标 Owner/带理由 admin 状态机、同事务 tenant audit 与六操作业务 UI 的 current-source/红绿回归，管理 payload 不进入 runtime 且 raw key 不渲染，但真 PG 和双 Owner 浏览器/审计 receipt 仍缺；UI-02 已有 canonical AI asset consumer 反证、GuardPolicy expected-version/lock/validation/tenant audit→Control Plane 业务投影、global FeatureFlag platform authority/strict audit/after-commit invalidation→typed Admin UI 的 current-source 与红绿回归，raw machine policy 不进入员工/Owner 页面，但真 PG/Redis/浏览器仍未操作；UI-03 已有唯一 AST inventory、exact dynamic/wrapper rule table、双语 catalog、CI gate 与纯 staged-tree 全量测试/build 证据，八项机械欠账均为 0，但真实中英文浏览器仍未操作；HN-05 已有 service authority→同事务 tenant AuditLog→外围 commit 的 current-source、红绿回归与 Docker-on 查询合同，历史无事实不伪造回填，但真 PG 仍未执行；HC-01 已有入队 authority→Local Message→result/outbox 同事务→parent continuation/`message_id` status 的 current-source、红绿回归与 Docker-on PG 测试定义，真 PG/真实设备唤醒仍缺；HC-02/HC-04 已被 canonical source 反证；UI-01 已被当前可执行测试合同重分类；UI-09 已有旧生产截图、修复前 current-source/错误契约、修复后 employee-safe health→overview、platform-only raw route→tenant/agent filter→plugin-only mutation/strict audit、startup allowlist→recoverable retirement 的红绿证据，但生产数据与真实浏览器仍未操作。测试、迁移头与包级 Go tests 有命令 receipt。Goal 1 行为质量、生产 DB、浏览器端到端、多进程、Railway 和真实设备态仍未证实。发布裁决必须消费 §11 中与本次变更相关的全部 receipt，不能只取前三项或把 full-suite 计数当替代品。

---

## 15. Current-source 复核修订记录

| 修订 | 原报告 | 当前裁决 | 复核证据 |
|---|---|---|---|
| C-01 | Model Agency 零违规，GV-08 仅观察 | current-source 复核确认原实现是 live P0 违规；本轮已完成 exact-authority 代码闭环，真环境验收待补 | `privacy_layer.py` pattern 只记候选；`credential_boundary_loader.py` 建可信 inventory；`execution_pipeline.py` 在 pre-effect seam 阻断 exact bytes；两组回归 `330 passed, 10 skipped` / `407 passed, 57 skipped` |
| C-02 | HC-02 canonical runner 不 ping | 撤销 | `hive-connect@0.1.9` 的 `platform/hive/hive.go:31,387-392,489-500`；Hive adapter tests 通过 |
| C-03 | HC-04 daemon install 未实现 | 撤销 | `cmd/cc-connect/daemon.go:16-105`、`daemon/launchd.go:42-78`；daemon/CLI tests 通过 |
| C-04 | UI-01 Office 专用面缺失是 P1 产品断点 | 重分类为 DOC-01 且已闭环 | 两个前端测试合同显式要求退役该 tab/section；AGENTS.md 已改为 OfficeCLI preview/ArtifactSurface |
| C-05 | i18n 精确缺失数为 349 | 原数字撤销；UI-03 已代码闭环 | 新 AST staged baseline 为 285 missingBoth/21 单边/2 重复 path/143 中文默认；修复后 215 files、3461/3461 keys、八项 gate=0，Node `9 passed`、Frontend `128 files / 774 passed` |
| C-06 | 修复六个 P0 后可进入 GO | 当前仍为 NO-GO / Acceptance incomplete；P0 代码修复已完成，但其余断点与真环境 acceptance debt 仍在 | Goal 1、PG/Redis/keyring/backfill/provider、多进程、浏览器、Railway、HC-01 来源唤醒与真实设备验收缺口 |
| C-07 | 员工用户自定义 hook 无注册面是 P2 缺失 | 撤销该员工产品缺失；UI-09 边界断点已代码修复，生产 retirement/浏览器验收待补 | 员工卡与 raw browser adapter 已删除；`/runtime-health` 只给业务健康；`/admin/.../runtime-hooks` 仅 platform role 且按 Agent/tenant 过滤；内置不可变、plugin-only mutation+strong audit、可恢复 retirement 与并发写保护；`112 passed` / `152 passed` |
| C-08 | A2A 后端已闭环，仅五个前端 API 无消费者 | 原因修正并代码闭环：runtime callable projection 隐藏 pending 是正确的，但缺少独立 human management projection 才是目标 Owner 无法审批的首个断点；随后才是前端零消费 | manager-only `/a2a/management`、tenant-bounded candidate search、目标 Agent path/Owner-admin 状态机、过期/群主不变式、同事务 audit、AgentA2ASection 六操作；`17 passed, 1 skipped` / `143 passed` |

### 附：审查过程声明

原始报告由 Kimi 的并行只读源码审计、主审复核与实跑验证合成。Codex current-source 复核发现原报告并非所有关键断点都完成了正确 source-boundary 与 product-boundary 反证，因此先在正文、矩阵、登记册、落地方向和验收边界中同步修订并保留撤销项追溯；随后进入逐项修复。每个修复部分都必须在本报告 §0 与对应 finding 中记录源码、测试、迁移/清理和残余验收边界，再独立提交；不得把后续尚未完成项混入已完成状态。
