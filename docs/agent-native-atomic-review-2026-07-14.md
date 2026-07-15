# Agent-Native 原子化架构审查报告 — 2026-07-14

> **历史账本提示（2026-07-14）**：本报告保留原始审查证据，但其 `69` 个断点分母和 §20 施工顺序已被 `docs/agent-native-unified-atomic-review-2026-07-14.md` 的四平面纠偏、canonical 对账与统一施工方案取代。后续引用数量、fleet、单根 Session 的 100-way fan-in、400 Skill/200 MCP、Session truth、跨渠道 A2A 与最终整改顺序时，以统一报告为准；不得把本文的 `69` 继续当当前总数。
>
> 依据 `docs/reusable-agent-native-atomic-review-prompt.md` 执行的一轮独立、证据驱动的只读审查。
> 原始审查基线:工作树 `HEAD 33fbecd9d`(commit "Commit Office authority before create response")。`4b9e96820..33fbecd9d` 的 5 个提交仅涉及 OfficeCLI/ONLYOFFICE 退役与 Artifact 预览链,未改变原轮次复核的 Agent-Native 核心路径。
> 本次 D-KB1 校正基线:当前 checkout `HEAD 501db6555` + 当前未提交工作树。补证涉及的源码按工作树实际字节读取;由于工作树含未提交改动,该 HEAD 仅作为 commit anchor,不得被解释为所有证据文件均等于该 commit。本次没有把整份报告冒充为已在新 HEAD 全量重跑。
> 综合复核来源:`docs/agent-native-atomic-review-501db655.md`。本报告保留该 session 的稳定 HEAD / production / dirty-worktree 三层快照边界,但按本仓库五态定义重新去重和定级;该报告的“18 个根因”不能直接与本报告旧 66 相加。
> 方法:原审查由 8 个领域审查员并行 → 4 组对抗验证员 refute-first 复核全部 P0/P1 候选 → 主审合成;随后以当前 checkout、Railway production health、生产 PostgreSQL 只读目录查询和针对性测试做第二轮校正。审查与补充核验未修改业务代码、数据库、部署配置或生产数据;本文件已按补充证据同步修订。
> Artifact 状态:本文件当前受 `.gitignore` 的 `docs/` 规则影响且未被 Git 跟踪;在显式纳入版本控制前,它不是可追溯的仓库事实源。

---

## 1. 执行摘要

本轮审查**未发现导致核心 Agent 完全不可运行的系统性 P0**;单 Agent 内核执行主链的四大历史命门(跨进程取消、compaction 语义截断、运行任务恢复幂等、单一运行事实源)在当前源码均已闭环。但按七原子标准,不能把单 Agent 全能力整体判为闭环:终态交付仍被同步 T2 hook 阻塞,失败路径仍会把平台文案持久化为 assistant,恢复授权也存在跨会话断点。系统整体应判为**局部闭环——内核强、权威与交付边界有洞**。

两份报告统一去重后的**当前快照工作账本**是 **69 个断点:P0 1、P1 9、P2 29、P3 30**。账务计算式为 `旧库存 66 - C-BP7(应归已知缺失) + 4 个第二报告独有断点 = 69`;该映射与算术已经复核,但 69 仍绑定本报告的多 HEAD/dirty-worktree 快照与分类规则,不是在一个冻结 checkout 上从零逐条重新认证后的永久分母。P0/P1 有当前源码直证;P2/P3 必须在各自施工前按 Group 0 重验。Enterprise Knowledge、行为级自进化 eval、AI Asset 未覆盖类型、retention/deletion/export/legal hold 单列“已知缺失”,不混入断点总数。

**最终施工裁决**:单个断点/同根家族一旦自身七原子、Red→Green、迁移/回填、回滚和发布验收闭合,即可独立 commit、部署并标“单项闭环”;P0/P1 不等待 P2/P3 或工作账本复核。`69/69` 只表示当前快照程序账本全部关闭。`C-BP7` 行为级自进化 eval 仍按五态归“已知缺失”,但因其属于 Goal 1 核心能力,成为独立的 **Goal 1 / North Star 完成声明门**:它不阻塞 P0/P1/P2 安全、正确性和恢复修复的施工或发布,但未闭合时禁止宣称 Goal 1 / North Star 已完成或把 Goal 2/UI/KISS 升为主建设方向。Enterprise Knowledge、AI Asset 未覆盖类型及 retention/deletion/export/legal hold 继续保留在 Goal 2 Missing ledger,不得借单项闭环、69/69 或 C-BP7 闭环冒充整个产品无已知缺失。

确证的上线阻断项(经对抗验证 CONFIRMED):

- **1 个 P0(安全)**:`web_fetch` SSRF —— agent 单次工具调用即可无前置触达云元数据 / 内网 / localhost,防护代码已存在却未复用。
- **P1 安全/权威家族**:durable 后台子代理身份被 creator 顶替(E-1);A2A outer permission profile/runtime frame 没有进入 custom executor(P1-004);SEC-002 恢复授权校验缺席(P1-F4);PKB 跨 principal sensitivity clearance、typed provenance 与持久化传播契约缺失(D-KB1)。
- **P1 运行时/证据家族**:TURN_STOP / TRIGGER_END / DELEGATION_END hook 同步 await T2 LLM(F-MEM);Memory storage/resident 故障被误当成全局 effect authority(P1-008);production transcript→T0 曾在 outer commit 前 publish,当前 dirty after-commit 修复尚未形成 clean/full/deployed 验收(P1-017)。
- **P1 表达权威断点**:平台错误文本既在前端冒充 agent 气泡,又在部分 backend failure path 持久化为 durable assistant + `includes('expired')` NL 子串推硬状态(G-01)。
- **P2 用户消费面**:AST 复扫 203 个 production TS/TSX 文件得到 1,905 个 unique literal `t()` key,其中 280 个中英文双缺(G-02);Messages 页已读接口 404 且 read schema 缺席(H-404a)。两项都是真断点,但不应抬成 P1 上线安全阻断。
- **部署纪律 P1/P2**:`entrypoint.sh` schema drift fail-open(降 P1);`CORE_DAEMON_STARTUP_ENABLED` 默认 False 且不在任何 env 样例中,自托管部署整条 Goal-1 自进化车道静默全暗(P2)。
- **证据消费 P2**:`runtime_control_bus.last_error` 在后续投影成功时不清空,生产 health 可持续展示已经消失的 T0 projection 错误(F-OBS1)。当前生产数据库实测 `243660 projected / 0 nonterminal`,因此这是 health 诚实性断点,不是 T0 数据丢失。

**五个旧审计结论被当前源码推翻**(见 §19):A8"子代理结果不回父"、A7"进化/编排 daemon 生产不跑"、A11"凭据明文"(大部分)、07-03"流式渲染无节流"、07-13"retrieval seam 恒空 / 激活方程待实施"。

**AI-Native / Model Agency 成功回答主路径合规度高**:未发现平台改写/压制成功模型终答的 P0 违规,compaction 四问全过,记忆三门边界守得住,fallback 合约成立。但失败路径不干净:`_handle_web_chat_failure` 把平台固定 `user_visible_error` 经 `finalize_with_assistant` 写成 durable assistant,叠加前端冒充气泡与 NL 子串硬状态,构成 G-01 P1。A-01/B-03 仍为 P2 机械 hard outcome。

---

## 2. 审查范围、环境与未覆盖范围

**已覆盖(生产路径正向+反向双追踪)**:单 Agent 运行主链、工具治理咽喉七阶、Memory T0/T2/T3/soul + 自进化链、Personal KB tool-only 全链、多智能体 spawn/delegate/team/A2A、编排 workflow/trigger/schedule/background、企业 RLS/身份/审计/AI 资产、UI 信息分层 + Artifact 九环、全仓 KISS/无消费者扫描。

**环境证据**:Railway 生产环境变量与 health 经只读实测(不引用凭据值),确认 `backend` 服务 `CORE_DAEMON_STARTUP_ENABLED=true` + `backend-api` 服务 `HIVE_PROCESS_ROLE=api` + worker 关 —— 单写者双进程拓扑正确;`backend`、`backend-api`、`frontend` 最新 deployment 均为 `SUCCESS`。生产 runtime DB role 为 `app_rls`,`rolsuper=false`,`rolbypassrls=false`,`RLS_RUNTIME_ROLE_ENFORCEMENT=strict`;129 张 public table 均由 `postgres` 持有。`force_all_tenant_rls_0615` 实际声明 68 张表,生产现存 67 张,67/67 均 `ENABLE + FORCE RLS`。

**未覆盖 / 降置信**:未起真实浏览器端到端走查(前端结论基于源码 + vitest 实跑);生产 PostgreSQL 已完成 role/owner/RLS catalog 只读核验,但未做跨租户行为探针或故障注入;外部消费面(webhooks/oidc/mcp_oauth/llm_proxy/desktop_*/渠道 webhook 回调/admin 运维 curl)只判"前端零调用";~290 动态构造 i18n key 未证实;`skills-lock.json` 外部消费、`connector_acl` `onlyoffice://` 保留意图未证实。

**补充核验命令与结果**:

```bash
cd backend && source .venv/bin/activate
pytest tests/services/test_subagent_run_service.py \
  tests/runtime/test_recovery_manifest_persistence.py \
  tests/memory/test_t2_segment_package_builder.py \
  tests/services/test_personal_knowledge_service.py \
  tests/tools/test_personal_knowledge_tool.py \
  tests/services/test_chat_transcript.py \
  tests/services/test_web_chat_runtime.py -q
# 238 passed in 8.33s

pytest \
  tests/services/test_runtime_task_worker.py::test_execute_claimed_business_task_passes_cross_process_cancel_to_kernel \
  tests/services/test_conversation_summarizer.py -q
# 11 passed in 2.41s

cd ../frontend
npm test -- --run \
  src/pages/agent-detail/sessionSocketEventProjector.test.ts \
  src/pages/agent-detail/ArtifactSurface.test.tsx \
  src/pages/agent-detail/AgentDetailSections.test.tsx
# 3 files passed, 114 tests passed

cd ../backend
source .venv/bin/activate
pytest \
  tests/api/test_office_preview.py \
  tests/services/test_office_document_service.py \
  tests/services/test_chat_artifact_delivery.py -q
# 59 passed in 7.93s

cd ../frontend
npm test -- --run src/pages/agent-detail/ArtifactSurface.test.tsx
# 1 file passed, 3 tests passed
```

绿色测试证明已覆盖的正向契约仍成立,不反证本文断点;其中 `sessionSocketEventProjector.test.ts` 还明确钉住了当前 error→assistant 的错误行为。本次补充复核未运行全量 backend/frontend suite,因此不作全仓测试通过声明。

SSRF validator 只读诊断实测 `_looks_like_url()` 对以下 URL 全部返回 `True`:`http://169.254.169.254/latest/meta-data`、`http://127.0.0.1:8000/admin`、`http://10.0.0.1/internal`、`https://example.com/`。未向元数据或内网地址发起真实请求。

生产只读证据:三服务最新 deployment 均为 `SUCCESS`;health 的 RLS component 为 `status=ok,role_name=app_rls,superuser=false,bypassrls=false,enforcement=strict`;PostgreSQL catalog 查询得到 public table owner=`postgres`(129 张),`force_all_tenant_rls_0615` 声明 68 张、生产存在 67 张且 67/67 ENABLE+FORCE;`chat_transcript_events` 为 243660 projected、0 nonterminal。health 中残留的 LookupError event 在数据库中不存在,证明 F-OBS1 是陈旧 health 状态而非未投影数据。

---

## 3. 权威顺序与北极星符合性

裁决顺序(北极星 > AI-Native/Model Agency > CC/FreeCode 语义 > Codex 增量 > Hive Native > 企业治理 > 七原子 > KISS)作为定级依据。

- **Goal 1(数字员工内核)**:运行主链、工具治理、记忆金字塔、自进化 promotion/rollback/audit 三环达可用闭环,内核质量对齐 CC 并在多处(云运行权威、durable resume、跨进程取消)超越。**削弱点**:TURN_STOP 内联阻塞损害"流畅"体感;CORE_DAEMON 默认暗使自托管环境自进化车道整体不可见。
- **Goal 2(控制中台)**:workflow 两人审批、配额预算三档硬顶+四维熔断、AI 资产控制平面(5 类真闭环)、身份链是强项。**债务区**:恢复路径身份契约缺席、审计表非不可变、retention/deletion/legal hold 整体缺失、model/soul/knowledge/eval/memory-policy 5 类资产未进控制平面。
- **AI-Native Design Law**:L1(释放模型)在记忆/压缩/自进化主路径守得住;L2(约束不替代)边界清晰;L3(模型平等)未发现供应商偏袒。

---

## 4. 仓库与运行拓扑

```
Frontend (React 19)
   │  /api、/api/v1 双前缀(main.py:886-887);/ws/chat 无前缀
   ▼
API 进程 (backend-api, HIVE_PROCESS_ROLE=api, CORE_DAEMON=false, worker=false)
   │  chat_sessions/websocket 创建 RuntimeTask(pending) → Redis wakeup
   ▼
Runtime 进程 (backend, CORE_DAEMON=true)
   │  runtime_task_worker claim(SKIP LOCKED + lease + claim_version)
   │  daemon: trigger(15s)/workflow/evolution + runtime_budget + channel_ingress
   ▼
invoke_agent() → AgentKernel.handle()(stateless)
   ▼
ToolRuntimeService.execute() → run_tool_governance()(七阶咽喉)
   ▼
PostgreSQL(asyncpg+RLS) + Redis(控制总线) + Agent Workspace(本地盘/Railway volume)
```

**双进程真值**:`ChatTranscriptEvent`(DB 事务)是云端 run 排序/resume/replay/fork 唯一权威;T0 session ledger 是 exactly-once 证据投影(非第二运行权威);UI 经 Redis stream forwarder 从 worker 转发事件 + transcript 重放。

---

## 5. 核心实体、状态机与事实源矩阵

| 能力 | 谁写 | 权威事实源 | 谁消费 | 恢复 |
|---|---|---|---|---|
| Run 排序/resume/fork | web_chat_run_orchestrator | `ChatTranscriptEvent`(advisory-lock 串行 sequence) | resume/replay/fork | DB |
| 运行状态 | runtime_task_worker | `RuntimeTask.status`(killed 保留语义) | UI/reconcile | claim+lease+fence |
| Memory 证据 | transcript→T0 桥 + heartbeat/dream/subagent 直写 | `events.jsonl`(O_APPEND+fsync+hash chain) | T2 打包/replay | sweeper 重投;生产当前 0 个 nonterminal projection |
| T2 语义 | 5 类终局 hook | Segment Package manifest(原子提交) | T3 Consolidator | startup 归一 + heartbeat 重试≤3 |
| T3 语义 | heartbeat 直调 T3 core(LLM×3 门) | `AgentAssetTransaction`(字节忠实+幂等) | prompt 常驻+检索 | held→下 tick |
| 工具执行决策 | ToolRuntimeService | decision ledger + invocation_spans | 审计/UI | — |
| 审批 | approval ticket(DB immutable trigger) | 不可变 ticket + input_hash | execute_approved | outbox 桥回 |
| 子代理结果 | subagent_run_service | RuntimeTask 终态 + completion_journal | outbox→父续跑 | reconcile_terminal_tasks |
| Artifact | workspace/office service | chat_artifacts 表 + 字节快照 | 父 agent/UI | 快照 30 天保留 |

**状态枚举三轨(KISS 债)**:`runtime_tasks` 9 态(completed/killed) vs `tasks` 7 态(done/cancelled) vs `ThreadItemStatus` 5 态(succeeded/cancelled);前端 `chat.ts:238` 手写 runtime task 状态 union(带 `| string` 逃生舱)与后端 CHECK 约束平行维护。

### 5.1 P0/P1 与关键生产事实七原子矩阵

> `✅`=当前真实消费路径成立;`⚠️`=主路径存在但有双事实源、旁路、恢复或验收断点;`❌`=原子断裂。最终状态只使用闭环/局部闭环/断点/缺失/排除。

| 能力/断点 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 | 最终状态 |
|---|---|---|---|---|---|---|---|---|
| P0-F1 `web_fetch` 网络访问 | ✅ Agent tool args | ❌ core/safe tool 无目标网络权威判定 | ❌ `follow_redirects=True` 直发,无 DNS/IP/redirect 私网门 | ⚠️ 有调用结果,无 egress authority receipt | ⚠️ HTTP 失败可返回,无 redirect/rebinding 恢复判定 | ✅ 模型消费抓取结果 | ❌ 无 SSRF 私网/重定向回归 | **断点(P0:权威→执行)** |
| E-1 durable subagent 身份 | ✅ enqueue 已持久化 `root_user_id` | ❌ dispatch 用 `agent.creator_id` 顶替 requester | ⚠️ 唯一 RuntimeTask worker 路径真实运行,但 principal 错 | ❌ 审计/审批/T0 actor 按错误 principal 记账 | ❌ restart/resume 重建继续取 creator | ❌ HR-agent PKB/工具/审批消费错误 user | ❌ 缺 creator≠requester 回归 | **断点(P1:权威)** |
| P1-004 A2A execution frame | ✅ outer runtime 持有 `permission_profile`/principal/session | ❌ custom executor signature 只接 `emit_runtime_hooks` | ❌ invoker 按 signature 注入 frame,因此 profile/principal/sandbox kwargs 被静默丢弃 | ⚠️ outer metadata 声称有 profile,inner receipt 无同一 policy hash | ❌ restart/retry 重复使用同一缺 frame executor | ❌ target Agent effect 只剩 global governance,未受 parent/session 收窄 | ❌ tests 只验 profile 到 orchestrator及 A2A depth,不验 effect boundary | **断点(P1:权威→执行)** |
| P1-F4 RecoveryManifest | ✅ canonical+legacy manifest 可加载 | ❌ legacy 无 session_id fail-open;无六元绑定 | ❌ post-compaction 路径跳过 `matches_session` | ⚠️ 有 manifest,但 provenance/owner 不足 | ❌ 跨会话/agent 恢复不 fail-closed | ❌ prompt/hydration 可消费错会话状态 | ❌ 缺伪造/跨会话故障注入 | **断点(P1:权威→恢复)** |
| F-MEM T0→T2 终局 | ✅ sealed T0 segment | ✅ tenant/agent/session 输入可得 | ❌ terminal hook 同步 await 多次 LLM | ✅ task body 开始后有 deterministic manifest | ⚠️ running/held 可 sweep;裸后台化存在 pre-start 无 manifest 窗口 | ⚠️ T2/T3 真消费,但 done 与 worker 槽被阻塞 | ❌ 缺延迟与 queued-before-start crash 回归 | **局部闭环(P1)** |
| P1-008 Memory effect freeze | ✅ memory status 有 storage/authority/resident error code | ❌ storage、identity、principal authority 被合成一个 boolean | ❌ `external_effects_available=false` 冻结所有 non-read-only tools | ✅ typed unavailable 有 event/span | ⚠️ Memory 恢复后可重试,但无 dependency-specific resume | ❌ 与 Memory 无关的 approved Workspace/Office/message effect 也被拒 | ❌ 当前测试反而钉住“任意 memory authority failure 冻结 mutation” | **断点(P1:权威→执行)** |
| G-01 失败表达 | ✅ typed error/quota/runtime exception | ❌ 平台错误越界成为 agent 表达 | ❌ backend 写 assistant row,frontend 再塑 assistant;NL `expired` 推硬状态 | ❌ canonical role/actor 标成 assistant,仅 forensic metadata 可能保留平台来源 | ⚠️ session 可重放,但会重放错误作者语义 | ❌ UI/composer/后续模型看到错误角色 | ❌ 现有 projector 测试反而钉住坏行为 | **断点(P1:权威→消费)** |
| G-02 会话 i18n | ✅ 1,905 个 unique production literal key | ⚠️ locale 资源是权威但双缺 280 key | ⚠️ 依赖 raw key/内联 fallback | ⚠️ AST 扫描可复现,无 build gate | ⚠️ 无资源级兜底/修复提示 | ❌ 核心治理/恢复旅程可显示 raw key/fallback | ❌ 无 missing-key 双语 gate | **断点(P2:消费→验收)** |
| D-KB1 PKB sensitivity | ✅ tool-only search/read 携 requester/agent/session,文档有 sensitivity | ❌ 无 requester-bound `sensitivity_ceiling`;owner-agent 分支可脱离当前 requester | ❌ search/read 无 sensitivity decision;canonical `PL3_sensitive` 与 extraction blocklist 枚举漂移 | ❌ full tool result 进入 transcript 并被送往 T0,无 Knowledge sensitivity receipt/typed provenance | ⚠️ replay pointer 只保护下一轮模型回放;重试仍仅重算 owner/grant | ❌ owner-direct 可能符合产品意图,但 shared/subagent 路径及 T2/outbound 无统一 ceiling | ❌ 缺 owner/cross-user/HR background/PL3/PL4/传播矩阵 | **断点(P1:权威→证据→消费)** |
| H-404a Messages read state | ✅ UI 发出单条/全部已读 PUT | ⚠️ 用户/agent scope 可求,但无 read-state authority model | ❌ backend 无两条 PUT 路由 | ❌ 无 read mutation/event,unread 固定 0 | ❌ mutation 无 `onError`,无幂等恢复 | ❌ UI 永不落已读且 payload 无 `read_at` | ❌ 无接口/页面验收 | **断点(P2:执行→消费)** |
| entrypoint Alembic | ✅ owner schema URL + deployment startup | ✅ migration owner authority | ❌ `alembic upgrade head` 失败后仅 echo 并继续启动 | ⚠️ 仅 warning,无 deployment hard-failure receipt | ❌ schema drift 时无 rollback/stop | ❌ runtime 可消费不完整 schema | ❌ 缺迁移失败启动门测试 | **断点(P1:执行→恢复)** |
| P1-017 transcript→T0 commit | ✅ transcript event + pending projection row | ✅ `ChatTranscriptEvent` 是 cloud truth,T0 是 projection | ❌ production snapshot 的 wakeup 早于 outer commit;dirty after-commit fix 未提交/未全量验收/未部署 | ⚠️ pending+sweeper 保留恢复证据,health 曾记录 visibility race | ✅ sweeper 可最终投影,但 wakeup/health 仍漂移 | ⚠️ T2/T3 最终可消费,时效与 health 诚实性受损 | ❌ 本次混合定向集合 8 tests 绿,after-commit 仅局部覆盖;无 clean full-suite + deployment closure | **断点(P1:执行→验收)** |
| F-OBS1 T0 projection health | ✅ projection failure/success state | ✅ DB projection status 是机械权威 | ✅ 生产当前 243660 projected/0 nonterminal | ❌ 进程 `last_error` 成功后不清空,与 DB 真相漂移 | ✅ sweeper 已恢复数据投影 | ❌ health 消费陈旧故障 | ❌ 缺 failure→success health 回归 | **局部闭环(P2)** |
| 生产 RLS | ✅ runtime 以 `app_rls` 连接 | ✅ runtime≠owner,非 superuser/BYPASSRLS,strict guard | ✅ 现存 migration 清单表 67/67 ENABLE+FORCE | ✅ health + PG role/owner/catalog live evidence | ✅ startup strict guard 可阻断危险 role | ✅ runtime DB 路径真实消费 RLS | ⚠️ 缺跨租户行为探针/迁移遗漏 fault injection | **局部闭环(主路径闭合,验收待补)** |

---

## 6. 单 Agent 结论(领域 A)

**状态:内核执行主链闭环,单 Agent 全能力按七原子为局部闭环。**

当前证据确认:跨进程取消主链(Redis 正常时 cancel→本地 event+publish→runtime listener→kernel 三点检查;killed 胜过后到 completed);compaction 四问全过(全字节 coverage chunk+sha256+map-reduce,失败→诚实 trim marker+熔断,`[-40:]`+2500cap 旧违规已根治);运行任务恢复(claim+lease+fence,mutating 工具帧→needs_reconciliation 显式门);terminal ghost 双点对账;终态幂等(final_decision_trace_id 唯一索引);transcript→T0 单一事实源;provider 层(10 次退避+thinking signature 缺失即省略不伪造+output-cap 续写+overload fallback);WS 断线≠取消;SA-09 frozen prefix 每 turn 全量重建后按 rendered-bytes hash 复用。这里的“恢复闭环”仅指 RuntimeTask 执行恢复,不包含 §13 P1-F4 的跨会话 RecoveryManifest 授权缺口。

CC/FreeCode 逐站对比 13 站:主循环/microcompact/轮次预算/stop hook/工具并发/工具结果预算/空响应对齐或 Hive 增强;**唯一语义差异 = compact 边界不回写重放路径**(A-03)。

单 Agent 断点:A-01(P2)、A-02(并入 F-MEM 家族)、A-03(P2)、A-04(P2)、A-05~A-09(P3)。

---

## 7. Hive Native 结论(领域 C、E)

**状态:Memory 数据与语义主链主体闭环,终态交付/恢复权威使全能力仍为局部闭环,存 2 个 P1 家族 + 若干债。**

**Memory 当前证据**:ChatTranscriptEvent→T0 exactly-once 的最终投影与 sweeper 主体成立;T0 append-only(全仓无第二写者);T2 全 LLM-authored + "无 LLM review 即 held" + "intentionally no mechanical summary fallback";T3 Platform Gate 只验机器契约,字节忠实提交;write_gate fallback 合约(regex fallback 只能 held/retryable);soul 车道 frozen-mission judge 逐条 LLM verdict,不可判即 abstain→held;hygiene 可逆。残余断点有三条:F-OBS1 成功后不清空 health `last_error`;P1-017 production snapshot 的 transcript wakeup 早于 outer commit且 dirty修复未完成 clean/full/deployed 验收;P1-008 把 Memory storage/resident 故障错误提升为所有 non-read-only effect 的全局 authority failure。

**自进化链**:candidate(LLM 真作者)/promotion(verification→LLM referee→provisional 试用→转正,携 rollback_ref)/rollback(字节级还原+sha256)/audit(纯 append)均真实现;**eval 行为级显式退役**(`baseline_reward` 硬编码 0.0,`agent_behavior_check` grader 无 handler)。按五态定义,该能力当前源码无实现,应标 **已知缺失(C-BP7 alias)**,不能同时计作 P2 断点。

**多智能体强项与边界**:后台子代理已从进程内 asyncio 重构为 durable RuntimeTask + outbox 续跑闭环(五环回父链全通);A2A 深度上限+trace visited set 防环;Agent Team 真容器+contract 强制;恢复五路启动泵+orphan 如实 failed+transcript resume;Runtime Budget 四维熔断真接线。但 P1-004 已证实 outer `permission_profile`/execution frame 只到 orchestrator,custom tool executor signature 不接收这些 kwargs,因此 target effect boundary 没消费 parent/session 收窄。

Hive Native 断点:C-BP1/A-02(P1 家族)、E-1(P1)、P1-004(A2A frame,P1)、P1-008(Memory effect freeze,P1)、P1-017(transcript commit boundary,P1)、C-BP2~BP-6(P2,其中 BP4=plane_read 锁外直写、BP5=T0 hash chain 只写不校验)、E-2(P2)、E-3~E-7(P3 死代码假治理)。C-BP7 已移入已知缺失。

---

## 8. 企业治理、安全与 AI 资产结论(领域 F)

**状态:1 个 P0 + 恢复身份 P1 家族 + 若干 P2 债;凭据加密/沙箱/MCP authz/路径遍历/注入隔离/两人审批为强项。**

**当前证据确认的强项**:凭据加密栈闭环(LLM key/channel secret/MCP oauth/tool-config password 均 Fernet+HKDF,noop provider 硬拒);沙箱闭环(未知 provider fail-closed,无沙箱二进制且未设逃逸 env 则拒执行,绝不回退裸命令);MCP authz 闭环(拒 userinfo/token passthrough);路径遍历闭环(resolve+relative_to+authority scope);MCP 注入隔离闭环(原始 description 绝不进模型);导入信任门闭环;workflow 两人审批闭环(reviewer≠requester+DB immutable trigger);配额预算闭环(三档硬顶+四维熔断)。RLS 当前生产执行主路径成立依赖三项同时成立:runtime 使用非 owner 的 `app_rls`;该 role 非 superuser 且无 `BYPASSRLS`;生产现存的 migration 清单表 67/67 均 `ENABLE + FORCE RLS`。`FORCE RLS` 本身不能约束 superuser/BYPASSRLS,且迁移清单声明数是 68,不是 18;因尚缺跨租户行为探针与 migration fault injection,七原子总状态仍为局部闭环。

**AI 资产矩阵**:agent/subagent/skill/workflow/external_capability 5 类真闭环(runtime revision 绑定+drift 即 raise+exactly-once usage);model config / soul.md / knowledge / eval / memory policy 5 类为治理债主区。

**retention/deletion/export/legal hold 整体缺失**(GDPR/legal hold 全仓 0 命中,皆软删,无统一导出 API)→ 判"已知缺失"。安全断点见 §14。

---

## 9. 用户使用体验与 UI/UX 结论(领域 G)

**状态:信息分层与 Artifact 链主体达标,存 1 个 P1 表达权威断点(G-01)+ 多个 P2/P3 消费断点。**

**当前证据确认的强项**:流式性能已根治(rAF 批量 store+useSyncExternalStore+双 memo+content-visibility,07-03 旧"每 chunk 无节流"已修);typed state 主干干净(canonical thread_item 按 item_type/item_status 分派,userFacingRuntimeStatus 有"never falls back to raw"测试);Artifact 九环现为 9/9 闭环——`ArtifactSurface.loadOfficeArtifactPreview` 对有 artifact id 的交付快照调用 authenticated `getArtifactPreview`,对 current Workspace file 调用 `getWorkspacePreview`,统一进入 sandboxed Office inspector,后端维持 resource authority、snapshot/current-file provenance、隔离 HTML 与 typed unavailable;rewind/checkpoint 反馈闭环;快照/preview cache GC 闭环;ONLYOFFICE 已从当前前后端消费链退役。补充测试:backend Office preview/document/delivery 59 passed,frontend ArtifactSurface 3 passed。

信息分层:主对话对 internal ID 处理正确(仅 data-attr/tooltip/序号);泄漏点集中在审批详情 raw JSON(G-06)、分支导航 session UUID(G-07)、平台 error detail 直渲(G-01)。当前 AST 复扫 203 个 production TS/TSX 文件得到 2,331 次 literal `t()` 调用、1,905 个 unique keys、280 个 zh/en 双缺、21 个 zh-only catalog keys;因此 G-02 采用第二报告的可复算口径并定为 P2。UI 断点见 §13 与领域 G 全 18 条。

---

## 10. Model Agency / 机械化限制专项结论

**成功回答主路径无 P0 违规,失败路径存在 G-01 P1。** 未发现平台改写/压制成功模型终答(历史 false-tool-evidence verifier 已物理退役,仅剩对其精确固定文法的字节忠实恢复);reviewer 不可用时机械裁决语义(write_gate/Memory Gate/Soul Gate 全部 held/abstain);静默 head/tail 截断复发(守卫墓碑测试全绿);秘密降级模型;NL 子串当权限/确认授予。不能把这一结论扩展到失败路径:`web_chat_run_orchestrator.py:945-957` 会把平台固定 `user_visible_error` 经 `finalize_with_assistant` 持久化为 assistant,违反“平台不得冒充模型结论”的表达权威边界。

**确证的机械 NL hard outcome(均 P2/P3,应逐条消除)**:

| 编号 | 位置 | 机械判定 | 后果 | 级别 |
|---|---|---|---|---|
| A-01 | `web_chat_run_orchestrator.py:767` + `llm_error_policy.py:138-143` | `startswith("[LLM Error]"/"[Runtime Limit]")` 判失败 | 模型合法终答以此前缀开头 → 强判 failed + 记忆车道剔除(TURN_ABORT+semantic_memory_eligible=False)。**typed terminal_reason 已存在但对"空回退"失败盲,NL 扫描当前在干真活;修复须先补 kernel typed 落点(`turn_orchestrator.py:1750`)再切判定** | P2 |
| G-01 | `sessionSocketEventProjector.ts:206-216` + `web_chat_run_orchestrator.py:945-957` | 前端把 error/quota 塑造成 assistant 并以 `message.includes('expired')` 推硬状态;backend exception path 把平台固定错误经 `finalize_with_assistant` 持久化 | 过期硬状态随文案漂移;无关文本可误触发;平台 prose 同时在 UI 与 durable transcript 冒充 agent 表达 | P1 |
| B-03 | `execution_pipeline.py:358-362` | `"approval_required"/"ASK" in str(block)` 决定决策分类 | 平台文本→typed 的机械映射(非扫模型/用户 NL),措辞一改证据分类翻转 | P2 |
| B-04/A-09 | `service.py:286`、`loop_guard.py:69-78` | 结果含 "failed"/"timeout"/"exception" 子串计失败 | 仅驱动 warn+计数(hard abort 仍需 typed 三证据),良性结果含该词虚增失败计数 | P3 |

守卫测试为"墓碑钉死"式(枚举已移除的精确坏片段断言不存在),真实生效、无豁免白名单;局限 = 只防原样复发,改名/改数值的再引入不报警。

---

## 11. Personal KB tool-only 与 Knowledge authority 结论(领域 D)

**Personal KB tool-only 六项:三项合规 + 两项局部 + 一项 P1 跨 principal 权威/证据传播断点。**

- **预取/静态注入**:✅ `invoker.py:851-863` `_resolve_retrieval_context` 恒返 ""(带契约注释"never prefetches Personal or Company KB");prompt_sections/prompt_builder 零 PKB import。**推翻 07-13"恒空 seam 是 bug"—— 现为文档化设计。**
- **可发现**:✅ B1 已修。三工具注册 CAPABILITY_MAP;personal_knowledge_pack 为 deferred L2;查询失败 fail-closed。
- **citation**:✅ `kb://person/{owner}/documents/{doc}#segment={seg}` 全链保留,前端呈现有测试断言。
- **继承**:⚠️ 前台 HR tool path 显式用 requester,但 E-1 会在 durable background subagent dispatch 把 requester 顶替为 creator;普通 shared agent 的 owner-agent 分支也未绑定当前 requester。前者属于 E-1 的 P1 根因,后者保留为 D-KB2(P2 信息经纪面)。
- **三态**:⚠️ 局部。治理层 typed 分离;handler 层 not-found/denied 合并为自由文本 warnings(D-KB4,P3)。
- **sensitivity / provenance / persistence**:❌ **D-KB1(P1,CONFIRMED,已校正定义)**。它不是“owner 自己的 agent 读取 owner PII 必然是 bug”,而是下面四个可独立复现的断点合并成的同一契约缺口：

  1. **读侧事实**:`personal_knowledge_access.py:49-107` 只检查 tenant、owner-agent/explicit grant 与 `agent_searchable`;search/read 没有 sensitivity decision 或 requester-bound `sensitivity_ceiling`。因此“当前读取由 agent_searchable + owner/grant 控制”是实现事实。
  2. **三方契约冲突**:`knowledge.py:237/:330` 仍向模型声称结果已做 sensitivity filter;canonical `personal-knowledge-base-spec.md:146,204-215`、`personal-company-knowledge-tool-boundary-2026-07-10.md:16,81,111,293` 和 `agent-permission-governance-spec-2026-07-07.md:183-214` 也明确要求 sensitivity / session / purpose decision。不能只改工具描述把缺口“诚实化”,也不能只在本审查报告里单方面改写产品契约。
  3. **所谓持久化硬闸并不完整**:`personal_knowledge_service.py:1200` / `personal_knowledge_extractor.py:235` 只跳过 Personal Knowledge graph projection,不阻止原始 `KnowledgeDocument` 与 segments 落库;且 exact-string blocklist 只有 `private/secret/restricted/pl3/pl4/credential`,而 proposal policy 会把 `confidential/sensitive` canonicalize 为 `PL3_sensitive`。当前探针得到 `proposal_sensitivity='PL3_sensitive'` 且 `blocked_by_extractor=False`,证明枚举已经漂移。
  4. **证据传播无 typed sensitivity**:`web_chat_runtime._persist_tool_call` 把完整 Personal KB tool result 写入 `ChatTranscriptEvent`,`content_replacement` pointer 只保护下一轮 model-visible replay;`runtime_control_bus.bridge_transcript_event_to_t0` 把完整 transcript content 交给 T0,T0 只从正文机械分类/脱敏,不消费 `KnowledgeDocument.sensitivity`。T2 `_build_source_bundle` 默认纳入未标 `semantic_memory_eligible=false` / `projection_only=true` 的 tool event;outbound privacy 明确要求调用方传 typed PL3 provenance,但当前 `content_sensitivity` 没有生产调用点。因而“敏感文档一定不进 T3 / 不会外传”没有机械保证。

**对 CC 反馈的裁决:半对。** 同意其三点:当前读侧事实确实是 `agent_searchable + owner/grant`;owner-direct 读取 PII 不应被自动判 bug;E-1 必须先修。不同意“sensitivity 已完整把住持久化与外传”以及“只改工具描述即可闭环”:graph extraction blocklist 已发生 canonical enum 漂移,full tool evidence 仍进入 transcript/T0→T2 候选链,Knowledge typed sensitivity 也没有自动传到 outbound。D-KB1 因此保留 P1,但按 cross-principal authority/provenance/persistence 重新定义。

**最终产品裁决(本施工方案采用):**

1. `requester == owner` 的交互式 owner-direct turn:读取 PL1-PL3 由 owner policy + `agent_searchable` 决定,sensitivity 不再作为第二个 blanket read deny;它仍必须作为持久化、蒸馏、outbound 与审计策略输入。PL4/credential 永不以 Knowledge 正文返回,只返回 Secret Store/credential reference。
2. autonomous owner agent:必须有 owner 明示的 agent grant,并携 `sensitivity_ceiling`、purpose、expiry;agent ownership 本身不替代 grant。
3. `requester != owner`、shared agent、A2A/subagent:必须有 requester/session/purpose-bound explicit grant + sensitivity ceiling;owner-agent relationship 单独不够。授权在 bytes 进入模型前判定,不得读取后再以自然语言扫描补救。
4. search 与 read 每次 fresh-check 同一 typed `PersonalKnowledgePermissionDecision`;decision/receipt 与 `KnowledgeDocument` canonical sensitivity 一起传播到 transcript/T0/T2/outbound,禁止下游靠正文关键词重新猜 sensitivity。
5. 工具描述、runtime contract 与三份 canonical specs 必须同步为同一事实:owner-direct PL1-PL3 的读闸是 owner policy + `agent_searchable`;cross-principal 额外受 explicit grant + ceiling;所有路径的持久化/蒸馏/outbound 消费 canonical sensitivity。任何未来产品改判必须先修改该权威契约和验收,不能只改一处描述。

**本次补充验证:**针对 Personal access/HR requester/extractor/proposal policy 的定向测试为 `7 passed in 2.32s`;`test_persist_personal_kb_tool_keeps_full_evidence_but_replays_pointer` 为 `1 passed in 29.28s`,明确断言 PRIVATE title/snippet 保留于 transcript evidence、仅 replay pointer 去除。两组绿灯钉住的是当前行为,不是 missing sensitivity/provenance contract 已闭环。

**Enterprise Knowledge:已知缺失(P1 产品缺口,非违规,无冒充)。** `db_bootstrap.py:130-131` 注释直认;legacy 公司面已收窄为两生成文件 + 只读导出;`knowledge_inject.py`/`viking_client.py` 已物理删除;PKB 未被当 EKB 用。

---

## 12. 代码极简性结论(领域 H)

**执行入口唯一性成立**(AgentKernel 仅一处实例化,工具执行单例咽喉,记忆写入单主路径);**兼容层无反客为主**(t2_store/extract_agent 已物理删除;officecli 退役后单路径;write_gate sync 双胞胎生产入口零调用者=测试专用死路径)。

主要 KISS 债:状态枚举三轨(§5);organization.py vs enterprise.py 部门 CRUD 双实现、advanced.py vs a2a.py 双路由面(前端只走后者);40 个 >800 行文件(top:web_chat_runtime 4220/engine 3546/orchestrator 3233/llm_client 3001/AgentDetail.tsx 2860,AgentChatSection 2409 已破自家 2400 契约)。无消费者清单见 §17。

---

## 13. 全部断点清单

> 编号沿用两份报告的原始编号,级别为本报告证据快照下的裁定。计数单位是“需要独立修复和独立验收的真实生产断裂 seam”:同一代码 seam 的别名只计一次,一个来源条目若同时描述两个可独立修复的既有 seam 则分别映射但不重复新增;REFUTED、已知缺失、排除项不计入。当前 **provisional working inventory** 为 **69 个断点:P0 1 个、P1 9 个、P2 29 个、P3 30 个**。账务计算式为 `旧库存 66 - C-BP7(应归已知缺失) + 4 个第二报告独有断点 = 69`;G-02、H-404a 仅从 P1 重定级为 P2,不改变总数。这里“精确”的只是本分类口径下的去重算术,不是对变化中工作树的永久认证;Group 0 必须在每组施工前重验存在性、严重级别和同根关系,任何增删都以 evidence-backed ledger delta 记录,不得偷偷维持或凑齐 69。

### 13.1 第二份报告逐项对账

来源快照:`docs/agent-native-atomic-review-501db655.md`(同一 HEAD `501db655`)。本报告维护者完成的 18 条 finding 对账结果是 **12 条既有断点别名 + 2 条已知缺失 + 4 条新增断点**;映射表和算术已机械校验,但尚未由另一位独立审阅者在冻结 checkout 上逐项复核全部底层代码事实,因此它是当前最佳工作账本,不是阻塞 P0/P1 发布的先决条件。

| 第二报告编号 | 主报告归并项 | 最终裁定 |
|---|---|---|
| P0-001 | P0-F1 | 同一 SSRF seam,重复;保留 P0 |
| P0-002 | D-KB1 | 同一 PKB authority/provenance seam,重复;按跨 principal 风险保留 P1,不把 owner-direct 设计分歧抬成 P0 |
| P1-003 | E-1 | 同一 durable subagent requester 丢失 seam,重复 |
| P1-004 | P1-004 | **新增 P1**:A2A custom executor 丢 outer permission/runtime frame |
| P1-005 | P1-F4 | 同一 Recovery Manifest 授权绑定 seam,重复 |
| P1-006 | C-BP1/A-02 | 同一终局 hook 内联 T2 LLM seam,重复 |
| P1-007 | G-01 + A-01 | 同时覆盖“平台错误冒充 assistant”与“NL 前缀判终态”两个既有 seam;均已计数,不新增 |
| P1-008 | P1-008 | **新增 P1**:Memory dependency failure 被提升为全局 effect authority failure |
| P2-009 | H-404a | 同一 Messages read-state 契约 seam,重复;最终定 P2 |
| P2-010 | G-02 | 同一 i18n 消费 seam,重复;第二报告 AST 口径 `280/1905` 取代旧漏算口径,最终定 P2 |
| P1-011 | P0-F2 | 同一 startup migration fail-open seam,重复;保留 P1 |
| P1-012 | Enterprise Knowledge | 已知缺失,不是现有生产链断裂,不计入 69 |
| P2-013 | B-03 | 同一 governance outcome 字符串反推 seam,重复 |
| P2-014 | C-BP5 | 同一 T0 hash chain 只写不验 seam,重复 |
| P2-015 | C-BP4 | 同一 profile Markdown 锁外直写 seam,重复 |
| P2-016 | AI Asset 未覆盖类型 | 已知缺失,不是现有生产链断裂,不计入 69 |
| P1-017 | P1-017 | **新增 P1**:transcript→T0 outer-commit visibility race 尚未形成 clean/full/deployed 验收 |
| P2-018 | P2-018 | **新增 P2**:canonical 架构文档的 4 个验收测试路径不存在 |

> `C-BP7` 不来自第二报告新增项,但本次对账暴露了主报告自己的分类矛盾:正文已证明行为级自进化 eval 当前源码无实现,依五态只能是“已知缺失”,不能继续占用 P2 断点库存。

### 家族 F-ID「恢复路径身份契约缺席」— P1(CONFIRMED)

**[E-1] durable 后台子代理身份被 creator 顶替** · P1 · 断点(权威原子)
- 断裂位置:`services/subagent_run_service.py:799`(`_resolve_parent_runtime` 构造 `SubagentSpawnContext(parent_user_id=agent.creator_id)`)vs `:418`(enqueue 已持久化 `root_user_id`=真实请求者);dispatch(`:1267→1302→1333`)拿到含 `root_user_id` 的 record 却只取 `parent_agent_id`。
- 根因:worker dispatch 重建 context 只查 Agent 行,不回读 `RuntimeTask.root_user_id`;`execution_backend="runtime_task_worker"` 写死 → **所有后台子代理的唯一执行路径**,非仅崩溃恢复。
- 用户可见:成员 B 在管理员 A 创建的 agent 会话触发后台子代理 → child 全部工具调用/审批 requested_by/T0 actor/审计归属记为 A;若为 HR agent,B 的子代理以 A 的 Personal KB 为读取 authority(`knowledge.py:50-51`)→ **B 间接读到 A 的个人知识库**。
- 提权边界:能力门 agent-scoped,任意提权不成立;实质越权 = 审计/审批/T0 身份伪造 + HR-agent PKB 跨用户读。
- 最小修复:dispatch 以 `record.root_user_id` 覆盖 `ctx.parent_user_id`,creator_id 仅 fallback + typed 降级事件(约几行)。同型待核:`subagent_wake_consumer.py:492`。
- 证据:`runtime_task_service.py:_task_to_dict:15` 证明 record 带 `root_user_id` 而 dispatch 全程不读。

**[P1-F4] SEC-002 恢复授权校验缺席** · P1 · 断点(权威原子)
- 断裂位置:`bac8442da` Revert 了 `d527d584a`(同日提交 27 分钟后回滚);当前 `runtime/recovery_manifest.py`(680 行)无恢复授权校验,`recovery_manifest_matches_session:456` 对无 session_id 的 legacy manifest 恒返 True(fail-open);`kernel/engine.py:2996-2999` post-compaction 恢复路径连 matches_session 都不调,零校验注入。
- 根因:被 revert 的版本(2760 行)含 fail-closed 六元授权绑定 + 退役了"agent-writable 无可信 provenance"的 `workspace/recovery_manifest.json`;revert 把这条伪造路径重新打开,manifest 还是 per-agent 非 per-session。
- 用户可见:同一 agent 下用户 A 的会话状态经 compaction 后被用户 B 的会话零校验恢复;或 agent 自己往 workspace 写伪造 manifest,重启后被平台当可信恢复证据消费。
- 最小修复:重新评审并落回 `d527d584a` 授权绑定契约(需先查清当日 revert 的 data-state 阻塞根因)。
- 与 E-1 同根:durable 后台/恢复路径重建执行身份与状态时无强制授权契约。

### 家族 F-MEM「TURN_STOP 内联记忆流水线阻塞」— P1(CONFIRMED)

**[C-BP1 / A-02] 终局 hook 同步 await T2 摘要 LLM 阻塞完成信号** · P1 · 局部闭环
- 断裂位置:`web_chat_run_orchestrator.py:913→923`(先 `await emit_terminal_hook` 再 `broadcast(done)`)→ `hooks_setup.py:445→481→614` → `segment_package.py:168-190`(summary→labels→review 串行 3 次全 source_bundle LLM,committed+stitch 再 +2);`hooks.py:1470-1473` emit() 无默认 timeout。
- 根因:T2 打包挂在 turn 终局 hook 同步 await 链。当前 manifest 只有在 `run_t2_segment_package_job()` coroutine 真正开始后才于 `segment_package.py:376-398` 创建,因此“job 已开始后的 LLM 阶段”有 crash boundary,但“仅 schedule 尚未开始”没有 durable boundary。
- 用户可见:答案 3 秒流完,输入框和 running 再锁 40-120 秒等三次记忆摘要 LLM(每个成功 web 轮次必然触发)。
- 加刑:TRIGGER_END/DELEGATION_END 同样内联 await T2(`hooks_setup.py:667/:687`),跑在 runtime task worker 内 **占用有界 worker 并发槽**。
- 减刑:DB 终态在 hook 之前已落(`:887-902`)→ 重启安全、下一轮放行;web chat run 是独立 asyncio task 不占 worker 池。
- 完整修复:finalize commit 后先同步持久化 deterministic queued T2 job/outbox(含 agent/tenant/session/segment/idempotency key),再 broadcast done,由后台 worker 执行 LLM 打包并由 sweep 恢复。**禁止只改成裸 `asyncio.create_task`**:进程可能在 task body 创建 manifest 前崩溃,此时 sweep 无 manifest 可发现。

### 安全 P0

**[P0-F1] web_fetch SSRF** — 见 §14(本轮最高优先级)

### 权威与消费面 P1(CONFIRMED)

**[P1-004] A2A custom executor 丢失 permission profile 与 runtime frame** · P1 · 断点
- outer A2A runtime 已持有 requester/principal/session/`permission_profile`,但 `_build_agent_message_tool_executor` 的 executor signature 只接 `emit_runtime_hooks`;`ToolInvoker` 仅按 custom executor signature 注入 profile/frame kwargs,因此 target Agent 的 inner tool effect 静默退化为 global governance,没有消费 parent/session 收窄。
- 当前测试只证明 profile 到 orchestrator、A2A depth/visited 传播,没有验证 target effect boundary 使用同一 policy hash/principal。完整修复必须定义一份可持久化、可重放的 A2A execution frame,在每次 inner effect 前 fresh-check,并让 receipt/span 记录 frame/policy hash。

**[P1-008] Memory storage/resident 故障冻结所有非只读 effect** · P1 · 断点
- `_build_memory_context_result` 把 settings/storage/resident/identity 等不同依赖失败合成为 `external_effects_available=false`;invoker 随后拒绝所有 non-read-only tools,即使 Workspace/Office/message effect 与 Memory 存储无依赖且已经获批。
- 这不是 authority fail-closed,而是把依赖可用性冒充全局权限。完整修复应拆成 dependency-specific typed availability/capability constraints;只有无法建立 principal/tenant 的 authority failure 才冻结相关 effect,普通 Memory storage/resident failure 只降级 Memory 读写并保持可恢复证据。

**[P1-017] transcript→T0 outer-commit visibility race 尚未完成验收** · P1 · 断点
- production snapshot 曾在 outer transaction commit 前唤醒 T0 projector,worker 可读不到刚写入的 transcript event并留下陈旧 health error。当前 dirty worktree 已改为 after-commit callback,相关定向测试为绿,但尚无 clean diff、全量 suite、部署与生产 health/reprojection 闭环。
- 在上述四个 acceptance gate 全部满足前,只能判“修复方向正确但断点仍开”。它与 F-OBS1 不同:P1-017 是 publish/commit 顺序,F-OBS1 是成功后陈旧 `last_error` 的消费诚实性。

**[G-01] 平台错误文本冒充 assistant 气泡 + `includes('expired')` NL 硬状态** · P1 · 断点
- 断裂位置:`sessionSocketEventProjector.ts:206-211`(error/quota → `{role:'assistant', content:'⚠️ '+message}`,`AgentDetail.tsx` 另有 5 处同模式)+ `:214-216`(`includes('expired')`→禁 composer+横幅);backend `_handle_web_chat_failure:945-957` 还会把平台 `user_visible_error` 交给 `finalize_with_assistant`,最终由 `web_chat_runtime.py:2667+` 写入 durable `ChatMessage(role="assistant")` 与 transcript assistant event。
- Model Agency 违规:平台 prose 以 agent 结论形态呈现 + NL 子串推硬结果,且部分 failure path 已进入 durable transcript。减刑仅限“未发现成功模型终答被平台重写”;不能再用“仅前端展示层”减刑。子项修正:"detail 直渲泄漏"仍降 PLAUSIBLE(live 发射点全用 content 字段)。
- 完整修复:失败状态以 typed event/thread item 持久化并在 UI 走 `role:'event'`;composer/expired 状态读取 typed code;不得创建平台作者的 assistant row。历史平台错误 assistant row 需有可识别的 source/type 迁移或兼容读逻辑。

**[D-KB1] PKB 跨 principal sensitivity clearance、typed provenance 与持久化传播契约缺失** · P1 · 断点 — 见 §11

### P2 编号清单(29 个;详情节选,CONFIRMED 或高证据)

| 编号 | 模块 | 断点 | 位置 |
|---|---|---|---|
| A-01 | 运行时 | `[LLM Error]` 前缀判失败(Model Agency) | `web_chat_run_orchestrator.py:767` |
| A-03 | 运行时 | 自动压缩边界不回写重放,长会话每 turn 重复压缩 + 超限消息静默滑窗 | `web_chat_runtime.py:3993-4003` |
| A-04 | 运行时 | Redis 不可用时跨进程取消降级不可观测 + phase 漂移 | `web_chat_runtime.py:2258-2271` |
| C-BP2 | 部署 | CORE_DAEMON 默认 False 藏自进化车道(自托管全暗) | `config.py:122` |
| C-BP3 | Memory | held/failed T2 重试 3 次耗尽后永久搁置(T0 证据保留,无 admin requeue) | `job_sweep.py:36,129-134` |
| C-BP4 | Memory | plane_read 锁外直写 T3 语义文件(唯一绕事务残留) | `plane_read.py:414-444` |
| C-BP5 | Memory | T0 hash chain 只写不校验 | `ledger.py:602-609` |
| C-BP6 | 自进化 | capability 三表只进不出 | `capability_factor.py` |
| F-OBS1 | 证据消费 | T0 projection 成功不清空 `runtime_control_bus.last_error`,health 持续展示已消失错误;生产 DB 当前 0 nonterminal | `runtime_control_bus.py:357/:374/:390` |
| B-02 | 治理 | unavailable 与 denied 在证据层合并 | `governance.py:908-915` |
| B-03 | 治理 | 决策 outcome 靠平台文本子串嗅探 | `execution_pipeline.py:358-362` |
| E-2 | 多智能体 | Hive Connect local A2A 结果不 wake 父 | `local_agent_channel_service.py:1667-1782` |
| P1-F5 | 安全 | 审计表 DB 可变 + tenant=None 审计静默丢弃 | 见 §14 |
| F-明文 | 安全 | agent_tools.config 明文 api_key(第三方 MCP 凭据) | `resource_discovery.py:412/:838` |
| P2-F8 | 安全 | grep_search 缺 `--` 分隔的 rg flag 注入 | `agent_tool_domains/workspace.py:1223` |
| P2-F6 | 安全 | model config 跨租户引用无写入端校验(有事故史) | `llm.py:19-21` |
| D-KB2 | Knowledge | 普通 shared agent 的 owner-agent 无 grant 分支未绑定 requester;E-1 修复后仍是独立信息经纪面 | `personal_knowledge_access.py:61-70` |
| G-02 | UI/i18n | 1,905 个 production literal `t()` unique key 中 280 个 zh/en 双缺,核心旅程依赖 inline fallback/raw key | frontend AST 扫描,见 §9 |
| H-404a | 契约 | Messages 单条/全部已读 PUT 后端均无路由/read-state schema,UI mutation 静默 404 | `frontend/src/api/messages.ts:10-11`;`backend/app/api/messages.py` |
| H-404b | 契约 | 渠道测试接口 404(agentbay-channel slug 后端零落点) | `channels.ts:14` |
| P2-018 | 验收 | canonical Memory 架构文档引用 4 个不存在的 pytest 路径,复制命令无法运行 | `memory-clean-loop-refactor-plan-2026-06-17.md:1623,1644,1678,1712` |
| G-03~G-10 | UI | 预览来源 header 丢弃前端猜测/字面量乱码/审批 raw JSON/分支 UUID/无行级 diff/rewind 静默/fanout 无聚合 等 8 条 | 领域 G |

### P3 编号清单(30 个;范围展开计数,详情节选)

B-01(hr 旁路,降 P3:确认流主执行道但受信固定业务体+认证锚+审计)、A-05~A-09、E-3~E-7(Signal 死代码假治理/trigger 限频假注释/agent_schedules 遗留表/worktree 单向无回写)、C-BP8~BP-12、B-04~B-07、G-11~G-18、D-KB3/D-KB4。RLS owner/bypass 不再列为当前缺陷:生产 runtime role 与 table owner 已分离,且 role 非 superuser/BYPASSRLS;未来新增表仍须通过 migration coverage gate 保持 `ENABLE + FORCE RLS`。

---

## 14. P0/P1 上线阻断项

**[P0-F1] web_fetch SSRF —— 本轮唯一 P0,当前仍 open,最高整改与独立发布优先级**
- 断裂位置:`services/agent_tool_domains/web_mcp.py:1267` `_web_fetch` 仅经 `_normalize_url→_looks_like_url:205`(只查 `netloc and "." in netloc`)后 `httpx.AsyncClient(follow_redirects=True):1302` 直发 GET;无任何 IP/host 校验、无 transport 拦截、无 egress allowlist。防护 `_is_private_url` 仅存于 `trigger_daemon.py:731`,web_fetch 未复用。
- 可达性:web_fetch 在 `governance_capability_taxonomy.py:80` `CORE_TOOL_NAMES`(默认可用无需 tool_search)+ `governance.py:60` `_STATIC_SAFE_TOOLS`(无审批门)。
- 失败场景:agent 调 `web_fetch("http://169.254.169.254/latest/meta-data/iam/security-credentials/")` → 被判合法 → 发起直接 GET;若部署网络可达,即可读取云元数据/IAM 凭据。`127.0.0.1:8008` 内部 API、`10.x` 内网、redirect-to-internal 同理。本文未向任何真实 metadata/localhost/内网目标发请求。
- 完整修复(不能只复用一次现有 `_is_private_url`):所有 agent-controlled direct-fetch 入口必须消费统一 governed egress transport,或证明其外部 provider 具备等价隔离;目标校验必须贯穿 URL 规范化、全部 DNS 结果、实际连接 peer 与每个 redirect hop,并同时覆盖 proxy 契约、DNS rebinding/TOCTOU、跨 origin 凭据清除、timeout/跳数/压缩前后 response-byte 上限及 typed deny receipt。§20.1 第 1 项与 §22.2 P0-F1 行是唯一规范性施工/验收定义,本节只陈述风险与优先级。先写完整 Red tests;该项自身验收通过后立即独立 commit并按三服务规则部署,不得等待其余 68 项。

**P1 共 9 个(同根合并口径)**:E-1、P1-004、P1-F4、C-BP1/A-02、P1-008、G-01、D-KB1、P0-F2(由 P0 降级)、P1-017。核心修复顺序是 `P0-F1(独立修复并立即发布) → P0-F2 → E-1 → P1-004 → D-KB1 → P1-F4 → P1-017 → G-01 → P1-008 → F-MEM`;其中 E-1 先恢复真实 requester,P1-004 再把同一 execution frame 贯穿 A2A effect,D-KB1 才能基于可信 principal 收紧 direct/shared/A2A/subagent Knowledge authority 与 evidence propagation。P0/P1 完成后 C-BP7 应成为 Goal 1 主建设项;它只封锁 Goal 1/North Star 完成声明和将主建设方向转向 Goal 2/UI/KISS,不封锁任何已授权的安全、正确性、数据完整性、恢复或紧急生产修复。

**降 P1**:P0-F2 entrypoint fail-open —— `entrypoint.sh:169` `alembic upgrade head || echo "non-fatal"` 其后无 gate 直达 `exec uvicorn`;`069ff5e88` fail-closed 被 `42f6b6081` Revert 且 HEAD 未重新应用。减刑:Step 1 `create_all`+ALTER 在 `set -e` 下 fail-fast 保障基本表,fail-open 真正放过的是 alembic **增量**安全约束(新 RLS policy/immutable trigger/NOT NULL)。

---

## 15. 双事实源和旁路清单

- **治理旁路 1**:`hr_provisioning_runtime.py:265-278` worker 直调业务体绕过咽喉(B-01,P3,受信+认证锚)。
- **无其它任意工具旁路**:全入口均重入 `ToolRuntimeService.execute`;`try_execute` 全仓唯一调用点 `service.py:1189`。
- **锁外直写**:`plane_read.py:414-444` `mark_profile_entry_promoted` read→replace→write 无锁无事务(C-BP4)。
- **双写(文档化,非断点)**:invocation spans PG+JSONL;heartbeat 直连 heartbeat_t3_core(CLAUDE.md 明文第二车道)。
- **状态枚举三轨**:runtime_tasks / tasks / ThreadItemStatus(§5)。

---

## 16. 治理、RLS、预算、审批冲突清单

- **RLS 当前生产执行主路径成立,原论证已修正** —— PostgreSQL 的 `FORCE ROW LEVEL SECURITY` 不能约束 superuser 或 `BYPASSRLS`,因此不能单靠 FORCE 推翻 owner/bypass 风险。生产只读实测:runtime=`app_rls`,owner=`postgres`,runtime `rolsuper=false/rolbypassrls=false`,enforcement=`strict`;`force_all_tenant_rls_0615` 声明 68 张,生产现存 67 张且 67/67 均 ENABLE+FORCE。当前主路径由“非 owner 安全 runtime role + role guard + FORCE 表覆盖”共同成立;未来新增表或 migration fail-open 仍必须通过 catalog acceptance 防回归。因跨租户行为探针与故障注入未完成,七原子总状态仍判局部闭环。
- **tenant=None 降级**:`governance.py:1431` tenant=None → CC session 权限(默认 "ask" 非 fail-open-allow);kernel P0-1b 上游 abort 为第二道防线。denied≠unavailable 处理正确。
- **审计 tenant=None 静默丢弃**:`core/policy.py:224-229` tenant_id is None 系统级安全事件 `logger.warning`+`return`,无 platform-level 兜底(P1-F5b,P2)。
- **approval 后原 run 恢复**:web-durable/企业 approval 变体闭环;但 SEC-002 revert 后恢复授权契约缺席使身份重建退化为 best-effort(P1-F4)。
- **无预算死锁**:预算拒绝典型化(RuntimeBudgetDenied/ApprovalRequired+durable 通知)。
- **policy update 无版本绑定**:model/memory policy 无 config_revisions。
- **RLS bypass 清单**:82 授权(22 session-state-only,少数 cross-tenant 均 platform-admin 聚合/迁移 grandfather 附理由),未见请求内业务写越权。

---

## 17. 无消费路径清单

**表/模型(应删,先证生产零行)**:`AgentWorkLedger`(ORM 死表,现役 Work Ledger 是文件态 JSON)、`AgentSchedule`(api/schedules.py 已是 AgentTrigger 后端)、`CharterProposal`(import-only 死模型)、`TenantScopeQuarantineRecord`、`AgentAgentRelationship`。只写不读:`ExternalPrincipalBindingEvent`、`ExternalExtensionComponent`。

**配置符号(零引用)**:`API_PREFIX`、`SUBAGENT_EVOLUTION_THRESHOLD`、`WORKFLOW_PROMOTE_SUGGESTION_THRESHOLD`(后两个有 `_ = threshold` 铁证=有意退役);`trigger_daemon.py` 死治理机械:`DEDUP_WINDOW`/`MAX_AGENT_CHAIN_DEPTH`/`MAX_FIRES_PER_HOUR` 零调用 + `_fire_history`/`_last_invoke` 定义后从不读写 → **"每小时上限"能力不存在**(能力缺口披露);`runtime_continuity_v1` 死 flag;`feature_flags.evaluate_all()` 零调用。

**API 路由**:`api/packs.py`(唯一未挂载 router + 与 capabilities.py 逐路径重复双份漂移);整文件级前端零调用:schedules/feature_flags/guard_policies/config_history/channel_deliveries/tenant_channels/plugins(4/5)/onboarding/organization 部门 CRUD;endpoint 级死路由 admin.py 14/24(疑运维 curl,删前问 owner)、advanced.py 5/6、chat_sessions.py 4/24 等。

**事件/hook**:`MEMORY_EXTRACTED` 注册无发射死 handler(旧发射方 extract_agent 已删)。

**服务**:唯一零 importer `office_workflow_examples.py`(有意保留测试语料)。

**前端**:死组件 `LocalAgentLinkCard.tsx`/`PromptModal.tsx`;~50 死 api 方法(officeApi.createDocument/taskApi.update/skillApi 群/a2aApi 群/ccParityApi 七个/localBridgeApi 七个等);~868 i18n 孤儿 key(channelGuide ~82/wizard ~72/enterprise.quotas 25 等,zh 独有 21 全死);`WorkspaceFeatureHub.tsx:780` 死链 `#office`。

**死文件**:`skills-lock.json`(git 跟踪零代码引用,删前问外部工具);`consume_subagent_signals`(subagent.py:1366 零调用+错平面)。

---

## 18. 应删除、合并或收敛的抽象

1. 修两个 P2 404 契约断裂(H-404a Messages read-state / H-404b channel test)。
2. 删 `api/packs.py`(能力由 capabilities.py 同路径继续)。
3. 删 AgentSchedule / AgentWorkLedger / CharterProposal / TenantScopeQuarantineRecord / AgentAgentRelationship 模型+表(合一 migration,先证零行)。
4. trigger_daemon 死治理二选一(真接线三常量到 fire 路径,或删死机械 + 文档记录"限频权威=Runtime Budget plane")——**禁只删注释留机械**。
5. 删 config 三零引用符号 + write_gate sync 双胞胎公开面(record_how/upsert_entry)+ flag 平面收缩。
6. 状态枚举三轨收敛(chat.ts:238 手写 union 并入生成物同源导出)。
7. 前端一揽子:死组件/~50 死 api 方法/i18n 整组死段(zh 独有 21 必删)。
8. **文档漂移修正**:CLAUDE.md hooks 章 `_DISABLED_NOOP` 已不存在(42 事件 41 有发射)、"kernel 3 files"实为 6、"kernel 零 DB import"字面已破;CLAUDE.md 记忆章 T3 布局过时(现为 profile plane 常驻 + knowledge plane 检索两面,LEGACY_T3_FILES 已退役);hr.py:429 提示词"T2 (episodic learnings)"术语过时。

每项简化保留的真实能力、唯一事实源、迁移方式见领域 H §三。

---

## 19. 已知缺失、排除项与未证实项 + 旧结论翻案

**本轮推翻的旧审计结论(以当前源码为证)**:
- **A8"local 委派/子代理结果不回父"**→ 推翻:后台子代理已 durable RuntimeTask + outbox 五环回父链全通。残留仅 Hive Connect local A2A 不 wake 父(E-2,P2)。
- **A7/A5"进化/编排 daemon 生产不跑"**→ 推翻:Railway 生产实测 `CORE_DAEMON=true`,车道真跑。残留 = 仓库默认 False + 自托管全暗(C-BP2,P2)。
- **A11"明文凭据"**→ 大部分推翻:LLM key/channel secret/MCP oauth/tool-config password 均加密。残留仅 `agent_tools.config` 直连导入 api_key(P2)。
- **07-03"流式渲染无节流+巨型组件重跑"**→ 推翻:rAF 批量 store+memo+测试钉住。
- **07-13"retrieval seam 恒空是 bug / 激活方程待实施"**→ 修正:seam 恒空是 tool-only 文档化设计;激活方程已实装真接线。
- **A11"held T2 重试耗尽永丢"**→ 修正:永久搁置但 T0 证据完整保留(C-BP3)。
- **F 域 P1-F3"RLS owner 绕过"**→ 当前生产风险关闭,但原“FORCE 全表即可推翻”的论证错误。补充 live evidence 证明 runtime=`app_rls`、owner=`postgres`、runtime 非 superuser/BYPASSRLS,且现存 migration 清单表 67/67 ENABLE+FORCE;应以这组联合条件作为关闭依据。

**已知缺失(不计入 69 个断点;按北极星优先级排序)**:

1. **Goal 1 完成声明门** — 行为级自进化 eval(`C-BP7` alias):`baseline_reward=0.0`,行为 grader 无生产者。它保持 Missing 分类;在 P0/P1 安全、身份和证据修复闭合后,其 Goal 1 建设优先级高于 P2 中非 Goal 1 项及 Goal 2/UI/KISS。它不阻塞任何先行修复的独立发布。
2. **Goal 2** — Enterprise Knowledge 运行时(`P1-012` alias,§11)。
3. **Goal 2** — AI Asset 尚未覆盖 model/memory/soul/knowledge/eval/policy 等类型(`P2-016` alias,§8;第二报告的“五类”与本报告分类口径不同,以真实 model inventory 为最终回填输入)。
4. **Goal 2** — retention/deletion/export/legal hold(§8)。

**排除项**:无 —— 本轮未发现被误报为 CC parity 债的供应商私有远程能力。

**未证实**:真浏览器端到端走查;真 PG 跨租户行为探针与 migration failure fault injection(角色/owner/RLS catalog 已 live 核验);外部消费面(webhooks/oidc/admin curl/渠道回调);~290 动态 i18n key;`skills-lock.json` 外部消费;`connector_acl` onlyoffice:// 保留意图;E-8 前台 spawn ToolMeta timeout=180s 与 200 轮预算张力。

---

## 20. North-Star-safe 最终施工方案

> owner 的“一次改完、零技术债”纪律作用于**每个开始施工前已完整界定的原子修复或同根家族**:一旦开工,其 Red→Green、边界/故障回归、迁移与历史回填、可观测性、恢复/回滚、真实消费、发布验收和文档证据必须在同一轮闭合,不得把家族内债务拆成“以后再补”。下列分组表达程序级依赖与排程,不是把 69 项绑成一个不可拆的发布列车;高优先级单项通过自身门禁后可独立 commit 和发布,尤其 P0-F1 不等待 P2/P3、库存再认证或 C-BP7。前一项未过验收,不得用后一项或总测试数掩盖。当前 dirty worktree 的既有改动属于其原作者,施工启动时必须记录 HEAD/status/diff ownership并做非破坏性隔离,不得把未知改动混入某个断点的完成证据。

### 20.0 每个修复都必须通过的 North Star 施工门

| Gate | 强制要求 | 直接判失败的实现 |
|---|---|---|
| NS-1 可机械证明的硬约束 | 每个 hard outcome 必须写明属于 authority/data ingress、side effect、isolation、resource/lifecycle、evidence/recovery 或 machine contract,并指向 authoritative fact source | 无事实源的关键词、正则、计数器或阈值决定任务意图、语义真相、权限、终态、学习价值 |
| NS-2 完整授权输入与能力面 | 未授权 bytes 在 model input 前阻断;授权证据必须完整 inline 或通过 lossless/discoverable/recoverable reference 可得;保持 task-sized output budget 与真实工具/委派能力面 | 按 generic task wording 删工具、静默 head/tail 截断、为方便预算丢证据、用治理故障削弱无关推理 |
| NS-3 模型语义主权 | 规划、总结、提炼、判断、优先级和最终表达由 LLM 完成;模型终答除 exact unauthorized-secret redaction 外字节忠实 | 平台生成 assistant 结论、NL scanner 改终态/改终答、机械 fallback 接受/拒绝/提升/删除/改写 Memory/Soul/Skill 语义 |
| NS-4 最窄 effect 治理 | 权限、approval、sandbox、quota、idempotency 在 effect 前按 principal/session/capability 交集 fresh-check;一个依赖或 effect denied 不得冻结无关能力 | 将 availability 冒充 authority、outer metadata 有 frame 但 inner effect 不消费、一次拒绝降级整轮模型能力 |
| NS-5 证据与恢复 | 每个失败返回 typed denied/unavailable/approval-required/retryable 状态和 receipt/span/ref;fallback 只能 hold/quarantine/retry/degrade/request-review,保留原证据并可回到 LLM 主路径 | catch-and-forget、固定 prose 冒充 agent、不可恢复 mechanical drop、callback/outbox 无幂等和 sweeper |
| NS-6 CC/FreeCode 语义下限 | 涉及 prompt/context/compaction/tool eligibility/delegation/hooks/final answer/Memory/Soul/Skill 的改动,先做当前 FreeCode 源码对照,再采用不缩小能力面的 Codex 工程增量 | 因实现更简单而删除 CC local lifecycle/tool/delegation 语义,或把 Codex 控制面当成缩权依据 |
| NS-7 七原子与完整交付 | Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七项必须有当前真实路径;先写失败回归,再最小修复,最后扩展/full suite/build/migration/live 验收 | 只有 API/schema/UI shell、只有 targeted green、没有 consumer/backfill/故障注入/生产证据却宣称 closed |
| NS-8 删除与收敛 | 只能删除重复实现或已被等价替代的 accidental complexity;先证明 replacement live、仓内/动态/仓外消费者与生产数据,再迁移和删旧路径 | 仅凭静态零引用删除 CC/Hive-native 能力、外部契约或历史恢复面 |

### 20.1 最终依赖顺序

**第 0 组 — 施工基线与证据隔离(只建立事实,不改变产品语义)**

- **每个单项开工前的最小事实冻结**:记录该项开始时的 HEAD、相关文件 status/diff/hash、既有改动 owner、原始失败症状、Red test、权威事实源、FreeCode 对照点、migration/backfill/rollback 与独立 commit 边界。禁止 reset/覆盖既有改动;重叠文件先做三方 diff 和 ownership 归属。P0-F1 只需完成它自身相关文件和测试的事实冻结即可开工,不得把 69 项全面重验变成安全修复前置。
- **程序账本滚动再认证**:P2/P3 及多 HEAD/dirty-worktree 证据在对应组开工前逐项重验;证据新增、合并、推翻或改级时同步更新分母、映射、原因和快照,不得为了维持“69”而保留已不存在的项,也不得无直证新增编号。首个业务修复 commit 前还必须显式将本 canonical 报告纳入 Git truth(例如审阅后执行 `git add -f docs/agent-native-atomic-review-2026-07-14.md`,或收窄 `.gitignore` 的 `docs/` 规则),否则逐项状态更新没有可追溯载体。

**第 1 组 — 封住 P0 入口并建立可信迁移底座**

1. **P0-F1 SSRF(闭合即独立发布)**:先枚举所有 agent-controlled direct HTTP fetch 入口;每个入口必须统一进入同一个 governed egress transport,或以测试证明其只调用具备等价隔离的外部 provider。
   - **URL / transport 边界**:只接受规范化的 `http/https` URL;拒绝 userinfo、控制字符、无效端口、歧义/混合 IP 表示与 IPv6 zone id。显式定义 proxy/`trust_env` 契约,未经治理的代理不得绕过目标校验。
   - **DNS / 连接边界**:对 hostname 的全部 A/AAAA 最终结果逐一校验,拒绝 metadata、loopback、private、link-local、multicast、reserved、unspecified 及 IPv4-mapped IPv6 禁止地址。通过 resolver pinning、socket peer 校验或等价 egress proxy,保证实际连接 IP 属于已验证集合,同时保留正确 Host/SNI/TLS 校验,关闭 DNS rebinding/TOCTOU。
   - **重定向 / 资源边界**:使用 `follow_redirects=False` 手工处理每一跳,每跳重新解析并执行同一校验,限制跳数,跨 origin 清除敏感 header;流式限制总 timeout、响应 bytes 与解压后 bytes。deny 只返回 typed `network_target_denied` receipt;validator 只判断网络事实,不得判断页面语义。
   - **TDD / 发布边界**:Red tests 覆盖 metadata aliases、IPv4/IPv6 私网、IPv4-mapped IPv6、混淆地址、多 A/AAAA 含一个禁止地址、DNS rebinding、跨 scheme/跨 origin redirect 与受控公网放行;不得向真实 metadata/内网地址发请求。该项自身七原子和发布验收通过后立即独立 commit,并按三服务规则部署,不等待其余 68 项。
2. **P0-F2 startup migration fail-open**:查清两次 revert 的 data-state 根因,恢复 `alembic upgrade head` fail-closed与 migration/RLS catalog preflight。后续 schema 修复均依赖这条部署底座。

**第 2 组 — 建立唯一真实 principal 与跨 Agent execution frame**

3. **E-1 requester identity**:durable dispatch 消费 `RuntimeTask.root_user_id`,覆盖 subagent run/wake/reconcile;身份缺失只能 typed unavailable/fail-closed,creator 不得 fallback 冒充 requester。
4. **P1-004 A2A execution frame**:在 E-1 的可信 principal 上持久化 requester/tenant/session/agent/delegation/policy/sandbox/budget refs;custom executor 显式接收并在 inner effect 前 fresh-check,receipt/span 记录同一 policy hash。frame 只收窄未授权 effect,不得按消息文本删除 target Agent 的授权证据、推理预算、工具发现或委派能力。
5. **D-KB1 Knowledge authority/provenance**:执行 §11 已锁定矩阵——owner-direct PL1-PL3 由 owner policy + `agent_searchable` 读,PL4 只返 credential reference;autonomous/cross-principal/shared/A2A/subagent 强制 explicit grant + ceiling/purpose/expiry;search/read fresh-check同一 typed decision;canonical sensitivity 贯穿 transcript/T0/T2/outbound;历史授权 dry-run inventory/quarantine;runtime tool description 与 canonical specs 同步。
6. **P1-F4 Recovery Manifest**:恢复六元授权绑定与 per-session provenance,补所有恢复入口校验;legacy 无 session_id manifest 隔离并给 typed review/recovery path,不可 fail-open,也不可静默丢弃原证据。

**第 3 组 — 修证据提交、终态诚实性与 effect 可用性**

7. **P1-017 transcript→T0 commit boundary**:DB pending row 是恢复事实,outer commit 后只做幂等 wakeup;callback/outbox + sweeper 覆盖 crash/retry/duplicate。完成 clean diff、targeted+full suite、三服务部署、production health/reprojection 四重验收后才关闭。
8. **G-01 + A-01 typed terminal**:provider/runtime failure 只写 typed event/thread item,不得生成 durable assistant prose;kernel 产出 typed terminal_reason,status/composer 只读 typed code。历史 row 只能按已有 exact source/type metadata 迁移;无可信 provenance 的 row 保留并标 unknown,禁止扫描文本猜作者。合法模型终答即使含 `[LLM Error]`/`expired` 等词也保持字节与 completed 语义。
9. **P1-008 dependency-specific availability**:拆分 Memory settings/storage/resident/principal typed states;只有真实 principal/tenant/ACL 不可建立时冻结相应 effect,普通 Memory 依赖失败只降级 Memory lane,不得冻结无关且已获批的 Workspace/Office/message effect。
10. **F-MEM durable T2 queue**:finalize commit 后同步写 deterministic queued job/outbox,再发 done;TURN_STOP/TRIGGER_END/DELEGATION_END 由 worker 消费。队列只负责调度/幂等/证据,summary/labels/review/episode stitching 继续由 LLM 使用完整 source bundle 与 task-sized budget完成;模型不可用只能 held/retryable,禁止机械摘要替代。

**第 4 组 — Goal 1 完成声明门(不改变 69 的断点计数,不阻塞先行修复发布)**

11. **C-BP7 行为级自进化 eval**:candidate 与 version-pinned baseline 在相同授权工具、model/config、资源预算和任务集上真实执行,保留 transcript/span/tool receipt/artifact refs;LLM referee 依据版本化 rubric 与完整证据生成逐场景 verdict/source refs,平台只校验 schema、evidence binding、runner 事实、资源硬约束和 rollback contract。reviewer unavailable/coverage 不完整一律 hold,不得以 `baseline_reward=0.0`、计数器、字符串或平台固定分数冒充行为判断。promotion 必须消费真实 behavior receipt,并通过 provisional→verification→rollback fault injection。**该门只约束 Goal 1/North Star 完成声明和把主建设方向转向 Goal 2/UI/KISS;不得阻塞 P0/P1/P2/P3 中任何已授权的安全、正确性、数据完整性、恢复、可观测性或紧急生产修复,也不得阻塞这些单项通过自身门禁后的独立发布。**

**第 5 组 — 把验收与治理面变成可执行事实**

12. **P2-018 canonical test manifest**:处理 4 个不存在的 pytest 路径——已有等价测试则修正文档,契约无测试则先创建 Red test;CI 增加 doc-command existence + collection gate。
13. **治理/RLS acceptance**:P1-F5 审计 immutable trigger + platform-level tenant=None audit;tenant-table ENABLE+FORCE catalog gate、跨租户行为探针、migration-failure fault injection。denied/unavailable/approval-required 保持不同 typed outcome。

**第 6 组 — Durable/Memory 与 compaction 完整性**

14. C-BP3 admin requeue;C-BP4 profile 更新统一走 `AgentAssetTransaction`;C-BP5 T0 hash-chain replay/启动校验。hash 只证明 bytes/order 完整性,发现异常只能 quarantine/hold/recover,不得判断或改写语义。A-04 补 Redis 降级取消的 typed observability + kernel killed-state 回读。
15. C-BP6 capability 三表建立真实回读消费者;F-OBS1 将历史 error 与当前 health 分离;A-03 只把 model-led compaction 的 lossless/source-ref/coverage-ledger projection 回写 active boundary,T0/transcript 仍是不可覆盖事实源,禁止机械滑窗成为常规路径。

**第 7 组 — 多智能体返回与产品消费**

16. E-2 Hive Connect local A2A `record_channel_result` enqueue `CompletionNotification` 并幂等 wake parent;父 Agent/LLM 解释结果,平台只交付 typed receipt。
17. H-404a 增加 scoped read-state model、单条/全部已读路由、幂等与错误 UI;H-404b 对齐 channel test route 或移除虚假交互。
18. G-02 补齐 **280/1905** 个双缺 literal key并加 AST/catalog CI gate;再修 G-03~G-10。平台错误、内部 ID、forensic payload 继续走 typed event 与渐进披露,不进入 assistant 表达。

**第 8 组 — P3、死路径与 KISS 收敛**

19. 先修仍影响 Goal 1 lifecycle 的 P3,再处理 Goal 2/UX P3。完成 §18 的双入口、状态枚举、死组件、死 API 与文档漂移收敛;每项删除必须通过 NS-8,证明替代能力真实消费、生产数据与仓外调用安全,不可逆删表走 dry-run + owner confirmation。

### 20.2 四层完成口径(不得混用)

| 层级 | 必须同时满足的完成条件 | 允许的声明与动作 | 明确不代表 / 不得阻塞 |
|---|---|---|---|
| **单项 / 同根家族闭环** | 开工前完整界定 scope;该 scope 的七原子均有当前真实路径;Red→Green、边界与故障回归、相关 full suite/build、migration head/backfill、observability、recovery/rollback、Model Agency regressions 和发布验收全部通过;canonical 报告回填证据 | 标记该项“闭环”,独立 commit、部署与关闭;P0-F1 通过后立即发布 | 不代表 69 项程序账本完成、Goal 1 完成或产品无 Missing;不得等待低优先级项、69 再认证或 C-BP7 |
| **程序账本完成** | 在一个记录了 HEAD/工作树/生产快照的冻结基线上重新认证 inventory;所有仍成立的断点逐项闭环,严重度计数为 0;新增、合并、推翻、改级均有直证和账务变更记录 | 声明“该冻结快照的原子化整改程序完成” | 当前 69 只是工作账本起点,不是永久 KPI;不能冻结分母来躲避新证据,也不等于已建设 Missing 能力 |
| **Goal 1 / North Star 完成** | 所有 Goal 1 类断点与 Missing 已闭环,至少包括无 open P0/P1、C-BP7 真实行为 eval 七原子闭环,并以真实 agent 行为证明不弱于基准;模型能力面与语义主权没有回归 | 声明 Goal 1 / North Star 本轮建设完成,并可把主建设方向转向 Goal 2/UI/KISS | C-BP7 未完成只能封锁本层声明与主路线切换;绝不能反向阻塞前三组或其它正确性修复 |
| **产品总目标完成** | Goal 1 与 Goal 2 均闭环;Enterprise Knowledge、AI Asset 未覆盖类型、retention/deletion/export/legal hold 等 Missing 已按独立完整 scope 建成;控制面有真实产品消费与验收 | 声明当前产品没有本文已知未建能力 | `69/69`、C-BP7 或两者同时完成都不足以作此声明 |

四条硬规则:

1. **P0 不排队**:P0-F1 完成自身事实冻结、Red→Green、全边界回归和三服务发布验收后立即独立上线;69 项全量再认证、P2/P3、C-BP7 和 Goal 2 都不是它的前置条件。
2. **one-pass 不得被钻空子**:授权 scope 可以是一个断点或不可拆的同根家族,但必须在开工前写全测试、边界、migration/backfill、故障、恢复、消费、发布和清理;不得人为拆小来延期已知必要工作。
3. **分母服从证据**:当前 `69=P0 1+P1 9+P2 29+P3 30` 只对本文快照成立;滚动再认证发现事实变化时必须同时改 inventory、映射、计数和原因,不得把数字本身当完成目标。
4. **声明必须指名层级**:任何“完成/清零/Goal 1 完成声明门已过”的记录必须明确是单项、程序账本、Goal 1 还是产品总目标,并链接对应 acceptance receipt;禁止用较低层证据冒充较高层完成。

---

## 21. 迁移、回填、清理与回滚方案

- **施工状态隔离**:实施前记录当前 HEAD、dirty diff hash、文件 owner/scope 和已有测试状态;重叠文件以三方 diff 吸收,不得 reset/checkout 覆盖用户或其他 session 改动。每个断点独立 commit只包含其 Red test、实现、migration/backfill、文档证据和必要生成物。
- **删表迁移**:先 `SELECT count(*)` 证生产零行;dry-run + 确认门(owner 交付纪律的唯一 MVP 例外=不可逆数据操作);连删 identity_lifecycle 防御写、rls_bypass_manifest 条目、bootstrap/backfill 引用。回滚:downgrade 重建空表。
- **P1-F4 授权契约回填**:落回 `d527d584a` 时需重放 `workflow_quota_reservations_0713` 迁移;legacy 无 session_id 的 manifest 走隔离而非 fail-open 放行。
- **P1-004 A2A frame 迁移**:为持久化 delegation/runtime record 增加 execution-frame version 与 policy/principal refs;存量 in-flight run 只能从 `RuntimeTask`、session、delegation receipt 等权威记录重建。无法完整重建的 run 进入 typed unavailable/review 队列,不得用 creator/global default 猜测补齐;rollback 保留旧 record,但禁止恢复无 frame 的 effect 执行。
- **D-KB1 sensitivity/grant 回填**:先 dry-run 统计 sensitivity 原值、canonical 映射、`agent_searchable=true` 文档、user/agent grants 与 owner-agent 隐式可达面;aliases 归一到 PL1-PL4。交互式 owner-direct PL1-PL3 保留 owner policy + `agent_searchable` 语义;autonomous/cross-principal grant 增补 ceiling/session/purpose/expiry。无法机械证明授权意图的历史 cross-principal grant 进入 quarantine/owner review,不得猜测放行;rollback 保留 quarantine,不得恢复旧隐式暴露或删除原文证据。
- **P1-017 transcript projection reconcile**:after-commit 改动本身不要求业务数据迁移;部署前 dry-run 枚举 pending/error projection rows,部署后由幂等 sweeper 重投并核对 transcript event count/T0 idempotency key/health。rollback 可退回 sweeper-only 降级,但不得恢复 commit 前 publish。
- **G-01 历史错误作者迁移**:只对已有 exact `source`/`actor_type`/`item_type`/event provenance 可证明为 platform failure 的 row 做可逆 reclassification;无可信 metadata 的历史 assistant row 保留原 bytes并标 `legacy_author_unknown`,不得以 `expired/error/failed` 等正文关键词推断作者或终态。
- **P1-008 availability contract**:为 memory context/result 增加 contract version 与 dependency-specific typed states;存量持久化任务若仅有 legacy `external_effects_available=false`,恢复时从当前 authoritative dependency facts 重建,无法重建则只冻结有依赖关系的 effect并发 typed unavailable,不得把 legacy boolean 当永久全局 authority。
- **F-MEM queued job 回填**:扫描 T0 sealed segment、T2 package manifest 与 staging job;已有 committed package 不重复入队,held/failed/running 依据现有 idempotency key reconcile,缺 manifest 的 sealed segment 生成 queued machine record但不机械生成 summary。rollback 停止新 consumer并保留 queue/manifest,不得丢 job 或改写 LLM 产物。
- **C-BP7 behavior eval 回填**:历史 eval/promotion 若无 version-pinned behavior receipt,统一标 `legacy_behavior_unverified`,不得把 `baseline_reward=0.0` 解释为通过。可重放候选在相同授权/model/config/budget 下重新执行并由 LLM referee 评审;不可重放者保留历史状态与证据,新 promotion fail-closed 为 hold。rollback 只能停止新 promotion并保留 eval artifacts/receipts,不得恢复“无行为证据也 promote”。
- **A-03 active_projection 回填**:历史 transcript/T0 不重写;下次真实 model-led compaction 生成带 source refs/coverage ledger 的新 active boundary。旧 boundary 作为 legacy projection 可回读但不覆盖 raw truth。
- **审计不可变触发器**:加 trigger 前先确认无合法 UPDATE 路径(对齐已有三快照表模式)。
- **P2-018 文档验收路径**:无业务数据迁移;修正路径或补齐测试后,CI 对 canonical docs 中的 repo-local test path/command 做 existence + collection gate。回滚只回文档/CI提交,不得保留指向不存在文件的 canonical 命令。

---

## 22. 验收矩阵与故障注入方案

### 22.1 跨断点 North Star 回归门

| Gate | 必须通过的回归 | 故障/反例注入 |
|---|---|---|
| NS-1 硬约束来源 | 每个 deny/block/hold/terminal outcome 都能定位到 authenticated principal、ACL/policy、approval、sandbox、quota、provider limit、transaction/evidence 或 exact schema fact | benign prompt/final/tool result 含 `error`、`expired`、`secret`、`delete`、`approval_required` 等词,不得改变权限、工具面、终态或语义 verdict |
| NS-2 输入与预算 | 决定性证据位于长输入末尾仍被 LLM 看见;每个 authorized source inline 或有 lossless reference + coverage ledger;task-sized output budget 不被方便常量饿死 | 超长 CJK/附件/嵌套 A2A evidence、provider context pressure、最后一块才出现关键事实 |
| NS-3 能力面与 effect 隔离 | denied/unavailable 的一个 effect 不删除无关 reasoning/read-only/tool discovery/delegation;inner A2A 与 outer frame 权威一致 | global allow + parent deny、Memory storage down + Workspace effect approved、generic task wording 含安全/工具关键词 |
| NS-4 模型终答与语义主权 | 模型终答在 exact secret redaction 外 byte-for-byte 保留;Memory/T2/Soul/Skill/behavior eval 的 semantic verdict 均有 LLM author/reviewer receipt | 模型终答以 `[LLM Error]` 开头或包含 `expired`;LLM reviewer unavailable;平台 fallback 试图摘要、reject、promote 或改写 |
| NS-5 fallback 与恢复 | typed state/span/metric + 原证据/ref + retry/hold/quarantine/review path 完整,恢复后重入 LLM-primary lane | queue publish失败、worker crash、DB commit delay、duplicate wakeup、manifest/frame version缺失、Redis unavailable |
| NS-6 CC/FreeCode 下限 | Skill/Subagent/Workflow/Hooks/compaction/session/final-answer 的 current FreeCode source comparison 有 file/function evidence;Hive 改动保持或扩展能力面 | 删除 local lifecycle/tool/delegation 语义后仅以“代码更少/测试绿”宣称完成 |
| NS-7 七原子与交付 | 每项 Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 全有当前真实 consumer;Red→Green、扩展/full suite/build/migration/live gate 全过 | route/schema/UI shell 无 consumer、targeted green 但 full suite 红、未回填/未部署却宣称 closed |

### 22.2 断点与 Goal 1 完成声明门验收

| 断点 | 验收测试 | 故障注入 |
|---|---|---|
| P0-F1 | `web_fetch` 及所有 advanced/direct fallback 要么统一消费 governed egress transport,要么有等价 provider-isolation 证据;只放行规范 `http/https` 公网目标;userinfo/控制字符/歧义 IP/metadata/loopback/private/link-local/multicast/reserved/unspecified/IPv4-mapped IPv6 全部 typed deny;全部 A/AAAA、实际 socket peer 与每个 redirect hop 均受同一校验;跨 origin 不转发敏感 header;timeout/redirect/压缩前后 response bytes 有硬上限;公网 allow case 与既有 wrong-tool contract 仍绿 | mock resolver 返回 public+private 混合答案、首次 public 后二次 private 的 rebinding、302/307→受控 reserved target、IPv6 zone/mapped 地址、十进制/十六进制/混合表示、redirect loop、chunked/decompression bomb、未经治理 proxy;测试不得真实访问 metadata/内网 |
| P0-F2 | alembic 失败 → 进程 exit 1 不启动;成功迁移后 tenant/RLS catalog gate 全绿 | 故意引入失败迁移、缺失 FORCE policy |
| E-1 | 成员 B 触发 A 的 agent 后台子代理 → child.user_id==B、审计归属==B、HR-agent PKB 读==B 的库 | creator≠requester 并发触发 |
| P1-004 | parent/session profile 收窄并拒绝某 effect时,target A2A inner tool 同样拒绝;receipt/span principal+policy hash一致;restart/replay不漂移;target 的其他授权证据、推理预算、read-only tools、tool discovery 与 delegation 不受影响 | global policy 允许但 parent profile 拒绝、frame version 缺失、嵌套 A2A、message 含 tool/security 关键词 |
| P1-F4 | 跨会话/跨 agent manifest 恢复拒绝;engine.py:2996 有 matches_session;agent 伪造 workspace manifest 被拒 | 无 session_id legacy manifest、per-agent 多会话并发 |
| P1-017 | outer rollback 不触发 projector;commit 后只触发一次且 worker 可见 event;pending/error rows 可幂等重投;clean full suite + 三服务部署 + production health 闭环 | commit 后 callback 前杀进程、callback 重复、worker 先启动/DB visibility 延迟 |
| F-MEM | queued job/outbox 在 done 前持久化;web done 延迟 < 阈值;worker 槽不被 T2 占用;summary/labels/review/stitch 均有 LLM receipt、完整 source refs 与 coverage ledger,无机械语义 fallback | T2 LLM 挂起/超时不阻塞 done;queued-before-task-body kill;reviewer unavailable 必须 held/retryable而非机械摘要 |
| A-01 | 模型合法终答以 "[LLM Error]" 开头 → completed + 记忆车道保留,最终 bytes 不变 | echo 错误日志的终答、"[Runtime Limit]"/`expired` 前缀 |
| P1-F5 | 审计行 UPDATE/DELETE 被 trigger 拒;tenant=None 安全事件写 platform 审计 | DB 层直接改删审计行 |
| G-01 | provider/runtime failure 只产生 typed event/thread item,不创建 assistant row;expired 状态由 typed code 驱动;历史 row 仅按 exact provenance 迁移,未知作者 bytes 保留 | 平台错误含/不含 `expired`;历史 row metadata 缺失;模型正常终答含相同文本 |
| P1-008 | Memory storage/resident unavailable 时,无关且已获批的 Workspace/Office/message effect 仍可执行;principal/tenant authority 缺失时相关 effect fail-closed | settings/storage/resident/principal 分别故障、恢复后 retry、legacy boolean resume |
| F-OBS1 | projection 后 health 的 error state 与 DB terminal state 一致;陈旧错误保留时间戳但不冒充当前故障 | 先制造一次不可见 event,随后成功投影;断言 health 恢复且历史 evidence 可追踪 |
| D-KB1 | interactive owner-direct 按 owner policy + `agent_searchable` 读取 PL1-PL3且不受 blanket sensitivity deny;autonomous/shared/cross-user 无 explicit grant 必拒并严守 ceiling/purpose/expiry;`confidential/sensitive/PL3_sensitive` 归一;PL4 只返 credential reference;typed decision+sensitivity 贯穿 transcript/T0/T2/outbound;tool/runtime/spec 一致 | owner-direct `agent_searchable=true` PL3、broad/expired/revoked cross-principal grant、creator≠requester HR child、restart/replay、T2 distillation、外部 delivery、正文含误导 sensitivity 关键词 |
| C-BP7 Goal 1 gate | candidate/baseline 同配置真实执行;逐场景 transcript/span/tool/artifact evidence 完整;LLM referee 输出 rubric verdict + source refs;promotion 消费 behavior receipt;reviewer unavailable/coverage缺失→hold;provisional/rollback闭环 | baseline_reward=0、LLM judge unavailable、关键证据在末尾、candidate 工具受限而 baseline 未受限、runner crash、rollback partial failure |
| C-BP5 | hash-chain replay/启动验证能发现删除、重排、篡改;原 bytes 保留并进入 typed quarantine/recovery,不产生语义结论 | 中间 event 删除/重排/单字节修改、tail partial write、legacy ledger 无 hash |
| A-03 | model-led compaction 全覆盖 authorized evidence并写 source refs/coverage ledger;active projection 可重建;transcript/T0 bytes 不变 | 决定性事实位于最后 chunk、map/reduce 单块失败、provider context overflow、重复 compaction/restart |
| H-404a | Messages 页单条/全部已读均落库 + 计数清零 + 返回 `read_at` | 并发 mark-all/read、重复请求幂等、无权 message id |
| G-02 | AST 复扫全部 production literal `t()` key:zh/en 双缺=0,动态 key 另有显式 allowlist;双语页面不显示 raw key | 删除任一 catalog key、fallback 缺失、动态 key 未登记 |
| P2-018 | canonical docs 引用的每个 repo-local pytest path 均存在、可 collect、复制命令 exit 0;CI gate 能抓到伪造路径 | 临时把路径改成不存在文件、测试重命名不更新文档 |
| H-KISS | 每个删除项有 replacement consumer、FreeCode/Hive capability mapping、仓外/动态使用证据、数据迁移与 rollback;删除后能力验收不减少 | 静态零引用但外部 curl/plugin/反射消费、replacement 未挂入口、生产表非零行 |

### 22.3 最终命令与生产验收门

```bash
cd backend
source .venv/bin/activate
pytest tests -q
ruff check app tests
ruff format --check app tests
alembic heads

cd ../frontend
npm test -- --run
npm run build
```

上述命令有两种不同的证据用途,不得混写:

1. **单项 / 家族关闭**:先保留正确失败的 Red test,再跑 targeted Green;随后运行受影响 package/domain 的完整测试、静态检查、build、migration/backfill 与对应 §22.1/§22.2 故障注入。能运行仓级 suite 时必须运行,不得只凭 targeted green 收口。优先在 clean、已知绿的隔离基线上施工;若仓库已有与该项无关的失败,必须用同一 HEAD 在改动前后复现并证明数量、case 和错误字节均未变化,同时将其保留在自己的 open ledger。只有**新增失败、相关失败或无法完成该项真实消费验收**会阻止该单项关闭;无关既有失败仍阻止“仓库全绿/程序账本完成”声明,但不得成为延迟 P0 独立修复发布的借口。
2. **程序账本完成**:必须在同一冻结 checkout 上得到 backend/frontend 零失败、ruff exit 0、恰好一个 Alembic head、所有相关 migration 在隔离数据库 upgrade/backfill/rollback/再 upgrade 成功、frontend build exit 0,并完成 inventory 重认证。任何失败或未解释的 skipped/xfail 都使程序级声明保持 open。
3. **生产发布**:每个涉及生产路径的独立提交都按仓库规则同时部署 `backend`、`backend-api`、`frontend`;三者 latest deployment 均 `SUCCESS` 后,执行与该项直接对应的 health、typed receipt、真实 consumer 与恢复验收。P0-F1 自身门禁通过后立即按此发布,并用受控公网 endpoint、受控 redirect 到 documentation/reserved address、连接/deny metric 与 typed `network_target_denied` receipt 验证 live path;禁止把真实 metadata、localhost 或企业内网当探针。P1-017 另验 projection/health,RLS 家族另验 catalog + 跨租户 behavior probe,UI 家族另验真实浏览器路径。某项生产验收失败只使该项与其依赖声明保持 open,不得抹掉已独立闭环项的证据。

---

## 23. 残余风险

- **P0-F1 仍在当前 checkout 开放**:本报告只把 SSRF 的边界、测试与独立发布条件写完整,没有修改 `web_mcp.py`。当前 `_looks_like_url` + `follow_redirects=True` 直连仍是最高现实风险;下一项实施动作必须是先写可控 DNS/transport Red tests,再完成 governed egress transport 并独立发布,不得继续用规划工作替代修复。
- **RLS 剩余验证边界**:生产 runtime role/owner/rolsuper/rolbypassrls/67 张现存表的 ENABLE+FORCE 已 live 核验;仍未做跨 tenant 行为探针、迁移失败故障注入及新增租户表遗漏 FORCE 的 CI catalog gate。
- **未起真浏览器走查**:UI 断点基于源码 + vitest;移动端响应式/LocalAgentChatSection 未审。
- **外部消费面盲区**:admin/plugins/feature-flags 等"前端零调用"路由可能被仓外运维脚本消费,删前需 owner 确认。
- **D-KB1 裁决已锁定但实现尚未发生**:§11 已采用 owner-direct PL1-PL3=`owner policy + agent_searchable`、cross-principal=`explicit grant + ceiling`、PL4=credential reference 的最终矩阵;当前 runtime/tool descriptions/canonical specs 仍未按该矩阵实现与同步,因此 D-KB1 继续 open。
- **C-BP7 仍是当前真实 Missing**:本方案把它提升为 Goal 1 完成声明门,不代表行为 evaluator 已存在。真实 execution dataset、LLM referee、behavior receipt 消费、provisional/rollback fault injection 任一未完成,均不得宣称 Goal 1/North Star 完成或把主路线转向 Goal 2/UI/KISS;它不阻塞其它修复及其独立发布。
- **dirty worktree 并发污染风险**:当前存在大量其他 session/用户未提交改动,P1-017 等相关文件还在变化。每个单项必须先执行 §20 第 0 组与其 scope 对应的最小 ownership/diff 隔离;P2/P3 再按组滚动重验。否则即使测试绿也无法把证据归属于单个断点,但不得以“全仓尚未隔离”为由拖住 P0 的相关文件隔离与 Red test。
- **对抗验证深度**:P0/P1 全部 CONFIRMED 且双向核实,但 P2/P3 多数为单域证据未再对抗;守卫墓碑测试防不住"改名/改数值"的违规再引入。
- **两次当日 revert 的根因未查**:P0-F2/P1-F4 的 fail-closed 修复被 revert 疑因生产 data-state 阻塞,恢复前必须查清,否则重演回滚。
- **health 证据陈旧**:生产 `runtime_control_bus.last_error` 当前显示一条数据库已不存在的 event LookupError;DB 实测 0 nonterminal projection。修复前运维必须以 DB projection state 复核,不能把该字段单独当当前故障事实。

---

## 24. 整体和分模块置信度

> 下表百分比是审查覆盖估计,不是从测试/trace 自动计算的机械指标。七原子覆盖只表示关键 P0/P1 已指出断裂原子,不表示每个能力都完成了 7×N 的逐项矩阵。

| 维度 | 估计覆盖 | 说明 |
|---|---|---|
| 生产路径正向+反向追踪 | ~88% | 主链全覆盖;外部消费面/desktop 未覆盖 |
| 七原子覆盖 | ~75% | 关键 P0/P1 已明确断裂原子;尚未对所有能力展开完整 7×N 矩阵 |
| 源码证据覆盖(file:line) | ~92% | 全部 P0/P1 有直证 + 对抗验证双核 |
| 两报告账务映射 | ~90% | 18 条第二报告 finding 已完成 12 duplicate / 2 known-missing / 4 new 映射,去重算术可复算;尚无第三位独立审查者在同一冻结 checkout 上逐项重证 |
| 当前分母认证 | ~75% | P0/P1 已按当前工作树直证;P2/P3 多来自多 HEAD 与单域证据,须在对应组开工前滚动重验,因此 69 是 provisional snapshot ledger |
| DB/RLS 覆盖 | ~90% | 生产 role/owner/catalog 已 live 实测;缺跨租户行为探针与故障注入 |
| UI 消费覆盖 | ~85% | 源码+vitest;无真浏览器走查 |
| 失败与恢复路径覆盖 | ~88% | timeout/cancel/restart/reconcile 全覆盖 |
| 基线源码对比(FreeCode/CC) | ~75% | 单 Agent 主循环逐站;工具/subagent/resume 对齐 |
| 测试与 live evidence | ~88% | 既有记忆/编排/UI + Office preview/delivery 定向证据;本次综合复核追加 8 个 backend 定向测试与 frontend AST 全量 literal-key 扫描;Railway deployment/health 与生产 PG 只读证据沿用已标日期快照;未跑本次综合后的全量 suite |

**分模块置信度**:单 Agent 主链 高(~90%);工具治理咽喉 高(~90%);Memory/自进化 中高(~85%);Knowledge tool-only 高(~90%);多智能体编排 中高(~85%);企业安全/资产 高(~90%,含对抗验证);UI/Artifact 中高(~85%);KISS/无消费者 高(全量枚举)。

**整体置信度:中高;北极星裁决与施工边界为高置信,当前分母的最终认证和实现合规性仍待各自验收。** P0/P1 主结论有当前工作树 file:line 直证并经 refute-first 核验;两份报告已逐 finding 对账,按本文分类得到可复算的**当前工作账本 69**,但未把该数字冒充为冻结 checkout 上的永久认证。最终方案已锁定 D-KB1 产品矩阵、加入 NS-1~NS-8 施工门,并把 C-BP7 定位为不改变断点分母、也不阻塞先行修复的 Goal 1 完成声明门。当前最先要消除的实现风险仍是 open 的 P0-F1,不是继续规划。提升到“程序账本也高置信”的剩余门槛:按组重验 P2/P3 并记录 ledger delta、P1-017 clean/full/deployed 验收、C-BP7 真实行为 eval closure、真 PG 跨租户行为与 migration fault injection、真浏览器 UI、仓外消费者确认、完整七原子 7×N 证据、全量测试/build 与三服务生产验收。

---

## 附:审查方法与团队

原始轮次:8 领域并行审查(A 单Agent主链 / B 工具治理 / C Memory自进化 / D Knowledge / E 多智能体编排 / F 治理安全资产 / G UI-Artifact / H 极简性无消费者)→ 4 组对抗验证(SEC / IDENTITY / RUNTIME / CONSUME,refute-first,默认判可证伪除非当前源码证实)→ 主审合成。原轮次 16 条 P0/P1 候选中,15 条 CONFIRMED、1 条(RLS owner 绕过)当时判核心 REFUTED;3 条定级修正(P0-F2→P1、P1-F5→P2、B-01→P3),TURN_STOP 裁定 P1。

第一轮补充复核:以 `HEAD 33fbecd9d`、Railway production health/deployments、生产 PostgreSQL 只读查询、SSRF validator 诊断与针对性测试重新核对。RLS 的“当前关闭”结果保留,但关闭依据改为 runtime/owner 分离 + 非 superuser/BYPASSRLS + 67/67 ENABLE/FORCE;G-01 从“仅前端”修正为含 durable backend failure path;F-MEM 施工从裸 `create_task` 修正为 durable queued job/outbox;新增 F-OBS1 health 诚实性断点。

D-KB1 校正复核:以当前 checkout `HEAD 501db6555` + 当前未提交工作树逐文件核对 access predicate、tool descriptions、三份 canonical specs、graph extractor/proposal sensitivity enums、tool evidence→transcript/T0→T2 与 outbound provenance。定向 access/extractor/proposal tests 为 `7 passed in 2.32s`;完整 evidence/replay-pointer test 为 `1 passed in 29.28s`;`PL3_sensitive` 探针证实 extraction blocklist 枚举漂移。该轮未修改业务代码、数据库、部署配置或生产数据,仅本 Markdown 报告被修改。

第二报告综合复核:逐项核对 `docs/agent-native-atomic-review-501db655.md` 的 18 条 finding 与当前源码,形成 §13.1 的 12 duplicate / 2 known-missing / 4 new 映射。定向命令
`cd backend && ./.venv/bin/pytest -q tests/services/test_agent_message_runtime.py::test_build_agent_message_tool_executor_persists_tool_calls tests/services/test_agent_message_runtime.py::test_agent_message_tool_executor_propagates_bounded_a2a_context tests/runtime/test_memory_authority_effect_freeze.py tests/services/test_chat_transcript.py::test_append_session_event_all_roles_use_committed_t0_projection tests/services/test_database_after_commit.py`
结果为 `8 passed in 5.50s`。这些绿灯只证明当前 A2A/Memory 行为被测试覆盖、dirty transcript after-commit 方向通过定向回归,不等于三个 P1 已关闭。frontend AST 复扫结果为 203 个 production TS/TSX、2,331 次 literal `t()`、1,905 unique keys、280 zh/en 双缺;另确认 P2-018 引用的 4 个 pytest 文件当前均不存在。本次综合仍只修改主 Markdown 报告,没有修改业务代码、数据库、部署配置或生产数据。

North Star 最终方案复核:重新读取当前 `AGENTS.md` 的 Product Goal 1-first、AI-Native/Model Agency、CC/FreeCode semantic floor、最窄治理、七原子与 one-complete-pass 约束,并对当前 A2A custom executor、Memory effect freeze、transcript after-commit、Recovery Manifest、T2 LLM job 与 behavior eval source path 做 current-worktree 复核。§20~§22 已将这些约束变为 NS-1~NS-8 可失败施工门、最终依赖顺序、迁移/回填和故障注入;当前仍是 docs-only 方案,未据此修改或认证任何业务实现。

完成口径终校:再次读取当前工作树的 `web_mcp.py` 直连路径,确认 P0-F1 仍未修复;同时逐段对照执行摘要、§13 账本、§14 优先级、§20 施工顺序、§22 验收与§23 风险。本文现统一采用四层语义:单项/家族可独立闭环发布;69 仅为当前快照程序账本;C-BP7 仅封锁 Goal 1/North Star 完成声明与主路线切换;Goal 2 Missing 另账。one-complete-pass 约束每个开工前完整界定的 scope,不把 P0 绑到 69/69 或 i18n/P3。该终校仍只修改本 Markdown 文档,没有修改业务代码、测试、数据库、部署配置或生产数据。
