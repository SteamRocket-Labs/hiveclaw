# Hive 原子化架构独立审查报告

> 审查对象：当前仓库 main@596dab169fe44ade1d6e86cb5628fb45bc02aedf
> 报告标识：由当前 Git commit 自动生成，不依赖固定日期或历史轮次
> 审查性质：全新、独立、源码与生产证据优先；未继承任何旧报告结论
> 变更边界：只新增本报告；未修改业务代码、数据库、部署配置或既有文档

## 1. 执行摘要

结论：当前 checkout 已经具备一个相当完整的云端 Agent 基础骨架，但不能判定为可直接上线闭环。原因不是“缺少代码量”，而是生产安全基线、恢复隔离、外部副作用幂等、多 Agent 产物消费和前端终态权威仍存在原子断裂。

本次审查确认四个 P0：

1. 生产数据库仍运行旧租户空值语义：11 张表存在 tenant_id 为空的数据，2301 条无法从现有关系推导租户的残留记录仍被旧 RLS 的 OR tenant_id IS NULL 放行；这是线上只读审计实证，不是静态推断。
2. 当前源码的 post-compaction 恢复路径会把 Session A 的私有文件路径、full_access 权限档案和未决 send_email 工具帧注入 Session B；临时目录复现三项结果均为 True。
3. Personal KB URL 导入和 core web_fetch 等出站入口缺少最终 IP、逐跳重定向与响应大小控制，普通用户或模型可让云端访问 loopback、RFC1918、link-local、云元数据等内网地址。
4. AgentTool 配置存在绕过加密的直接 writer，并且缺 config_schema 时 API 的 mask 逻辑不能识别 api_key，可将长期明文密钥写入 JSON 并回显。

当前源码也有明确的闭环基础：

- 用户消息先与 RuntimeTask 在 API 事务中落库，提交后才唤醒 Worker。
- Web chat 的 claim、lease、claim_version fence、唯一 active run 和终态 finalizer 已成型。
- ChatTranscriptEvent 是云端 session 顺序与 replay 的数据库事实源，并投影 T0。
- 标准单 Agent artifact 已有 ChatArtifact、快照、workspace authority、content/download API 和 Workspace 消费链。
- Personal KB 遵循按需 search/read 工具边界，不默认注入原始上下文。
- T3 Platform Gate、已接入的五类 AI Asset、标准 workflow/approval 执行路径有真实消费和测试证据。
- 当前 checkout 全量验证为：后端 6706 passed, 1 skipped；前端 663 passed；前端生产 build 与 bundle budget 通过；ruff check app 通过。

但测试全绿不等于生产闭环。现有测试恰好没有覆盖本报告发现的多会话压缩、真实 Worker tenant ContextVar、外部副作用提交后超时、AgentTool 明文 writer、private-IP/redirect SSRF、A2A artifact 真实读取、取消请求失败和分布式 Personal KB spool 等路径。

最终判定：

| 领域 | 结论 | 分项置信度 |
|---|---|---:|
| 单 Agent 核心架构 | 局部闭环 | 88% |
| Hive Native | 局部闭环；Company KB 为已知缺失 | 80% |
| 企业治理、安全与 AI 资产 | 断点；存在生产 P0 | 92% |
| 用户真实体验与 UI/UX | 局部闭环 | 70% |
| 整体 | 不满足上线门槛 | 84% |

## 2. 审查范围与未覆盖范围

### 2.1 已覆盖

- 当前 Git checkout、worktree、代码索引与项目结构。
- backend/app、backend/alembic、backend/tests、frontend/src、frontend/e2e、Railway/Nginx/entrypoint 配置。
- Session、RuntimeTask、Transcript、Tool、Approval、Workflow、Sub-agent、Agent Team、A2A、Trigger、Memory、Knowledge、Skill/Evolution、Artifact、Workspace、AI Asset、RLS、审计和 UI 消费。
- Railway production 三个服务的最新 deployment、rootDirectory、volume、replica 和上传提交标识。
- 生产数据库 Alembic/RLS/tenant-null 只读审计。
- FreeCode TS baseline 7dc15d6c8fb0c40c7fcc02ce9b58204324252632 的 QueryEngine、transcript、permission、compaction、abort、resume 源码对照。
- 本地真实浏览器可达性；确认页面停在 /login?next=%2F。

### 2.2 未覆盖或受限

- 未使用生产用户凭据，因此未对生产 AgentDetail 做登录后的真实点击、下载和多 Agent 交互；本地数据库没有可用用户，为遵守只读边界，没有创建临时账号。UI 结论来自真实 React/API 消费路径、Vitest、Playwright 源码和未认证浏览器可达性，故 UI 分项置信度最低。
- 未对真实外部邮件、Feishu、MCP、Firecrawl、Tavily、Vercel Sandbox 做会产生副作用或费用的线上调用。
- 当前生产每个服务只有 1 replica，无法用现网行为证明多副本一致性；只验证了源码中的 lease/fence 与 PostgreSQL 测试。
- 未修改或 dry-run apply 生产数据；tenant backfill 仅执行默认 dry-run。
- 生产 SSH 一次遇到 Railway key verification temporary unavailable，重试成功；不影响最终 live evidence。
- 唯一 skipped 测试是 app/templates/system_skills/dingtalk-integration/SKILL.md 没有声明工具，被测试按 MCP-dynamic/pure guide 跳过。

## 3. 证据方法与置信度

### 3.1 证据顺序

1. 当前源码真实调用链。
2. 当前 schema、Alembic、约束和 RLS。
3. API、Worker、queue/lease、文件与数据库运行路径。
4. 前端从 API/Transcript 到实际组件的消费路径。
5. 自动化测试与故障路径。
6. 当前 Railway 部署和 live read-only 审计。
7. 注释与文档仅作为线索，不作为完成证据。

没有使用既有审查报告来继承结论。当前 checkout 的 codebase graph 为 ready，包含 44,407 nodes / 212,658 edges；代码发现优先使用图索引，字符串、配置、脚本与负向消费者检查使用 rg。

### 3.2 置信度公式

整体置信度按以下权重机械计算，而非主观打分：

| 因子 | 权重 | 本次得分 | 加权 |
|---|---:|---:|---:|
| 生产路径覆盖 | 20% | 88 | 17.60 |
| 七原子覆盖 | 15% | 84 | 12.60 |
| 源码/调用链证据 | 15% | 94 | 14.10 |
| DB、migration、RLS | 15% | 96 | 14.40 |
| UI 最终消费 | 10% | 70 | 7.00 |
| 失败/恢复路径 | 10% | 82 | 8.20 |
| 自动化验收 | 10% | 96 | 9.60 |
| 不可访问范围折损 | 5% | 10 | 0.50 |
| 合计 | 100% |  | 84.00 |

84% 表示本报告对已列结论具有较高可复核性，不表示系统完成度为 84%。

## 4. 当前事实快照

### 4.1 Git 与验证

- branch：main
- HEAD：596dab169fe44ade1d6e86cb5628fb45bc02aedf
- 用户原有 dirty state：.ultra/debug/subagent-log.jsonl、.ultra/sessions/orphan-trail.md、task.md；审查未覆盖或清理这些文件。
- 后端全量：

~~~text
cd backend
source .venv/bin/activate
pytest tests -q
# 6706 passed, 1 skipped in 208.66s
~~~

- 代表性七原子回归：

~~~text
pytest tests/services/test_runtime_task_claim_service.py \
  tests/services/test_runtime_task_worker.py \
  tests/agents/test_orchestrator.py \
  tests/test_database_tenant_scoped_session.py \
  tests/architecture/test_tenant_null_semantics.py \
  tests/architecture/test_rls_complete_coverage.py \
  tests/migrations/test_tenant_null_semantics_migration.py \
  tests/migrations/test_rls_complete_coverage_migration.py \
  tests/services/test_chat_artifact_delivery.py \
  tests/services/test_chat_transcript.py \
  tests/services/test_session_command_runtime.py \
  tests/services/test_team_memory_service.py -q
# 179 passed in 16.68s
~~~

- 前端：

~~~text
cd frontend
npm test -- --run
# 115 files passed, 663 tests passed

npm run build
# exit 0; AgentDetail 287168/380000 bytes; vendor 591449/620000 bytes
~~~

- 静态：

~~~text
cd backend
source .venv/bin/activate
ruff check app
# All checks passed
~~~

### 4.2 Railway production

| 服务 | deployment | 状态 | 创建时间 | 上传标识 | replica | volume |
|---|---|---|---|---|---:|---|
| backend | ec2507c0-c9ac-46d7-84e9-706af97344cb | SUCCESS | 2026-07-09T13:48:03Z | deploy 28b96cbc runtime budget migration recovery backend | 1 | /data/agents |
| backend-api | e4070381-c40e-4163-8cd0-355308921ce8 | SUCCESS | 2026-07-09T13:48:06Z | deploy 28b96cbc runtime budget migration recovery backend-api | 1 | 无 |
| frontend | 91b40cce-b01b-494f-9815-b3cd68ecc7e5 | SUCCESS | 2026-07-09T13:48:09Z | deploy 28b96cbc runtime budget migration recovery frontend | 1 | 无 |

production 运行的是 28b96cbc，而非本报告审查的 596dab1。两者相差 1218 个文件和约 13.3 万新增行；当前 checkout 的 tenant_null_semantics_0712、workspace manifest、approval execution、runtime fencing 等不能当成已上线能力。

### 4.3 Live RLS/tenant-null 证据

railway ssh 上执行 app.scripts.audit_rls_coverage：

- app role 不拥有 agents 表。
- UNPROTECTED=0，INERT=0，ENFORCED=98。

这证明 RLS 在当前 app_rls 角色上确实执行，但不证明谓词安全。旧策略仍包含 OR tenant_id IS NULL。

railway ssh 上执行 app.scripts.backfill_stage2b_tenant_id 默认 dry-run：

| 表 | null_before | 可回填 | 无法归属 |
|---|---:|---:|---:|
| agent_permissions | 13 | 13 | 0 |
| agent_plan_requests | 41 | 41 | 0 |
| agent_tools | 1974 | 1974 | 0 |
| agent_triggers | 31 | 31 | 0 |
| approval_requests | 2 | 2 | 0 |
| audit_logs | 1809 | 4 | 1805 |
| channel_configs | 5 | 5 | 0 |
| chat_messages | 310 | 310 | 0 |
| chat_sessions | 170 | 170 | 0 |
| runtime_tasks | 543 | 47 | 496 |
| 合计 | 4898 | 2597 | 2301 |

脚本自身输出的警告为：2301 rows keep tenant_id NULL；旧 policy 的 OR tenant_id IS NULL leaves these globally visible。

当前 production Alembic version 的只读查询结果为 external_capability_strict_rls_0709；当前 checkout 单一 head 为 hr_draft_recovery_0712，中间包含 rls_complete_coverage_0712 与 tenant_null_semantics_0712。

## 5. 系统架构地图

~~~mermaid
flowchart LR
  U["Web / Channel User"] --> F["React 19 / Nginx"]
  F -->|control plane routes| A["backend-api<br/>no volume"]
  F -->|heavy/file routes| R["backend runtime<br/>/data/agents"]
  F -->|WebSocket| A
  A --> PG[("PostgreSQL + RLS")]
  R --> PG
  A --> REDIS[("Redis live wakeup")]
  R --> REDIS
  R --> W["RuntimeTask Worker"]
  W --> K["AgentKernel / TurnOrchestrator"]
  K --> L["LLM providers"]
  K --> T["ToolRuntimeService"]
  T --> G["Governance / Approval / Preflight"]
  G --> X["External providers / Sandbox / MCP"]
  T --> VOL[("/data/agents<br/>workspace, memory, snapshots")]
  K --> E["ChatTranscriptEvent / InvocationSpan"]
  E --> PG
  VOL --> C["ChatArtifact + WorkspaceResourceManifest"]
  C --> PG
  PG --> P["ThreadItem projection / REST replay"]
  P --> F
~~~

部署路由的当前事实：

- Nginx 把 auth、users、tenant、agent list/detail、session list、session runs 转发 backend-api。
- 未显式分类的 /api 路径，包括 files/artifacts、Personal KB、session permission resolve，回落到有卷 backend。
- /ws 转发 backend-api。
- 因此不能仅从 main.py 的 API allowlist 推断某条路由实际进入无卷服务；本报告按 Nginx 最终匹配判定。

## 6. 核心实体与事实源矩阵

| 概念 | 机械事实源 | 唯一/主要写入口 | 消费者 | 可修正者 | UI 来源 | 判定 |
|---|---|---|---|---|---|---|
| tenant/user/agent | PostgreSQL models + RLS | API/service transactions | runtime、tools、UI | 管理 API/operator | REST | 局部闭环；production NULL 语义 P0 |
| thread/session | ChatSession | session API/channel ingress | runtime、lineage、UI | session commands | session list/detail | 局部闭环 |
| run | RuntimeTask | start_web_chat_run、workflow/subagent/trigger admission | Worker、control plane、UI | fenced finalizer/reconciliation | active-run + transcript | 局部闭环 |
| turn/event | ChatTranscriptEvent(session_id, sequence) | append_session_event | replay、T0、ThreadItem、UI | rewind/branch projection | transcript endpoint | 闭环 |
| model/tool span | InvocationSpan | record_invocation_span | operator、usage/debug | append-only intent | operator view | 局部闭环；audit immutability gap |
| tool call | Transcript parts + span | ToolRuntimeService pipeline | model loop、UI | approval/reconciliation | ThreadItem | 局部闭环；idempotency/recovery gaps |
| approval | ApprovalRequest / session permission event | approval ticket or session resolve | Worker/runtime/UI | approver/operator | approval card | 局部闭环 |
| workflow/task | RuntimeTask + workflow steps/leaves / Task | workflow/task services | Worker、UI、ledger | typed state machine | workflow/task views | 局部闭环 |
| memory T0 | ChatTranscriptEvent transactional truth + T0 projection | transcript projector | T2/T3 curation | repair/reprojection | memory/operator | 闭环主路径 |
| memory T2/T3 | governed Markdown vault | Memory Gate candidate + Platform Gate | dynamic activation, skill evidence | governed rollback | memory UI | 局部闭环 |
| Personal KB | KnowledgeDocument/Segment/Grant + canonical Markdown | PersonalKnowledgeService | search/read tool | owner/API | Personal KB UI | 局部闭环 |
| Company KB | 无生产实体/工具链 | 无 | 无 | 无 | legacy export only | 已知缺失 |
| Shared Team Memory | shared_memory tenant/workspace Markdown | TeamMemoryStore/API | 仅人类 UI | tenant user/admin | TeamMemorySummaryCard | 断点 |
| standard artifact | ChatArtifact snapshot + workspace content | create_chat_artifacts_for_message | files API、Workspace | authority service | ArtifactSurface | 闭环 |
| A2A artifact | child ChatArtifact + parent transcript projection | orchestrator | parent UI/Workspace | 无完整入口 | 当前丢失/404 | 断点 |
| workspace | 文件内容 + WorkspaceResourceManifest authority | governed file/office/upload paths | tools、artifact、UI | owner/operator | Workspace | 局部闭环 |
| AI Asset | native runtime authority + AIAssetRecord control index | adapters/native writer | runtime usage + admin UI | revision/rollback | enterprise UI | 局部闭环，仅五类 |

源码锚点：

- ChatSession ownership/lineage：backend/app/models/chat_session.py:13-65。
- RuntimeTask state/claim/idempotency：backend/app/models/runtime_task.py:35-163。
- Transcript sequence/causation/projection：backend/app/models/chat_transcript_event.py:18-95。
- InvocationSpan canonical operator surface：backend/app/models/invocation_span.py:15-68。
- ChatArtifact：backend/app/models/chat_artifact.py:13-61。
- WorkspaceResourceManifest：backend/app/models/workspace_resource.py:15-52。

## 7. 核心状态机

### 7.1 Agent run

~~~mermaid
stateDiagram-v2
  [*] --> pending: accepted prompt committed
  pending --> running: worker claim + claim_version
  running --> running: lease renew / tool rounds
  running --> suspended: workflow wait/gate
  suspended --> resumable: signal/approval
  resumable --> running: fenced reclaim
  running --> completed: unique finalizer
  running --> failed
  running --> killed: cancel
  running --> needs_reconciliation: uncertain side effect
  pending --> skipped: policy/budget reject
~~~

数据库允许 pending、running、completed、failed、killed、skipped、needs_reconciliation、resumable、suspended。主 web-chat 路径符合此状态机；workflow/delegation/subagent/trigger 的 wrapper 异常路径没有可靠推进终态，见 RUNTIME-002。

### 7.2 Tool / approval

~~~mermaid
stateDiagram-v2
  [*] --> validated
  validated --> governed
  governed --> denied
  governed --> waiting_approval
  governed --> executing
  waiting_approval --> executing: allow
  waiting_approval --> denied: deny/expire
  executing --> succeeded
  executing --> failed_retryable
  executing --> needs_reconciliation: timeout/unknown
~~~

代码当前把 timeout 渲染为 retryable，但没有 ToolExecutionReceipt；session permission 又先持久化 decision，再执行，失败后的第二次 resolve 被 409 拒绝。图中的 failed_retryable/needs_reconciliation 因此不是所有工具路径都真实可达。

### 7.3 Artifact

~~~mermaid
stateDiagram-v2
  [*] --> file_written
  file_written --> authority_registered
  authority_registered --> snapshot_created
  snapshot_created --> artifact_row
  artifact_row --> transcript_part
  transcript_part --> workspace_visible
  workspace_visible --> opened_or_downloaded
  file_written --> quarantined: authority unknown
~~~

标准单 Agent 路径可达 opened_or_downloaded。A2A path 在 snapshot_created 后只生成候选 UUID 而没有 artifact_row，前端又在 transcript_part 转换时丢弃 artifact parts。

### 7.4 Branch/rewind

准备 revision/active-run fence → workspace snapshot/stage → DB projection mutation → control event → commit workspace swap；任何中间失败走 rollback journal。后端状态机闭环，前端 lineage API 失败却把 unknown 转成空数组并隐藏 UI。

## 8. 七原子总矩阵

| 能力 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 | 状态 |
|---|---|---|---|---|---|---|---|---|
| accepted prompt / web run | 成立 | 成立 | 成立 | 成立 | 成立 | 成立 | 成立 | 闭环 |
| transcript/replay/T0 | 成立 | 成立 | 成立 | 成立 | 成立 | 成立 | 成立 | 闭环 |
| claim/lease/fence | 成立 | 成立 | 主路径成立 | 成立 | web-chat 成立 | 成立 | 成立 | 局部闭环 |
| context assembly | 成立 | 恢复清单越界 | 成立 | 双事实源 | 断裂 | 模型真实消费错误内容 | 缺跨会话测试 | 断点 |
| tool single entry | 成立 | 成立 | 成立 | 部分 | 幂等/unknown 断裂 | 成立 | 只验 metadata | 局部闭环 |
| standard artifact | 成立 | 成立 | 成立 | 成立 | 快照成立 | 成立 | 成立 | 闭环 |
| A2A/subagent artifact | 成立 | 伪 ID | 部分 | parent event 有 | 无 | UI 丢失 | 无 E2E | 断点 |
| branch/rewind | 成立 | 成立 | 成立 | 成立 | 成立 | UI error-as-empty | 后端强、浏览器弱 | 局部闭环 |
| Personal KB | 成立 | 成立 | 主路径成立 | 成立 | 分布式 spool 断裂 | 工具按需消费 | 单机强 | 局部闭环 |
| Company KB | 无 | 无 | 无 | 无 | 无 | 无 | 无 | 已知缺失 |
| Shared Team Memory | 成立 | 成立 | CRUD 成立 | 成立 | revision 成立 | Agent 不消费 | 仅 CRUD | 断点 |
| Memory T0→T2→T3 | 成立 | Platform Gate | 成立 | source_refs | replay/rollback | activation/skill evidence | 较强 | 局部闭环 |
| Skill/evolution | 成立 | owner/policy | 成立 | eval/revision | rollback | Agent 真实加载 | 较强 | 局部闭环 |
| workflow/plan/task | 成立 | 多实体并存 | 成立 | DB/ledger | wrapper 异常缺口 | Agent/UI | 较强 | 局部闭环 |
| RLS | API/Worker | current source 严格 | 成立 | policy/audit | migration quarantine | 全系统 | migration tests | production 断点 |
| AI Asset | 五类成立 | native authority | 五类成立 | usage event | rollback/reconcile | 五类成立 | 14 tests | 局部闭环 |
| UI run state | 成立 | 本地可写镜像 | 取消先乐观终态 | DB 与 UI 漂移 | 失败无恢复 | 用户直接消费 | 缺 reject 测试 | 断点 |

## 9. 四域结论

### 9.1 单 Agent 核心架构

判定：局部闭环。

闭环部分：

- web-chat accepted-prompt-first：backend/app/services/web_chat_runtime.py:1729-1943。
- SKIP LOCKED claim、lease、claim_version：backend/app/services/runtime_task_claim_service.py:159-212。
- fenced renewal/cancel：backend/app/services/runtime_task_fence.py。
- transcript 先持久化后广播、终态 finalizer：backend/app/services/web_chat_run_orchestrator.py。
- standard artifact、workspace rewind journal、provider fallback、empty response、人类可读错误均有源码与测试。

主要断点：

- post-compaction 恢复越过 session match。
- ToolMeta 的 retry/idempotency 只停留在声明。
- worker wrapper 的异常不总能 terminalize/reclaim。
- loop guard 漏记 usage。
- UI 取消可以先于后端权威宣告终态。

与 FreeCode 7dc15d6 对照：

- FreeCode QueryEngine.ts:436-455 在进入模型循环前同步记录用户 transcript，Hive 的 accepted-prompt-first 已达到同等级语义并适配 DB/Worker。
- FreeCode QueryEngine.ts:243-270 将 permission decision 包裹在 tool loop，Hive 增加企业治理，但 session permission 的 execution attempt/retry 状态不如其单进程语义一致。
- FreeCode QueryEngine.ts:687-731 与 conversationRecovery.ts:416-520 明确 transcript chain/compact boundary/resume；Hive 的 DB replay 更适合云端，但 agent 级 recovery_manifest 破坏 session 隔离。
- FreeCode QueryEngine.ts:1158-1160 的 abort authority 单一；Hive 后端取消权威存在，但 React 另写了本地终态。
- 服务商私有远程执行不计入本报告 parity 债务。

### 9.2 Hive Native

判定：局部闭环。

- T0/Transcript、T2/T3 Platform Gate、dynamic memory activation、Personal KB search/read、Skill progressive disclosure、workflow、Plan、Task、subagent/team/A2A 都有真实代码入口，不是空壳。
- Company KB 明确为已知缺失；generic scope schema 和 legacy company files 不能替代 organization ACL、connector、citation 与 Agent tool。
- Shared Team Memory 是真实 CRUD，但没有任何 Agent runtime/tool 消费者，UI 的“跨会话和压缩恢复复用”承诺不成立。
- 多 Agent 的执行与父会话 notification 已存在，但 tenant resume、artifact authority 和父 UI 消费未闭环。

### 9.3 企业治理、安全与 AI 资产

判定：断点。

- 当前源码 tenant-null migration 的固定点回填、隔离租户、NOT NULL、strict RLS 设计正确；生产尚未运行。
- app_rls 确实是 non-owner、non-bypass role；但 production 旧 predicate 把 NULL 当全局。
- BYPASS 入口只有日志，没有 durable AuditLog；app_rls 又被授予 audit_logs UPDATE/DELETE。
- Outbound egress 只有零散 transport/userinfo 检查，没有统一最终 endpoint policy。
- AI Asset 五类是真实局部闭环，不能外推为 Prompt、Tool、Connector、Model config、Memory policy、Knowledge source、Eval、Template、Artifact 全资产闭环。

唯一治理决策顺序应收敛为：

身份/tenant 解析 → RLS session pin → capability policy → Plan/approval gate → immutable decision/receipt → ToolRuntimeService → side-effect evidence → artifact/resource authority → Transcript/UI projection。

当前旁路/冲突：

- worker delegation 在 tenant resolver 后丢失 ContextVar。
- session permission decision 与 execution attempt 混为一个终态。
- trigger immediate path忽略 lease。
- tool idempotency policy未进入 handler/provider。

### 9.4 用户体验与 UI/UX

判定：局部闭环。

正面：

- ThreadItem 有 user/operator audience；operator_details 已 gate。
- Workspace Documents 对标准 message.artifacts 做 path 去重与 revision 分组。
- 有窄屏 overlay/bottom-sheet、reduced-motion、forced-colors 与 bundle budget。
- Plan、Workflow、Sub-agent、approval、error、cancel 等不是纯文本猜测，而有显式类型。

断点：

- Stop 请求失败时 UI 仍宣告 cancelled。
- 上传 Cancel 无 abort，部分成功被 Promise.all 隐藏。
- tool_failure 显示 done。
- session/lineage API failure 被伪装为空状态。
- subagent artifact parts 在 canonical ThreadItem 转换时丢失。
- AgentDetail 与 AgentChatSection 仍是巨型、跨职责可写状态集合。
- 普通用户看到 raw transcript sequence；resize separator 无键盘/触摸契约。

## 10. 代码极简性结论

### 10.1 应保留

- RuntimeTask 作为云端执行权威，Task 作为业务任务，可保留概念区分。
- ChatTranscriptEvent 作为事务事件事实源、T0 作为 portable evidence projection，可保留。
- Workspace 文件内容与 WorkspaceResourceManifest 权威元数据是内容/授权分工，不是错误双源。
- Native AI asset 与 AIAssetRecord 控制 index 的 authority 分工合理。

### 10.2 应合并

- recovery_manifest 的初始 hydrate 与 post-compaction restore 必须共用 session-scoped loader。
- PermissionDecision 与 PermissionExecutionAttempt 分离，但所有 execution 统一进入 approval execution worker/receipt。
- 所有 MCP generic/dynamic/resource 路径统一 resolve_agent_mcp_execution_config。
- 所有出站 URL 统一 OutboundEndpointPolicy。
- 所有 upload 入口统一 UploadBatchController。
- 前端 runtime phase 统一 reducer/store，从 Transcript/active-run 派生，不再多组 state/ref 双写。

### 10.3 应删除或迁移

- agent_work_ledgers 表当前没有任何生产读写者；真实 Work Ledger 位于 runtime_artifacts 的文件服务。先审计 live row，再迁移/归档/drop。
- web_chat_stream_bus 的 Redis XADD/INCR 没有 XREAD/XRANGE 消费者且无 TTL；若 DB Transcript 是 durable truth，应删除 stream 持久镜像，只保留 Pub/Sub live hint。
- skill_distiller.py 的 _cursor_value 仅定义无调用，可删除。
- 若 Shared Team Memory 不计划给 Agent 使用，应改名 Shared Workspace Notes 并删除 Agent-memory 文案。
- RuntimeTask 模型 docstring 仍称只用于 subagent，已与多类型事实漂移，应修正，但这是文档清理而非核心断点。

## 11. 断点总览

| ID | 级别 | 状态 | 根因摘要 |
|---|---|---|---|
| SEC-001 | P0 | 断点 | 生产旧 RLS 对 tenant NULL 放行，2301 条无法归属记录全局可见 |
| SEC-002 | P0 | 断点 | 每 Agent 单 recovery 文件且 post-compact 未匹配 session |
| SEC-003 | P0 | 断点 | 无统一最终 endpoint/redirect/DNS 出站策略 |
| SEC-004 | P0 | 断点 | AgentTool config writer 绕过加密与保守 mask |
| GOV-001 | P1 | 局部闭环 | BYPASS 无 durable audit，audit_logs 可被 app_rls 更新删除 |
| RUNTIME-001 | P1 | 断点 | delegation worker 在二次 DB 读取前丢失 tenant ContextVar |
| RUNTIME-002 | P1 | 断点 | 部分 RuntimeTask wrapper 吞异常且类型不可 lease reclaim |
| TOOL-001 | P1 | 断点 | retry/idempotency metadata 未形成 receipt/provider key |
| TOOL-002 | P1 | 断点 | unsafe recovered frame 的 reconciliation 只留内存且模型继续 |
| APPROVAL-001 | P1 | 断点 | decision 先终结，execution 失败声称 retryable 但重试 409 |
| TRIGGER-001 | P1 | 断点 | immediate fire 忽略 lease=False |
| ARTIFACT-001 | P1 | 断点 | A2A 生成无 DB 行的 UUID，前端再丢弃 artifact parts |
| UX-001 | P1 | 断点 | React 本地终态先于后端取消权威，多组可写运行态镜像 |
| UX-002 | P1 | 断点 | upload abort 未绑定/不 settle，批次部分成功不可见 |
| DATA-001 | P1 | 断点 | tenant quarantine 有 receipt 无 operator restore/rebind 流程 |
| SESSION-001 | P1 | 断点 | 显式无效 WebSocket session 静默改投最近/新会话 |
| MEMORY-001 | P2 | 断点 | Shared Team Memory 没有 Agent consumer |
| KNOWLEDGE-001 | P2 | 已知缺失 | Company KB 无 organization lifecycle/tool |
| KNOWLEDGE-002 | P2 | 局部闭环 | Personal KB queued payload 依赖发起实例本地文件 |
| ASSET-001 | P2 | 局部闭环 | AI Asset 只覆盖五类 |
| MCP-001 | P2 | 断点 | generic MCP 忽略 AgentTool override |
| USAGE-001 | P2 | 断点 | loop guard terminal branch 漏记 token ledger |
| STREAM-001 | P2 | 局部闭环 | Redis stream 只写不读且无 TTL |
| UX-003 | P2 | 断点 | tool_failure 被映射为 done |
| UX-004 | P2 | 断点 | session/lineage error 被转为空数据 |
| A11Y-001 | P2 | 断点 | Workspace separator 无键盘/Pointer 路径 |
| SIMPLIFY-001 | P2 | 局部闭环 | DB work ledger 等无消费者抽象 |
| INFO-001 | P3 | 局部闭环 | 普通用户 UI 暴露 transcript sequence |

## 12. 全部断点详情

### [SEC-001] 生产 tenant NULL 记录被旧 RLS 当作全局数据

- 所属模块：Production PostgreSQL、RLS、Alembic、deployment entrypoint。
- 严重级别：P0。
- 当前状态：断点。
- 影响对象：所有 tenant、Agent、Session、RuntimeTask、审计和权限记录。
- 用户可见现象：多数情况下无直接提示；错误 tenant 可能查询/消费本应属于其他主体或无法归属的记录。
- 触发条件：app_rls 查询旧策略覆盖的 tenant_id IS NULL 行。
- 输入原子：历史写入或迁移遗留的 NULL tenant 行。
- 权威原子：旧 policy 将 NULL 误定义为跨 tenant 可见；当前源码则定义 tenant-owned 非空，生产与源码冲突。
- 执行原子：app_rls 非 owner 角色正常执行 RLS，但谓词本身放行 NULL。
- 证据原子：live dry-run 统计 4898 NULL，2597 可回填，2301 无法归属；旧策略文本包含 OR tenant_id IS NULL。
- 恢复原子：当前 checkout 可 quarantine residual，但生产未运行；entrypoint 对 alembic failure 仍继续启动。
- 消费原子：API、Worker、tool、audit 查询均可能消费这些行。
- 验收原子：当前 migration tests 通过，但没有 production cutover、post-migration live audit 和 quarantine operator closure。
- 断裂位置：生产 schema/RLS authority → tenant-scoped consumer。
- 根因：部署停留在 28b96cbc/external_capability_strict_rls_0709；历史 NULL 被当兼容全局语义；migration gate fail-open。
- 是否存在双事实源：是；596dab1 的 strict migration 与 production old schema。
- 是否存在治理冲突：是；RLS 机制生效，但规则违反 tenant ownership。
- 是否存在跨租户或安全风险：是，已证实 P0。
- 是否可能导致 Agent 无法继续运行：是；收紧策略后无法归属的 RuntimeTask/Session 会消失，未迁移前则可能错误恢复。
- 源码证据：backend/alembic/versions/tenant_null_semantics_0712.py:24-48,156-170,396-499；backend/entrypoint.sh:167-176,189-213。
- 数据库或迁移证据：production app role enforced=98；audit_logs orphan=1805、runtime_tasks orphan=496；production Alembic external_capability_strict_rls_0709。
- UI 消费证据：无专用风险提示或 quarantine UI；普通 UI 只消费 API 结果。
- 测试证据：backend/tests/migrations/test_tenant_null_semantics_migration.py 与 architecture RLS tests 在本轮 179-test 集合中通过。
- 反证或不确定性：没有证明 2301 条每一条都包含敏感 payload；但策略可见性和行数已确认，P0 不依赖 payload 抽样。
- 修复方案：先 production dry-run/备份；部署严格 migration；唯一 tenant authority 为 non-null tenant_id；冲突/无法推导行只进入隔离 tenant 并写 receipt；migration/schema check 失败时服务不得启动。
- 最小化方案：立即用 owner role 把 tenant-owned policy 改为 fail-closed 并暂停消费 NULL；随后按完整 migration 恢复可归属数据。该操作必须经过 dry-run 与明确生产确认。
- 需要删除或合并的旧实现：删除所有 tenant-owned OR tenant_id IS NULL policy 和 background legacy backfill convenience path。
- 依赖项：数据库备份、maintenance/cutover、current backend/backend-api/frontend 同版本部署、quarantine operator flow。
- 验收标准：production 所有 strict tenant 表 NULL=0；policy 不含 NULL bypass；app_rls 跨 tenant矩阵全拒绝；三服务同 commit SUCCESS。
- 建议测试：真实 PostgreSQL migration、legacy conflict、all-null table matrix、rolling version compatibility。
- 建议故障注入：migration 中断、backfill 冲突、API 先启动、Worker 旧版本运行、rollback 后重新升级。
- 预计风险：高；错误归属或直接删除会造成不可逆损失，必须可逆 quarantine，不得猜 tenant。

### [SEC-002] Agent 级恢复清单造成跨 Session 泄漏与覆盖

- 所属模块：Kernel compaction、RecoveryManifest、workspace runtime artifacts。
- 严重级别：P0。
- 当前状态：断点。
- 影响对象：共享同一 Agent 的不同用户/Session、权限档案、未决工具、私有路径。
- 用户可见现象：Session B 在压缩后可能提及 Session A 文件、工具或权限，并据此继续行动。
- 触发条件：同 Agent 存在 manifest A，另一 Session B 发生 mid-loop compaction。
- 输入原子：B 的当前上下文加 agent 级 recovery_manifest.json。
- 权威原子：manifest 声明 session_id，但 post-compact reader 不调用 recovery_manifest_matches_session。
- 执行原子：TurnOrchestrator 将 restoration text 直接插入 B 的 system message。
- 证据原子：文件路径只含 agent_id；A/B last-writer-wins；写入非 atomic。
- 恢复原子：恢复本身成为泄漏点，并发/崩溃还会覆盖或损坏清单。
- 消费原子：B 的 LLM 真实消费权限 profile、pending tool frame 和路径。
- 验收原子：现有 hydrate 跨会话拒绝测试未覆盖 _build_restoration_context。
- 断裂位置：session-scoped checkpoint authority → post-compaction model consumption。
- 根因：backend/app/kernel/engine.py 的第二读取入口绕过安全 loader；单文件路径设计不支持并行 Session。
- 是否存在双事实源：是；安全初始 hydrate 与不安全 post-compact restore。
- 是否存在治理冲突：是；A 的 permission profile 进入 B。
- 是否存在跨租户或安全风险：是；共享 Agent 的跨用户越权，tenant 信息也未进入 manifest key。
- 是否可能导致 Agent 无法继续运行：是；A/B 覆盖会丢失当前工具帧，损坏 JSON 返回 None。
- 源码证据：backend/app/kernel/engine.py:2909-2961；backend/app/kernel/turn_orchestrator.py:2459-2476；backend/app/runtime/recovery_manifest.py:19-27,251-307,376-409,561-612。
- 数据库或迁移证据：当前无 session recovery DB row、FK、CAS 或 claim fence。
- UI 消费证据：UI 无法识别 restoration 来源错误；只看到后续模型行为。
- 测试证据：临时目录复现 private_path_leaked=True、permission_leaked=True、tool_frame_leaked=True。
- 反证或不确定性：初始 hydrate 已正确拒绝 session mismatch，说明修复原语存在；漏洞集中在 post-compact 分支。
- 修复方案：唯一事实源改为 session-scoped checkpoint，键绑定 tenant/agent/session/run/claim_version；初始与 post-compact 共用一个 load+match+hydrate 入口；atomic write/CAS；legacy 无 session 文件不得恢复权限或工具帧。
- 最小化方案：立即在 _build_restoration_context 调用 recovery_manifest_matches_session，mismatch 时完全跳过 manifest；随后完成 session 分区迁移。
- 需要删除或合并的旧实现：删除第二套直接 load_recovery_manifest 分支和 agent 级可写 canonical path。
- 依赖项：SessionContext tenant/run identity、legacy manifest migration、fork/rewind semantics。
- 验收标准：A/B 并发、压缩、重启、fork、rewind 均只消费自己的清单；legacy 权限字段 fail closed。
- 建议测试：两个用户共享 Agent、A/B 并发写、cross-session compaction、legacy manifest、claim_version mismatch。
- 建议故障注入：kill-mid-write、半 JSON、并发 Worker、manifest 删除、旧版本 writer。
- 预计风险：中高；迁移时不能丢失正在运行 Session 的恢复状态。

### [SEC-003] 出站 URL 缺少统一 SSRF 与响应边界

- 所属模块：Personal KB、web_fetch/advanced fetch、MCP、HTTP plugin hooks。
- 严重级别：P0。
- 当前状态：断点。
- 影响对象：云元数据、内部控制面、Redis/PostgreSQL sidecar、tenant 私有服务和 Worker 内存。
- 用户可见现象：攻击者可把内网响应导入 KB 或交给模型；大响应可造成内存压力。
- 触发条件：提交 127.0.0.1、169.254.169.254、RFC1918/ULA，或 public URL 302 到 private IP。
- 输入原子：认证用户 URL、模型 web_fetch URL、MCP/SSE endpoint、hook URL。
- 权威原子：当前只校验 scheme/netloc/userinfo/transport，未决策最终网络地址。
- 执行原子：httpx follow_redirects=True 直接请求；不同模块各自实现。
- 证据原子：响应内容可写入 Personal KB、tool result 或 hook output。
- 恢复原子：无逐跳检查、DNS pin、响应总字节与流式中止。
- 消费原子：Personal KB search/read 和模型 tool loop 会消费抓取内容。
- 验收原子：无 localhost、metadata IP、IPv6、redirect、DNS rebind、SSE hop、max-bytes 测试。
- 断裂位置：URL syntax validation → actual socket destination。
- 根因：没有单一 OutboundEndpointPolicy，egress guard 只做能力级 allow/deny。
- 是否存在双事实源：是；trigger_daemon 有私网检查，Personal KB/web fetch/MCP 没有统一复用。
- 是否存在治理冲突：是；平台声称 egress governed，但网络层未执行目标级 authority。
- 是否存在跨租户或安全风险：是。
- 是否可能导致 Agent 无法继续运行：是；大响应/慢流可耗尽资源。
- 源码证据：backend/app/services/personal_knowledge_service.py:1611-1649；backend/app/services/agent_tool_domains/web_mcp.py:196-215,1260-1297；backend/app/services/mcp_authz.py:36-59；backend/app/services/plugin_hook_service.py:140-172。
- 数据库或迁移证据：KB document 会持久化抓取结果；无 endpoint policy/decision receipt 表。
- UI 消费证据：frontend Personal Knowledge URL import 暴露此入口；无内网拒绝解释。
- 测试证据：现有 mcp_authz 只覆盖 token/userinfo/transport；全量绿但无 SSRF matrix。
- 反证或不确定性：MCP 禁止 token passthrough、本地 transport，plugin hook 默认 deny；均不是最终 IP 防护。
- 修复方案：建立统一 OutboundEndpointPolicy；解析 A/AAAA，拒绝 loopback/RFC1918/link-local/CGNAT/ULA/multicast/metadata；每一 redirect/SSE endpoint 重验；限制端口、content type、字节、时间；固定 Host/SNI。
- 最小化方案：在所有入口调用同一 private/reserved IP validator，禁用自动 redirect 并手动逐跳验证。
- 需要删除或合并的旧实现：合并 trigger private URL helper、Personal KB scheme check、MCP transport check 和 hook network policy 的 endpoint 层。
- 依赖项：DNS resolver、防 rebinding策略、允许企业私网 connector 的显式 admin allowlist。
- 验收标准：所有入口对同一恶意 URL 得到同一 deny reason；public URL 仍可用；无 payload 泄漏。
- 建议测试：security/test_outbound_endpoint_policy 参数矩阵。
- 建议故障注入：DNS 首次 public 二次 private、302 链、IPv4-mapped IPv6、chunked infinite body、SSE endpoint swap。
- 预计风险：中；企业合法私网 connector 需显式、审计化例外，不能偷偷放宽。

### [SEC-004] AgentTool 密钥明文落库并可能回显

- 所属模块：Tool API、AgentTool assignment、MCP import、secret provider。
- 严重级别：P0。
- 当前状态：断点。
- 影响对象：MCP/API/provider credentials、拥有 Agent manage 权限的用户、数据库备份。
- 用户可见现象：category config API 可返回原密钥；数据库泄露直接暴露 credential。
- 触发条件：通过 tool-config/category-config/direct MCP/Smithery 写入 api_key，尤其 Tool.config_schema 为空。
- 输入原子：CategoryConfigIn 或 import credential。
- 权威原子：tenant/manage access 存在，但 secret ownership/storage contract 未统一。
- 执行原子：多个 writer 直接 assignment.config=data.config。
- 证据原子：JSON 存明文；空 schema 让 mask_tool_config_secrets 原样返回。
- 恢复原子：scrub_global_tool_secrets 不处理 AgentTool；没有 rotation/backfill receipt。
- 消费原子：runtime 直接从 AgentTool config 取 key；API 可读 raw category merge。
- 验收原子：helper encryption tests 不覆盖真实 writer/API。
- 断裂位置：authorized secret input → encrypted durable storage / masked read。
- 根因：tool config helper 不是唯一写入口；secret detection 只信 schema。
- 是否存在双事实源：是；global/tenant writer 可加密，AgentTool writer 明文。
- 是否存在治理冲突：是；权限合法不等于可以回显密钥。
- 是否存在跨租户或安全风险：跨 tenant 由 RLS 限制，但 tenant 内 credential 越权与备份泄漏成立。
- 是否可能导致 Agent 无法继续运行：是；强制回填/rotation 若不兼容会使 MCP 失效。
- 源码证据：backend/app/api/tools.py:951-1004,1030-1051；backend/app/services/agent_tool_assignment_service.py:14-72；backend/app/services/tool_config_service.py:59-175；backend/app/services/resource_discovery.py:395-421,837-916。
- 数据库或迁移证据：AgentTool.config 为普通 JSON；无 encrypted type/migration。
- UI 消费证据：category config 编辑页直接消费 config；serializer 仅按 password schema mask。
- 测试证据：backend/tests/services/test_agent_tool_assignment_service.py 明确允许 api_key=secret 原样保存。
- 反证或不确定性：配置完整 schema 的 global/tenant secret 可正确 encrypt/mask。
- 修复方案：唯一 AgentToolConfigService 负责 merge masked sentinel、保守敏感键识别、encrypt before write、decrypt only runtime；所有 API read 兜底 deny-list mask；dry-run/backfill/rotation。
- 最小化方案：先禁止任何 AgentTool API 返回 api_key/token/password/secret/private_key，并在 writer 强制 encrypt。
- 需要删除或合并的旧实现：删除 API/resource_discovery 的直接 config 赋值。
- 依赖项：SECRETS_MASTER_KEY、legacy plaintext inventory、provider rotation。
- 验收标准：DB/日志/API 无明文，runtime 可解密，不同 Agent key 隔离，历史数据回填可审计。
- 建议测试：真实 reversible secrets provider 的 API→DB→runtime round trip；empty schema MCP。
- 建议故障注入：缺 master key、key rotation 中断、masked round-trip、旧 ciphertext、新旧 Worker 并存。
- 预计风险：高；迁移前必须确认 master key 与回滚策略，不能打印明文。

### [GOV-001] BYPASS 与审计证据不具备持久不可变性

- 所属模块：RLS bypass、AuditLog、database grants。
- 严重级别：P1。
- 当前状态：局部闭环。
- 影响对象：跨 tenant Worker claim、daemon、合规调查和 incident trace。
- 用户可见现象：通常仅管理员事后发现证据缺失或被修改。
- 触发条件：enter_rls_bypass 被调用，或 app_rls 凭据执行 audit_logs UPDATE/DELETE。
- 输入原子：reason 可非空，actor_id 可为空。
- 权威原子：BYPASS 是合法跨 tenant authority，但没有 durable decision record。
- 执行原子：只 logger.warning；grant 给 app_rls 全表 SELECT/INSERT/UPDATE/DELETE。
- 证据原子：日志可丢；AuditLog 普通表可修改删除。
- 恢复原子：被删记录没有 hash chain/WORM 副本。
- 消费原子：operator、治理、调查依赖可变证据。
- 验收原子：没有 app_rls 身份的 immutability PostgreSQL 测试。
- 断裂位置：privileged authority entry → durable immutable evidence。
- 根因：注释声称 audit written first，但实现只有 logger；通用 grant 未区分 append-only 表。
- 是否存在双事实源：是；process log 与 AuditLog。
- 是否存在治理冲突：是。
- 是否存在跨租户或安全风险：是；BYPASS 可见所有 tenant。
- 是否可能导致 Agent 无法继续运行：否；主要是不可追责与安全检测失效。
- 源码证据：backend/app/database.py:269-360；backend/app/services/runtime_task_worker.py:202-239；backend/app/scripts/grant_rls_app_role.py:29-35；backend/app/models/audit.py:26-59。
- 数据库或迁移证据：current grant all tables UPDATE/DELETE；无 audit_logs immutable trigger。
- UI 消费证据：admin audit view 不能判断日志是否完整。
- 测试证据：BYPASS allowlist tests 只约束调用位置；config_revisions 已有 immutable trigger 可作正例。
- 反证或不确定性：代码未发现正常业务主动 update/delete AuditLog；RLS 限制同 tenant。
- 修复方案：BYPASS 前写 immutable SecurityAuditEvent/AuditLog，actor/service identity 必填；audit/evidence 表撤销 UPDATE/DELETE；DB trigger 禁止修改；retention 用独立 operator role。
- 最小化方案：撤销 app_rls 对 audit_logs 的 UPDATE/DELETE，并让 worker claim 写带 worker identity 的 durable event。
- 需要删除或合并的旧实现：删除“logger 即 audit pipeline”的注释与假设。
- 依赖项：grant migration、operator retention role、out-of-band sink。
- 验收标准：app_rls INSERT 成功，UPDATE/DELETE SQLSTATE 拒绝；每次 BYPASS 都有 actor/reason/trace receipt。
- 建议测试：integration/test_audit_log_immutability 与 BYPASS fail-before/after audit。
- 建议故障注入：audit insert 失败、transaction rollback、log sink unavailable、retention job越权。
- 预计风险：中；BYPASS audit 写入必须避免递归和跨 transaction 丢失。

### [RUNTIME-001] Delegation Worker 恢复目标时丢失 tenant scope

- 所属模块：A2A/delegation orchestrator、RuntimeTask Worker、RLS。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：持久化 delegation、Worker restart、严格 RLS 环境。
- 用户可见现象：委派被标为 target runtime unavailable 或后台失败，父 Agent 收不到结果。
- 触发条件：Worker claim delegation 后 dispatch_persisted_async_delegation。
- 输入原子：RuntimeTask record 含 tenant_id、target_agent_id。
- 权威原子：get_runtime_task_record 临时 pin tenant，返回后 ContextVar 恢复。
- 执行原子：_resolve_resumable_target_runtime 再开 tenant_scoped_session()，未传 tenant。
- 证据原子：严格 RLS session fail closed，看不到 Agent/LLM。
- 恢复原子：同一路径在 restart resume 重复失败。
- 消费原子：父 Agent/parent session 无 child result。
- 验收原子：orchestrator tests mock resolver，未跑真实 RLS Worker chain。
- 断裂位置：persisted tenant authority → target Agent/LLM DB read。
- 根因：注释假设 caller 保持 tenant ContextVar，实际 get_runtime_task_record scope 已退出。
- 是否存在双事实源：否；是 context propagation 丢失。
- 是否存在治理冲突：是；RLS 正确 fail closed，却阻断合法 Worker。
- 是否存在跨租户或安全风险：当前表现为拒绝；若改成 bare owner session 会变成泄漏风险。
- 是否可能导致 Agent 无法继续运行：是。
- 源码证据：backend/app/agents/orchestrator.py:1155-1189,2397-2487；backend/app/services/runtime_task_service.py:546-568；backend/app/services/runtime_task_worker.py:389-398。
- 数据库或迁移证据：RuntimeTask.tenant_id 在 current model non-null。
- UI 消费证据：Sub-agent/Agent Team UI 只会收到 failed/unavailable，不暴露 scope 原因。
- 测试证据：backend/tests/agents/test_orchestrator.py:849-932,1355-1403,1511-1602 使用 mock。
- 反证或不确定性：某些调用者可能已经在外层手动 pin tenant；Worker 主路径没有。
- 修复方案：_build_delegation_request 从 record 先解析 tenant_id，显式传给 resolver；resolver 使用 tenant_scoped_session(tenant_id, require_tenant=True)。
- 最小化方案：仅修正显式参数与 precondition，不引入 bypass。
- 需要删除或合并的旧实现：删除依赖隐式 caller ContextVar 的注释/签名。
- 依赖项：tenant resolver、LLMModel tenant predicate。
- 验收标准：真实 app_rls Worker claim 后可读同 tenant target，跨 tenant target 被拒。
- 建议测试：PostgreSQL integration 从 pending RuntimeTask 到 child run。
- 建议故障注入：ContextVar 空、错误 tenant、Agent 已归档、model fallback、Worker restart。
- 预计风险：低中；必须确保 record tenant 不能被 metadata 覆盖。

### [RUNTIME-002] 部分 RuntimeTask 异常后永久停在 running

- 所属模块：RuntimeTask worker、claim/reclaim、workflow/delegation/subagent/trigger。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：workflow、delegation、subagent、trigger；HR/dream 有 lease reclaim，不完全相同。
- 用户可见现象：任务长期 running，无结果、无 Retry、无 reconcile。
- 触发条件：wrapper 内 execute/dispatch 抛异常或返回 false。
- 输入原子：已 claim 的 running task。
- 权威原子：RuntimeTask 是权威，但 wrapper 只写内存 _STATE/日志。
- 执行原子：异常被 catch，async task 正常结束。
- 证据原子：DB 无 terminal error；lease 到期。
- 恢复原子：LEASE_RECLAIMABLE_RUNTIME_TASK_TYPES 不含 workflow/delegation/subagent/trigger。
- 消费原子：UI/parent Agent 持续读 running。
- 验收原子：测试钉住 reclaim 类型集合，却无 wrapper failure recovery。
- 断裂位置：Worker exception → RuntimeTask terminal/reclaim。
- 根因：不同 task type 各自 wrapper，未共用统一 failure finalizer；reclaim allowlist不完整。
- 是否存在双事实源：是；_STATE last_error 与 DB running。
- 是否存在治理冲突：否，主要是恢复断裂。
- 是否存在跨租户或安全风险：间接；卡住 lease/预算。
- 是否可能导致 Agent 无法继续运行：是。
- 源码证据：backend/app/services/runtime_task_worker.py:340-422；backend/app/services/runtime_task_claim_service.py:15-24,159-212。
- 数据库或迁移证据：RuntimeTask status/claim fields存在，但未更新。
- UI 消费证据：session control plane 从 DB 状态渲染。
- 测试证据：backend/tests/services/test_runtime_task_claim_service.py:73-97；全量绿无异常终结测试。
- 反证或不确定性：business_task/approval_execution 会进入 needs_reconciliation；HR/dream 可过期 reclaim。
- 修复方案：统一 claimed-task runner，所有 wrapper 未处理异常必须原子 terminalize 为 failed_retryable 或 needs_reconciliation；所有安全可重试类型可 fenced reclaim；设置 attempt cap/dead-letter。
- 最小化方案：catch 后 update RuntimeTask failed/needs_reconciliation，补齐 reclaim 类型。
- 需要删除或合并的旧实现：合并重复 wrapper catch/log 模式。
- 依赖项：side-effect classification、claim fence、notification outbox。
- 验收标准：每种 supported task 注入异常后在 bounded time 内进入合法终态或被唯一 reclaim。
- 建议测试：参数化 12 task types 的 fail-before/fail-after-side-effect。
- 建议故障注入：worker kill、lease renewal loss、DB finalizer failure、重复 reclaim、poison task。
- 预计风险：中；错误把 unknown side effect 标 retryable 会造成重复。

### [TOOL-001] Tool retry/idempotency 声明未进入执行事实

- 所属模块：ToolMeta、execution pipeline、外部邮件/消息/MCP。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：send_email、reply_email、Feishu/message、Plaza、MCP 等外部副作用。
- 用户可见现象：超时/重试可能重复发邮件、消息或创建资源。
- 触发条件：provider 已提交但本地 timeout，模型/Worker 重试。
- 输入原子：tool_call_id、canonical args、ToolMeta idempotency_scope。
- 权威原子：metadata 声明 tool_call/runtime_task/session scope，但无 durable receipt。
- 执行原子：pipeline 只读取 timeout_seconds。
- 证据原子：InvocationSpan 有 call evidence，但不能证明 external commit exactly-once。
- 恢复原子：timeout 被标 retryable；handler context 没有 canonical tool_call_id。
- 消费原子：模型看到失败后可再次调用。
- 验收原子：test_tool_contract 只断言 metadata。
- 断裂位置：ToolExecutionPolicy → provider execution/replay。
- 根因：无 ToolExecutionReceipt/Outbox；idempotency key 未注入 adapter/provider。
- 是否存在双事实源：是；metadata contract 与实际 provider behavior。
- 是否存在治理冲突：是；approval 决策不能防重复执行。
- 是否存在跨租户或安全风险：可能；重复外发/数据披露。
- 是否可能导致 Agent 无法继续运行：是；unknown loop。
- 源码证据：backend/app/tools/decorator.py:58-64；backend/app/tools/registry.py:172-194；backend/app/tools/runtime.py:20-50；backend/app/tools/execution_pipeline.py:390-419。
- 数据库或迁移证据：无 tool_execution_receipts 唯一键/状态表。
- UI 消费证据：UI 显示单个 tool result，无法识别重复/unknown。
- 测试证据：backend/tests/tools/test_tool_contract.py:85-109。
- 反证或不确定性：少数 provider/业务服务可能自行幂等，不能覆盖统一工具面。
- 修复方案：建立 receipt/outbox，唯一键 tenant + scope + canonical key；executing/succeeded/failed/unknown；provider 原生 key 或本地 outbox；成功从 receipt 回注。
- 最小化方案：先把 external_visible timeout 一律标 unknown 并阻塞自动重试，同时传递 tool_call_id。
- 需要删除或合并的旧实现：删除仅声明不消费的 retry/idempotency 假契约或完成其执行。
- 依赖项：TOOL-002 reconciliation、approval attempt、provider adapters。
- 验收标准：同 key 并发/重放只发生一次副作用，unknown 不自动重放。
- 建议测试：fake provider commit 后断连、同 key asyncio.gather、fallback。
- 建议故障注入：timeout after commit、worker kill、DB receipt commit failure、provider 409 duplicate。
- 预计风险：高；错误 key 规范化会把不同合法动作误去重。

### [TOOL-002] 未知副作用 reconciliation 未持久化且模型继续

- 所属模块：Kernel recovered tool frames、RecoveryManifest、session control plane。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：崩溃时处于 executing 的 mutating tool。
- 用户可见现象：系统提示 requires reconciliation 后仍继续模型循环，模型可能再次发起相同动作。
- 触发条件：恢复 non-parallel-safe/destructive pending frame。
- 输入原子：manifest pending_tool_frames。
- 权威原子：frame 原本是恢复证据；代码将其从 pending metadata 删除。
- 执行原子：不 replay 是正确的，但 run 未进入 needs_reconciliation gate。
- 证据原子：reconciliation 只写 session_context.metadata 内存 key，manifest schema不序列化。
- 恢复原子：再次重启后该 unknown 状态消失。
- 消费原子：只有模型看到一段 system text；operator/UI 无 resolution object。
- 验收原子：测试只断言内存 key 和 frame 被清空。
- 断裂位置：unknown side effect detection → durable blocking state/operator decision。
- 根因：把 semantic warning 当 durable state machine。
- 是否存在双事实源：是；内存 metadata 与 RuntimeTask DB。
- 是否存在治理冲突：是；unknown 应阻塞，LLM 仍可行动。
- 是否存在跨租户或安全风险：可能造成重复外发。
- 是否可能导致 Agent 无法继续运行：是；安全做法应等待人工决议。
- 源码证据：backend/app/kernel/engine.py:2318-2481；backend/app/kernel/turn_orchestrator.py:936-953；backend/app/runtime/recovery_manifest.py:92-117。
- 数据库或迁移证据：无 ToolReconciliation row；RuntimeTask 未被更新。
- UI 消费证据：admin runtime reconciliation 只消费 DB needs_reconciliation，不见此内存 key。
- 测试证据：backend/tests/kernel/test_engine.py:1667-1717。
- 反证或不确定性：read-only/parallel-safe frame replay 路径可通过 normal governed runtime。
- 修复方案：在清除 frame 前原子写 ToolExecutionReceipt/ToolReconciliation，并把 run 置 needs_reconciliation；operator/provider probe 决议后 exactly-once continuation。
- 最小化方案：保留 frame、立即停止 invocation 并持久化 RuntimeTask needs_reconciliation。
- 需要删除或合并的旧实现：删除 recovered_tool_frame_reconciliation 内存-only事实源。
- 依赖项：TOOL-001 receipt、operator API/UI。
- 验收标准：二次重启仍可见 unknown；同 key不能重发；决议后只 continuation 一次。
- 建议测试：crash before/after receipt、UI resolve、provider probe。
- 建议故障注入：reconciliation DB write fail、operator 并发决议、恢复时旧 Worker。
- 预计风险：中高；错误“已成功”决议会掩盖副作用失败。

### [APPROVAL-001] Session permission 执行失败后不可重试

- 所属模块：session permission API、tool execution、continuation。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：需要用户批准的 session tool。
- 用户可见现象：首次允许后执行失败显示 retryable，但再次点击/请求得到 409 already resolved。
- 触发条件：decision commit 后 execute_session_permission_tool 或 continuation 抛错。
- 输入原子：permission_request_id、allow/deny、pending frame。
- 权威原子：session_permission_decision 被当作整个请求终态。
- 执行原子：decision 在 tool execution 前 commit。
- 证据原子：failure event retryable=True。
- 恢复原子：resolver 扫到任何 decision 就 409。
- 消费原子：UI 没有真实可用 Retry/Reconcile。
- 验收原子：测试钉住 existing decision 409，未覆盖 execute failure second attempt。
- 断裂位置：PermissionDecision → PermissionExecutionAttempt。
- 根因：不可变授权决定与可变执行尝试混为同一状态。
- 是否存在双事实源：是；event 声称 retryable，API state machine拒绝。
- 是否存在治理冲突：是。
- 是否存在跨租户或安全风险：timeout after commit 还可能副作用 unknown。
- 是否可能导致 Agent 无法继续运行：是。
- 源码证据：backend/app/api/chat_sessions.py:2126-2252,2312-2407。
- 数据库或迁移证据：session permission 用 transcript event，无 execution attempt row/unique receipt。
- UI 消费证据：permission card 消费 retryable 状态但没有可达执行状态机。
- 测试证据：backend/tests/api/test_chat_session_runs.py:892-972。
- 反证或不确定性：企业 ApprovalRequest/approval_execution RuntimeTask 有更完整状态机，可复用。
- 修复方案：分离 immutable PermissionDecision 和 PermissionExecutionAttempt；同 permission/tool call 唯一 receipt；not_started/known failure 可重试，unknown 只 reconcile；成功唯一 continuation。
- 最小化方案：失败时不要返回 retryable；转 needs_reconciliation 并提供 operator入口，直至完整 attempt state落地。
- 需要删除或合并的旧实现：删除 scan transcript 判定“任何 decision 都终态”的简化逻辑。
- 依赖项：TOOL-001/002、approval execution worker。
- 验收标准：fail-before-call 可同 key重试；timeout-after-commit 不重复；continuation exactly-once。
- 建议测试：duplicate HTTP、worker crash、unknown decision、deny continuation。
- 建议故障注入：commit 后进程 kill、tool success 后 broadcast fail、continuation unique conflict。
- 预计风险：高；授权不可被重试逻辑扩大到不同参数。

### [TRIGGER-001] 即时触发忽略重复租约

- 所属模块：Trigger daemon、immediate fire、RuntimeTask admission。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：/loop immediate、外部副作用、预算。
- 用户可见现象：双击/重试产生两个 runtime_task_id 和两次执行。
- 触发条件：同秒并发 fire_trigger_once_now 或 Redis lease 返回 False/异常。
- 输入原子：agent_id、trigger_id、event_key。
- 权威原子：正常 daemon 把 Redis NX lease 当 dedupe authority。
- 执行原子：immediate path await lease 但忽略 bool。
- 证据原子：创建多条 RuntimeTask/预算记录。
- 恢复原子：响应丢失后重试无法取回原 receipt。
- 消费原子：Agent/provider 执行两次，UI看到多个 task。
- 验收原子：即时测试始终 mock lease=True。
- 断裂位置：fire lease decision → RuntimeTask creation。
- 根因：注释将 lease降为 advisory，_mark_trigger_fire_started 不是 CAS。
- 是否存在双事实源：是；Redis lease 与 config _fire_inflight。
- 是否存在治理冲突：预算/approval 各自正确，但重复 admission。
- 是否存在跨租户或安全风险：通常同 tenant，但可重复外发。
- 是否可能导致 Agent 无法继续运行：是；重复工作/成本。
- 源码证据：backend/app/services/trigger_daemon.py:1228-1241,2344-2426,2467-2475。
- 数据库或迁移证据：无 trigger_fire_intents unique(event_key)。
- UI 消费证据：返回每次新 runtime_task_id。
- 测试证据：backend/tests/services/test_trigger_daemon_loop.py:269-425。
- 反证或不确定性：正常 tick 对 lease=False 会 continue；RuntimeTask write fail会停止。
- 修复方案：DB fire intent 唯一约束作为 authority，事务创建 intent+budget+RuntimeTask/outbox；Redis仅优化；duplicate返回原 receipt。
- 最小化方案：lease=False 立即返回 duplicate，不创建 task。
- 需要删除或合并的旧实现：删除 best-effort dedup 注释与无锁 _fire_inflight authority。
- 依赖项：RuntimeTask idempotency、budget reservation。
- 验收标准：并发 N 次只有一 task/一次 invocation/一个 receipt。
- 建议测试：asyncio.gather、跨进程 PG unique、响应丢失重试。
- 建议故障注入：Redis unavailable、DB commit fail、Worker kill、lease expiry执行中。
- 预计风险：中；event key 粒度必须区分合法多次手动运行。

### [ARTIFACT-001] A2A/Sub-agent 交付链同时断在 authority 与 UI

- 所属模块：orchestrator、ChatArtifact、ThreadItem、Workspace Documents。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：委派/子 Agent 生成文件、父 Agent、最终用户。
- 用户可见现象：子任务显示完成但 Workspace 无文件；修复前端后 content/download 仍会 404。
- 触发条件：child session 产生 ChatArtifact，orchestrator 投影 parent child_session event。
- 输入原子：child artifact parts。
- 权威原子：应复用 child ChatArtifact 或创建受治理 reference；当前 build_artifact_candidate 新生成 UUID。
- 执行原子：candidate 写 snapshot，但 _project_a2a_artifact_refs_to_parent_session 不 insert ChatArtifact。
- 证据原子：parent transcript 有 artifact part 和 source_artifact_id。
- 恢复原子：replay 重复消费无 DB 行的 artifact_id。
- 消费原子：canonical ThreadItem 转换只取 event part，subagent_activity 不调用 extractArtifactParts。
- 验收原子：后端只断言 metadata，前端只测独立 helper；无 end-to-end content/download。
- 断裂位置：child artifact → parent governed reference → message.artifacts → Workspace。
- 根因：候选对象被误当 durable entity；后端/前端 contract未共测。
- 是否存在双事实源：是；source_artifact_id 与伪 projected artifact_id。
- 是否存在治理冲突：是；父会话无显式 resource grant/reference。
- 是否存在跨租户或安全风险：潜在；错误复用必须防止父 Agent越权读 child workspace。
- 是否可能导致 Agent 无法继续运行：主聊天可继续，但任务交付失败。
- 源码证据：backend/app/agents/orchestrator.py:1884-1988,2017-2148；backend/app/services/chat_artifact_delivery.py:512-605；backend/app/api/files.py:404-476,555-587；frontend/src/pages/session-workbench/threadItemReducer.ts:343-485；frontend/src/pages/session-workbench/timelineModel.ts:983-1007。
- 数据库或迁移证据：projected UUID 无 chat_artifacts row。
- UI 消费证据：Workspace 只遍历 message.artifacts；subagent branch不填该字段。
- 测试证据：artifact/backend 与 reducer/helper 单测分别通过，缺完整 contract。
- 反证或不确定性：标准单 Agent artifact path 已闭环；child source artifact row真实存在。
- 修复方案：建立 ArtifactReference/ResourceGrant 或事务创建真实 projected ChatArtifact，绑定 tenant/source/delivery/root session；统一 ThreadItem artifact extraction；历史 event回填。
- 最小化方案：父 UI 使用 source_artifact_id + source agent 读取，但必须先新增授权检查，不能裸跨 workspace。
- 需要删除或合并的旧实现：删除展示用随机 projected artifact UUID。
- 依赖项：workspace authority、root session ownership、A2A return contract。
- 验收标准：child file → parent event → Workspace → preview/download 全链，源删除时快照仍可读，跨 tenant拒绝。
- 建议测试：真实 API JSON 经 reducer 到 Workspace，再调用 content/download。
- 建议故障注入：source file删除、child artifact quarantined、parent replay、重复投影、binary大文件。
- 预计风险：中高；reference grant设计不严会扩大 Agent权限。

### [UX-001] 前端取消终态先于后端权威

- 所属模块：AgentDetail runtime state、cancel API、Session controls。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：运行中的 web-chat、工具副作用、计费和用户信任。
- 用户可见现象：Stop 后立即显示 cancelled/可重新输入，但后端可能仍执行。
- 触发条件：cancel API 401/404/500/network reject 或响应延迟。
- 输入原子：用户 Stop。
- 权威原子：RuntimeTask + run_cancelled Transcript 才是终态。
- 执行原子：前端 fire-and-forget API 后立即 markActiveRunTerminal。
- 证据原子：UI local state 与 DB/transcript 分叉。
- 恢复原子：失败只 console.warn；未保留 cancelling/retry。
- 消费原子：composer、Stop、phase 卡片都消费本地镜像。
- 验收原子：无 promise reject/delay/reconnect cancellation测试。
- 断裂位置：cancel intent → server terminal evidence → UI terminal。
- 根因：sessionUiStateRef、activeRun refs/maps、activePhase、isWaiting、isStreaming、locallyTerminal 多组 writer。
- 是否存在双事实源：是。
- 是否存在治理冲突：可能；UI允许用户继续，而旧 run仍持有权限/预算。
- 是否存在跨租户或安全风险：通常无跨 tenant，但可能继续外部副作用。
- 是否可能导致 Agent 无法继续运行：是；同 session active run conflict或并发意图。
- 源码证据：frontend/src/pages/AgentDetail.tsx:1292-1308,2188-2205；文件总体约 2803 行；AgentChatSection约 2369 行。
- 数据库或迁移证据：后端 cancel 在提交 RuntimeTask killed 后才广播。
- UI 消费证据：SessionRunControls 根据 running 显示 Stop。
- 测试证据：后端 cancel tests通过；前端无拒绝场景。
- 反证或不确定性：正常成功取消主路径可工作，active-run requery可部分纠正。
- 修复方案：单一 session runtime reducer；Stop 进入 cancelling，等待 API成功或 canonical event；失败保留 running并提供 Retry；重连 active-run对账。
- 最小化方案：await cancelSessionRun 成功后再 mark terminal；catch显示错误不改状态。
- 需要删除或合并的旧实现：删除 local terminal set和多处 setIsWaiting/setIsStreaming直接写。
- 依赖项：ThreadItem event normalization、active-run endpoint。
- 验收标准：取消失败绝不显示 cancelled；成功/重复/断线后状态一致。
- 建议测试：reject、delayed resolve、duplicate click、reconnect、late tool result。
- 建议故障注入：cancel commit成功 broadcast失败、网络断开、旧事件乱序。
- 预计风险：中；reducer迁移必须防止终态被旧 running event回退。

### [UX-002] 上传取消与部分成功均不闭环

- 所属模块：AgentDetail upload、upload-progress XHR、SessionComposer。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：聊天附件、Workspace orphan、用户重试。
- 用户可见现象：Cancel 按钮无动作；一个文件失败会隐藏已成功上传的其他文件，重试产生重复。
- 触发条件：点击 Cancel，或多文件批次部分失败。
- 输入原子：最多十个 File。
- 权威原子：每个 /chat/upload 单独写 workspace/resource authority。
- 执行原子：caller只解构 promise，未保存 abort；XHR没有 onabort。
- 证据原子：成功文件已经落盘，但 Promise.all整体 reject。
- 恢复原子：abort Promise不 settle；partial success 无 retry/idempotency contract。
- 消费原子：attachedFiles 只在全部成功后更新。
- 验收原子：无 mocked XHR abort/partial batch测试。
- 断裂位置：per-file write evidence → batch UI consumption/cancel recovery。
- 根因：文件选择与 paste 重复实现，没有 UploadBatchController。
- 是否存在双事实源：是；Workspace 已有文件，composer附件列表没有。
- 是否存在治理冲突：可能留下已授权但用户不知情资源。
- 是否存在跨租户或安全风险：无直接跨 tenant。
- 是否可能导致 Agent 无法继续运行：局部；uploading可能卡住或用户无法引用文件。
- 源码证据：frontend/src/pages/AgentDetail.tsx:1962-2056；frontend/src/api/core/upload-progress.ts:8-52；frontend/src/pages/session-workbench/SessionRunControls.tsx:121-136。
- 数据库或迁移证据：upload 后 authority manifest逐文件注册。
- UI 消费证据：uploadAbortRef 只有清空与读取，无赋值。
- 测试证据：SessionComposer props tests不执行真实 abort。
- 反证或不确定性：单文件成功正常可附加。
- 修复方案：唯一 UploadBatchController 保存所有 abort；onabort reject typed error；allSettled 展示部分成功；每文件 idempotency key；定义保留/清理语义。
- 最小化方案：把 abort 聚合写入 ref、补 xhr.onabort、改 Promise.allSettled。
- 需要删除或合并的旧实现：合并 file input/paste 两套上传。
- 依赖项：upload API idempotency、workspace cleanup。
- 验收标准：Cancel终止全部 in-flight 且 promise settle；2成功1失败显示两附件+一可重试项。
- 建议测试：mock XHR、多文件乱序、同名重试、unmount。
- 建议故障注入：authority register失败、响应丢失、同名冲突、取消恰逢完成。
- 预计风险：低中；不能误删已被其他消息引用的成功文件。

### [DATA-001] 租户隔离 quarantine 有 receipt 无恢复消费

- 所属模块：tenant_null migration、operator data recovery。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：production 将被隔离的 2301 条 legacy rows。
- 用户可见现象：升级后历史审计/RuntimeTask 可能从正常 tenant UI 消失，管理员无 rebind入口。
- 触发条件：tenant_null_semantics_0712 把无法推导/冲突行移动到 quarantine tenant。
- 输入原子：residual table,row,reason。
- 权威原子：receipt 正确拒绝猜 tenant。
- 执行原子：migration insert receipt，再 update source tenant_id。
- 证据原子：tenant_scope_quarantine_records。
- 恢复原子：无 inspect/export/rebind/delete workflow。
- 消费原子：只有 audit script count；无 API/UI/operator service。
- 验收原子：migration验证 receipt，但不验证业务恢复。
- 断裂位置：quarantine evidence → operator remediation → tenant consumer restoration。
- 根因：migration完成安全隔离，但未建设生命周期消费者。
- 是否存在双事实源：否；是无消费者。
- 是否存在治理冲突：是；安全收紧会造成合法历史不可见。
- 是否存在跨租户或安全风险：错误 rebind 会造成泄漏。
- 是否可能导致 Agent 无法继续运行：对被隔离 active RuntimeTask 是。
- 源码证据：backend/alembic/versions/tenant_null_semantics_0712.py:371-499；backend/app/models/tenant_scope_quarantine.py:15-28；backend/app/scripts/audit_tenant_null_semantics.py:99-155。
- 数据库或迁移证据：production orphan audit_logs=1805、runtime_tasks=496。
- UI 消费证据：无组件/API。
- 测试证据：migration tests断言 2 receipts，未验证 restore。
- 反证或不确定性：部分 orphan可能应永久删除，而不是恢复。
- 修复方案：operator-only dry-run inspect/export/rebind/delete；权威证据、双人确认、高风险操作、immutable audit；active runtime统一转 reconcile而非直接恢复执行。
- 最小化方案：先提供只读 inventory/export 和明确 runbook，不自动 rebind。
- 需要删除或合并的旧实现：无；应补 consumer。
- 依赖项：SEC-001 cutover、GOV-001 immutable audit。
- 验收标准：每条 receipt最终有 retained/rebound/deleted decision；无越权；active task不自动重放。
- 建议测试：正确/冲突 rebind、deleted parent、tenant不存在、bulk dry-run。
- 建议故障注入：rebind事务中断、并发 operator、旧 FK缺失。
- 预计风险：高；属于生产数据修复，必须确认门而非自动化猜测。

### [SESSION-001] 无效显式 WebSocket Session 被静默改投

- 所属模块：WebSocket compatibility start、frontend transport controller。
- 严重级别：P1。
- 当前状态：断点。
- 影响对象：删除/无权/wrong-agent session、重连用户消息。
- 用户可见现象：用户在旧会话输入，消息实际落入最近会话或新会话；UI仍投影到旧 session key。
- 触发条件：URL显式 session_id 不存在、不属于用户/Agent。
- 输入原子：WebSocket path/session_id。
- 权威原子：客户端意图为显式 Session；后端把查不到改成 None。
- 执行原子：随后 latest/create 并注册实际 conv_id。
- 证据原子：DB transcript属于实际 session。
- 恢复原子：frontend仍按原 sessionId重连/backfill，持续错配。
- 消费原子：onMessage投到原 session state。
- 验收原子：无 invalid/deleted/foreign explicit session测试。
- 断裂位置：explicit session identity → canonical handshake/UI key。
- 根因：把“未提供 session”与“提供但无效”合并。
- 是否存在双事实源：是；server conv_id 与 frontend runtime key。
- 是否存在治理冲突：无权 session查询正确拒绝，但执行退到另一个有权 session，掩盖错误。
- 是否存在跨租户或安全风险：不会直接进入无权 session，但可能把敏感消息写入错误有权 session。
- 是否可能导致 Agent 无法继续运行：是；历史/状态混乱。
- 源码证据：backend/app/api/websocket.py:517-560,595-599,739-760；frontend/src/pages/agent-detail/useSessionTransportController.ts:143-245。
- 数据库或迁移证据：实际 ChatSession/Transcript与 UI key不同。
- UI 消费证据：transport controller以 caller sessionId构建 key。
- 测试证据：11个 websocket tests无 invalid explicit matrix。
- 反证或不确定性：REST durable run是主要入口；WebSocket仍是兼容 start/订阅入口。
- 修复方案：显式 ID 无效必须 4404/4004 fail closed；只有未提供才 latest/create；或先 session_resolved handshake 并原子 re-key。
- 最小化方案：删除 fallback-to-create 对 explicit ID 的分支。
- 需要删除或合并的旧实现：合并 WebSocket start 与 REST canonical session resolution。
- 依赖项：frontend reconnect、session deleted event。
- 验收标准：消息永不落入非请求 session；错误可理解并可重新选择会话。
- 建议测试：deleted、foreign user、wrong agent、concurrent delete、canonical handshake。
- 建议故障注入：连接建立后 session删除、重连 race、latest session变化。
- 预计风险：低中；旧客户端依赖 fallback 时需明确兼容错误。

### [MEMORY-001] Shared Team Memory 写入后 Agent 永不消费

- 所属模块：TeamMemoryStore、Memory API、AgentAware/Workspace UI。
- 严重级别：P2。
- 当前状态：断点。
- 影响对象：租户共享笔记、操作手册、Agent 回答。
- 用户可见现象：UI 声称“跨会话与压缩恢复阶段都会复用”，Agent 实际不会搜索或读取。
- 触发条件：用户创建 shared memory 后要求 Agent 使用其中知识。
- 输入原子：tenant/workspace/key/title/content/revision。
- 权威原子：TeamMemoryStore 按 tenant/workspace 文件分区，revision/sync token成立。
- 执行原子：API CRUD/search/get/upsert/delete 成立。
- 证据原子：Markdown、checksum、revision、updated_by。
- 恢复原子：conflict、soft delete、sync token成立。
- 消费原子：runtime/tools/memory_service 无 TeamMemoryStore caller 或 search/read tool。
- 验收原子：service/API测试只证明 CRUD。
- 断裂位置：durable evidence → Agent tool/runtime consumption。
- 根因：将人类 UI CRUD命名为 Agent Memory，没有定义 runtime contract。
- 是否存在双事实源：否；存在产品语义与实际消费者冲突。
- 是否存在治理冲突：若新增消费，需补 team membership/sensitivity ACL。
- 是否存在跨租户或安全风险：当前路径按 tenant分区；未来工具若直接信 workspace_key 会有风险。
- 是否可能导致 Agent 无法继续运行：否；会导致错误回答/承诺。
- 源码证据：backend/app/services/team_memory.py:147-226,350-450；backend/app/api/memory.py:241-362；frontend/src/pages/agent-detail/TeamMemorySummaryCard.tsx:73-205。
- 数据库或迁移证据：内容不在 DB；文件系统是事实源。
- UI 消费证据：frontend/src/i18n/zh.json:1111-1117 明确承诺跨 session/compaction 复用。
- 测试证据：backend/tests/services/test_team_memory_service.py、backend/tests/api/test_memory_api.py 在本轮通过；无 Agent E2E。
- 反证或不确定性：不是空壳，CRUD、conflict、secret scan真实可用。
- 修复方案：产品二选一；若为 Team Memory，增加 governed search_team_memory/read_team_memory 工具，identity解析 tenant/team，ACL、citation、read audit、span；若为便笺，改名 Shared Workspace Notes并删承诺。
- 最小化方案：立即修正文案与分类，防止用户误信。
- 需要删除或合并的旧实现：若建设工具，复用 Personal KB tool contract而非 prompt全量注入。
- 依赖项：Agent Team membership、sensitivity、workspace authority。
- 验收标准：Agent 对授权条目回答带 source_ref；跨 workspace/tenant拒绝；删除后不再消费。
- 建议测试：两个 tenant同名条目、team member/非member、revision conflict、restart。
- 建议故障注入：文件损坏、并发写、volume unavailable、stale revision。
- 预计风险：中；全量自动注入会造成 prompt injection，应保持按需工具。

### [KNOWLEDGE-001] Company Knowledge Base 当前未建设

- 所属模块：Knowledge Core、Enterprise workspace、Agent tools。
- 严重级别：P2。
- 当前状态：已知缺失。
- 影响对象：organization/department 共享知识、企业 Agent。
- 用户可见现象：没有可用的 Company KB source、同步、ACL、检索、引用或管理 UI；legacy export明确不可供 Agent使用。
- 触发条件：用户期望企业知识被 Agent按组织权限检索。
- 输入原子：缺失 company source/connector ingestion。
- 权威原子：缺失 organization/department owner、ACL、connector identity。
- 执行原子：PersonalKnowledgeService/search硬编码 scope_type=person。
- 证据原子：无 company document/segment/citation/source chain。
- 恢复原子：无 sync cursor、retry、delete propagation、re-index。
- 消费原子：无 search_company_kb/read_company_kb。
- 验收原子：无 company RLS/ACL/citation lifecycle测试。
- 断裂位置：能力从输入开始缺失。
- 根因：generic scope schema被留作扩展，但没有 vertical slice。
- 是否存在双事实源：legacy company files不能作为 KB truth；若误用将形成双源。
- 是否存在治理冲突：尚无执行；未来必须解决 organization ACL/RLS。
- 是否存在跨租户或安全风险：当前无入口；草率复用 Personal KB会产生风险。
- 是否可能导致 Agent 无法继续运行：否；会缺企业知识。
- 源码证据：backend/app/models/knowledge.py:34-59；backend/app/services/personal_knowledge_service.py:1104-1133；backend/app/services/personal_knowledge_index_search.py:94-116。
- 数据库或迁移证据：KnowledgeDocument scope_type 可泛化，但所有生产 writer/searcher为 person。
- UI 消费证据：frontend/src/pages/workspace/LegacyCompanyFilesExportCard.tsx:100-124 声明退休文件非 KB、Agent不可访问。
- 测试证据：architecture company knowledge retirement tests确认该缺失。
- 反证或不确定性：Personal KB本身较完整，可复用索引原语但不是完成证据。
- 修复方案：一次性建设 CompanyKnowledgeSource/Document/Segment、tenant+department ACL/RLS、connector principal/cursor、canonical Markdown/source_ref、delete propagation、search/read tool、read audit、admin recovery UI。
- 最小化方案：维持已知缺失并在产品面清晰标记，不暴露空壳入口。
- 需要删除或合并的旧实现：不要恢复 legacy company file注入；可只保留一次性 export。
- 依赖项：organization authority、connector identity、OutboundEndpointPolicy、retention/legal hold。
- 验收标准：跨 department/tenant矩阵、sync resume、source delete、citation、Agent实际回答全部成立。
- 建议测试：integration/test_company_knowledge_lifecycle 全七原子。
- 建议故障注入：connector token失效、分页重复、delete race、index重建中断。
- 预计风险：高；属于新产品能力，不应通过简单改 scope_type 冒充闭环。

### [KNOWLEDGE-002] Personal KB 队列正文依赖发起实例本地文件

- 所属模块：PersonalKnowledgeService、evolution daemon、cloud storage。
- 严重级别：P2。
- 当前状态：局部闭环。
- 影响对象：上传/粘贴后的异步知识导入。
- 用户可见现象：API 已返回 queued，但 Worker 报 queued_source_missing，文档永不索引。
- 触发条件：queue 与 process 在不同 data_root、API/容器重建、原实例崩溃。
- 输入原子：上传 bytes/markdown。
- 权威原子：DB job绑定 tenant/owner，但 payload仅本地相对路径。
- 执行原子：worker从自己的 AGENT_DATA_DIR读取 spool。
- 证据原子：正文只在发起实例文件；DB无 blob/object key。
- 恢复原子：fleet stale-job claim可跨实例，但 payload不可达。
- 消费原子：索引未生成，search/read工具无内容。
- 验收原子：测试 queue/process使用同一 tmp root。
- 断裂位置：durable DB job → durable cross-instance payload。
- 根因：本地文件被当作云队列 payload truth。
- 是否存在双事实源：是；DB job与实例文件。
- 是否存在治理冲突：Worker RLS可正确 claim，storage却阻断合法执行。
- 是否存在跨租户或安全风险：object store修复时需 tenant-scoped key/ACL。
- 是否可能导致 Agent 无法继续运行：局部；KB任务失败。
- 源码证据：backend/app/services/personal_knowledge_service.py:970-1022,2100-2160；backend/app/services/evolution_daemon.py:263-280。
- 数据库或迁移证据：KnowledgeIndexJob metadata只保存 queued_source_path。
- UI 消费证据：Personal KB job UI可见 failed，但无跨实例恢复。
- 测试证据：现有 service tests单 root。
- 反证或不确定性：当前 backend有持久卷且单 replica，常见路径可工作；backend-api无卷但Nginx当前未路由该 endpoint过去。
- 修复方案：tenant-scoped object/blob store；先上传+hash，再事务创建 job pointer；worker流式读取校验；成功按 retention清理；missing可重试/可观测。
- 最小化方案：在单 replica约束下明确 routing到 volume backend，并禁止无卷/多副本 claim；这不是长期闭环。
- 需要删除或合并的旧实现：迁移后删除 queued_source_path 本地 canonical语义。
- 依赖项：blob provider、tenant encryption/retention、job idempotency。
- 验收标准：service A/B不同 root仍可 queue/process；重启/重复 claim不丢不重。
- 建议测试：distributed import integration、checksum mismatch、blob missing。
- 建议故障注入：upload-before-DB commit、commit-before-worker、download中断、worker kill。
- 预计风险：中；对象存储删除时序必须避免早删。

### [ASSET-001] Enterprise AI Asset 只覆盖五类

- 所属模块：AIAssetRecord、adapters、enterprise API/UI。
- 严重级别：P2。
- 当前状态：局部闭环。
- 影响对象：Prompt、Tool、Connector、Model config、Memory policy、Knowledge source、Evaluation、Template、Artifact。
- 用户可见现象：企业目录名称暗示统一资产治理，但大量资产没有 revision/usage/reconcile。
- 触发条件：管理员尝试统一治理非五类资产。
- 输入原子：五类 native writer存在，其他类型无 adapter。
- 权威原子：已接入类型保留 native runtime authority，设计正确。
- 执行原子：register/revision/rollback/reconcile只处理 agent/skill/workflow/subagent/external_capability。
- 证据原子：AIAssetUsageEvent只覆盖已接入类型。
- 恢复原子：五类有 rollback/reconcile，其余无。
- 消费原子：五类 runtime usage真实投影，其余无。
- 验收原子：五类 14 tests通过；无类型完整性矩阵。
- 断裂位置：Enterprise AI Asset产品范围 → native asset adapters。
- 根因：数据库 CheckConstraint硬限制五类，UI/命名未声明边界。
- 是否存在双事实源：已接入类型不是双源，是 control index/native authority分工；未接入类型散落各自表。
- 是否存在治理冲突：是；企业策略无法统一覆盖未接入资产。
- 是否存在跨租户或安全风险：取决于各原生表；不能靠 AI Asset catalog证明治理。
- 是否可能导致 Agent 无法继续运行：否，主要是治理/回滚缺失。
- 源码证据：backend/app/models/ai_asset.py:15-34,88-121；backend/app/services/ai_asset_adapters.py。
- 数据库或迁移证据：ck_ai_asset_type只允许五值。
- UI 消费证据：enterprise AI assets页面只能展示五类。
- 测试证据：tests/services/test_ai_asset_adapters.py + integration/test_ai_asset_control_plane.py = 14 passed。
- 反证或不确定性：五类是实闭环，不应评价为全盘缺失。
- 修复方案：先定义资产类型覆盖表、native authority、revision schema、runtime resolver、usage key、rollback semantics；migration/backfill现有 native records；参数化 contract test。
- 最小化方案：若产品只治理五类，改名 Core Executable Assets并显示 exclusions。
- 需要删除或合并的旧实现：不要复制 native content进 AIAssetRecord。
- 依赖项：各资产 owner/visibility/version contract。
- 验收标准：每个声明类型都有 writer/resolver/usage/reconcile/permission测试。
- 建议测试：integration/test_ai_asset_type_coverage 参数矩阵。
- 建议故障注入：native drift、rollback不可逆、usage duplicate、deleted dependency。
- 预计风险：中高；一刀切 version/rollback会伪造不可逆资产能力。

### [MCP-001] Generic MCP 路径忽略 AgentTool 配置

- 所属模块：MCP handler、dynamic MCP execution、tool config resolution。
- 严重级别：P2。
- 当前状态：断点。
- 影响对象：call_mcp_tool、resource list/read、prompt、Agent-specific credential。
- 用户可见现象：同一 MCP 动态工具可成功，generic call/resource却报未授权或 provider failure。
- 触发条件：credential/transport只保存在 AgentTool.config。
- 输入原子：agent assignment + Tool + per-agent config。
- 权威原子：assignment enable/trust检查存在。
- 执行原子：generic select Tool后只读 Tool.config；dynamic path会合并 AgentTool.config。
- 证据原子：同 server出现两种 execution result。
- 恢复原子：generic重试不会补回丢失 config。
- 消费原子：model generic MCP core surface失败。
- 验收原子：happy test把 secret放 Tool.config。
- 断裂位置：per-agent config authority → generic MCPClient。
- 根因：多套 resolver，各自读取不同层级。
- 是否存在双事实源：是；Tool.config 与 AgentTool.config。
- 是否存在治理冲突：是；同一 assignment通过 trust却执行错credential。
- 是否存在跨租户或安全风险：错误 fallback到global key可能造成 credential混用。
- 是否可能导致 Agent 无法继续运行：局部。
- 源码证据：backend/app/tools/handlers/mcp.py:348-529；backend/app/services/agent_tool_domains/web_mcp.py:2289-2325；backend/app/services/tool_config_service.py:178-213。
- 数据库或迁移证据：Tool/AgentTool均有 config。
- UI 消费证据：Agent tool config UI写 AgentTool层。
- 测试证据：backend/tests/tools/test_mcp_call_tool.py:358-375。
- 反证或不确定性：dynamic path展示了正确合并方向。
- 修复方案：唯一 resolve_agent_mcp_execution_config，合并 global→tenant→agent，decrypt、OAuth、token policy、outbound policy；所有 MCP入口复用。
- 最小化方案：generic handler调用 resolve_tool_config(agent_id=...)。
- 需要删除或合并的旧实现：删除各路径直接 row.config读取。
- 依赖项：SEC-003/004。
- 验收标准：AgentTool-only key在 generic/dynamic/resource/prompt 四路等价且跨 Agent隔离。
- 建议测试：参数化 fake MCPClient config equivalence。
- 建议故障注入：OAuth refresh、encrypted secret、assignment disabled、key rotation。
- 预计风险：中；merge优先级必须防 global credential越权。

### [USAGE-001] Loop Guard 终态漏记 TokenUsage

- 所属模块：Kernel LoopGuard、token tracker、budget/quota。
- 严重级别：P2。
- 当前状态：断点。
- 影响对象：Agent/User/Tenant usage counters、成本、预算。
- 用户可见现象：最昂贵的循环调用可能显示比实际更少的 token/成本。
- 触发条件：cost loop guard或text loop guard硬终止。
- 输入原子：provider response usage已存在。
- 权威原子：InvocationSpan generation usage有证据，但 TokenUsage ledger/counters是预算消费者。
- 执行原子：_abort_for_loop_guard只 persist before exit。
- 证据原子：cost guard甚至在 accumulated_tokens累加前 return。
- 恢复原子：无 call-id幂等补录。
- 消费原子：quota/budget/UI读低估 counter。
- 验收原子：LoopGuard测试不 spy record_token_usage。
- 断裂位置：provider usage evidence → durable usage accounting。
- 根因：usage在终态分支批量写，而非每 provider response幂等写。
- 是否存在双事实源：是；InvocationSpan usage 与 token tracker。
- 是否存在治理冲突：是；budget guard自身漏账。
- 是否存在跨租户或安全风险：无直接泄漏，但可绕预算。
- 是否可能导致 Agent 无法继续运行：反向可能；预算耗尽晚发现。
- 源码证据：backend/app/kernel/turn_orchestrator.py:595-633,1158-1175,1605-1625；backend/app/services/token_tracker.py:203-290。
- 数据库或迁移证据：TokenUsageEvent/counters存在但此分支不写。
- UI 消费证据：runtime usage badge/enterprise budget依赖 persisted summary/counters。
- 测试证据：LoopGuard tests只验 terminal reason。
- 反证或不确定性：InvocationSpan仍保留 response usage，可用于离线修复。
- 修复方案：每次 provider response按 generation span/call id写唯一 TokenUsageLedger；终态聚合，不再分支写。
- 最小化方案：在 _abort_for_loop_guard 前累加当前 response并调用 record_token_usage。
- 需要删除或合并的旧实现：合并十余处分支 record_token_usage。
- 依赖项：provider retry/fallback id、budget outbox。
- 验收标准：所有终态下 span usage=ledger=sum counters，无 duplicate。
- 建议测试：cost/text loop、fallback、cancel、error参数矩阵。
- 建议故障注入：usage写后崩溃、重放、provider重复 response。
- 预计风险：中；错误 dedupe会漏记或双记。

### [STREAM-001] Redis WebChat Stream 是只写不读的伪恢复源

- 所属模块：web_chat_stream_bus、Redis、frontend reconnect。
- 严重级别：P2。
- 当前状态：局部闭环。
- 影响对象：每个 run的 Redis keys、运维理解、内存。
- 用户可见现象：Pub/Sub断开时 stream虽写入却不会补洞；实际由 DB transcript backfill恢复。
- 触发条件：任意 web-chat event发布。
- 输入原子：run event。
- 权威原子：ChatTranscriptEvent才是 durable truth。
- 执行原子：publish同时 INCR、XADD、PUBLISH。
- 证据原子：Redis stream与DB重复保存。
- 恢复原子：forwarder只 pubsub.listen，无 XREAD/XRANGE/cursor。
- 消费原子：没有 stream消费者；keys无 EXPIRE。
- 验收原子：bus tests只验 live publish/forward。
- 断裂位置：Redis durable-looking write → replay consumer。
- 根因：传输日志设计未完成，却保留为长期状态。
- 是否存在双事实源：是；DB Transcript与Redis stream。
- 是否存在治理冲突：否。
- 是否存在跨租户或安全风险：key payload长期留存增加暴露面。
- 是否可能导致 Agent 无法继续运行：否；DB backfill仍工作。
- 源码证据：backend/app/services/web_chat_stream_bus.py:60-95；frontend/src/pages/agent-detail/chatTransportRecovery.ts:46-63。
- 数据库或迁移证据：DB transcript有sequence唯一约束。
- UI 消费证据：onopen/visibility走 transcript backfill。
- 测试证据：stream bus tests无 gap replay/TTL。
- 反证或不确定性：Pub/Sub作为 live hint合理。
- 修复方案：KISS优先删除 XADD/INCR，仅保留 PUBLISH；或真正实现 cursor/XREAD/TTL并明确唯一传输日志。
- 最小化方案：给 stream/seq相同 TTL，终态清理并标注非恢复源。
- 需要删除或合并的旧实现：删除无消费者 stream persistence。
- 依赖项：reconnect contract、monitoring。
- 验收标准：无永久 key；断线恢复只依赖一个明确 durable source。
- 建议测试：forwarder downtime、duplicate、DB backfill consistency、TTL。
- 建议故障注入：Redis restart、Pub/Sub gap、终态清理失败。
- 预计风险：低；删除前确认没有外部未索引消费者。

### [UX-003] Canonical tool_failure 被 UI 显示为 done

- 所属模块：ThreadItem reducer、chat runtime disclosure。
- 严重级别：P2。
- 当前状态：断点。
- 影响对象：失败工具、可恢复错误、总运行状态。
- 用户可见现象：工具卡片/步骤显示完成，实际 backend item_status=failed。
- 触发条件：event_type=tool_failure 或 success=false。
- 输入原子：canonical ThreadItem。
- 权威原子：backend已给 item_status failed。
- 执行原子：frontend tool_result分支固定 toolStatus=done。
- 证据原子：item_data.success=false被丢弃。
- 恢复原子：UI不提供与失败一致的 retry/next step。
- 消费原子：disclosure/timeline按 done计算。
- 验收原子：后端分类和独立 reducer形状未做全链断言。
- 断裂位置：canonical failure evidence → UI semantic status。
- 根因：前端再次推导状态而非消费 item_status。
- 是否存在双事实源：是；ThreadItem status与 toolStatus。
- 是否存在治理冲突：permission/capability denial也可能被美化。
- 是否存在跨租户或安全风险：无。
- 是否可能导致 Agent 无法继续运行：用户可能无法正确恢复。
- 源码证据：frontend/src/pages/session-workbench/threadItemReducer.ts:140-146,413-430；frontend/src/pages/agent-detail/chatRuntime.ts:1130-1148。
- 数据库或迁移证据：ChatTranscriptEvent.item_status是持久状态。
- UI 消费证据：tool card/timeline读 toolStatus。
- 测试证据：缺 user/operator tool_failure end-to-end。
- 反证或不确定性：成功 tool_result正常显示。
- 修复方案：AgentChatMessage保留 running/done/failed/cancelled，机械映射 item_status/success；所有 selectors只读规范化值。
- 最小化方案：tool_result分支按 item.item_status设置 failed。
- 需要删除或合并的旧实现：删除 chatRuntime第二套推断。
- 依赖项：统一 frontend event types。
- 验收标准：tool_failure、capability_denied、cancelled在卡片/步骤/总状态一致。
- 建议测试：user/operator snapshot + reducer + disclosure。
- 建议故障注入：乱序 running→failed、重复 event、legacy payload。
- 预计风险：低。

### [UX-004] Session 与 Lineage 请求失败被伪装为空数据

- 所属模块：AgentDetail session list、branch lineage。
- 严重级别：P2。
- 当前状态：断点。
- 影响对象：历史会话、管理员 all scope、Branch/Fork UI。
- 用户可见现象：网络/401/500显示 No conversations yet；lineage失败时整个 Branch UI消失。
- 触发条件：listSessions或lineage API reject。
- 输入原子：mine/all/lineage request。
- 权威原子：backend DB/API。
- 执行原子：empty catch返回 []，lineage catch set []。
- 证据原子：错误仅 console warning或完全吞掉。
- 恢复原子：无 retry/last-good/stale state。
- 消费原子：empty state和 conditional render把 unknown当 absent。
- 验收原子：只测成功/真实空列表。
- 断裂位置：operational error evidence → user-facing recoverable state。
- 根因：数据模型只有 array，没有 loading/success/error/stale discriminant。
- 是否存在双事实源：否；是 unknown被错误归类。
- 是否存在治理冲突：403可能被伪装无数据。
- 是否存在跨租户或安全风险：无直接泄漏。
- 是否可能导致 Agent 无法继续运行：用户无法选择/恢复历史分支。
- 源码证据：frontend/src/pages/AgentDetail.tsx:526-560,757-784；SessionLineageSurface 对 length<=1 return null。
- 数据库或迁移证据：session/lineage数据仍存在。
- UI 消费证据：No conversations/隐藏 Branch。
- 测试证据：无 401/403/500/offline matrix。
- 反证或不确定性：重新加载成功可恢复。
- 修复方案：query state data/error/isFetching/stale；保留 last-good；401 auth恢复、403权限提示、其他 Retry；只有 success+0才空。
- 最小化方案：catch设置显式 error并显示 Retry，不清空 last-good。
- 需要删除或合并的旧实现：合并 mine/all fetch逻辑到 Query source。
- 依赖项：telemetry/error codes。
- 验收标准：任何请求失败不再显示“没有数据”；恢复后原位置更新。
- 建议测试：mine/all/lineage各状态矩阵、session切换 race。
- 建议故障注入：offline、token过期、slow response后切 Agent。
- 预计风险：低。

### [A11Y-001] Workspace resize 仅支持鼠标

- 所属模块：SessionRuntimePanel、responsive layout。
- 严重级别：P2。
- 当前状态：断点。
- 影响对象：键盘、触屏、辅助技术用户。
- 用户可见现象：无法调整 Workspace宽度。
- 触发条件：非鼠标输入。
- 输入原子：当前只有 MouseEvent/mousemove。
- 权威原子：localStorage panel width偏好。
- 执行原子：onMouseDown resize。
- 证据原子：宽度变化对鼠标成立。
- 恢复原子：保存宽度成立，但无 keyboard/pointer cancel。
- 消费原子：布局读取宽度。
- 验收原子：静态 ARIA测试无真实键盘/触控行为。
- 断裂位置：role=separator semantic → operable interaction。
- 根因：使用 mouse events且缺 aria value/tabIndex/key handler。
- 是否存在双事实源：否。
- 是否存在治理冲突：否。
- 是否存在跨租户或安全风险：否。
- 是否可能导致 Agent 无法继续运行：否。
- 源码证据：frontend/src/pages/agent-detail/SessionRuntimePanel.tsx:728-735。
- 数据库或迁移证据：无。
- UI 消费证据：实际 panel resize handle。
- 测试证据：无 Playwright keyboard/touch。
- 反证或不确定性：窄屏 overlay/bottom-sheet已存在。
- 修复方案：Pointer Events + pointer capture/cancel；focusable separator；aria-valuemin/max/now；Arrow/Home/End；窄屏明确禁用或调高度。
- 最小化方案：补 tabIndex、键盘 handler和 ARIA values。
- 需要删除或合并的旧实现：替换 mouse-only listeners。
- 依赖项：responsive design tokens。
- 验收标准：keyboard/touch/mouse均可调整且持久化，越界受限。
- 建议测试：Testing Library keyboard + Playwright touch/narrow。
- 建议故障注入：pointercancel、resize中切 viewport、localStorage非法值。
- 预计风险：低。

### [SIMPLIFY-001] AgentWorkLedger DB 表无生产消费者

- 所属模块：Work Ledger、schema、RLS。
- 严重级别：P2。
- 当前状态：局部闭环。
- 影响对象：维护成本、迁移/RLS表面、概念事实源。
- 用户可见现象：无直接现象；开发者可能误以为 DB 表是 canonical ledger。
- 触发条件：阅读模型或未来代码选择错误 store。
- 输入原子：DB model存在但无 writer。
- 权威原子：docstring称 future canonical；当前真实 ledger是 runtime_artifacts文件。
- 执行原子：agent_work_ledger.py service/tool/kernel使用文件路径。
- 证据原子：rg仅找到 model定义，无生产 caller。
- 恢复原子：文件 ledger有 resume；DB无数据流。
- 消费原子：DB表无人消费。
- 验收原子：无 contract确保单一事实源。
- 断裂位置：schema existence → production input/write/read。
- 根因：预先泛化的 future table从未落地或退休。
- 是否存在双事实源：潜在；当前只有文件实际写，但命名制造双源。
- 是否存在治理冲突：增加无意义 RLS/migration面。
- 是否存在跨租户或安全风险：低；死表仍被授予 DML。
- 是否可能导致 Agent 无法继续运行：否。
- 源码证据：backend/app/models/work_ledger.py:1-62；backend/app/services/agent_work_ledger.py 为实际服务；skill_distiller.py:1033 的 _cursor_value无调用。
- 数据库或迁移证据：agent_work_ledgers在 strict RLS migration列表。
- UI 消费证据：UI读文件/runtime API，不读该表。
- 测试证据：无 DB ledger消费测试。
- 反证或不确定性：需先查询 production row count，不能无证据直接 drop。
- 修复方案：只读统计+引用审计；若无 rows/caller，migration drop；若有数据，导出/归档后drop；更新 contract明确文件或新 DB二选一。
- 最小化方案：先将 model标 legacy/deprecated并禁止新 writer。
- 需要删除或合并的旧实现：AgentWorkLedger model/table、dead _cursor_value。
- 依赖项：live row inventory、backup。
- 验收标准：唯一 ledger contract；schema/代码/测试不再出现未消费表。
- 建议测试：architecture test扫描 model有 writer+reader或 explicit exception。
- 建议故障注入：历史行存在、旧版本仍写、migration rollback。
- 预计风险：低中；drop前必须验证生产数据。

### [INFO-001] 普通用户看到 raw transcript sequence

- 所属模块：ThreadItemRenderer、information layering。
- 严重级别：P3。
- 当前状态：局部闭环。
- 影响对象：普通用户 session timeline。
- 用户可见现象：过程卡片显示 #123 等机械序号。
- 触发条件：渲染任意 ThreadItem。
- 输入原子：ChatTranscriptEvent.sequence。
- 权威原子：sequence是replay排序权威。
- 执行原子：renderer无 audience gate显示。
- 证据原子：机械编号真实但不属于普通用户必要信息。
- 恢复原子：不受影响。
- 消费原子：user/operator都看到。
- 验收原子：无 user audience隐藏测试。
- 断裂位置：operator evidence → ordinary information layer。
- 根因：sequence显示未复用 operator_details gate。
- 是否存在双事实源：否。
- 是否存在治理冲突：无。
- 是否存在跨租户或安全风险：低；主要是内部实现噪音。
- 是否可能导致 Agent 无法继续运行：否。
- 源码证据：frontend/src/pages/session-workbench/ThreadItemRenderer.tsx:173-189。
- 数据库或迁移证据：sequence必须保留在 DB。
- UI 消费证据：普通卡片无条件显示。
- 测试证据：无 user/operator snapshot差异。
- 反证或不确定性：技术详情按钮已正确 operator gate。
- 修复方案：仅 operator/diagnostic显示 sequence；普通用户显示语义状态、时间、下一步。
- 最小化方案：加 item.audience === operator 条件。
- 需要删除或合并的旧实现：无需删除数据，只调整显示。
- 依赖项：audience contract。
- 验收标准：user view无 #sequence，operator仍可查。
- 建议测试：user/operator snapshots。
- 建议故障注入：legacy item无 audience。
- 预计风险：低。

## 13. P0/P1 阻塞上线项

### 13.1 P0 release gate

| Gate | 关闭条件 | 当前证据 |
|---|---|---|
| SEC-001 production RLS | strict migration在 production成功；所有 strict tenant表 NULL=0；三服务同一 current commit；live cross-tenant matrix通过 | 未关闭 |
| SEC-002 recovery isolation | session-scoped checkpoint；A/B压缩/重启/fork测试；legacy权限帧fail closed | 未关闭 |
| SEC-003 outbound SSRF | Personal KB/web/MCP/hooks共用 endpoint policy；private/redirect/DNS/max-bytes矩阵通过 | 未关闭 |
| SEC-004 AgentTool secrets | writer统一加密；API全mask；legacy dry-run/backfill/rotation；DB无明文 | 未关闭 |

任何一个 P0 未关闭，都不能宣称生产安全闭环。

### 13.2 P1 release gate

- GOV-001：BYPASS durable audit与 audit evidence immutability。
- RUNTIME-001：真实 app_rls delegation worker tenant propagation。
- RUNTIME-002：所有 supported RuntimeTask 的 failure terminal/reclaim矩阵。
- TOOL-001/002：外部副作用 receipt、unknown reconciliation、禁止自动重放。
- APPROVAL-001：decision/attempt 分离与 exactly-once continuation。
- TRIGGER-001：immediate fire DB idempotency。
- ARTIFACT-001：A2A artifact authority + parent UI/content/download E2E。
- UX-001/002：cancel/upload的可恢复用户路径。
- DATA-001：production quarantine operator lifecycle。
- SESSION-001：explicit WebSocket session fail closed/canonical handshake。

## 14. 双事实源与有意分工清单

### 14.1 必须收敛的错误双源

| 概念 | Source A | Source B | 风险 | 收敛目标 |
|---|---|---|---|---|
| tenant isolation | current strict migration | production old NULL policy | live cross-tenant暴露 | production schema/policy唯一 |
| recovery | safe hydrate+session match | post-compact direct load | 跨 session泄漏 | session checkpoint loader唯一 |
| runtime UI state | RuntimeTask/Transcript | React state/ref/locallyTerminal | 假终态 | reducer派生只读 projection |
| tool idempotency | ToolMeta声明 | provider实际无 key/receipt | 重复副作用 | ToolExecutionReceipt |
| permission | decision event | execution retryable response | 409矛盾 | Decision + Attempt |
| A2A artifact | source ChatArtifact | random projected UUID | 404/越权 | governed reference/real row |
| MCP config | Tool.config | AgentTool.config | 相同工具不同结果 | typed resolver |
| usage | InvocationSpan usage | TokenUsage counters | budget低估 | generation-id ledger |
| streaming durability | ChatTranscriptEvent | unread Redis XADD | 假恢复源/泄漏 | DB durable truth |
| Work Ledger | runtime_artifacts file | unused agent_work_ledgers table | 维护误导 | 选择一个并删另一个 |

### 14.2 正确的分层，不应错误合并

- ChatTranscriptEvent 是 cloud run/session transactional authority；T0 events.jsonl/source.md 是 exactly-once portable Memory evidence projection。两者用途不同。
- Workspace 文件是内容事实源；WorkspaceResourceManifest 是 ownership/authority事实源。
- Native Agent/Skill/Workflow 等仍是内容/执行 authority；AIAssetRecord 是企业控制 index。
- ChatArtifact snapshot是交付时不可变证据；workspace current file是当前可变内容。UI必须明确 snapshot/current，不应强行只留一个。
- Redis Pub/Sub可以保留为可丢 live hint，但不能冒充 durable replay。

## 15. 治理、RLS 与运行冲突清单

| 冲突 | 治理意图 | 实际运行后果 | 唯一修复点 |
|---|---|---|---|
| old NULL RLS | 兼容 legacy | NULL全局可见 | migration/policy |
| delegation tenant ContextVar | fail-closed RLS | 合法 Worker读不到 target | explicit tenant session |
| BYPASS log only | 跨 tenant后台枚举 | 无 durable actor/decision | enter_rls_bypass |
| audit_logs全 DML | 通用 app role | 审计可篡改 | DB grants/trigger |
| permission decision先 commit | 保留用户授权 | tool失败不可重试/unknown | execution attempt |
| immediate lease advisory | 用户立即运行 | 重复副作用 | fire intent unique key |
| AgentTool plaintext | Agent级定制 | credential泄露 | config write service |
| egress ability gate | 允许 Web/KB/MCP | 可访问内网目标 | endpoint policy |
| unsafe frame提示模型 | 防自动 replay | 模型仍可重发 | durable reconcile gate |
| quarantine无 consumer | fail-closed隔离 | 合法历史不可恢复 | operator remediation |

唯一治理决策点不是一个巨型函数，而是一条不可绕过的顺序：

~~~mermaid
flowchart LR
  I["Trusted identity + tenant"] --> R["RLS pin"]
  R --> C["Capability/asset policy"]
  C --> P["Plan/approval decision"]
  P --> E["Immutable execution receipt"]
  E --> T["ToolRuntimeService / Worker"]
  T --> S["Side-effect evidence"]
  S --> A["Artifact/resource authority"]
  A --> U["Transcript + user/operator UI"]
~~~

## 16. 无消费路径的代码、表、API 和组件

| 类型 | 对象 | 当前消费者 | 结论 |
|---|---|---|---|
| 表/模型 | agent_work_ledgers / AgentWorkLedger | 仅模型定义，无生产读写 | 审计 live rows后迁移并删除 |
| 表/模型 | tenant_scope_quarantine_records | audit count有读；无 remediation consumer | 补 operator lifecycle，不是直接删除 |
| Redis | web_chat_run stream + sequence key | 无 XREAD/XRANGE | 删除或完成 replay+TTL |
| 服务 | TeamMemoryStore search_entries | 只有 HTTP API/UI，无 Agent runtime/tool | 增加工具或改名便笺 |
| schema能力 | KnowledgeDocument company scope | current writer/searcher均 person | 已知缺失，不能算完成 |
| helper | skill_distiller._cursor_value | 无调用 | 删除并加 dead-code check |
| UI字段 | ThreadItem.sequence普通视图 | 用户看到但不需要 | 只留 operator |

没有把 ChatArtifact snapshot、T0 projection、WorkspaceResourceManifest 列为“重复”，因为它们有明确不同消费职责。

## 17. 应删除、合并或收敛的抽象

| 动作 | 对象 | 为什么不破坏能力 | 迁移与风险 | 应补契约测试 |
|---|---|---|---|---|
| 合并 | 两个 recovery manifest reader | 安全 loader已存在 | session path/backfill | compaction/resume/fork matrix |
| 合并 | Tool/MCP config resolver | dynamic path已有正确方向 | merge优先级/secret migration | generic/dynamic等价 |
| 合并 | frontend runtime state writers | DB/Transcript仍为 authority | reducer迁移防乱序 | property/event ordering |
| 合并 | upload file/paste flows | API相同 | partial/abort语义 | XHR batch |
| 删除 | unread Redis stream persistence | DB backfill已消费 | 先查外部 consumer | downtime/reconnect |
| 删除 | agent_work_ledgers dead table | 文件 ledger是真路径 | live inventory/backfill | architecture consumer test |
| 删除 | random A2A projected artifact ID | source artifact仍在 | 改 governed reference | real download |
| 删除 | direct AgentTool config writes | 统一 service替代 | secrets backfill | DB/API/runtime round trip |
| 重命名或补消费 | Shared Team Memory | 防虚假产品承诺 | 选择工具或 Notes | Agent consumption E2E |
| 删除 | _cursor_value dead helper | 无 caller | 低风险 | lint/dead-code |

## 18. 已知缺失与明确排除项

### 18.1 已知缺失

- Company/Enterprise Knowledge Base：已知缺失。Personal KB、generic schema、legacy company files均不能冒充。
- 全类型 Enterprise AI Asset lifecycle：只有五类局部闭环，其余类型是能力缺口。
- 多副本 production行为验收：当前每服务1 replica，仅有源码/测试，不是现网闭环证据。
- 登录后的真实 production UI任务旅程：因无授权凭据且不创建账号，本轮未验证；需用户提供安全测试 tenant或自动化 fixture。
- Quarantine operator recovery：缺失。

### 18.2 排除

- FreeCode/Claude/Codex 服务商私有远程执行、托管 planning session、不可访问 first-party cloud能力：排除，不计 Hive parity债务。
- 本地 CLI 的文件、session、transcript、tool、permission、compaction、resume语义不排除；Hive必须用 Web/API/Worker形态映射。
- 本报告不要求机械复制 FreeCode UI；只要求同等级的事实源、恢复与用户表达。

## 19. 单轮完整落地方案

以下是一个完整 release 内的依赖波次，不是 MVP/分期欠债。任何波次都不能作为“先上线一半”的理由；最终 release 必须一起通过第 21 节验收。唯一需要额外确认的是生产数据 quarantine/rebind，这是不可逆操作的安全门。

### Wave 0：立即 containment 与生产事实统一

1. 冻结生产高风险变更，保存 schema/policy/NULL counts 与备份。
2. current checkout 的 tenant-null migration先 dry-run；对 2301 residual生成 payload-free inventory。
3. 将 backend、backend-api、frontend构建自同一 commit；migration失败、schema version不匹配必须阻止健康。
4. 先关闭 production NULL global visibility，再完成 quarantine。
5. 临时禁用 Personal KB URL import/core direct fetch的 private/reserved目的地；禁止 AgentTool API回显敏感键。

文件：

- backend/entrypoint.sh
- backend/alembic/versions/tenant_null_semantics_0712.py
- backend/app/scripts/audit_tenant_null_semantics.py
- backend/app/services/personal_knowledge_service.py
- backend/app/services/agent_tool_domains/web_mcp.py
- backend/app/api/tools.py

### Wave 1：身份与事实源

1. 新建 session recovery checkpoint model/migration，键 tenant/agent/session/run/claim_version。
2. RecoveryManifest initial/post-compact共用唯一 loader。
3. AgentToolConfigService成为唯一 config writer/reader；legacy secrets dry-run/backfill。
4. OutboundEndpointPolicy成为 Personal KB/Web/MCP/hooks唯一 endpoint决策。
5. audit/evidence表撤销 app_rls UPDATE/DELETE；BYPASS写 durable actor receipt。

### Wave 2：状态机与执行入口

1. 新建 ToolExecutionReceipt/PermissionExecutionAttempt/TriggerFireIntent。
2. 所有外部副作用由 ToolRuntimeService通过 receipt执行。
3. RuntimeTask Worker wrapper共用 failure finalizer与 reclaim/dead-letter contract。
4. session permission、trigger immediate、MCP generic全部进入统一状态机。

### Wave 3：权限、恢复与幂等

1. fail-before/after-side-effect显式分类。
2. unknown必须 RuntimeTask needs_reconciliation + operator gate。
3. generation usage按 call/span id即时幂等记账。
4. Personal KB payload迁至 tenant-scoped blob store。
5. Quarantine提供只读 inventory，再经确认完成 rebind/delete。

### Wave 4：Artifact/Workspace 与多 Agent返回

1. 设计 governed ArtifactReference 或真实 projected ChatArtifact。
2. A2A/Sub-agent return contract绑定 source/delivery agent、root session、tenant与快照。
3. ThreadItem所有类型统一提取 safe ArtifactRef。
4. 父 Agent、Session附件、Workspace、content/download一次性闭环。

### Wave 5：UI 信息架构

1. 单一 session runtime reducer，输入只有 canonical Transcript、active-run observation和UI intent。
2. cancelling/error/stale/partial-success成为一等状态。
3. 上传统一 controller。
4. user/operator信息分层；隐藏 sequence；tool failure保真。
5. keyboard/touch/窄屏真实 Playwright验收。

### Wave 6：能力边界与清理

1. Team Memory二选一：Agent工具闭环或重命名 Notes。
2. Company KB保持已知缺失，除非一次性建设完整 vertical slice。
3. AI Asset改名为五类边界或补齐声明类型。
4. 删除 dead DB ledger、unread Redis stream、dead helper和旧 adapter。
5. 删除所有迁移兼容 writer/reader，防长期双源。

## 20. 依赖关系与建议实施顺序

~~~mermaid
flowchart TD
  P0["SEC-001 production containment"] --> ID["Identity / RLS / immutable evidence"]
  P0 --> SEC["Outbound + secrets"]
  ID --> REC["Session recovery checkpoint"]
  ID --> RT["RuntimeTask failure finalizer"]
  SEC --> RECEIPT["Tool receipt / permission attempt"]
  RT --> RECEIPT
  RECEIPT --> TR["Trigger/MCP/approval convergence"]
  REC --> MULTI["A2A/subagent return"]
  RECEIPT --> MULTI
  MULTI --> ART["Artifact reference + Workspace"]
  ART --> UI["Single frontend reducer + recoverable UX"]
  UI --> CLEAN["Delete compatibility/dead paths"]
  P0 --> Q["Quarantine inventory"]
  Q --> QR["Confirmed rebind/delete"]
~~~

严格顺序：

1. 身份、tenant、事实源和生产 schema。
2. 状态机与唯一执行入口。
3. 权限/审批/egress/secret冲突。
4. recovery、idempotency、usage。
5. artifact/workspace。
6. multi-agent return。
7. UI information architecture。
8. 删除兼容层和 dead paths。

## 21. 验收矩阵

| 能力 | Input | Authority | Execution | Evidence | Recovery | Consumption | Acceptance |
|---|---|---|---|---|---|---|---|
| tenant isolation | legacy/current rows | non-null tenant | app_rls strict | policy+audit | quarantine | API/Worker | live cross-tenant matrix |
| recovery | session/run checkpoint | tenant+session+claim | single loader | versioned row/file | CAS/atomic | kernel only own session | A/B/fork/restart |
| outbound | URL | endpoint policy | pinned/validated hop | decision span | bounded retry | KB/tool | SSRF matrix |
| secret config | masked input | owner+tenant | unique config service | ciphertext+audit | rotation | runtime decrypt | DB/API/runtime |
| tool side effect | canonical args | approval+receipt | Worker/ToolRuntime | receipt/span | retry/reconcile | model/UI | commit-after-timeout |
| trigger | trigger+event key | DB unique intent | RuntimeTask | intent/task | reclaim | Agent/UI | concurrent fire |
| A2A artifact | child artifact | governed reference | parent projection | source+delivery receipt | snapshot | parent/Workspace | content/download E2E |
| Personal KB | source blob | tenant/owner | index worker | blob hash/job | cross-instance | search/read | A/B data root |
| UI cancel | user intent | RuntimeTask/event | cancel API | cancelled event | retry/reconnect | reducer | reject/delay tests |
| usage | provider response | generation id | ledger writer | span+ledger | idempotent replay | budget/UI | all terminal paths |

关闭“闭环”状态必须同时满足七列；不得用“有 API/表/页面”替代。

## 22. 测试与故障注入方案

### 22.1 新增测试套件

~~~text
backend/tests/security/test_outbound_endpoint_policy.py
backend/tests/api/test_agent_tool_secret_contract.py
backend/tests/integration/test_audit_log_immutability.py
backend/tests/integration/test_runtime_delegation_rls.py
backend/tests/integration/test_tool_execution_receipt.py
backend/tests/integration/test_trigger_fire_idempotency.py
backend/tests/integration/test_team_memory_agent_consumption.py
backend/tests/integration/test_company_knowledge_lifecycle.py
backend/tests/integration/test_personal_knowledge_distributed_import.py
backend/tests/integration/test_mcp_execution_config_equivalence.py
backend/tests/integration/test_a2a_artifact_delivery.py
frontend/src/pages/AgentDetail.cancel.test.tsx
frontend/src/pages/AgentDetail.upload.test.tsx
frontend/src/pages/session-workbench/threadItemFailure.test.ts
frontend/e2e/thread-workbench-recovery.spec.ts
~~~

### 22.2 必做故障矩阵

| 故障 | 预期 |
|---|---|
| API commit用户消息后崩溃 | Worker仍可执行；无重复run |
| Worker claim后崩溃 | fenced reclaim或needs_reconciliation |
| provider副作用提交后timeout | 不自动重发；operator可决议 |
| cancel请求失败 | UI仍running/cancelling，可Retry |
| cancel commit后broadcast失败 | reconnect从DB恢复cancelled |
| compaction A/B并发 | 不跨 session读取 |
| recovery file/row半写 | 原子读旧版或明确reconcile |
| Redis不可用 | durable run/Transcript仍工作，不重复trigger |
| production migration中断 | health不通过，旧服务不接受新schema流量 |
| Personal KB queue实例消失 | 另一Worker从blob继续 |
| A2A source文件删除 | snapshot仍可预览/下载或明确expired |
| tenant quarantine rebind中断 | 事务回滚，receipt不漂移 |
| AgentTool key rotation | API无明文，旧/新Worker可控过渡 |
| public URL 302 private | 拒绝且不保存响应 |
| DNS rebind | connection pin/second lookup拒绝 |
| 前端旧running事件晚到 | terminal state不可回退 |

### 22.3 完整验收命令

~~~text
cd backend
source .venv/bin/activate
ruff check app
pytest tests -q
alembic heads
alembic upgrade head
python -m app.scripts.audit_rls_coverage
python -m app.scripts.audit_tenant_null_semantics

cd ../frontend
npm test -- --run
npm run build
npx playwright test frontend/e2e/thread-workbench-recovery.spec.ts
~~~

Production 只读验收：

~~~text
railway deployment list --service backend --environment production --project dd959a13-19f9-497a-9704-42c310eae230 --limit 1 --json
railway deployment list --service backend-api --environment production --project dd959a13-19f9-497a-9704-42c310eae230 --limit 1 --json
railway deployment list --service frontend --environment production --project dd959a13-19f9-497a-9704-42c310eae230 --limit 1 --json
curl -fsS https://backend-production-326d.up.railway.app/api/health
curl -I -fsS https://frontend-production-0346.up.railway.app/
~~~

生产 apply/backfill/rebind不属于自动验收命令；必须单独 dry-run、备份、确认。

## 23. 残余风险与最终置信度

### 23.1 残余风险

- 即使 current strict migration部署成功，2301 residual的业务归属仍需人工/权威证据决策。
- 外部 provider是否真正支持 idempotency key需逐个确认；不支持者必须本地 outbox/at-most-once + reconciliation。
- 本地 volume不是多 region object store；Workspace/Artifact在未来多 replica/region仍需统一存储或明确单写者。
- Agent Memory T2/T3路径复杂、文件与DB projection很多；本报告验证了主 gate/消费，但未对每种 memory category做真实 LLM行为评测。
- Skill/evolution已有 gate/rollback，但 supply-chain、恶意 package和跨 Agent污染仍需持续 canary/eval。
- authenticated real-browser旅程未执行；UI 70%置信度需测试 tenant提升。
- 本轮没有抽样真实生产 payload；所有 live DB报告均为计数/元数据，只读且不泄露内容。

### 23.2 被明确反驳的推断

并行审查曾提出“session permission resolve 在无卷 backend-api 内联执行工具”。源码 allowlist确实允许该 route，但当前 frontend/nginx.conf 只有 session runs路径进入 backend-api，permissions路径落入 /api fallback的有卷 backend。因此本报告没有把它列为当前生产断点。若未来 Nginx把 permission resolve迁到 backend-api，必须先改为 durable Worker execution。

### 23.3 最终置信度

- 整体：84%。
- 单 Agent：88%，因为核心调用链、全量测试、恢复复现覆盖较强；真实外部 provider副作用未执行。
- Hive Native：80%，因为主要模块均沿生产路径检查，但 Memory所有类别未逐一做行为 eval，Company KB明确缺失。
- 企业治理/安全：92%，因为有 live RLS/NULL/Alembic/deployment证据和直接源码复现；未读取生产 payload。
- UI/UX：70%，因为消费链与测试覆盖广，但没有 authenticated生产浏览器旅程。

提高置信度所需的最小补充证据：

1. 提供隔离测试 tenant和非敏感 Agent数据，执行登录后的桌面/窄屏/键盘 Playwright旅程。
2. 在 staging或临时 provider使用 fake commit-after-timeout服务做副作用故障注入。
3. 将 current commit部署到受控环境并运行真实 multi-replica Worker、volume/blob和rolling migration测试。
4. 完成 production strict migration后重新采集 NULL=0、policy、quarantine、三服务 commit与健康证据。

最终结论：系统的基础运行时不是“空架构”，但当前生产 P0、恢复越权、出站与密钥边界、外部副作用幂等和 A2A artifact 消费使其仍不满足原子化上线标准。应按第 19—22 节作为一个完整 release 一次性关闭，而不是把其中任一波次包装成可上线 MVP。
